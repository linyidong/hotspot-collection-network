# 提示词变更日志

> 按 AGENTS.md 专用条款 3 维护：每次提示词改动递增版本号并在此留痕。

| 轮次 | 日期 | 版本 | 改了什么 | 效果（ok/rejected） | 典型反例（来自 tools/baseline/raw/） |
|---|---|---|---|---|---|
| 1 | 2026-10-19 | v0.1 → v0.2 | ①引入「跨境信息差套利 SOP」S1–S4 映射骨架，粗筛/深加工都要对号入座；②深加工升级为六件套，新增 playbook_stage、money_path{payer,receiver,your_cut}、verify_cost{money,time}，填不出钱流就认输（opportunity_type:none）；③校验新增 r7（阶段必填且∈S1–S4）、r8（money_path 三要素齐全非空话）、r9（action 不得只是查资料，除非含联系/报价等交易动作） | v0.1：ok 20 / rejected 0（人工判定仅 2–4 条可执行）→ v0.2：ok 0 / rejected 6（均 no_playbook 按规则认输）/ skipped 14 | 认输反例：s17（欧盟考虑对混动汽车进口限制）→ no_playbook「需大额资金与认证资质，超出个人无本金模式」；s11（亚马逊支持海防企业出口）→ no_playbook「无具体品类，无法构建交易路径」 |
| 2 | 2026-10-19 | v0.2 → v0.3 | ①**粗筛回归简单判断**：只输出 `is_economic` + `anchors{country,category}` + `potential`，**不再判 S1–S4**，也不再因「看起来是宏观数据」一票否决——能指向品类或国家就放行（修复 s07/s08/s09 误杀）；②**深加工输出改为线索卡**：fact / stage / anchor{country,category,hs_hint} / signal / verify_plan[3] / buyer_hypothesis / money_hypothesis{form,payer,your_cut,status} / next_action / verify_cost / verifiable_data / confidence；`money_hypothesis.status` 允许「待验证」只需写出钱流形态；`buyer_hypothesis` 写买家类型；**只有国家和品类都定位不出才输出 none**；③**校验 9 条 → PRD F-006 的 8 条**（r1 字段齐全 / r2 stage∈S1–S4 / r3 anchor 非空 / r4 verify_plan 恰好 3 条含查询对象 / r5 money.form 非空 / r6 next_action 不得只查资料 / r7 fact 禁传闻词 / r8 none 判 no_playbook） | v0.2：ok 0 / rejected 6 / skipped 14 → v0.3：ok 19 / rejected 1 / skipped 0 | 认输反例：s01（新华财经早报）→ no_playbook「仅新闻早报标题，无具体经济事件，无法定位国家或品类」（符合「连国家+品类都定位不出才 none」的设计，认输合理） |
