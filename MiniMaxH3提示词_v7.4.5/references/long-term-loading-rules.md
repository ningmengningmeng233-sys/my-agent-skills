# SKILL.md §3 长期规则

> 归属：`MiniMaxH3提示词_v7.4.2` 技能仓 SKILL.md 挪出段全文，governance 第2轮 REFACTOR 自 SKILL.md 整体逐字挪入（一字未改），SKILL.md 原位留指针。

---

## 原文：SKILL.md §3 长期规则

**长期规则**：
- 普通任务初始参考 ≤3 份，**一次绝不加载整个 references 目录**；`references/craft/*.md` 是兼容跳转卡，不进入普通初始集；`CHANGELOG.md`、`references/legacy/`、`references/examples/`、`文档/柠檬` 不进入普通路由。
- 长命令输出写入 `.logs/` 日志，对话里只报告结论与证据路径。
- 每个大阶段结束更新阶段交接摘要（固定字段照 `templates/execution-summary.md` 填；这是开发柜侧过程件，**不随发布包分发**，收件人可放在自己选定的位置）；上下文…上下文占用 70%—80% 时建立检查点。
- 已完成阶段的详细工具输出不重复读入上下文，需要时只查 `.logs/` 证据文件。
- 子代理只接收完成其任务所需的**最小上下文**；主模型只接收子代理的结构化结论与证据位置。
