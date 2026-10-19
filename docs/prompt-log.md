# 提示词变更日志

> 按 AGENTS.md 专用条款 3 维护：每次提示词改动递增版本号并在此留痕。

| 轮次 | 日期 | 版本 | 改了什么 | 效果（ok/rejected） | 典型反例（来自 tools/baseline/raw/） |
|---|---|---|---|---|---|
| 1 | 2026-10-19 | v0.1 → v0.2 | ①引入「跨境信息差套利 SOP」S1–S4 映射骨架，粗筛/深加工都要对号入座；②深加工升级为六件套，新增 playbook_stage、money_path{payer,receiver,your_cut}、verify_cost{money,time}，填不出钱流就认输（opportunity_type:none）；③校验新增 r7（阶段必填且∈S1–S4）、r8（money_path 三要素齐全非空话）、r9（action 不得只是查资料，除非含联系/报价等交易动作） | v0.1：ok 20 / rejected 0（人工判定仅 2–4 条可执行）→ v0.2：ok 0 / rejected 6（均 no_playbook 按规则认输）/ skipped 14 | 认输反例：s17（欧盟考虑对混动汽车进口限制）→ no_playbook「需大额资金与认证资质，超出个人无本金模式」；s11（亚马逊支持海防企业出口）→ no_playbook「无具体品类，无法构建交易路径」 |
