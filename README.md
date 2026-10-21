# Hotspot Collection Network（热点采集网络）

单人本机的跨境热点采集与线索加工工具。本仓库当前为 **T2 项目骨架**：一条命令起服务，接上配置层与 SQLite 八张表，提供健康检查接口。采集（T3）、LLM 加工（T4/T5）、大屏（T7）尚未实现。

## 一、安装

1. 安装 **Python 3.11 或更高**（安装时勾选 **Add Python to PATH**）。
2. 在项目根目录执行：

   ```bash
   pip install -r requirements.txt
   ```

3. （可选但强烈建议）配置密钥：把 `.env.example` 复制为 `.env`，填入 `DEEPSEEK_API_KEY`（获取地址：https://platform.deepseek.com → API Keys）。不填也能启动，只是页面会提示去申请，模型功能暂不可用。

### 国内网络安装提示

直连 PyPI 常出现 `SSL: UNEXPECTED_EOF_WHILE_READING`，换国内镜像即可：

```bash
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

> 已在干净目录 + 全新 venv 环境实测：装完依赖后 `python run.py` 可直接启动，
> 自动建 8 张表、拉起 RSS 桥、无 API Key 时页面提示申请而不报错（2026-10-21）。

## 二、启动

```bash
python run.py
```

- 启动时会依次检查：Python 版本、依赖、端口（默认 9000）、数据库。
- 成功后在浏览器打开 <http://127.0.0.1:9000>（首页为占位页）。
- 健康检查：<http://127.0.0.1:9000/api/health>，返回 JSON（status / version / db.ok / db.tables / key_configured / uptime_seconds）。
- 改端口：在 `.env` 中设置 `PORT=8001` 后重启。

## 三、常见错误

| 现象 | 原因 | 怎么办 |
|---|---|---|
| `需要 Python 3.11 或更高版本` | 版本过低 | 安装 Python 3.11+，勾选 Add Python to PATH |
| `缺少依赖：fastapi…` | 依赖未装 | 执行 `pip install -r requirements.txt` |
| `端口 9000 已被占用` | 端口被其他程序占用 | 改 `.env` 的 `PORT`，或关闭占用进程 |
| `数据目录不可写` | 权限问题 | 检查 `data/` 目录写入权限 |
| 页面提示「尚未配置 DeepSeek API Key」 | 没配密钥 | 按页面指引复制 `.env.example` 为 `.env` 填 Key 后重启 |

## 目录结构（当前已实现）

```
run.py              # 唯一入口：检查 → 建表 → 起服务
app/config.py       # 配置层：.env + data/config.json + 默认值
app/db.py           # SQLite：WAL + 幂等建 8 张表
app/api/routes.py   # /api/health + 静态首页
static/index.html   # 深色占位页
data/app.db         # 数据库（首次启动自动生成，不进 git）
```

## 一键启动（Windows）

- 双击项目根目录的 **`启动.cmd`**：自动检查端口、启动服务、等健康检查通过后**自动打开浏览器**（http://127.0.0.1:9000/）
- 双击 **`停止.cmd`**：结束 9000（服务）与 8053（RSS 桥）进程
- 桌面已创建快捷方式：`热点采集网络-启动.lnk`、`热点采集网络-停止.lnk`
- 关闭启动窗口 = 停止服务；窗口里的日志就是运行日志

> 注意：`.cmd` 文件是 **GBK 编码**，用编辑器修改时请保持 GBK（或另存为 ANSI），否则中文会乱码、命令可能被截断。

## 数据源一览（v1.1，共 18 个）

| 类型 | 源 | 说明 |
|---|---|---|
| 直连 API | 华尔街见闻全球/A股/外汇、新浪财经7×24 | 走直连，可切代理 |
| 本地桥 | 财联社、东方财富、同花顺、金十、见闻RSS | 由 8053 桥进程抓取 |
| 新闻 RSS | Google新闻 ×5（跨境电商/选品爆款/关税出海/平台政策/海外仓物流） | T12 A 方案 |
| 热搜 | 微博热搜 | T12 A 方案（需求侧信号） |
| 平台榜单 | Amazon 厨房家居 / 电子配件 / 美妆个护 | T12 C 方案，**日更快照** |

**平台榜单是日更快照**（headless Chrome 抓取，约 1 分钟），不跟 3 小时轮次跑。
手动刷新：

```bash
python run.py --platform-snapshot
```

调度器每天 05:00 自动刷新一次；启动时若快照超过 20 小时也会补刷。

## 四、如何换源（新增 / 停用 / 走代理）

**日常操作（不用改代码）**
- 大屏左侧「采集来源」每行右侧有「直连 / 代理」按钮，点一下即切换，**下一轮生效**。
- 设置页（`/static/settings.html`）可批量勾选启用/停用。

**新增一个数据源（要改代码）**

1. 在 `app/sources/registry.py` 的 `SOURCES` 里加一条 `SourceSpec`：
   - `type="api"`：直连型（参考 `wallstcn.py` / `sina.py`）
   - `type="bridge"`：走本地 RSS 桥（参考 `third_party/china-finance-rss/server.py` 里已有的路由）
2. 直连型还要在 `app/sources/collect.py` 的 `_build_source()` 里分发到你的适配器。
3. 重启服务（`停止.cmd` → `启动.cmd`）。启动时会按 `name` 幂等写入 `sources` 表，
   **不会覆盖你已改过的开关/代理设置**。

> 注意：源必须能直连或能走本地代理；不可达时会在「采集来源」标红并在日志页列进「失败源」。

## 五、如何改提示词

- 粗筛：`prompts/coarse.md`；深加工：`prompts/deep.md`
- 文件顶部有 `<!-- version: x.y -->`，**每次改动必须递增**，并记一笔到 `docs/prompt-log.md`（改了什么、合格率变化、典型反例）。
- 改完用基准题对比，不要凭感觉：
  ```bash
  python tools/baseline_test.py --set gold
  python tools/baseline_test.py --set live
  ```
  报告输出在 `tools/baseline/report-gold.md` / `report-live.md`。
- **底线**：`app/validate.py` 的 12 条校验（含 r11 能力闸）不要为了让样本通过而放宽。
  校验太严导致大屏没内容时，正确做法是改提示词，不是关校验。

## 六、备份与恢复

- **自动**：每天 03:30 备份到 `data/backups/app-YYYYmmdd-HHMMSS.db`，只保留最近 **7 份**；
  服务启动时若发现超过 24 小时没有备份，会自动补一份。
- **手动备份**：
  ```bash
  python -c "from app.backup import run; print(run())"
  ```
- **恢复**：
  1. `停止.cmd` 停服务
  2. 把 `data/app.db` 改名留底（如 `app.db.before-restore`）
  3. 复制一份备份为 `data/app.db`
  4. `启动.cmd`，打开大屏核对批次与线索数
- 备份使用 SQLite 在线备份 API（WAL 安全），并做 `PRAGMA quick_check` 校验，损坏的备份会被删除。
