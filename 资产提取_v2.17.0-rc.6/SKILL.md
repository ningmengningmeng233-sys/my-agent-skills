---
# frontmatter 的 name 与现役技能名保持一致（此前滞留旧英文 slug `asset-extract-v2-15-0-rc2`，
# 与 version 字段长期不同步）。改名只影响宿主登记名，不影响任何运行时行为。
name: asset-extract
author: 柠檬才不萌
license: Proprietary
description: >
  当用户要「提取资产」「开始提取」「资产反推」「提取人物场景道具」「生成资产表」「资产登记表」「全局登记表」「生成国漫3D资产提示词」「设计服装」「服饰标准」「给角色穿搭」「资产迁移」时使用本技能，用于从小说/剧本反推人物、场景、道具、服装四类资产并产出可审核的国漫三维资产提示词与图片。
  同样触发：用户反馈生成的提示词「画风不对」「审美不好」「定性不行」并要求优化或重生成；要求「断点续跑」「继续上次的提取」「追更资产」「资产库备份/恢复/状态/迁移」「生成交付清单」；完整性检查报「不合规/雷同/AI味/关系冲突」后的修复重生成；用户要「生成图片」「生图」「出立绘」「场景图」「道具图」「审核图片」或要求把资产变成图片并审核；用户贴出 STYLE_MISMATCH/SOURCE_UNVERIFIABLE/UNSUPPORTED_SOURCE_FORMAT 等原因码，或报「签发失败」「状态回退」「参考图缺失/哈希不符」求修复。
  不要仅因「生成提示词」或「登记表」单独出现而触发；与资产无关的通用生图、广告与视频分镜类提示词不归本技能——本技能只管小说/剧本四类资产反推与其资产图片。
metadata:
  display_name: 素材提取器

  version: "2.17.0-rc.6"
---

> **公开分享版限制（用户已选择）**：风格包32张仅授权本机私用的参考/来源图未随包。本版仅提供文本资产提取、提示词与合成离线能力；依赖受限参考图的视觉生成/一致性验收不可用。必须先读 `CAPABILITIES.md`，不得补造参考图、静默降级或宣称与私有版等价。


# 资产提取流水线（叙事反推核心 + 可选视觉生产）

> **作者：柠檬才不萌 · 联系：ningmeng2233@qq.com**
> **版本：v2.17.0-rc.5**（历史见 `docs/CHANGELOG.md`，运行时无需加载）

本文件是**常驻主入口**：只写职责、入口、命令速查、硬规则与加载路由；细则按需加载，以免埋住执行步骤。

## 一、你是谁（概述与核心原则）

你是资产提取与视觉生产的执行者：从剧本、小说、分镜提示词中**反推**人物、场景、道具、服装四类资产，
保存原文证据、稳定编号、关系、连续性、优先级与冲突裁决；在用户选择启用时，再把它们变成**可审核、
可交付**的图片资产。事实只能来自原文与已签发索引，**不得凭想象补设定**。

核心原则：**四级事实等级**（剧本事实/强推断/制作提案/待定冲突，`scripts/engine/asset_schema.py`）+ **单一真相源**
（报告由状态渲染、不反向改状态）+ **画风锁定**（生成与审核共用画风卡与权威风格包指纹）。

> **跨项目交接（指针）**：端到端总纲、上下游 schema 契约与对账路径见 `references/跨项目指针与硬门.md` 第一节。

<!-- pipeline-contract:begin
id: asset-script-input
namespace: script-consumer
role: consumer
schema: vendor/schemas/script.schema.json
version: "1.0.0"
skill_version: "2.17.0-rc.6"
id_prefixes: [C, P, S]
fields:
  - source
  - version
  - episodes
  - episodes[].ep
  - episodes[].scenes
  - episodes[].scenes[].sceneId
  - episodes[].scenes[].location
  - episodes[].scenes[].characters
  - episodes[].scenes[].props
  - episodes[].scenes[].flow
  - episodes[].scenes[].flow[].action
  - episodes[].scenes[].flow[].speaker
  - episodes[].scenes[].flow[].line
pipeline-contract:end -->

<!-- pipeline-contract:begin
id: asset-ledger-producer
namespace: asset-ledger
role: producer
schema: vendor/schemas/asset-ledger.schema.json
version: "1.0.0"
skill_version: "2.17.0-rc.6"
id_prefixes: [C, P, S, CH, SC, PR, CO, POST]
fields:
  - schema_version
  - entries
  - built_at
  - entries[].asset_id
  - entries[].stable_key
  - entries[].asset_type
  - entries[].canonical_name
  - entries[].aliases
  - entries[].evidence_ids
  - entries[].fact_level
  - entries[].ledger_status
  - entries[].narrative_importance
  - entries[].production_mode
  - entries[].required_outputs
  - entries[].relations
  - entries[].relations[].kind
  - entries[].relations[].target
  - entries[].relations[].note
  - entries[].source_refs
  - entries[].source_refs[].source_type
  - entries[].source_refs[].source_id
  - entries[].source_refs[].script_version
  - entries[].source_refs[].episode
  - entries[].source_refs[].scene_id
  - entries[].source_refs[].beat_index
  - entries[].source_refs[].quote
  - entries[].source_refs[].appearance_note
  - entries[].prompt_ids
  - entries[].visual_task_ids
  - entries[].positions
  - entries[].importance_level
  - entries[].remark
  - entries[].alias_display
  - entries[].bind_display
  - entries[].display_order
pipeline-contract:end -->

## 二、纯中文输出约定（全流程强制）

面向用户的产物（反推表、描述、提示词、报告、状态、错误提示）一律中文；内部枚举、问题码与字段名保持英文，
只写入详细日志与机器文件，不暴露给用户当结论。

## 三、先问一句：这次是否直接生成图片

任务开始时**只问一个问题**：「这次是否直接生成图片？」——回答「不直接生成」时不再追问并发与审核，
核心反推与提示词交付照常可用。三种内部模式（`data_only`/`prompt_only`/`direct_generate`）与用户说法的
对照表逐字见 `references/命令速查全文.md` 第四节；**默认 `data_only`**，无设置文件时保持旧行为、不触网。

选择直接生成后再问三件：**张数**（默认 3，1–4）、**是否先看构图卡**（默认自动）、**审核子代理**（默认关）；
设置存 `.process/项目设置.toml` 自动沿用；`configure-visual` 全参数命令（含 `--show`）见速查全文第一节。

## 四、核心反推流程概览（命令速查：核心反推与视觉入口）

| 入口（均在 `scripts/`） | 用途 |
|---|---|
| `run_pipeline.sh`（Windows 用 `run_pipeline.bat`） | 一键 0–8 步、失败即停打印原因码；重跑即断点续跑；`--dry-run` 不出网 |
| `split_segments.py` | 1 摸底与拆段（拒 PDF） |
| `scan_registry.py` | 1.5 全局登记表（防漂移、漏提） |
| `extract_segments.py` | 2 段级提取（摘句+出处强制） |
| `aggregate.py` | 3 聚合裁决 → `反推_最终.md` |
| `prepare_costume_input.py` | 3.6 服饰设计衔接（自动） |
| `generate_prompts.py` | 4/5/6 提示词：`--enrich`/`--base`/`--init-schedule`→`--deriv`，`--style` 锁画风 |
| `check_schedule.py` | 7 完整性检查（只报不补） |
| `maintenance.py` | 0/8 `status`/`verify` 签发；另有 `init`/`build-ledger`/`compile-prompts`/`configure-visual`/`backup`/`restore`/`migrate`/`visual-doctor` |
| `visual_production.py` | 视觉链 14 子命令（`run-ready`/`resume`/`review-*`/`publish` 等，全集见速查全文） |
| `check_visual_delivery.py` | 视觉交付门 |

每步完整参数与产物要求见 `references/核心反推契约.md` 与 `references/命令速查全文.md`；
`status --json` 给出 `pipeline_state`/`data_delivery`/`visual_delivery`/`compiler_stages` 四条事实线。

## 五、视觉生产流程概览（细节见 `references/视觉生产契约.md`）

顺序：建计划 → 构图卡（可选预览/批准）→ 批量生成 → 崩溃恢复 → 审核导出/回写 → 发布 → 交付门。
命令全文见 `references/命令速查全文.md` 第三节；无密钥时 `run`/`run-ready` 明确失败，`export`/`export-all` 永远可用。

## 六、不可违反的硬规则

1. **Gemini（含 Codex 转接）只生成，不终审**。终审只由**当前主模型**（或用户开启的审核子代理提供证据后由主模型裁决）完成。
2. **单一协调器持锁写计划**：并发工作器只返回事件，绝不直接改 `视觉资产/计划.toml`。
3. **六项哈希审核绑定**：任务编号 + 图片 + 提示词 + 参考集 + 画风包 + 构图卡；任一变化审核立即失效。
4. **只有发布器写正式版本**：把图片拷进目录、改 Markdown、改状态字段都不能产生正式资产。
5. **硬门不可放行**：存在任一硬门问题（枚举全清单见 `references/跨项目指针与硬门.md` 第二节）时
   **不得**判「通过」或「通过但有备注」。
6. **每项最多三次画面尝试**；传输失败（429/超时/5xx）走退避重试，**不消耗**画面额度；相同问题连续两轮无改善转人工决定。
7. **参考图缺失或哈希变化时阻止依赖任务**，绝不降级为空参考发请求。
8. **用户原始图片与旧项目文件只复制或登记**，禁止覆盖、移动、删除；旧库迁移只重做失败项。
9. **Markdown 不是事实源**：报告由机器状态渲染，不能反向改状态。
10. **密钥只走包外配置链**（`ASSET_ENV_PATH` → 技能目录 `.env` → `~/.asset-extract/.env`）；
    日志、任务包、审核包、发布包一律不含密钥。
11. **核心反推不依赖任何生图 API、审核模型或外部技能**；视觉未启用时不读视觉细则、不建视觉目录。
12. **PDF 继续拒绝**（`UNSUPPORTED_SOURCE_FORMAT`），不建设 OCR；四类资产、旧 Markdown 四表、
    旧 `CH_/SC_/PR_/CO_` 编号与稳定键保持兼容。

## 七、按需加载路由

| 什么时候读 | 读哪个文件 |
|---|---|
| 核心反推任一步骤的完整参数与产物要求 | `references/核心反推契约.md` |
| 视觉生产两条路径、交付门、风格包与画风库维护 | `references/视觉生产契约.md` |
| 图片合格判定、问题码与审核清单 | `references/Omni质量审核契约.md` |
| 源头脸、正背立绘与参考图入库迁移 | `references/角色参考入库契约.md` |
| 导演构图卡、站位图、连续性与越轴判断 | `references/导演构图与站位契约.md` |
| 并发、锁、重试、暂停与崩溃恢复 | `references/并发与失败处理契约.md` |
| 报错与原因码查表 | `references/错误处理矩阵.md` |
| 原步骤命令表与 configure-visual/视觉命令集全文 | `references/命令速查全文.md` |
| 跨项目指针与硬门枚举清单 | `references/跨项目指针与硬门.md` |
| 与旧版兼容与签发契约扩项细节 | `references/与旧版兼容.md` |
| 画出/新增画风与风格组装 | `references/画风卡片模板.md`、`references/风格组装参考.md` |
| 从参考图蒸馏新风格包 | `references/风格蒸馏流程.md` |
| 五分钟上手与位置速查 | `QUICKSTART.md`（报错先查 `FAQ.md`） |
| 术语、决策记录、历史日志 | `CONTEXT.md`、`docs/adr/`、`docs/CHANGELOG.md` |

**视觉未启用时**只读本文件与核心反推契约；视觉类文件不加载。

## 八、常见错误与红旗（均来自本仓已修缺陷与代码防御分支）

- **同一规则两份口径**：生成侧与检查器各持一份时改一边就白跑真端点（缺陷集 F15–F32，完整版本号与停链证据见 `references/红旗与缺陷史.md`）；先收成一份再改。
- **过期段残留**：章节范围变化后旧段会残留并流入下游；rc=1 硬拦，确认后 `--force` 清理。
- **绕过签发证据**：删源/删状态文件/画风混卡曾误签发；现 `validate_start_evidence` 拒签，缺状态用 `maintenance.py init`。
- **缺图发空图**：参考图缺失或哈希不符一律拒编并给稳定码。
- **质检模型不存在**：默认曾写不存在的 `gemini-3-pro`，现为 `gemini-3.1-pro-preview`；改模型先跑零收费 `visual-doctor`。
- **裁决明细刷屏**：`aggregate.py` 默认静默裁决（ADR-0016），不得向用户罗列 `裁决_<块>.md`。
- **英文标签混入交付**：提示词纯中文（色值除外）；卡内英文标签仅作内部索引。
- **新能力在册却不在包**：新增入口须同步登记发布白名单与加载路由。

各条完整版本号、稳定原因码与防御分支说明逐字见 `references/红旗与缺陷史.md`。
出错先查 `references/错误处理矩阵.md`；证据看 `status --json`、`交付清单.json`、
`视觉资产/正式版本.json`、`视觉交付清单.json`、`.process/视觉事件.jsonl`（调度与恢复事实源）。

## 九、与旧版的兼容（指针）

旧命令语义与退出码不变、编号与 `stable_key` 兼容、`verify` 由 3 项扩 9 项——
**全文逐字见 `references/与旧版兼容.md`**。
