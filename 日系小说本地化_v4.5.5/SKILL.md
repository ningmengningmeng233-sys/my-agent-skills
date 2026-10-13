---
name: jp-novel-localization
description: 当用户要「日系小说本地化」「中国化改写」「去日系腔」「本地化审查」「继续改写某章节」「跑 rewrite/check/approve/merge 章节流水线」，需要把中文日系小说稿改写为地道中国网文并过确定性质量门，或遇到「台版腔」「日式翻译腔」「必替换残留」「passed 不能 merge」「盲审结论/确认来源 缺字段」「PromptBudgetExceeded」时使用（旧称「日文小说本地化」、英文名 jp-novel-localization，同一技能）。不适用：把日文原文译成中文的翻译任务、仿已索引作家文风写作（成人文学索引）、通用中文去 AI 味文本清理（去AI味）、小说转短剧剧本（小说转短剧）。
author: 柠檬才不萌
license: Proprietary
metadata:
  display_name: 日系小说本地化

  version: "4.5.5"
---

# 日系小说本地化 v4.5.4

> **方向声明**：本技能的「本地化」= 把中文稿里的日系腔改写成地道中文网文（去掉日本外壳），**不是**译自日文的翻译任务。

## 概述

核心原则：模型只产改写稿，质量判定全部由确定性代码完成；状态推进必须有检查报告与指纹证据，交付只认一条主线，任何环节不得跳步、不得自评通过、不得手工改状态。

## 命令速查

调用形态 `& $launcher <命令> <workspace> [--chapter <id>]`。

| 命令 | 用途 |
|---|---|
| `assets` | 预检：资产、索引库与 LLM 配置确认（配合 `scripts/doctor.ps1`） |
| `init` / `ingest` / `confirm-boundary` | 建工作区；接入源稿；确认章节边界与指纹 |
| `resolve-events` / `decide` | 事件图解析；用户决策确认 |
| `rewrite` / `check` / `repair` | 单章改写；确定性检查；按证据返修 |
| `approve` / `merge` / `publish-split` | 批准（唯一质量闸门）；合并；平台切分交付 |
| `versions` / `rollback` | 列出不可变版本；撤销批准指针 |
| `status` / `blockers` / `pause` / `resume` / `recover` / `refresh` | 状态查看与异常恢复 |
| `consistency` / `card` / `lib` | 全书一致性旁车；卡工厂；索引库管理 |

## 目标与边界

- 输入必须是中文小说稿；不把日文原稿直接当作本地化输入。
- 保留人物目标、事件顺序、因果链、情感结果和世界观功能。
- 现实日本外壳按剧情功能适配；西幻、架空力量体系和非日本文化默认保留。
- 剧情级改动、源稿矛盾、译名冲突和平台审核必须由用户决策，不能静默修改。
- 正式合并只接受 manifest 判定为 `approved` 或 `delivered` 且指纹复核通过的章节。
- `passed`（检查通过）不是交付状态：必须先 `approve` 批准版本，`passed → approved → delivered` 是唯一交付主线。

## 当前已完成

- 状态机收敛：`rewritten/checking/repairing/rechecking` 冗余状态已删除；`passed` 不可直接合并。
- 正式 Prompt 全部经 ContextCompiler Token 预算编译并原子落盘 `chapters/<id>/prompts/`；检查报告强绑定 `checks/vNNN.json`（version/output_sha256/prompt_run_id/ruleset_sha256）；approve 经 `state/approval-journal.json` 两阶段事务提交 continuity 与 SQLite；merge 只读 `approved_version` 锁定正文；KnowledgeHit 协议收敛后外部库只读零写入（FTS 缓存重定向 `<workspace>/.cache/knowledge/`）。
- 七类门齐备：五项长篇质量门 + 去 AI 味与长短两极节奏门 `texture_check`。开发史详单见 `v4/CHANGELOG.md`。

## 运行入口

使用 Skill 自带启动器，不要依赖当前工作目录：

```powershell
$skillRoot = "<实际 Skill 根目录>"
& "$skillRoot\scripts\novel-localization.ps1" <command> <workspace>
```

把 `<实际 Skill 根目录>` 换成你安装本技能的目录（Codex 默认约 `$env:USERPROFILE\.codex\skills\jp-novel-localization`，其它宿主以各自技能根为准）。下文 `$launcher` 指 `"$skillRoot\scripts\novel-localization.ps1"`（先定义 `$launcher` 再跑各步骤）。

## Step 0：启动预检

先执行：

```powershell
& "$skillRoot\scripts\doctor.ps1"
& "$skillRoot\scripts\novel-localization.ps1" assets
```

确认 Python/uv、工作区权限、索引库和 LLM 配置。索引库是默认增强项；找不到时必须提示用户指定路径或明确使用 `--no-index-lib`。写作指导找不到时可以继续，但必须诚实记录为未启用。

真实改写前，默认后端缺少 API key 时应停止；只有显式使用 `--backend placeholder` 才允许测试占位稿。

## Step 1：建立工作区并接入源稿

```powershell
& $launcher init <workspace> --title <书名> --author <作者> --language zh-CN
& $launcher ingest <workspace> <原文件或目录>
& $launcher confirm-boundary <workspace>
```

先检查章节边界和输入指纹；插图、目录、版权页等非正文由 ingest 按规则分流，不要手动混入。

## Step 2：确认事件与决策

```powershell
& $launcher resolve-events <workspace> --approve-all
& $launcher decide <workspace>
```

复杂或高风险节点必须逐项确认。源稿矛盾未闭环时，章节保持阻塞，不得 rewrite、check 或 merge。

## Step 3：单章改写

```powershell
& $launcher rewrite <workspace> --chapter <chapter-id>
```

一次只处理一个章节工作项。改写 Prompt 由 ContextCompiler 按 Token 预算编译：系统规则 > 源稿段落 > 事件图 > 已接受决策 > 锁定词典 > 连续性快照 > 风格卡 > 章节索引 > 写作指导；必选块超预算直接失败（`PromptBudgetExceeded`），绝不静默截断。外部参考按复杂度注入：普通章最多 1 张风格卡和 1 条章节索引，复杂章最多 2+2；不得复制示例原句，不得把参考资产当作事实依据。

每次正式 Prompt 运行会原子落盘不可变 `PromptManifest`（块清单/哈希/预算）到 `chapters/<chapter-id>/prompts/`，供检查绑定与回放审计。

`rewrite` 只生成纯正文，不负责宣布质量通过，也不要求模型自我评分。输出段落数、事件、人物和锁定词必须保持可定位。

## Step 4：独立检查与返修

```powershell
& $launcher check <workspace> --chapter <chapter-id>
```

检查由确定性代码完成：正文纯度、台版腔、日式翻译腔、必替换残留、段落证据、事件对照、改写深度、篇幅和指纹，以及五项长篇质量门（死亡人物行动、同一时间多地出现、锁定术语残留、提前剧透、未回收承诺）。

检查通过时写入版本绑定报告 `checks/vNNN.json` 和状态提案 `state_deltas/vNNN.proposed.json`；此时章节为 `passed`，但尚未提交长篇状态（continuity/SQLite 提交点在 approve）。

检查失败时章节进入 `rework`。返修只能使用检查证据和失败段落；返修后必须重新 `check`。自动返修最多两轮，之后转 `needs_review` 等待人工处理。不要直接编辑 `manifest.json` 改状态。

## Step 5：审批、合并与发布

```powershell
& $launcher approve <workspace> --chapter <chapter-id> --version v001
& $launcher merge <workspace>
& $launcher publish-split <workspace> --platform <platform>
```

`approve` 是唯一质量闸门：核验检查绑定报告与版本正文哈希一致后，执行两阶段提交（SQLite 事务 + continuity），章节进入 `approved`；任何失败都会拒绝批准并保留 `passed` 状态。

合并前必须确认没有 `error`、`rework`、`needs_review`、`passed` 章节，并通过指纹复核；`merge` 只读取各章 `approved_version` 锁定正文，绝不读取未锁定草稿。合并失败时保留旧成品，不覆盖已交付输出。

查看或回滚不可变版本：

```powershell
& $launcher versions <workspace> --chapter <chapter-id>
& $launcher rollback <workspace> --chapter <chapter-id>
```

`rollback` 只清除批准指针并把章节退回 `needs_review`（带 `ROLLED_BACK` 阻塞项），物理历史版本一律保留。

## 批稿路径的交付字段（与脚本判定同源）

按批稿路径交付（每章一份 `*-批NNN.md`，由 `scripts/merge_batches.py` 合并）时，批稿「本批说明」区
有两个字段被脚本机械判定，字段名必须与 `references/流程指南.md` 的模板逐字一致：

- `盲审结论: 通过`（或 `返工（原因）`）——`状态: 已完成` 的必要条件；缺该字段即判「待盲审」，合并时该批标红。
- `确认来源: 用户拍板·批<NNN>·选项<X>`——红线桥段的确认凭证；缺该字段视为红线未确认，AI 无权自行宣布已确认。

六件套验收细则、证据句三验与返工条款见 `references/流程指南.md`。字段改名必须同改
`流程指南.md`、本节与 `merge_batches.py` 三处——契约用例「回归-文档模板字段=脚本判定」会拦住漏改。

## 异常恢复

```powershell
& $launcher status <workspace> --verbose
& $launcher blockers <workspace>
& $launcher recover <workspace>
& $launcher resume <workspace>
& $launcher refresh <workspace>
```

输入、事件、决策或规则变化后，旧输出视为过期，必须按 blockers 指引重新处理。锁文件残留可以由程序自愈；不要删除锁文件来绕过并发保护。`continuity/state.json` 是 SQLite 的派生缓存，损坏时自动从 `state/novel_state.db` 重建，不要手工编辑。

## 常见错误 / 红旗

- 把 `passed` 当交付直接 merge：merge 只接受 `approved`/`delivered`，必须先 `approve` 提交状态。
- 手改 `manifest.json` / `continuity/state.json` 改状态，或删锁文件绕过并发保护：指纹/审计对账会当场发现并降级；锁文件与 continuity 均可由程序自愈重建。
- 源稿矛盾未闭环就 rewrite/check/merge：章节必须保持阻塞，不许带病推进。
- 静默跳过索引库或缺 key 时用占位稿冒充真实改写：前者必须让用户指定路径或显式 `--no-index-lib`，后者仅 `--backend placeholder` 明示后允许。
- 批稿漏写 `盲审结论: 通过` 或 `确认来源: 用户拍板·批<NNN>·选项<X>`：分别判「待盲审」与红线未确认（v4.5.1 因漏挂字段被契约用例当场判红，见 CHANGELOG）。
- 实验版（`experimental_risk`）混进正式合并：永不参与。

## 硬边界

- 外部索引库/写作指导目录严格只读：全文索引缓存只写 `<workspace>/.cache/knowledge/`。
- 数据库或状态提交失败必须立即阻断 approve 与 merge；可选的风格增强降级时写审计继续。
- 实验版（`experimental_risk`）永不参与正式合并。
- 审计与状态提案只追加不改写；任何绕过状态机的手工编辑都会被指纹/审计对账发现并降级。

## 质量声明

- 全量测试 592 passed / 5 skipped（2026-09-23 离线实跑；含 `scripts/` 30 条契约用例与 1 条收集守卫，全部经 pytest 收集）。
- 所有质量门（含五项长篇门、节奏门与去 AI 味约束）与状态提交均为确定性代码，可离线复现。
- 每次正式生成可经 `PromptManifest` + `checks/vNNN.json` + `approval-journal.json` 三重证据回放审计。

## 按需读取的详细文档

- 卡工厂：`docs/card-factory.md`
- 索引库管理：`docs/index-library.md`
- 状态机和恢复：`docs/workflow.md`
- API、重试和长章：`docs/llm-runtime.md`
- 长篇批处理：`docs/long-novel-operation.md`
- Core 契约：`docs/v4-core-spec.md`
- 测试矩阵：`docs/v4-core-test-matrix.md`

历史开发说明已归档到 `docs/SKILL-development-history-4.4.1.md`。
