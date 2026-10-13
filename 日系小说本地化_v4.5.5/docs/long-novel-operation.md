# 长篇运行

先用 1 章验证，再扩展到 10 章、10 万字，最后进行全书批处理。每级都记录成功率、重试、返修、耗时、token、峰值内存和恢复结果；存在 `error/rework/needs_review` 时禁止完整合并。
每章提交后，Core 会在工作区 `continuity/state.json` 保存轻量连续性快照（角色、事件动作/结果和输出规模），下一章只按角色交集读取最近快照，不加载整部历史。快照写入失败不会阻断正文，但应在交付前检查账本完整性。

外部资料统一通过 `v4/enhance/providers.py` 的 `KnowledgeProvider` 协议接入；现有 Markdown `IndexLibrary` 由 `IndexLibraryProvider` 兼容。新增 SQLite、向量库或 HTTP 适配器时，不应修改 Core 状态机。

长篇验收至少检查：连续性账本是否随章节增长、角色称谓是否稳定、伏笔是否有登记、断点恢复后是否重复写章，以及单章 Prompt 是否始终在预算内。
