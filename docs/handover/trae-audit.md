# Trae 只读审计报告

- 项目：Hotspot Collection Network
- 任务编号：tj-20261019-174135-fa1be1
- 审计时间：2026-10-19
- 性质：**只读审计**。本轮未创建/修改除本文件之外的任何文件（RESULT.md 由任务书「交付要求」另行规定，见矛盾 F1）。
- 标注约定：**[文档]** = 文档明确写的；**[推断]** = 我的推断，未经文档确认。

---

## 一、我对项目目标和功能的理解（约 200 字）

本项目是运行在用户 Windows 本机的单人「商机雷达」：每 3 小时（08/11/14/17/20/23 共 6 轮）自动抓取国内财经源（财联社/东财/同花顺/金十/华尔街见闻，复用 MIT 的 china-finance-rss 桥），先由低成本模型粗筛全量条目，砍掉约 90% 噪音；再对每轮限额内的少数高潜条目（默认 10 条）做深加工，输出「事实 + 机会类型（固定 6 类）+ 谁在缺什么 + 可执行下一步」四件套；经结构化「反废话闸」校验后才发布到监测大屏（最近 3 小时新增 vs 更早）。配套详情、备忘收藏、设置（源/代理/模型/限额）、日志成本五页。预算 200 元/月硬约束，10%/2% 两级熔断；编码前必须先跑 20 条样本的加工质量基准题（≥60% 才允许开发界面）。**[文档]**：PRD §1/§4/§7、tech-stack §1、complexity-decision §2。

## 二、当前实际目录结构（命令输出核对）

以 `git ls-files` + 文件系统扫描核实，实际结构如下（docs/ 内 22 个 .md，全为文档；无任何代码/静态文件目录）：

```
D:\Deepseek Harness\Project\Hotspot Collection Network\
├── .gateway-job.json          # 任务网关元数据（非业务文件，未跟踪）
├── TASK.md                    # 本次任务书（未跟踪）
├── AGENTS.md                  # AI 编码协作规则（阶段 5 产出）
├── .gitignore                 # 含 .env / data/ / *.db 等
├── .env.example               # 环境变量模板（真实 .env 不存在，已核实）
└── docs/
    ├── 项目需求文档PRD.md          # PRD v1.0（待用户审核）
    ├── project-brief.md           # 阶段 1 访谈简报 v0.1（已确认）
    ├── existing-solutions-research.md  # 阶段 2 调研
    ├── reuse-decision.md          # 阶段 2 复用决策（13 项）
    ├── market-opportunity.md      # 阶段 3 商机分析
    ├── complexity-decision.md     # 阶段 3 复杂度判定
    ├── user-flows.md              # 阶段 4 用户流程 + 页面清单
    ├── tech-stack.md              # 阶段 5 技术栈
    ├── architecture.md            # 阶段 5 架构与模块设计
    ├── implementation-plan.md     # 阶段 5 任务拆解 T0–T11
    ├── acceptance-test-plan.md    # 阶段 5 验收测试计划
    ├── 人工参数与环境配置.md        # 阶段 5 参数指南
    ├── progress.md                # 项目进度总表
    └── handover/                  # 阶段交接摘要（6 份，阶段0–5）
```

**存在性核实（命令输出）**：
- 业务代码文件（`.py/.js/.html`）：**0 个**。`Get-ChildItem -Recurse -Include *.py,*.js,*.html` 计数为 0。
- `src/ tests/ static/ app/ prompts/ data/ README.md requirements.txt`：**全部不存在**（`Test-Path` 均为 False）。
- `.env`：**不存在**；仅 `.env.example`。与 TASK.md 预期一致。**[命令]**
- `docs/prompt-log.md`：**不存在**（AGENTS.md 专用条款 3 引用它，当前尚未创建）。
- `docs/handover/`：存在，含阶段0–5 共 6 份交接摘要。**[命令]**

**git 状态（命令输出）**：
- 已有 1 个提交：`9d713c6 docs: 阶段0-5 全部产品与工程文档 + AGENTS.md`。
- 工作区有 2 个**既有**未跟踪文件：`.gateway-job.json`、`TASK.md`（均非本次审计产生）。
- 结论：**仓库目前只有文档，没有任何业务代码**；阶段 6（本次审计）后尚未提交。**[命令]** `git log --oneline -1`、`git status --porcelain`

## 三、技术栈与依赖现状

**[文档]** 定的技术栈（tech-stack §1、architecture §1）：Python 3.11+ / FastAPI + Uvicorn / SQLite（stdlib sqlite3，WAL）/ APScheduler 3.x / httpx / feedparser / openai SDK（指向 DeepSeek，默认粗筛 `deepseek-flash`、深加工 `deepseek-v4-pro`）/ 原生 HTML+CSS+JS（零构建）/ ECharts 5.x（本地 vendor）/ Canvas 2D 轻量地球 / `china-finance-rss` 子进程（127.0.0.1:8053）/ `logging`+JSONL。运行形态：`python run.py` 单进程。明确不引入：Docker、Node/npm、Redis/消息队列、向量库/RAG、ORM、任何 GPL 代码。

**仓库现状**：没有任何代码、没有 `requirements.txt`、没有依赖安装记录。全部选型停留在设计文档层面。**[命令]** 文件清单无 .py/.js/.html、无 requirements.txt。

## 四、人工必填参数与环境配置要求

**[文档]**（`docs/人工参数与环境配置.md` §1 清单，与 `.env.example` 逐项一致）：

| 参数 | 必填 | 缺失后果 |
|---|---|---|
| `DEEPSEEK_API_KEY` | **唯一必填** | 启动不崩溃，页面显示「未配置密钥」+ 获取路径；**不跑任何模型调用** |
| `DEEPSEEK_BASE_URL` | 否 | 默认 `https://api.deepseek.com` |
| `COARSE_MODEL` | 否 | 默认 `deepseek-flash` |
| `DEEP_MODEL` | 否 | 默认 `deepseek-v4-pro` |
| `PROXY_DEFAULT_URL` | 否 | 国外源不可用、国内源正常 |
| `PORT` / `RSS_BRIDGE_PORT` | 否 | 默认 8000 / 8053 |
| `MONTHLY_BUDGET_CNY` / `DEEP_LIMIT_PER_RUN` / `RUN_INTERVAL_HOURS` / `ITEM_RETENTION_DAYS` | 否 | 默认 200 / 10 / 3 / 90，建议在设置页改，不写 `.env` |

启动校验链（§3）：Python≥3.11 → 依赖齐全 → Key 存在（缺失不退出）→ 端口占用 → prompts 必需段落 → data/ 可写。安全清单（§5）：.env 进 .gitignore、源码/日志/数据库/错误信息不含 key。**上述清单已与文档逐条核对，无遗漏、无编造。**

## 五、文档之间的差异与矛盾（最重要）

> 每条均给出「文档 A 的哪一节 vs 文档 B 的哪一节」。按严重程度分组。

### 5.1 功能缺失 / 无任务承接（可能导致 v1 范围漏洞）

1. **检索功能被成本提及但无功能承接**：`project-brief.md §6` 功能 11「两种检索模式（计划式按 6 类定向搜 + 漫游式抓热榜）」vs `PRD.md §4` 功能清单（F-001～F-017 无任何检索功能）vs `PRD.md §10`「单轮全流程 ≤5 分钟（300 条粗筛 + 10 条深加工 + **20 次检索**）」与 `existing-solutions-research.md §4` 成本表「计划检索 20 次/轮 ≈18 元/月」。→ 检索既不在功能清单，成本推算却包含它；brief 的「两种检索模式」在 PRD 完全消失。
2. **候选池上限与淘汰逻辑无任务承接**：`PRD.md §7` 规则 2「候选池上限 200 条，超出按 potential 淘汰最低」vs `implementation-plan.md` T5（仅「超出限额 → 候选池」，无上限、无淘汰）。
3. **条目保留 90 天清理无任务承接**：`PRD.md §7` 规则 7「条目保留 90 天」vs `implementation-plan.md` T10（只做「日志保留 30 天」清理）。
4. **候选池在 v1 无任何页面入口**：`PRD.md §6` 五个页面（P1–P5）vs F-005「超出进候选池不丢弃」——候选池内容 v1 无处可见（F-103 手动提级属 P1）。

### 5.2 校验规则口径不一致（影响「反废话闸」实现）

5. **校验条数：3 条 vs 6 条**：`PRD.md §4 F-006` 只列 3 条校验（字段齐全 / action 含动词 / verifiable_data≥1）vs `architecture.md §3.3` 列 6 条（多出「opportunity_type∈6 类枚举」「next_action 必须含目标对象」「fact 不得以据悉/业内人士称/有消息称开头」）vs `AGENTS.md` 专用条款 2「pipeline/validate.py 的 **6 条**校验」。→ 以哪份为准需拍板。
6. **禁用词表不一致**：`PRD.md §4 F-006`「关注/留意/研究/重视」vs `architecture.md §3.3`「关注/留意/研究/重视/**了解/把握**」（多 2 个词）。
7. **验收用例未覆盖 6 条规则**：`acceptance-test-plan.md §2 F-006`（T-050～T-053）只覆盖 4 个场景，缺「类型枚举」「fact 开头禁用」两类校验的测试。

### 5.3 表结构口径不一致（8 张表 vs 数据流字段）

8. **batches 表缺 status 字段**：`PRD.md §8` batches 表（id/started_at/finished_at/trigger/stats_json）无 status 列 vs `architecture.md §4.9` 数据流「更新 batches（status=running/done/degraded/failed）」——degraded/failed 状态无处落库。
9. **items 表无去重标记**：`PRD.md §8` items 表无 duplicate 列 vs `architecture.md §4.4`「重复的标 duplicate」。
10. **llm_calls 表无单价字段**：`AGENTS.md` 专用条款 1 要求 llm_calls 记录「tokens、**单价**、金额」vs `PRD.md §8` llm_calls 表（id/batch_id/stage/provider/model/prompt_tokens/completion_tokens/cost/created_at）无 unit_price 列。
11. **表数量口径**：`complexity-decision.md §1`「**7–8 张表**」vs `PRD.md §8` / `tech-stack.md §3`「**8 张表**」（列出的其实也是 8 张）。
12. **architecture.md 无数据模型章节**：`architecture.md` 全文未列出表结构与字段（仅散见 llm_calls/batches/sources/analyses 引用），无法与 PRD §8 做「表名与字段口径」逐项核对——PRD §8 是当前唯一权威表定义。

### 5.4 成本数字口径不一致

13. **粗筛单条成本上限冲突**：`PRD.md §4 F-004`「单条成本 ≤ 0.002 元」（300 条可达 0.6 元）vs `acceptance-test-plan.md §2 T-030` / `implementation-plan.md` T4「300 条粗筛 ≤ 0.5 元」（单条≈0.00167 元）。两个上限数学上不兼容。
14. **轮次口径历史遗留**：`project-brief.md §9`「按 8 轮/天估、约 0.8 元/轮」vs `existing-solutions-research.md §4`「修正为 6 轮/天（8/11/14/17/20/23）」与 `PRD.md §7` 规则 1「6 轮/天」。brief 为 v0.1 未回改。

### 5.5 页面状态口径不一致

15. **每页 5 种状态 vs 页面清单状态集**：`PRD.md §6`「每页必须覆盖加载中/空/部分失败/完全失败/正常 5 种状态」vs `complexity-decision.md §4` P1 状态（空/采集中/部分源失败/正常，无「加载中」「完全失败」）vs `user-flows.md §二` P-001 状态（首轮未跑/采集中/部分源失败/无新增/正常）。

### 5.6 任务书自身的矛盾

16. **验收标准 vs 交付要求冲突**：`TASK.md` 验收标准 6「除 docs/handover/trae-audit.md 外没有创建或修改任何文件；git status 显示只有一个新增文件」vs `TASK.md` 交付要求 3「完成后在工作目录写入 RESULT.md」——两者并存会导致新增 **2 个**文件。
17. **git 现状与验收标准 6 不符**：git 工作区已有 2 个既有未跟踪文件（.gateway-job.json、TASK.md），加上审计报告后未跟踪文件会是 3 个（再加 RESULT.md 则 4 个），不可能「只显示 1 个新增」。（已在第 2/6 部分如实说明，未删除任何既有文件。）

### 5.7 模块归属空白（次要）

18. **架构目录树中部分模块无任务承接**：`architecture.md §2` 的 `app/models.py`、`app/api/routes.py`、`app/api/schemas.py`、`app/llm/prompts.py` 在 `implementation-plan.md` T0–T11 的「做什么」清单中均未被点名。
19. **T1 交付物不在架构树**：`implementation-plan.md` T1 交付 `static/mockup.html` 未出现在 `architecture.md §2` 目录树（临时稿，可接受，建议注明不进入最终结构）。

### 5.8 门禁表述收窄（次要）

20. **5 轮后的处置选项被收窄**：`PRD.md §7` 规则 11.3「由用户决定：继续迭代 / 调整样本 / 降低阈值」vs `acceptance-test-plan.md §1` 门 1 不通过处理「5 轮后由用户决定**是否降阈值**」——AC 漏了「继续迭代 / 调整样本」两个选项。

### 5.9 一致项确认（交叉比对通过的部分）

- 17 个 P0 功能（F-001～F-017）在 T0～T11 中**全部有任务承接**，无孤儿功能（映射：F-001→T6、F-002→T3/T9、F-003/F-004→T4、F-005/F-006→T5、F-007→T7、F-008/009/010→T8、F-011→T9、F-012→T10、F-013→T9、F-014→T6、F-015→T7、F-016→T10、F-017→T0）。
- 基准题门禁主体一致：`PRD.md §7` 规则 11（5 轮上限、降标需用户同意、≥40% 底线）与 `acceptance-test-plan.md §1` 门 1、`implementation-plan.md` T0 口径吻合。
- AC 用例编号（Q-01～Q-05、T-001～T-131）与 F-001～F-017 **逐项有映射**，无功能遗漏。
- `.env.example` 与 `docs/人工参数与环境配置.md` 清单完全一致；`.gitignore` 覆盖 .env/data/*.db 与 PRD §10 安全要求一致。
- AGENTS.md 与 PRD/architecture 无直接冲突的硬矛盾，仅 5.2/5.3 所列口径差异（validate 条数、llm_calls 字段）。

## 六、推荐实施顺序、风险、待确认问题

### 6.1 推荐实施顺序

**[文档]**（implementation-plan 排期原则 + complexity-decision §6）已明确：T0（基准题，硬闸门）→ T1（静态大屏稿，硬闸门）→ T2～T11 串行，一次只做一个任务，每任务完成后更新 progress.md 并经用户验收。

**[推断]** 我的补充建议（不替代文档）：
1. 进入 T0 前，先由项目负责人拍板 5.1/5.2/5.3 的口径（见 6.3 问题 1–3），避免 T5 校验层与 T2 建表返工。
2. 第 5.1 条「检索功能」若不实现，需同步把 PRD §10 的「20 次检索」与成本推算中的 18 元/月剔除或注明；若实现，则需补一个 P0 功能条目。
3. T2 建表时建议以 PRD §8 为基准，补齐 batches.status、items 去重标记、llm_calls 单价字段（5.3）。

### 6.2 风险清单

| 风险 | 依据 | 影响 |
|---|---|---|
| 校验口径 3 vs 6 条未定，T5/T0 按不同文档实现会导致「合格率」口径漂移 | 5.2 | 基准题结论作废 |
| 检索功能「被成本提及但无功能承接」，预算推算可能虚高 18 元/月 | 5.1-1 | 预算富余被误判 |
| 候选池无上限逻辑无页面，v1 可能「承诺不丢但不可见」 | 5.1-2/4 | 与 F-005 验收口径不符 |
| PRD 是唯一表定义且已发现 3 处字段缺口，直接建表会返工 | 5.3 | T2 返工 |
| 粗筛成本上限 0.002 元/条与 0.5 元/300 条冲突，验收时可能「单条合规但总量超标」 | 5.4-13 | 验收判定争议 |

### 6.3 需要项目负责人拍板的问题（Top 3）

1. **校验规则以哪份为准**：PRD F-006 的 3 条，还是 architecture §3.3 / AGENTS 的 6 条？（关系基准题合格率统计与 T5 实现）
2. **「两种检索模式 / 20 次检索」是否属于 v1**：若否，需从 PRD §10 与成本推算剔除；若是，需补 P0 功能条目与任务。
3. **候选池 v1 的落库与展示边界**：仅落库（上限 200 + 淘汰由哪条任务实现？），还是需要一个可见入口（这会使 5 页面变 6 页面，与「无第 6 个页面」冲突）？

---

*依据说明：本文所有「文档写了什么」均引用对应文件原文位置（节号/表号）；所有「命令核实」均已标注 `[命令]` 并在第 2 部分给出命令类型。未做任何推断性补全，未修改除本报告外的任何文件。*
