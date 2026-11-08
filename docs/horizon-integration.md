# Horizon 项目评估与融合记录

> 来源：https://github.com/Thysrael/Horizon （MIT License，Python）
> 评估时间：2026-10-21；结论：**借鉴思路，不整体引入**。

## 一、Horizon 是什么

「AI 新闻雷达」：多源采集 → 画像（profile）打分 → 事件去重 → 富化 → 双语简报输出。
技术栈 Python + uv，支持 RSS / HN / Reddit / Telegram / X / GitHub / OpenBB /
OSS Insight / GDELT / Google News，输出 Markdown / 邮件 / Webhook / 微信 / Pages。

## 二、逐项对比

| Horizon 能力 | 我们的现状 | 决定 |
|---|---|---|
| 事件级去重（topic_dedup） | 只做 URL + 标题相似度去重，同事件多篇报道仍会重复进深加工 | ✅ **已借鉴实现** |
| 品类配额（balanced digest） | 深加工候选按 id 顺序取前 N，容易被同一热点刷屏 | ✅ **已借鉴实现** |
| 画像打分 + 阈值（0–10） | 粗筛给 `potential`（0–100）并据此排序 | ✅ 部分（已改为按潜力排序） |
| 全文抽取（trafilatura） | 只用 RSS/接口摘要 | ⏸ 待评估（会新增依赖） |
| 多源（RSS/HN/Reddit/Telegram/X/GDELT/Google News） | 9 个国内财经源 + 新浪 | ⏸ 待评估（先测 GDELT/Google News 可达性） |
| 双语简报 / 邮件 / Webhook / 微信 / Pages | 单用户本地大屏 | ❌ 不需要 |
| MCP Server 暴露流水线阶段 | 无 | ❌ 当前无需求 |
| uv + profile 目录结构 | FastAPI + 零构建前端 | ❌ 架构不兼容，不搬代码 |

## 三、已落地（本项目）

1. **`app/dedup_topic.py`（新）**
   - 每轮取粗筛通过、未去重的条目（最多 60 条），把「编号 + 标题」交给粗筛模型分组。
   - 同组保留信息量最大的一条，其余 `is_duplicate=1` 且 `duplicate_of=代表条目`。
   - 失败只记 `topic_merged_error`，不阻断整轮。
2. **`pipeline.run_once` 接入**
   - 粗筛后调用事件去重；统计写 `topic_merged`。
   - 深加工候选改为：**按 `potential` 降序** + **每品类上限 `deep_per_category`（默认 3）**。
3. **设置项**
   - `settings_store` 新增 `deep_per_category`（0–20，0 = 不限制）。

## 四、实测（批次 28）

```
抓取 420 → 去重后 310 → 粗筛 ok 192
事件级合并 9 条（例：国债期货/人才房/锂价 的同事件多篇）
深加工候选 4 条，品类：人形机器人零部件(S1) / 庭院阳台光储(S2) /
                      瓦楞纸披萨盒(S3) / 锂电池(S3)   ← 品类分散，不再被单一热点占满
本轮花费 ¥0.4573（含 1 次去重调用）
```

## 五、许可证与归属

- Horizon 为 MIT；本项目**未复制其源码**，仅借鉴「事件去重」「品类配额」两个工程思路，
  并自行实现（`app/dedup_topic.py`、pipeline 候选选择）。
- 后续若直接复用其代码，需在 `third_party/` 保留 MIT 许可与版权声明。
