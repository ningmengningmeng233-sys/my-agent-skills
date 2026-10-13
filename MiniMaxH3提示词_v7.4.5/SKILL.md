---
name: minimax-h3-craft
description: 当用户要为 MiniMax Hailuo H3（海螺视频，含 H3-Max、H3-Max-Turbo）编写、路由或严格审计工业级视频提示词时使用本技能；点名 H3 平台并要求模式裁决（T2V/I2V/文生视频/图生视频/首尾帧/尾帧生视频/Ref2VA 多模态参考，别名 t2v、i2v、fl2v、first-last-frame、l2v、multimodal）、要求审计凭证、短剧连续生产或 Mode A 写实/Mode B 国漫 3D CG 画风锁、英文官方六字段与 ComfyUI 节点格式时同样触发。触发词「H3」「海螺视频」「Hailuo」「工业级提示词」「提示词审计」「视频提示词」「分镜表转提示词」「六段式」「关键帧提示词」「NOT_RUN 凭证」「虚假 PASS 拦截」「提示词不生效」。不适用：小说转剧本、资产提取等上游工序（另有独立技能）；Seedance、可灵、即梦等非 MiniMax H3 平台的通用视频提示词各有专用技能，除非用户点名 H3/海螺，不抢占。
author: 柠檬才不萌
license: Proprietary
user-invocable: true
metadata:
  display_name: MiniMax H3制作

  version: 7.4.5
---

# MiniMax H3 工业级提示词工程规范 (v7.4.5)

## 概述

把 H3 提示词写作做成确定性工程：模型能力以 `config/` 为单一事实源，不支持的组合硬阻断（Fail-Closed）；写法按固定骨架输出，交付必须附真实审计凭证，没跑脚本就如实写 `AUDIT_STATUS: NOT_RUN`。关键取舍：宁可显式阻断、不静默替用户猜；静态规则审计永远不等于真实出片成功。

## 速查（模式·命令·决策一屏）

| 我要做什么 | 入口/命令 | 备注 |
|---|---|---|
| 判定输出协议 | `native-drama-series`（短剧连续生产）/ `native-general`（通用视频）/ `ref2va-en`（明确点名英文/Ref2VA/ComfyUI 时） | 见 §2 |
| 选生成模式 | t2va / i2va / fl2va / l2va / ref2va | 别名与能力边界见 §1，单一事实源 `config/model-capabilities.json` |
| 静态规则审计 | `python scripts/audit_cli.py prompt.txt --mode auto --warn-policy report` | 返回 0 合格，非 0 阻断交付 |
| 复核验证凭证 | `python scripts/verification_protocol.py check` | BLOCKED-CRED 两档未解锁前不得宣称通过 |
| 采集闭合门证据 | `python scripts/collect_closure_evidence.py --local-run-dir <批次>`（维护重验专用） | 六档闭合门第 1、2 档当场执行；收件人自验用上两行命令 |
| 挑输出档位 | A 直接（默认纯净）/ B 标准（含设计解析）/ C 制作（多镜头连戏台账） | 见 §5 |
| 按任务加载参考 | §3 按需加载路由表 | 普通任务初始 ≤3 份，绝不整目录加载 |

## 0. 启动引导与交互填空卡协议 (Onboarding Protocol)
当用户输入模糊意图时，先输出引导卡原文逐字段填空（`references/onboarding-card.md`）；非阻塞判定与「判定不了不得静默二选一」协议全文见 `references/onboarding-protocol.md`。

## 1. 模型能力判定与生成模式裁决
模型能力边界严格以 `config/model-capabilities.json` 为单一真实事实源，执行 Fail-Closed 强校验，不支持的组合硬阻断。

### 1.1 规范生成模式与兼容别名
5 大规范模式 `t2va`/`i2va`/`fl2va`/`l2va`/`ref2va` 与外部别名归一化：映射表唯一事实源 `scripts/router.py`，全文详见 `references/model-mode-adjudication.md`。

### 1.2 生产模型能力约束 (按 config/model-capabilities.json)
能力现场读 `config/model-capabilities.json`（公开 API 边界另见 `config/provider-capabilities.json`），Fail-Closed 硬阻断。两条常驻红线：H3-Max-Turbo 内部条目不得当可调用；H3-Max 单图 `i2va`/`l2va` 路由层阻断。镜像漂移处理与核对记录见 `references/model-mode-adjudication.md`。

### 1.3 验证层级四级区分 (Verification Hierarchy)
严禁把静态审计混淆为模型真实生成成功。四级验证与六档闭合门的逐级定义、BLOCKED-CRED 解锁条件与登记要求全文见 `references/verification-hierarchy.md`；未解锁前不得宣称通过。

## 2. 输出协议判定
- **native-drama-series**：短剧工作流、连续台词对白、角色深度表演，使用固定六段式骨架。
- **native-general**：通用商业视频、风景、概念特效镜头，使用“全局基准 + 镜头演进”流式结构。
- **ref2va-en**：仅在明确要求“英文 / Ref2VA / ComfyUI节点”时触发，使用官方六字段。

### 2.1 多片段与整集时长合同（短剧连续生产）
- 六段式是单条提示词骨架、组数动态规划、片段默认4—10秒、剧本声明时长为权威、本段无对白保留声音签名段、普通任务不加载全量 references；合同五条全文见 `references/duration-contract.md`；多片段/整集台账另读 `references/duration-ledger.md`。

> **跨项目交接（指针）**：全文见 `references/cross-project-handoff.md`（逐字外移，可反查）。

<!-- pipeline-contract:begin
id: h3-shot-table
namespace: h3-shot-table
role: consumer
schema: schemas/storyboard.schema.json
version: "1.0.0"
skill_version: "7.4.5"
id_prefixes: [SC, SH]
h3_spec_source: "MiniMaxH3提示词规范 v7.4.2（本技能 SKILL.md 全文 + config/ 能力事实源）"
h3_spec_role: derived  # C6：工艺化派生（模型能力事实源仍是 config/model-capabilities.json），写法骨架的权威在官方 h3-video-prompt.md
derived_from: "minimax-official-workflows:drama-series/reference/h3-video-prompt.md"  # 唯一权威=官方 drama-series 骨架（上游逻辑名；作者机绝对路径已去除，包内写法段为可执行副本；改权威须同步本技能写法段）
columns:
  - { column: 镜头号, path: "episodes[].segments[].id" }
  - { column: 时长, path: "episodes[].segments[].cuts[].seconds" }
  - { column: 景别·角度, path: "episodes[].segments[].cuts[].angle" }
  - { column: 画面内容, path: "episodes[].segments[].cuts[].frame" }
  - { column: 对白/台词, path: "episodes[].segments[].cuts[].dialogue" }
  - { column: 声音, path: "episodes[].segments[].soundscape" }
pipeline-contract:end -->

## 3. 按需加载路由（任务类型 → 初始最小参考集）

| 任务类型 | 初始参考集 |
|---|---|
| single-native-drama（单段短剧） | `references/native/drama-series-h3.md` |
| multi-segment-drama（多片段/整集短剧） | `references/native/drama-series-h3.md`、`references/duration-ledger.md` |
| continuation（跨镜接戏/续写） | `references/continuation-bridging.md`、`references/blocking-memory.md` |
| dialogue（对白与声音） | `references/dialogue-audio.md` |
| high-risk-action（动作指导/高风险镜头） | `references/action-director.md`、`references/high-risk-shots.md` |
| review（质量评审/排障） | `references/review-checklist.md`、`references/failure-guards.md` |

**增量加载（按触发读取，不进初始集）**：共 9 组触发项（通用动作→英文官方标准），组名与文件逐条映射见 `references/incremental-loading.md`——命中哪个触发条件才读哪几份，未命中不加载。

**长期规则**：普通任务初始参考 ≤3 份、一次不加载整个 references 目录（`references/craft/` 兼容卡与 legacy/CHANGELOG 不进普通初始集）；长输出写入 `.logs/` 只报告结论与证据路径；每阶段结束更新当前执行摘要（字段照 `templates/execution-summary.md` 填；柜内过程件不随发布包分发），上下文占用 70%—80% 建检查点；子代理只接收最小上下文——五条全文见 `references/long-term-loading-rules.md`。

## 4. 核心生成规则
1. **英文 Ref2VA 规则**：风格描述位于 `[Shot 1]` 之前；`[Shot 1]` 严禁时间戳；`[Shot 2]` 及后续必须携带递增 `[Shot N] At MM:SS.mmm, ...`；四类标签（`<Subject N>`、`<Picture N>`、`<Video N>`、`<Audio N>`）须在 `subject_definitions` 闭环；保留标记限 `fully_preserved` / `partially_preserved` / `attribute_transfer` / `weak_reference`。
2. **中文 Native 规则**：开篇空间锁（`全画幅画面，遵守对话轴和 180 度原则...`）；参考图声明 `图N = 名称，锁定具体属性`，宫格图强制置于末行；对白必须在 `【画面演进】` 引号中且说话人在 `【角色声音签名】` 中定义；`【声音设计】` 严禁混入 BGM；Mode A 必须以 `写实真人摄影质感；真实皮肤质感；自然真实的人类面部特征，带有轻微的生理不完美。` 结尾；Mode B 严禁真人皮肤质感词。
3. ⚠ `[Xs-Ys]` 区间窗口是本地自创约定，输出到外部系统前必须按目标平台语法转换（官方写法与细则见 `references/official-h3-grammar.md`，本条全文见 `references/core-generation-rules.md`；处置口径见「常见坑与红旗」）。

## 5. 输出契约（三档标准）

- A 直接（默认纯净，未跑审计必须如实 `AUDIT_STATUS: NOT_RUN`，严禁虚假 PASS）/ B 标准（前置创作理解与关键假设）/ C 制作（多镜头连戏台账）——三档字段级说明全文见 `references/output-tiers.md`。

三档可复制的模板全文（含凭证块两种填法）见 `references/output-contract.md`。

## 6. 自动校验与命令行门禁
```powershell
python scripts/audit_cli.py prompt.txt --mode auto --warn-policy report
```
返回 0 为合格，非 0 阻断交付。未运行时标注 `AUDIT_STATUS: NOT_RUN`；实际运行时输出真实数值与 SHA-256。

## 常见坑与红旗

- 现象：`[Shot 1]` 带了时间戳。成因：误以为所有镜头格式统一。处置：第一镜严禁时间戳，`[Shot 2]` 起必须携带 `At MM:SS.mmm,`（§4.1）。
- 现象：`[Xs-Ys]` 区间窗口贴进 MiniMax 控制台/ComfyUI 后不生效。成因：该语法是本地自创约定，官方与社区均无。处置：输出到外部系统前按目标平台转换，官方镜内时间写法是 `[Shot N] At MM:SS.mmm` 切点（§4.3）。
- 现象：`【声音设计】` 里混入 BGM。成因：套用通用视频习惯。处置：中文短剧协议全程同期声、严禁 BGM；仅英文 `ref2va-en` 允许 `non_diegetic_music`。
- 现象：Mode B（3D CG/国漫）成片出现真人毛孔/皮肤质感词。成因：误带 Mode A 材质锁。处置：Mode B 严禁真人皮肤词；Mode A 反之必须以 §4.2 的写实质感句结尾。
- 现象：校验凭证写着 PASS 但从未运行 audit_cli。红旗：虚假 PASS 属交付事故。处置：未执行必须如实写 `AUDIT_STATUS: NOT_RUN`，运行后输出实际数值与 SHA-256（§5A/§6）。
- 现象：`<Subject N>`/`<Picture N>` 等标签在后续章节首次出现才定义。处置：四类标签必须在 `subject_definitions` 闭环，先定义后引用（§4.1）。
- 现象：意图模糊时静默二选一 Mode A/B 或工作流。处置：遵循 §0 非阻塞原则，生成前用一句话复述推断或缺口请用户确认——选错即整段返工。
- 现象：frontmatter description 退化为工序链或裸顿号触发词清单（SDO 陷阱：描述写流程会让代理照描述干活而跳过正文——真实案例：描述写一次 review、正文要求两次）。处置：description 只留触发条件与不适用谓词、用户原话入「」；工序以 §0—§6 正文为准；`_审查测试/test_skill_discovery_sdo.py` 四判据常驻把关，改 description 先跑它。
