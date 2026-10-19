# 架构与模块设计

- 项目：Hotspot Collection Network
- 版本：v1.0（待用户确认）
- 日期：2026-10-19
- 状态说明：**本文全部为设计，尚未实现。** 实现进度以 `progress.md` 为准。

---

## 1. 整体形态

单机、单进程、本地服务。浏览器访问 `127.0.0.1:8000`。

```
┌──────────────────────────────────────────────────────────────┐
│  浏览器（静态页面 + ECharts + Canvas 地球）                    │
└───────────────▲──────────────────────────────────────────────┘
                │ HTTP / JSON
┌───────────────┴──────────────────────────────────────────────┐
│  API 层（FastAPI）                                            │
│  /api/feed /api/item/{id} /api/notes /api/favorites           │
│  /api/settings /api/run /api/stats /api/health                │
└───────────────▲──────────────────────────────────────────────┘
                │ 读写
┌───────────────┴──────────────────────────────────────────────┐
│  流水线编排（pipeline）                                        │
│  批次 → 采集 → 去重 → 粗筛 → 深加工 → 校验 → 落库              │
│  限流 / 限额 / 预算熔断 / 失败隔离                             │
└──▲────────────▲───────────────▲───────────────▲──────────────┘
   │            │               │               │
┌──┴───┐   ┌────┴─────┐   ┌─────┴─────┐   ┌─────┴─────┐
│采集层│   │ 去重层   │   │ 加工层    │   │ 记账层    │
│adapt-│   │ dedup    │   │ LLM×2 级  │   │ cost      │
│ers   │   │          │   │ + 校验    │   │           │
└──▲───┘   └──────────┘   └───────────┘   └───────────┘
   │
┌──┴───────────────────────────────────────────────────────────┐
│  外部：china-finance-rss(8053) / 行情接口 / DeepSeek API       │
│        / 用户代理软件（直连或 http/socks）                     │
└──────────────────────────────────────────────────────────────┘
                │
          ┌─────┴─────┐
          │  SQLite   │  data/app.db
          └───────────┘
```

---

## 2. 目录结构

```
hotspot-network/
├── run.py                     # 唯一入口：起子进程 + 调度 + API
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md                  # 傻瓜式启动说明
├── AGENTS.md
├── app/
│   ├── config.py              # 配置读取与校验（.env + data/config.json）
│   ├── db.py                  # 连接、建表、迁移、WAL
│   ├── models.py              # 数据类（非 ORM）
│   ├── pipeline/
│   │   ├── orchestrator.py    # 一轮的编排、限额、熔断
│   │   ├── dedup.py           # URL 归一化 + 标题相似度
│   │   └── validate.py        # 四件套校验（反废话闸）
│   ├── sources/
│   │   ├── base.py            # 适配器接口
│   │   ├── rss_bridge.py      # 走 china-finance-rss
│   │   ├── market.py          # 金价/油价/汇率（待验证）
│   │   └── registry.py        # 源注册表（含代理模式）
│   ├── llm/
│   │   ├── client.py          # OpenAI 兼容封装、重试、超时
│   │   ├── coarse.py          # 粗筛
│   │   ├── deep.py            # 深加工
│   │   └── prompts.py         # 提示词加载与校验
│   ├── cost.py                # token/金额记账与预算判定
│   ├── api/
│   │   ├── routes.py
│   │   └── schemas.py
│   └── scheduler.py           # 6 轮定时 + 启动补跑
├── prompts/
│   ├── coarse.md
│   └── deep.md
├── static/
│   ├── index.html             # P1 大屏
│   ├── pages/{detail,notes,settings,logs}.html
│   ├── js/{app,globe,charts,api}.js
│   └── vendor/echarts.min.js
├── data/
│   ├── app.db
│   ├── config.json            # 界面可改的业务配置
│   └── logs/app.jsonl
├── tools/
│   └── baseline_test.py       # F-017 基准题脚本
├── tests/
│   └── ...
└── docs/                      # 本目录
```

---

## 3. 核心设计决策

### 3.1 批次（batch）是一等公民

PRD 要求「最近 3 小时新增 vs 更早」。因此**每一轮采集产生一个 batch**，条目都挂在 batch 上。
好处：增量视图不用比较时间戳，直接按 batch 分区；成本也能按 batch 汇总。

### 3.2 两级漏斗，且限额在「深加工」这一层

```
原始条目 (≈300/轮)
   ↓ 去重
去重后 (≈250)
   ↓ 粗筛 flash（全量，便宜）
is_economic=true 且 potential≥阈值 (≈40-60)
   ↓ 排序 + 限额（默认 10 条）
深加工 v4-pro
   ↓ 校验
通过 (≈8-10) → 大屏主区
不通过 → rejected（详情页可见原因，可重试）
超出限额 → 候选池（不丢）
```

**为什么粗筛不限额**：粗筛单条 ≈0.001 元，300 条才 0.3 元；限额反而会漏掉机会。
**为什么深加工必须限额**：单条 ≈0.05 元，放开就爆预算。

### 3.3 校验层是「反废话闸」，独立于模型

模型输出后，`pipeline/validate.py` 逐条检查：

| 规则 | 不通过时 |
|---|---|
| 必需字段齐全（fact / opportunity_type / who_lacks_what / next_action / verifiable_data） | rejected |
| `opportunity_type` ∈ 6 类枚举 | rejected |
| `next_action.action` 含动词，且不在禁用词表（关注/留意/研究/重视/了解/把握） | rejected |
| `next_action` 必须包含「目标对象」（查什么/联系谁） | rejected |
| `verifiable_data` 至少 1 条，且不是「相关数据」这种空话 | rejected |
| `fact` 不得以「据悉/业内人士称/有消息称」开头 | rejected |

**这是硬闸：不通过就不进主区。** 宁可少一条，也不让废话混进去。

### 3.4 提示词外置 + 版本号

`prompts/deep.md` 顶部必须有 `<!-- version: x.y -->`。每次改提示词，版本号必须递增，并记入 `docs/prompt-log.md`（基准题迭代记录）。这样能回答「合格率从 40% 到 70% 是因为改了什么」。

### 3.5 成本记账在「调用处」，不在「统计时」

每次 LLM 调用完成立即写 `llm_calls`（tokens、单价、金额、batch_id、stage、model）。
**不允许**先跑完再从日志里补算——那种统计永远对不上。

### 3.6 失败隔离

- 单源失败：不影响其他源，`sources.last_error` 记录，界面标红。
- 单条加工失败：重试 ≤2 次，仍失败标 `failed`，不阻塞本轮。
- LLM 整体不可用：本轮标记 `degraded`，只保留粗筛结果并明确提示「本轮未深加工」。
- **禁止**：任何一处异常导致整轮静默产出空结果。

### 3.7 代理分流

每个源配置 `proxy_mode: direct | proxy`，`proxy_url` 形如 `http://127.0.0.1:7890` 或 `socks5://...`。
采集时按源建 httpx client（带/不带 proxy），**并行执行**。
不读写系统代理，不解析订阅链接，不改网络设置。

---

## 4. 数据流（一次自动轮次）

1. `scheduler` 到点触发（或用户点一键采集）→ 建 `batches` 记录（status=running）
2. `orchestrator` 按 `sources` 取启用的源，并行抓取
3. 抓到的条目先落 `items`（batch_id 标记）
4. `dedup` 与库内历史比对 → 重复的标 `duplicate` 不参与后续
5. `coarse` 分批调用 flash → 写 `analyses(stage=coarse)`
6. 按 potential 排序取前 N → `deep` 调用 pro → 写 `analyses(stage=deep)`
7. `validate` 校验 → `status=ok/rejected`
8. `cost` 累计本轮花费；触达阈值则熔断
9. 更新 `batches`（status=done/degraded/failed、stats_json）
10. 前端轮询 `/api/feed` 拿最新批次

---

## 5. API 草案

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/feed?batch=latest` | 返回本批与更早分区 |
| GET | `/api/item/{id}` | 条目详情 + 加工结果 + 笔记 + 收藏态 |
| POST | `/api/item/{id}/note` | 写备注 |
| POST | `/api/item/{id}/favorite` | 收藏/取消 |
| POST | `/api/item/{id}/reanalyze` | 重试深加工 |
| POST | `/api/run` | 手动采集（有轮次在跑则 409） |
| GET | `/api/settings` / PUT | 设置读写 |
| POST | `/api/settings/test-proxy` | 代理连通性测试 |
| GET | `/api/stats?by=day\|batch\|model` | 成本统计 |
| GET | `/api/logs?limit=50` | 运行日志 |
| GET | `/api/health` | 服务与各源健康状态 |
| GET | `/api/market` | 金价/油价/汇率 |

---

## 6. 关键风险与对策

| 风险 | 对策 |
|---|---|
| 上游网页端点变更 | 每源独立健康检查 + 失败原因入库；换了端点只改 `sources/registry.py` |
| 提示词改坏导致 JSON 解析失败 | 解析失败重试 1 次（要求纯 JSON），仍失败标 `failed` |
| 预算被夜间档跑超 | 每轮调用前先查剩余额度，按剩余额度折算本轮可深加工条数 |
| 数据库被误删 | 每日自动备份 `data/backup/app-YYYYMMDD.db`，保留 7 份 |
| 用户改配置改坏 | 保存前校验；`config.json` 出错时回退到默认值并提示 |

---

## 7. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-10-19 | 首版设计，待用户确认 |
