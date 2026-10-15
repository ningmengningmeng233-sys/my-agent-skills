# v4 需求追踪矩阵

> 规则：只有同时具备实现路径、测试 ID 和审计证据，需求才能标记为 `verified`。只有文档承诺而没有实现/测试的项必须保持 `planned` 或 `blocked`。

| ID | 规范化要求 | 来源 | 实现路径 | 测试/验收 | 审计证据 | 状态 |
|---|---|---|---|---|---|---|
| R01 | 输入必须是中文小说稿；日文原稿/非小说阻塞 | v4 spec §2.1；v3 SKILL 边界 | `v4/core/snapshot.py`、`precheck.py` | C-014、H-005 | `audit.jsonl` source/precheck | planned |
| R02 | 输入先归一化为只读快照；源文件变化不污染工作区 | v4 spec §2.1/§5 | `v4/core/snapshot.py`、`fingerprints.py` | C-015、C-016、F-018 | snapshot manifest + source hash | planned |
| R03 | 单章节工作项是最小可信单位，批次只调度 | v4 ADR；v4 spec §6 | `v4/core/workspace.py`、`manifest.py` | C-012、C-080 | chapter manifest state | planned |
| R04 | 正文与计划/质检/状态/审计分离 | v4 spec §5/§13；v3 写前计划泄漏问题 | `v4/core/checks.py`、`merge.py` | C-007、C-083、F-001、F-002 | output purity check | planned |
| R05 | 三级本地化：必须替换/功能适配/默认保留 | v4 ADR；v3 一刀切缺陷 | `v4/rules/localization-tiers.md`、`checks.py` | C-045、C-046、F-015 | tier decision audit | implemented |
| R06 | A/B/C 用户交互分层 | v4 CONTEXT §A/B/C | `v4/core/decisions.py` | C-030~C-035、F-017 | decision audit | planned |
| R07 | 源稿矛盾分为确定/疑似/可解释；未决阻塞 | 翻译质量报告；v4 spec §7 | `v4/core/precheck.py` | C-027、C-028、F-004、F-006 | contradiction decision record | planned |
| R08 | 修复“第二天/四天”误报，保留疑似人工核标记 | 翻译质量报告 L7；v3 detector | `v4/core/precheck.py` | C-028、F-004 | checks contradiction result | planned |
| R09 | 修复“金额矛盾”只检测不闭环 | 翻译质量报告 L8 | `precheck.py`、`decisions.py`、`manifest.py` | F-006、H-005 | decision source + revision | planned |
| R10 | 事件图初稿、结构校验、高风险节点用户确认 | v4 spec §8 | `v4/core/events.py` | C-020~C-026、F-014 | events.json + decision audit | planned |
| R11 | 事件级对照，段落 ID 证据，不能只写“见正文” | v4 spec §9 | `v4/core/checks.py` | C-041~C-046、F-014 | checks.json event results | planned |
| R12 | 事实/交付硬门槛与风格软评估分离 | v4 拷问决策 | `checks.py`、`rules/structural-writing-checks.json` | C-047~C-050、H-006 | checks hard/soft sections | planned |
| R13 | 篇幅只作异常提示，不是事实证明 | v4 spec §9.3；v3 check_ratio | `checks.py` | C-047、C-048 | warning record | planned |
| R14 | manifest 是唯一状态真相；frontmatter/文件名不能放行 | v4 ADR §manifest | `manifest.py`、`merge.py` | C-064~C-069、F-007、F-017 | manifest hash + audit | planned |
| R15 | passed 必须无 blocker、硬门槛/事件对照/决策/盲审闭环 | v4 manifest schema | `manifest.py`、schema loader | C-060~C-063、F-019、F-020 | manifest validation | planned |
| R16 | 指纹变化自动使旧正文失效；无关审计不触发 | v4 spec §12.3 | `fingerprints.py` | C-066~C-069、F-010、F-011 | fingerprint audit | planned |
| R17 | 正式合并只读 passed/delivered，缺章/风险拒绝 | v4 spec §13 | `merge.py` | C-080~C-087、F-018 | merge audit | planned |
| R18 | 带风险版独立路径，不得冒充正式成品 | v4 spec §11/§13 | `merge.py`、manifest schema | C-087 | experimental_output + risk | planned |
| R19 | 历史 13 条实测反馈全部保留决策来源 | `references/实测反馈修订.md` | `v4/rules/dictionary.json`、决策层 | 规则映射审查、历史 fixture | decision source audit | implemented |
| R20 | 6 条流程修复不回退 | `references/实测反馈修订.md` | v4 state/merge/audit/checks | F-003、F-016、F-017 等 | audit/manifest | planned |
| R21 | v3 冻结只读，v4 并行，不自动迁移 | v4 ADR §15 | `v4/` 独立目录 | 目录边界审查 | git/file diff | implemented |
| R22 | 索引库不能是 Core 硬依赖 | v4 CONTEXT；用户拷问决策 | `v4/core` 不 import enhance | Core 无库运行测试 | manifest enhancements disabled | planned |
| R23 | 用户索引库作为强力可选 Enhance，多参考但只读 | 用户本轮明确要求 | `v4/enhance/index_lib.py` | enhance audit tests | asset hash/use records | planned |
| R24 | 索引库：章节索引爽点/看点、金句/名场面、伏笔、原文上下文、已验证卡 | 用户索引库结构；v3 参考协议 | `index_lib.py`、`reference_audit.py` | asset integration tests | enhance_use audit | planned |
| R25 | 写作指导：六项一致性 | 写作指导库 consistency_review | `v4/enhance/writing_guide.py`、checks | H-003~H-008 | method asset audit | planned |
| R26 | 写作指导：plot_logic 五环 | 写作指导库 plot_logic | events/checks plan | C-020~C-026、H-003 | plan/check audit | planned |
| R27 | 写作指导：character_consistency 五维 | 写作指导 character_consistency | chapter plan/soft checks | H-003、H-007 | plan audit | planned |
| R28 | 写作指导：anti_ai_voice 四层 | 写作指导 anti_ai_voice | Enhance soft evaluation | H-010、enhance tests | soft evaluation audit | planned |
| R29 | 写作指导：transition/dialogue/chapter_ending 诊断和局部计划 | 写作指导专项模块 | `chapter_plan.json`、writing guide adapter | H-004、H-006 | method/evidence audit | planned |
| R30 | 写作指导本地语料：2-4 正例、1-2 反例、回查上下文、只借结构不抄句面 | webnovel_corpus_guide；用户要求多参考 | `writing_guide.py` | enhance tests、人工 H-002/H-010 | source hash + reference audit | planned |
| R31 | 平台审核与本地化分离；Core 只做风险标记 | v4 拷问决策 | `precheck.py`/optional plugin boundary | F-016 | review decision audit | planned |
| R32 | 三重验收：契约、故意失败、人工小样本；无未解释 P0 才 beta | v4 spec §16 | `v4/tests`、beta report | C/F/H 全集 | requirements report | planned |

## 当前审计结论

- 已落地仅指规则/边界/目录层已写入，不能等同于实现验证。
- R01-R04、R06-R18、R20、R22-R32 在 v4 实现完成前不得宣称完成。
- R19 的历史决策已进入 v4 规则文件，但仍需测试证明没有与三级策略冲突。
- v3 旧脚本和报告仍保留为 legacy 证据，不自动改写为 v4 结论。
