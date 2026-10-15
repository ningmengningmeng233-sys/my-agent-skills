---
name: jp-novel-localization
description: >
  中文日系小说稿的可审计中国化本地化。v4 以单章节工作项、事件图、用户决策、manifest 状态和事件级质检为核心；
  最大程度吸收本地 v3 legacy 规则、用户爽文索引库与写作指导资产；索引库是默认强制项，调用本 skill 的第一步必须先运行
  `python -m v4.core.cli assets` 展示索引库/写作指导证据，再向用户收集原文件；索引库和写作指导作为只读增强层，不能绕过事实硬门槛。
  当前仍处于开发版，未达到 Beta；C/F 自动测试已全覆盖（186 passed），H 人工验收尚未完成，不会把开发版输出宣传为最终质量保证。
  触发词：「日系小说本土化」「日本小说中国化」「日语小说本地化」「把这部日本小说改成中国的」「无痕汉化」「全面中文化」「日系小说本地化审查」「本地化审查」。
metadata:
  version: "4.4.1-dev"
---

# 日系小说本地化 v4.4.1（开发版）

> **当前入口已切换到 v4.4.1-dev。**
> v3.2.0 legacy 资产仍保留在本目录的 `references/`、`scripts/`、`templates/` 和 `索引库/` 中，作为只读参考与历史基线。
> v4 当前不是 Beta：C/F 自动测试已全覆盖（186 passed），H-001~H-010 人工验收尚未执行完毕，不能把开发版输出宣传为最终质量保证。

## 启动检查清单（agent 每次被调用必须执行，未完成不得进入改写）

### 第 0 步：先亮出参考资产，再问原文件

1. **先运行**：`uv run --project v4 python -m v4.core.cli lib list` 和 `uv run --project v4 python -m v4.core.cli assets`（或 `--workspace <工作区>`），把已登记索引库、当前主库、写作指导的实际路径、资产条数和可用性**明确打印给用户**。
2. 索引库默认**强制要求**：`rewrite` 已默认 `--require-index-lib`。找不到可用索引库时必须停下来向用户要 `--index-lib <路径>` 或让用户把书放入 `索引库/`，**不得只问原文件后继续**。
3. 只有用户明确说“不用索引库”时，才允许 `rewrite --no-index-lib`，并在回复中说明本次已显式降级。
4. skill 自带 `索引库/` 只作兜底；发现顺序：`--index-lib <路径>` ＞ 工作区 `.enhance.env`/`.env` 的 `INDEX_LIB_DIR` ＞ 环境变量 ＞ **本机自备索引库（真实用户索引库）** ＞ **skill/索引库（内置兜底）**。

### 索引库调用与本地规则（v4.3.0 审查整改，质量优先）

- `rewrite` 默认自动加载索引库；使用证据写进 `manifest.json` 的 `enhancements.reference_assets.enabled/used` 和 `audit.jsonl` 的 `enhance_use/enhance_unavailable`——**没读就是没读，不许假装读过**。
- 每次 LLM 改写默认注入硬指令：台版腔清零、日式翻译腔清零、锁定已替换词（禁止把蘑菇弹改回核弹等）、去 AI 味、章末钩子。
- `references/中国化规则.md`、`references/风格基准.md`、`references/去AI味-问题清单.md`、`references/质检规范.md` 的规则摘录默认进 prompt。
- `check` 执行 v3 回迁的台翻腔/台版用词/繁体扫描（`v4/rules/tone-scan.json`），字典已补台→中词（软体→软件、网路→网络、土司→吐司……）。
- 索引库按章节标题+正文信号推导场景（打脸/战斗/感情线/吐槽…）检索文风卡与章节索引的「创作手法/金句」条目，不再拿长标题硬匹配导致零规则注入。
- `rewrite` 注入「中国网文感改写指令」：强制重写句式、围观式打脸、金句、情绪外放、对话口语化。
- `check` 新增改写深度门 `style_depth`：去掉映射词后仍与原文相似度 ≥0.96 判 fail、≥0.90 判 blocked——只换词不重写的浅改写无法通过。
- 运行入口见下文“运行入口”；`assets` 命令是启动检查专用证据命令。


## 当前已完成

- v4 Core 独立实现目录：`v4/core/`
- 单文件/文件夹只读快照与谨慎章节切分
- 无标题边界确认机制；超长章内部技术分段
- 源稿预检：确定矛盾/疑似矛盾/可解释差异
- “第二天/四天”“第一次/第二次”序数防误报
- 事件图结构、高风险节点与决策闭环
- A/B/C 决策与项目级/章节级决策文件
- 正文纯度、段落 ID、事件级对照、必替换残留、篇幅异常提示
- manifest 唯一状态真相、审计、完整 SHA-256、原子写、锁和正式合并
- 实验/待人工版与正式成品隔离
- 用户爽文索引库只读 Enhance：`v4/enhance/index_lib.py`
- 用户写作指导库只读 Enhance：`v4/enhance/writing_guide.py`
- 增强引用 SHA-256 和 `enhance_use`/不可用/跳过审计
- 预检矛盾自动生成 A 档决策项，`decide` 一键确认，`refresh` 对账重开
- 事件图规则分层（normal / low_confidence / cultural_function），可选 `--event-engine=llm`
- 改写后端：规则层应用译名决策，在线整章调用 LLM，失败/拒答/段落数不符整章回退
- `pause` / `resume` / `recover` 与写操作异常自动进 `error`
- `ProjectLock` PID/时间戳 + 崩溃残留锁自愈
- `align-events` 必须 `--confirm` 并写审计；已收紧重定基准漏洞
- ingest 自动分离后记/插图/目录等非正文；output 指纹失效检测
- 项目译名冲突纳入决策闭环；平台审核启用时强制等待审核
- 真实验收：《影之强者》第二卷 11 章全量 DeepSeek 改写并合并成品
- 全书级一致性检查：`consistency` 命令（译名落实/标题/序号/词典残留，只读旁车）
- Enhance 接入：`rewrite --index-lib/--writing-guide` 可选只读参考，prompt 注入结构/规则，审计 enhance_use/unavailable；`check` 写作指导旁车加载并诚实报告“语义未自动判定”
- 盲审执行器：`blind-review --result pass/blocked/fail` 写入 manifest/checks 并迁移 passed/rework
- 平台审核执行器：`platform-review --platform <name> --result pass/blocked/fail`；未配置平台一律拒绝
- 事件确认三模式：`resolve-events --list / --event-id <id> [--reject] / --approve-all`
- 风格软评估：6 维启发式指标（网文语感/爽点词/AI味/情绪/叙述者声线/读者观感提醒）写入 checks.soft_evaluations，不参与硬门槛
- 附录策略：后记/附录/致谢/人物介绍/设定集/书评走完整本地化流程并作为附录输出；插图/目录/版权/彩页跳过。真实书籍已于 v4.3.0-dev 新硬门槛下重新验收：12/12 delivered（含附录），style_depth 0.45~0.58，consistency error=0。
- 发布拆章：`publish-split --platform <name>` 把 delivered 章节拆成单章文件并生成 index.json
- v4.3.0 审查整改：v3 台翻腔扫描回迁为硬门槛（喔/耶/嘛/喽/呐/的说/软体/网路/繁体…）；句尾呢/啦密度预警
- v4.3.0 审查整改：改写 prompt 默认注入本地规则硬指令；词典锁定替换防止 LLM 把「蘑菇弹」改回「核弹」
- v4.3.0 审查整改：字典新增台→中词表（软体/网路/部落格/计画/滑鼠/硬碟/超商/捷运/土司/社群软体…）；合并前剥离重复标题；章末钩子启发式预警
- v4.3.0 审查整改：索引库默认自动发现并写 manifest/审计；skill 自带最小兜底索引资产；`--require-index-lib` 质量优先模式
- v4.3.0 审查整改：文风卡解析器修复（此前真实索引库注入 prompt 的规则为空）；按场景信号检索文风卡+章节索引创作手法
- v4.3.0 审查整改：新增「中国网文感改写指令」与 `style_depth` 改写深度门，只换词/只删台腔的浅改写 fail/blocked
- v4.3.0 真实验收：DeepSeek 全量 12/12 passed（style_depth 0.43~0.58、consistency error=0）；长章分块容差与台版腔定向返修已固化
- v4.3.0 质量专项：逐段重写硬约束、翻译腔句式黑名单、动作戏短句专项、未重写段占比门、项目级 `voice-hints.json` 可选声线表
- v4.3.0 章末钩子专项：跨书共识钩子指令 + 章末定向返修 + 钩子启发式增强；全卷 consistency error=0 / warning=0
- v4.3.0 索引库三件套：反例/禁止清单单独注入；原文只提炼结构摘要不喂原句；句面相似度 warning 审计
- v4.3.0 卡工厂：`card init/samples/verify-anchors/judge/register/index/status/list` 全链路；默认目录 `卡工厂/`；已验证卡优先自动入选
- v4.4.0 索引库管理：`lib list/add/remove/refresh/status`；登记文件 `卡工厂/index-libraries.json`；开始菜单新增“索引库管理”


## 当前未完成 / Beta 前必须补齐

### 核心项（已实现，仅留全书规模验收）

1. **源稿矛盾自动转决策项闭环**
   - `precheck.py` 检测到的疑似矛盾需自动生成 A 档 review 决策项写入 `chapters/<id>/decisions.json`。
   - 未决前章节保持 `awaiting_decision` 并阻塞 `rewrite/check/pass`；用户决策后只影响相关章节。
    - 对应测试：`test_failure.py:F-006`、`test_human_acceptance.py:H-005 auto`。
    - ✅ 已实现：`ingest` 自动生成 + `decide` 一键确认 + `refresh` 对账。

2. **并发锁与原子写入压力验收**
   - 两个进程同时更新 `manifest.json` 时不得静默覆盖，一个成功另一个失败或重试。
    - 对应测试占位：`test_contract.py:C-071`。
    - ✅ 已实现：`ProjectLock` PID/时间戳 + 残留锁自愈 + 并发拒绝测试。

3. **运行时异常与状态恢复**
   - 工具/运行异常需自动把章节迁移到 `error` 状态并写入审计，人工确认后进入 `needs_review`。
   - `paused` 恢复需回退到暂停前安全状态；输入指纹变化时不得直接继续旧正文。
   - `needs_review` 校验通过后回到 `ready_for_rewrite` 或暂停前状态；仍失败则保持或转 `rework`。
    - 对应测试：`test_contract.py:C-072`、`C-073`、`C-074`。
    - ✅ 已实现：`error/recover/pause/resume` 命令 + 自动进 `error` + 指纹体检。

### 不阻塞 Core Beta 的 Enhance/Optional 项

4. **盲审流程**
   - 启用开关、盲审结果写入 `checks.json`、状态机 `awaiting_review → passed/rework`。
   - 对应测试：`test_contract.py:C-061/C-062/C-063`、`test_runtime.py:blind-review`、`test_failure.py:F-003/F-019`。
   - ✅ 已实现：`blind-review --result pass/blocked/fail` + C-063/F-003 返工隔离测试。

5. **平台审核与平台规则包**
   - 平台规则包加载、审核结果记录、未加载时诚实报告“未执行”。
   - 对应测试占位：`test_contract.py:C-062/C-063`、`test_failure.py:F-016`。

6. **索引库/写作指导接入 Core pipeline**
   - v4.3.0 起：索引库默认强制加载进 `rewrite` prompt，写入 manifest/audit；写作指导读取多模块规则与语料并旁车检查。
   - 相关需求：`docs/v4-requirements-traceability.md` R23~R30（只读/哈希/用途审计已实现；写作指导语义自动判定仍诚实 `not_run`，不在未实现前冒充已完成）。

7. **Optional 功能**
   - 文风蒸馏、发布拆章、高级 AI 味检测、多书并行、复杂索引库轮换。
   - 按 `docs/v4-core-spec.md §14`，Core Beta 前不实现。

### 普通使用质量视角下的急需项

即使不冲刺 Beta，日常想让 skill 产出可接受的本地化成品，也还需要补齐：

1. **真正的 AI 改写后端需稳定可用**
   - `placeholder` 后端只是原文规范化复制，**不做中国化改写**；
   - `default` 后端依赖外部 LLM API key（默认 DeepSeek），目前只经过小样本验证，未经过大量真实长章节稳定性测试；
   - 需要补齐 LLM 改写失败、内容过滤、空输出、超时重试后的降级策略。

    - ✅ 已实现：整章调用 + 失败回退 + 段落数/纯度/拒答校验；尚需大规模长章真实验收。
2. **源稿矛盾需自动提示并支持一键确认**
   - 当前检测到数量/时间/身份矛盾后只会把章节卡住，需要用户手动编辑 `decisions.json`；
    - 急需提供“发现矛盾 → 生成 A 档决策项 → 用户确认/修正 → 自动继续”的交互闭环。
    - ✅ 已实现：`decide` 一键确认，默认保留原文。

3. **事件图（故事大纲）提取质量需更稳**
   - 事件图是后续事件对照的基准，抓不准会导致误判剧情缺失或剧情被改；
   - 需要提升复杂段落、长对话、隐式因果的事件抽取准确率，减少 `world_function="待确认"` 等占位字段依赖。

    - ✅ 已实现：规则主 + LLM 可选增强，段落级事件 + 段内证据。
4. **异常、暂停、恢复需更健壮**
   - 程序崩溃后章节状态不会自动恢复；
   - `paused`/`needs_review` 的恢复流程未完善；
   - 单人使用影响较小，但想让 skill 成为可反复依赖的工具，这部分必须补。

    - ✅ 已实现：`pause/resume/recover` + 自动 `error` + 残留锁自愈。
## 硬边界

1. 输入必须是中文小说稿；v4 不负责从日文原稿翻译。
2. 普通本地化默认保留事件图、因果链、人物目标、关系变化、情感结果和世界观功能。
3. 采用三级本地化：必须替换 / 按剧情功能适配 / 默认保留。
4. 魔法、骑士、精灵和非日本来源的西幻/架空力量体系默认保留。
5. 剧情级变更、源稿矛盾处理和平台审核处理必须有用户决策，不能静默改写。
6. 正式合并只读取 manifest 判定为 `passed`/`delivered` 且指纹复核通过的章节。
7. 索引库和写作指导只读、默认自动发现并优先参考（可用 `--no-index-lib` 显式关闭）；它们不能替代事件图事实验收，也不能复制具体桥段、名场面或句面。
8. v3 legacy 批稿模板中的 `盲审结论:` 字段与红线 `确认来源` 字段仍与 `scripts/merge_batches.py` 保持一致；v4 Core 的盲审/红线流程尚未接入状态机。

## 索引库管理

```bash
# 列出全部已登记索引库
uv run --project v4 python -m v4.core.cli lib list

# 登记一个新的索引库（加入后自动成为可发现候选）
uv run --project v4 python -m v4.core.cli lib add "D:\新索引库路径" [--name 名字]

# 移除登记
uv run --project v4 python -m v4.core.cli lib remove <名称或路径>

# 刷新全部已登记库的卡索引/缺口清单
uv run --project v4 python -m v4.core.cli lib refresh

# 查看某个库状态
uv run --project v4 python -m v4.core.cli lib status <名称或路径>
```

登记文件：`卡工厂/index-libraries.json`

## 做卡入口（文风蒸馏/场景卡）

```bash
# 初始化卡草稿
uv run --project v4 python -m v4.core.cli card init --book 书名 --author 作者 --lib "<你的索引库路径>" --type style
# 场景卡
uv run --project v4 python -m v4.core.cli card init --book 书名 --author 作者 --lib "<你的索引库路径>" --type scene --scene 感情线

# 取样/锚点验证/判官/注册
uv run --project v4 python -m v4.core.cli card samples --card <卡工作区>
uv run --project v4 python -m v4.core.cli card verify-anchors --card <卡工作区>
uv run --project v4 python -m v4.core.cli card judge --card <卡工作区> --phase writer
uv run --project v4 python -m v4.core.cli card judge --card <卡工作区> --phase reviewer
uv run --project v4 python -m v4.core.cli card register --card <卡工作区>

# 索引/状态
uv run --project v4 python -m v4.core.cli card index --lib "<你的索引库路径>"
uv run --project v4 python -m v4.core.cli card status --card <卡工作区>
uv run --project v4 python -m v4.core.cli card list
```

默认卡工厂目录：`<skill>/卡工厂`（开发期目录，未随包）

## 运行入口

启动检查（每次必做，先于任何改写）：

```bash
uv run --project v4 python -m v4.core.cli assets
```

初始化 v4 工作区：

```bash
uv run --project v4 python -m v4.core.cli init "<工作区路径>" --title "<书名>" --author "<作者>" --language zh-CN
uv run --project v4 python -m v4.core.cli ingest "<工作区路径>" "<原文件或目录>"
uv run --project v4 python -m v4.core.cli confirm-boundary "<工作区路径>"
uv run --project v4 python -m v4.core.cli resolve-events "<工作区路径>" --approve-all
uv run --project v4 python -m v4.core.cli rewrite "<工作区路径>"            # 默认要求索引库；关闭需 --no-index-lib
uv run --project v4 python -m v4.core.cli check "<工作区路径>"
uv run --project v4 python -m v4.core.cli merge "<工作区路径>"
```

Core 设计规格：`docs/v4-core-spec.md`
manifest 契约：`docs/v4-manifest-schema.json`
旁车契约：`docs/v4-artifact-schemas.json`
需求追踪：`docs/v4-requirements-traceability.md`
资产整合：`docs/v4-asset-integration.md`
测试矩阵：`docs/v4-core-test-matrix.md`

## v4 工作区与状态

每章使用独立工作项，正文与审计旁车分离：

```text
<工作区>/
├── source/
├── chapters/<chapter-id>/
│   ├── source.md
│   ├── events.json
│   ├── decisions.json
│   ├── precheck.json
│   ├── output.md
│   ├── checks.json
│   └── audit.jsonl
├── project-decisions.json
├── manifest.json
├── audit.jsonl
└── output/localized.md
```

`manifest.json` 是唯一状态真相。正文不携带状态、计划、质检、映射说明或审计元数据。

## 参考资产

生产环境可配置：

```text
INDEX_LIB_DIR=<你的索引库路径>
WRITING_GUIDE_DIR=<你的写作指导路径>
```

实际接入遵循 `docs/v4-asset-integration.md`：

- 用户索引库默认自动发现并读取章节索引、小说索引、原文上下文、已验证文风卡和判官验证信息；加载不到会写 `enhance_unavailable`，可用 `--require-index-lib` 强制；
- 写作指导读取 `plot_logic` 五环、`character_consistency` 五维、`consistency_review` 六项、`anti_ai_voice` 四层及转场/对白/章末模块；
- 本地语料按 2-4 个正例、1-2 个反例检索并回查上下文；
- 全部外部资产只读并记录来源哈希；
- 无法读取时诚实记录，不得伪称已使用。

## 质量声明

v4 Core 目前能验证事实与交付安全，但不承诺自动保证：

- 文风一定像中国网文；
- 爽点一定“够味”；
- AI 味完全消除；
- 读者无法识别原作来源；
- 已通过任何平台专项审查。

完整 Beta 条件见 `docs/v4-core-spec.md` §16。
