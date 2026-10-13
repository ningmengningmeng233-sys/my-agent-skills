# 状态与恢复

`manifest.json` 是唯一状态真相。正常路径为 `ready_for_rewrite → rewriting → awaiting_checks → passed → approved → delivered`；检查失败进入 `rework`，运行异常进入 `error`，输入指纹变化进入重新处理队列。

v4.5.0-dev.3 语义收敛：

- 冗余状态 `rewritten / checking / repairing / rechecking` 已从状态机删除；合法流转由 `v4/core/state_machine.py` 单点约束。
- `passed` 不是交付状态：`passed → delivered` 直接流转已被禁止，必须经 `approve` 进入 `approved` 后才能合并（`approved → delivered` 在 merge 成功时发生）。
- `merge` 只接受 `approved / delivered` 章节并强制读取 `approved_version` 锁定正文；`rollback` 可清除批准指针（章节退回 `needs_review`，带 `ROLLED_BACK` 阻塞项），物理历史版本永不删除。

长篇状态的两阶段提交：

- `check` 通过（`passed`）只写提案 `chapters/<id>/state_deltas/vNNN.proposed.json`，不写 continuity / SQLite；
- `approve` 是唯一提交点：读取提案 → `state/approval-journal.json` 记录 intent → SQLite `BEGIN IMMEDIATE` 事务 + continuity 追加 → journal 标记 committed；任何一步失败标记 failed、回滚 continuity，批准被拒绝（fail-closed）；
- `continuity/state.json` 是 SQLite 的派生读缓存，损坏时 `load_ledger` 自动从 `state/novel_state.db` 的 facts 表重建。

使用 CLI 的 `status --verbose`、`blockers`、`recover`、`resume`、`refresh`、`approve`、`rollback`，不要手动编辑 manifest 绕过状态机。
