---
name: dramacraft
description: 当用户说「小说转短剧」「小说转剧本」「写短剧剧本」「批量生成短剧集」「校验剧本质量门」「跑产线」「跑整条产线」「dramacraft-pipeline」「/dramacraft」时使用本技能（小说转短剧剧本引擎，单门面技能，交付物止于剧本文本与质量门报告）；「质量门不过」「回炉重试」「断点续跑」「缓存安检」「伏笔未回收」「退出码 1/2/3」「时长门凑闸」「收尾报告冒工程黑话」这类症状也归本技能诊断；「编剧室压榨前 3 集」「无人值守跑长篇」这类说法同样构成触发。不适用：分镜/镜头组之后的配音、渲染与剪映合成（交付物是 render_plan/成片，归成片合成）、资产提取、H3 视频提示词精修等下游工序（本树各有独立技能）。
author: 柠檬才不萌
license: Proprietary
metadata:
  display_name: 小说转短剧

  version: "1.5.4"
---

# DramaCraft — 小说转短剧剧本引擎

## 概述

一个引擎、一个门面。内部是五模块流水线（输入适配 → 多节点网关 → 分集生成 → 质量门自愈 → 多格式导出），对外只有本 SKILL.md 一个入口。确定性质量门负责"零容忍的机械错误"，模型只负责"戏本身"。

**工作目录**：先 `cd` 到本 SKILL.md 所在目录（下称技能根），下文路径与命令一律相对技能根执行；仓内文件布局清单见 `references/repo-layout.md`。

## 速查（入口与命令，均在技能根执行）

| 我要做什么 | 命令/入口 |
|---|---|
| 无人值守跑长篇 | `python run_batch.py --input "小说.txt" --outline "outline.json" --workdir workspace --target-seconds 150 --formats markdown csv json html` |
| 环境健康体检 | `python run_batch.py --doctor`（可选 `--probe-api`） |
| 仅校验任意剧本 | `python run_batch.py --validate-only <剧本.json> --outline <大纲.json>` |
| 仅聚合导出（不调模型） | `python run_batch.py --outline outline.json --target-seconds 90 --assemble-only` |
| 纪要预审 | `python -m dramacraft.premise_lint <premise.json>` |
| 单集创作纵深体检 | `python -m dramacraft.continuity_lint <workdir> --outline outline.json --art art.json`（`--json` 落盘） |
| 词表体检 | `python -m dramacraft.vocab_stats <workspace>` |
| 12 道质量门 CLI | `skills_runtime/dramacraft_patch/validate.mjs`（G1~G10 + G11 文学病 + G12 AI味，契约详见 `references/WORKFLOW.md`） |
| 五个子模块自测（Node，无需模型） | `node skills_runtime/<模块>/scripts/selftest.mjs`，`<模块>` 取 novel-art / novel-characters / novel-outline / novel-script / novel-storyboard；全过各打印 `✓ N 项自测全部通过`，非 0 退出即断链 |
| 命名工作流跑全链 | 在 ZCode 里跑 `dramacraft-pipeline`（见「产线工作流」一节） |
| 改过引擎后自检 | `python -m pytest tests/ -q` |
| 退出码含义 | 0 成功交付 / 1 质量门未过或终扫未过 / 2 预检违规拒跑 / 3 部分未完成可续跑 |

## 开工硬门禁（v1.0.1 起为铁律，违反即事故）

**第一回合只准问参数。** 拿到小说/题材后的第一次回复必须问齐下列三件事；问齐之前，禁止产出大纲、剧本、任何分场内容：

1. **单集目标时长**：秒数（常见 90~150），**全剧统一一个值**，逐集不同须用户明说。
2. **集数与改编范围**：先做前 N 集还是全本；输入源形态（TXT/章节/节选）不确定时直接丢 ingestor，它能自适应。
3. **导出格式**：导演台本 md / 场记拆解表 csv / 机读底稿 json / 验收报告页 html，默认四件套让用户挑。

严禁反塞业务参数：时长门不过只能把剧本写厚或删水，不得改 `targetSeconds` 凑闸。引擎两层硬防御与模型节点池三级自动发现细则见 `references/hard-gate-details.md`。

## 双轨制工作流

### 轨道 A：编剧室单步压榨孵化（前 3 集基准 / 单元剧选题会）

轨道 A 八步协议全文（素材卡池入库、柠檬思考三大铁律、P1~P9 纪要预审门、大纲转译、12 门质检、盘问军规五条、心理契约三档、粗稿 Fallback 四字段）详见 `references/track-a-writers-room.md`。

### 轨道 B：无人值守批量跑长篇

完整跑批命令块（含 `--art` 门10对账与 `--concurrency` 并发可选项）见 `references/track-b-command.md`。

大纲加载即过 O1-O6 预审门（不过拒跑）。跑批机制细则（断点续跑、自愈回炉、预算熔断、缓存安检、伏笔账本、术语哨兵、恢复卡等）与单点命令示例全文见 `references/track-b-run-details.md`；大纲变更走登记（模板 `references/大纲变更单模板.md`）。

### 退出码合同

速查表已列四档含义；各档处置指引全文与 `--validate-only` 单集模式边界见 `references/exit-codes.md`。

## 交付物与收尾报告（白话铁律）

向用户收尾禁用工程黑话（`validate.mjs`、「12 道门禁」「自愈循环」），交付物一律行业名：导演台本 / 场记拆解表 / 机读底稿 / 验收报告页 / 生产记录；收尾必附逐集时长对照表，说话人必须人名。五件套契约与白话铁律全文见 `references/deliverables.md`。

## 产线工作流（v1.5.0：一条命令跑全链）

命名工作流 `dramacraft-pipeline` 一条指令跑完导演规划、镜头组、监督、会议与终验导出；两形态边界、五张规范卡（在 `skills_runtime/dramacraft_patch/spec_cards/`）、人工拍板四类、起跑前三条全文见 `references/pipeline-workflow.md`，机器契约见 `references/WORKFLOW.md`。

## 自检（改过引擎后必跑）

```bash
cd <技能根> && python -m pytest tests/ -q   # 预期见 README 的当版基线（v1.4.0 起不再写死数字）
```

详细契约（outline 结构、beat schema、12 门清单、F/T 门字段契约、导出格式规范、产线工作流合同）见 `references/WORKFLOW.md`，按需 Read，不通读。

## 常见坑（红旗清单）

- 现象：时长门不过，报告里 targetSeconds 被悄悄改小。成因：反塞业务参数凑闸。处置：严禁——只能把剧本写厚或删水；引擎有硬防御（缺 `--target-seconds` 拒跑、逐集目标与统一值冲突拒跑、无论过不过都裸露逐集时长对照）。
- 现象：收尾报告对用户冒出 `validate.mjs`、「12 道门禁」「自愈循环」这类工程黑话。处置：一律用行业名——导演台本 / 场记拆解表 / 机读底稿 / 验收报告页 / 生产记录（白话铁律）。
- 现象：改 frontmatter 时把「三轨：…；…；一条指令依次跑全链」等流程摘要写回 description。成因：SDO——description 写流程会让代理照描述干活不读正文（本轮实测判红）。处置：description 只留触发词与症状词，流程细则留在正文「双轨制工作流」「产线工作流」两节；闸门 tests/test_skill_discovery.py。
- 现象：用户笼统说「跑产线/最后一英里」时被本技能接走。成因：与「成片合成」触发词互相吸引。处置：可观察谓词——交付物止于剧本文本与质量门报告归本技能；分镜之后要配音渲染出片的归成片合成（两侧排除句已互写）。
- 素材卡池盘问军规、旧缓存对账、`C01` 编号回潮、终验硬闸拒交付四条红旗全文见 `references/红旗细则.md`。

## 契约指针 · H3 写法派生关系（机器可读，勿手删）

<!-- pipeline-contract:begin
id: dramacraft-v150-h3-spec-mirror
namespace: storyboard
schema: schemas/storyboard.schema.json
version: "1.0.0"
skill_version: "1.5.4"
h3_spec_source: "novel-storyboard 已内化 skills_runtime/novel-storyboard/references/h3-prompt.md（v1.5.1 副本，与 v1.4.0 逐字同哈希）"
h3_spec_role: derived
derived_from: "~/.agents/skills/minimax-official-workflows/config/workflows/drama-series/reference/h3-video-prompt.md"
# 字段契约指针原集中在被隔离的 v1.4.0（PIPELINE.md D10）；旧版本被 v1.5.1 全覆盖后，
# script / storyboard 两块生产侧字段契约随下面两块移入本树，D10 就此收口。
pipeline-contract:end -->

<!-- pipeline-contract:begin
id: dramacraft-script
namespace: script
role: producer
schema: schemas/script.schema.json
version: "1.0.0"
skill_version: "1.5.4"
id_prefixes: [C, S, P]
fields:
  - source
  - episodes
  - version
  - params
  - episodes[].ep
  - episodes[].targetSeconds
  - episodes[].hook
  - episodes[].cliff
  - episodes[].hookBeat
  - episodes[].beatsClaimed
  - episodes[].foreshadowsPaid
  - episodes[].foreshadowsPaid[].id
  - episodes[].foreshadowsPaid[].evidence
  - episodes[].scenes
  - episodes[].scenes[].sceneId
  - episodes[].scenes[].location
  - episodes[].scenes[].lighting
  - episodes[].scenes[].characters
  - episodes[].scenes[].props
  - episodes[].scenes[].flow
  - episodes[].scenes[].flow[].type
  - episodes[].scenes[].flow[].action
  - episodes[].scenes[].flow[].speaker
  - episodes[].scenes[].flow[].line
  - episodes[].scenes[].flow[].delivery
pipeline-contract:end -->

<!-- pipeline-contract:begin
id: dramacraft-storyboard
namespace: storyboard
role: producer
schema: schemas/storyboard.schema.json
version: "1.0.0"
skill_version: "1.5.4"
id_prefixes: [C, S, P, E, SC, SH]
h3_spec_source: "novel-storyboard 已内化 skills_runtime/novel-storyboard/references/h3-prompt.md（该子技能 SKILL.md:7-8，声明不依赖外部 skill）"
h3_spec_role: derived  # C6：本副本只是内化派生，不删（运行时要读），但不再自称独立事实源
derived_from: "~/.agents/skills/minimax-official-workflows/config/workflows/drama-series/reference/h3-video-prompt.md"  # 唯一权威=官方 drama-series 骨架；改权威须同步本副本
fields:
  - source
  - episodes
  - lang
  - style
  - promptLang
  - params
  - episodes[].ep
  - episodes[].segments
  - episodes[].segments[].id
  - episodes[].segments[].sceneIndex
  - episodes[].segments[].cuts
  - episodes[].segments[].h3Prompt
  - episodes[].segments[].soundscape
  - episodes[].segments[].note
  - episodes[].segments[].cuts[].shot_id
  - episodes[].segments[].cuts[].beats
  - episodes[].segments[].cuts[].seconds
  - episodes[].segments[].cuts[].size
  - episodes[].segments[].cuts[].angle
  - episodes[].segments[].cuts[].camera
  - episodes[].segments[].cuts[].characters
  - episodes[].segments[].cuts[].props
  - episodes[].segments[].cuts[].frame
  - episodes[].segments[].cuts[].dialogue
  - episodes[].segments[].cuts[].dialogue_refs
  - episodes[].segments[].cuts[].recipe
  - episodes[].segments[].cuts[].note
pipeline-contract:end -->
