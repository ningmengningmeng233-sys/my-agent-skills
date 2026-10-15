# 六档闭合门与 BLOCKED-CRED 细则（v7.4.2 从 SKILL.md §1.3 挪入）

四层验证层级（Rule Audit / Local Pipeline E2E / Real Agent E2E / Real Model Render
Validation）的定义与「严禁把静态审计混同为模型真实生成成功」总则仍在 `SKILL.md` §1.3；
本文件承载挪出的长文细则。改动本文件即改动验证合同口径，须同步跑
`python scripts/verification_protocol.py check`。

## Real Agent E2E：BLOCKED-CRED

原因：本机所有渠道 key 均为 REPLACE 占位，无可用外部 Agent 会话凭证（原凭证协议文件已随
历史套件丢失，现重建为 `references/protocol/verification-protocol.json`，仅登记取用方式、
不存密钥）。解锁条件：按该协议 channels 填入可用 `MINIMAX_API_KEY`，完成 1 次真实 Agent
自主会话（自主读本 SKILL、自主选 profile/mode）并把脱敏请求/响应凭证与日期登记进 records；
复核命令：`python scripts/verification_protocol.py check`。凭证未解锁前不得宣称此档通过。

## Real Model Render Validation：BLOCKED-CRED

原因：无凭据即无法真调 MiniMax 官方 API/后端集群出片。解锁条件：挑 ≥5 条提示词真调官方
API 出片，按物理连续性/画质/台词口型/风格一致逐项打分，产物路径与日期登记进
`references/protocol/verification-protocol.json`；复核命令同上。凭证未解锁前不得宣称此档通过。

## 六档闭合门

六档闭合门（`closure_gate_contract.GATE_ORDER`）逐档登记在
`references/protocol/verification-protocol.json`，校验器拒绝三种情形：漏登记任一档、
PASS 无日期/note、PASS 的产物不在 `evidence/` 运行日志里（源码存在不等于跑过）。
第 1、2 档由 `python scripts/collect_closure_evidence.py` 当场执行并按 run_id+commit 绑定；
该采集器的套件登记表已改为按 `_审查测试/` 目录发现——写死的九份历史套件名曾让静态段与
CLI 段**长期假红**（文件不存在，50ms 退出码 2），2026-09-23 修复，两份报告对比见
`evidence/20260923T005125Z-e57f9206`（红）与 `evidence/20260923T010501Z-fd5712fd`（绿）。
第 3 档按契约口径记 NOT_RUN：它要 30 例冻结批次（`e2e/expected/` 已丢失），五夹具全链
（`_审查测试/test_episode_pipeline.py`）只是子集，不足以判 PASS。
