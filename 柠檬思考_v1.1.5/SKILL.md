---
name: lemon-thinking
description: "当用户说「柠檬思考」「/柠檬思考」「柠檬」「拷问」「Grilling」「grill me」「开始研发」「做个新功能」「重构」「superpowers」「总控」「研发全流程」，或研发侧出现症状「不知道现在处于哪个阶段」「设计文档/工单该不该有」「该不该问人类」「未经同意就开工」「跳过红绿直接修 bug」时，用本技能按工作区物理文件证据做阶段判定与推进对齐。不适用：用户只点名 gstack 单点模式命令（/office-hours、/ceo-review、/eng-review、/investigate、/retro 之一）且不要求按物理文件证据自动判定阶段并单向棘轮推进时，归 gstack-flow，不触发本技能；短剧产线与剧本（小说转短剧）、资产提取、视频提示词等专用工序技能各自执行工序，但研发侧总控入口仍是本技能；modules/ 各模块不作独立技能注册。"
triggers:
  - 柠檬思考
  - /柠檬思考
  - 柠檬
  - 拷问
  - 开始研发
  - 做个新功能
  - 重构
  - superpowers
  - grill me
author: 柠檬才不萌
license: Proprietary
metadata:
  display_name: 柠檬思考

  version: "1.1.5"
  upstream-pin: "改写自 obra/superpowers @ commit 8ca22dba9a94f28898bbce59f2537ff4d87c747d（上游 v6.4.2，MIT）；详见同目录 UPSTREAM-PIN.txt / UPSTREAM.md"
---

> 来源：基于obra/superpowers的上游模块，并包含柠檬思考自有总控规则、触发路由及本地适配；模块层采用UPSTREAM.md列明的7条映射。上游MIT署名与本地许可边界分别保留。

# 柠檬思考 (Lemon Thinking) — 研发全流程总控

## 概述（核心原则）

将**需求对齐、任务计划、测试驱动开发、质量终验**凝练为自闭环中枢：**不需要人类手动切换模式**——每次被唤醒，先按工作区的物理文件证据判定所处阶段并严格推进。

**本文件是骨架（单一事实源），规则正文全部在 `modules/` 的 MODULE.md 里**，按阶段 Read，不重复抄写。

> **宿主能力映射**：宿主提供 Skill/子代理工具时，按该宿主机制加载本技能与模块；没有 Skill 工具的宿主，用其受支持的技能加载或文件读取方式直接读 `modules/<阶段>/MODULE.md`——各宿主工具对照见 [modules/using-superpowers/references/](modules/using-superpowers/references/)。无论用哪种方式，物理文件状态机判定与「先问后做」审批约束不减免，也不要求宿主并不存在的工具。

## 一、物理状态机（收到指令先静默自检）

```
【触发本技能】→ 当前工作区存在哪些交付文件？
  ├─ 无设计文档 .................. 状态 1：Grilling 单步拷问对齐（modules/brainstorming）
  ├─ 有设计方案，未拆工单 ........ 状态 2：Writing Plans 拆原子工单（modules/writing-plans）
  ├─ 有工单，尚有 - [ ] ......... 状态 3：SDD 按 TDD 红绿推进（modules/subagent-driven-development、
  │                                        executing-plans、test-driven-development）
  └─ 工单全 - [x] ................ 状态 4：终验把关与交付（modules/verification-before-completion）
```

## 二、中文工件路径合同（唯一事实源）

| 产物 | 落盘路径 |
|---|---|
| 设计方案 | `文档/柠檬/设计/YYYY-MM-DD-<主题>-设计方案.md` |
| 实施计划（工单） | `文档/柠檬/工单/YYYY-MM-DD-<任务>-实施计划.md` |
| 运行期工作区 | 仓库根 `.lemon/`（SDD 工作区、进度台账；不污染 git 跟踪区） |
| 分支收尾 | 见 `modules/finishing-a-development-branch/MODULE.md` |

## 三、四大门槛（摘要；展开见指针模块）

1. **先问后做**：人类明确同意前，不写业务代码、不搭脚手架；三段路由（Spike 2~3 句假设／Bounded 聊天内 1~2 轮／Architectural 全流程）见 `brainstorming`。
2. **一次一问**：每轮只抛一个决策问题，禁止并列提问轰炸。
3. **客观事实自己查、主观决策问人类**：代码库/配置/环境自己检索；提问必附推荐项与 trade-off。
4. **单向棘轮**：发现潜在复杂性只准提高严谨度，不准偷懒降级。
5. **自主裁决**（执行期）：不违反 Spec 的歧义不停下来打扰人类，按 `柠檬裁决: [决定] - [理由] - [代价]` 记录在案。

## 四、模块索引（不注册为独立技能，仅供按阶段 Read）

> 门面文件统一为 `MODULE.md`（改名自上游 SKILL.md，避免 15 个幽灵技能条目）。来源与映射表见 `UPSTREAM.md`。

| 阶段 | 模块 | 用途 |
|---|---|---|
| 状态 1 | `brainstorming` | 需求发散与设计对齐 |
| 状态 2 | `writing-plans` | 工单拆解（原子化、可测试） |
| 状态 3 | `subagent-driven-development` | 子 Agent 隔离执行 + 任务级评审 |
| 状态 3 | `executing-plans` | 主会话按单推进（含并行会话路径） |
| 状态 3 | `test-driven-development` | 红绿循环 |
| 状态 3 | `dispatching-parallel-agents` | 并行派发 |
| 状态 3 | `systematic-debugging` | 疑难 Bug 系统排查 |
| 状态 3 | `using-git-worktrees` | 隔离工作区 |
| 状态 4 | `requesting-code-review` / `receiving-code-review` | 评审双向 |
| 状态 4 | `verification-before-completion` | 终验清单（全量回归／零占位符／现场清理／构建零警告） |
| 状态 4 | `finishing-a-development-branch` | 分支收尾 |
| 全程 | `writing-skills` | 新技能撰写规范 |
| 全程 | `using-superpowers` | 元技能导航（各运行环境工具对照） |
| 排障 | `diagnosing-superpowers` | 会话复盘诊断（新建，随 2026-09-21 上游同步纳入） |

## 五、骨架层速查（判据与入口，规则正文仍回对应模块 Read）

> 本节只汇总「本骨架这一层」可当场拍板的判据——状态机自检、路径合同、门槛触发线。展开细则一律 Read 上表对应模块，不在此抄录。

| 我要判什么（本层） | 当场判据 | 落点/入口 |
|---|---|---|
| 现在处于哪个状态 | 只看工作区物理文件证据：无设计文档→1；有设计未拆工单→2；有工单且尚有 `- [ ]`→3；工单全 `- [x]`→4 | 状态 1~4 分别 Read 第四节模块索引对应行 |
| 设计方案落哪 | 文件名带日期前缀、中文主题 | `文档/柠檬/设计/YYYY-MM-DD-<主题>-设计方案.md` |
| 实施工单落哪 | 同上，按任务命名 | `文档/柠檬/工单/YYYY-MM-DD-<任务>-实施计划.md` |
| 运行期工作区落哪 | 仓库根、不进 git 跟踪区 | 仓库根 `.lemon/` |
| 该不该先问人类 | Spike 2~3 句假设可自证／Bounded 聊天内 1~2 轮／Architectural 走全流程 | 三段路由细则见 `brainstorming` |
| 执行期遇歧义 | 不违反 Spec 的歧义不打断人类，就地裁决 | 记 `柠檬裁决: [决定] - [理由] - [代价]` |

**骨架自检示例（本层唯一示例，只用第一节状态机与第四节指针，不展开模块细则）**：被唤起时列工作区 `文档/柠檬/设计/`、`文档/柠檬/工单/`（PowerShell：`ls 文档\柠檬\设计, 文档\柠檬\工单`）。若现场是「设计方案 `2026-09-25-<主题>-设计方案.md` 已存在、实施计划 `2026-09-25-<主题>-实施计划.md` 里仍有 `- [ ]`」，判为**状态 3**，Read `modules/subagent-driven-development/MODULE.md` 与 `modules/test-driven-development/MODULE.md` 后按单推进（预期行为：不重开设计、不再问已裁决项）；工单全部转 `- [x]` 后的下次唤起即判**状态 4**，改 Read `modules/verification-before-completion/MODULE.md`。

## 六、骨架层常见坑（红旗）

- 现象：跳过静默自检、凭对话印象猜阶段就开工。成因：没先扫工作区物理文件证据。处置：收到指令先按第五节判据定位状态 1~4，再 Read 对应模块，未定位不动手。
- 现象：把规则正文抄回本骨架，导致骨架与 `modules/*/MODULE.md` 两处各说一套。成因：图省事在总控里就地展开细则。处置：本文件是单一事实源骨架，只留状态机/路径合同/门槛摘要与指针，细则一律回模块 Read，改规则只改模块。
- 现象：工件写到仓库根裸目录或英文路径，破坏唯一事实源。成因：没按第二节中文工件路径合同落盘。处置：设计方案、工单按 `文档/柠檬/…` 命名落盘，运行期产物只进仓库根 `.lemon/`，不得污染 git 跟踪区。
- 现象：人类尚未明确同意就写业务代码或搭脚手架。成因：越过「先问后做」门槛。处置：拿到人类对方案的明确同意前不产出代码/脚手架，属 Architectural 的必须走全流程对齐。
- 现象：一轮抛出多个并列问题轰炸人类。成因：违背「一次一问」。处置：每轮只抛一个决策问题，且必附推荐项与 trade-off；客观事实自己查、主观决策才问人。
- 现象：发现潜在复杂性时为省事降低严谨度。成因：违背「单向棘轮」。处置：只准上调严谨度档位，不准偷懒降级。
- 现象：无测试就修 bug、跳过红绿循环。成因：违背 TDD 门槛。处置：先写失败测试再修复，红线见 `modules/test-driven-development/MODULE.md`（MANDATORY. Never skip / Never fix bugs without a test）。
- 现象：未经人类确认就在 main/master 分支直接开始实现。成因：跳过隔离工作区约定。处置：按 `modules/executing-plans/MODULE.md` 红线先确认或走 `using-git-worktrees` 隔离。
- 现象：把 `modules/` 注册为独立技能。成因：无视第四节口径。处置：模块仅供按阶段 Read，不注册（避免幽灵技能条目）。
- 现象：把 frontmatter description 写成阶段链摘要（触发词后接着串「需求对齐、架构、拆解、推进、终验」）。成因：SDO 陷阱——描述里写流程，代理会照描述干活而跳过正文（真实案例：描述写一次 review、正文要求两次）。处置：description 只放触发词、症状与不适用谓词；阶段判定一律按第一节物理文件状态机。回归门：`tests/test_skill_discovery_sdo.py` 四判据。
- 现象：用户只点名 gstack 单点模式命令（/office-hours、/ceo-review、/eng-review、/investigate、/retro 之一）就被本技能以「总控/拷问/研发流程」抢接。成因：与已安装技能 gstack-flow 触发词碰撞。处置：按 description 排除谓词让位——判据是「是否点名单点命令且不要求物理文件证据棘轮推进」这一可观察事实，不是语气印象；两条路径都沾的整活才进本技能状态机。
