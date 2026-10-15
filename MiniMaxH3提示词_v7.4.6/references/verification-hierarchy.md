# SKILL.md §1.3 验证层级四级区分

> 归属：`MiniMaxH3提示词_v7.4.2` 技能仓 SKILL.md 挪出段全文，governance 第2轮 REFACTOR 自 SKILL.md 整体逐字挪入（一字未改），SKILL.md 原位留指针。

---

## 原文：SKILL.md §1.3 验证层级四级区分

严禁将静态审计混淆为模型真实生成成功：
1. **Rule Audit（规则审计）**：确定性静态语法规则扫描（`scripts/audit_cli.py` 执行）。
2. **Local Pipeline E2E（本地管线集成测试）**：剧本解析 → 动态装箱 → 门禁校验 → 原子渲染的确定性全链路测试（`_审查测试/test_episode_pipeline.py`，五夹具）；原 30 例黄金集与其 runner 已随历史套件不可用（`HISTORICAL_FULL_SUITE=UNAVAILABLE`）。
3. **Real Agent E2E（真实 Agent 会话测试）：BLOCKED-CRED**——本机无可用外部 Agent 会话凭证；解锁条件、复核命令与登记要求见 `references/closure-gates.md`。凭证未解锁前不得宣称此档通过。
4. **Real Model Render Validation（真实模型渲染验证）：BLOCKED-CRED**——无凭据即无法真调官方 API 出片；解锁条件同上。凭证未解锁前不得宣称此档通过。

六档闭合门（`closure_gate_contract.GATE_ORDER`）逐档登记在 `references/protocol/verification-protocol.json`，校验器拒绝三种情形：漏登记任一档、PASS 无日期/note、PASS 的产物不在 `evidence/` 运行日志里（源码存在不等于跑过）。第 1、2 档由 `python scripts/collect_closure_evidence.py` 当场执行；历史假红修复记录与第 3 档 NOT_RUN 口径详见 `references/closure-gates.md`。
