# DramaCraft — 小说转短剧剧本引擎 v1.5.5

把任意格式的小说（单 TXT / 章节目录 / 无标题节选 / 零散灵感点子）自动改编为**可拍级短剧剧本**：前置编剧室单步压榨选题会（P1~P9 预审门），大纲六预审门守住改编的根，12 道确定性质量门零容忍机械错误并自愈回炉，专治 AI 改编十大顽疾（心理描写直搬、潜台词说破、对白辩论腔、形容词动作、章节结构照搬、高潮错位、信息失衡、一致性漂移、白开水/油腻两极、表面迭代）。

> 从 v1.3.x 及更早升级：整目录替换（先删旧 `dramacraft` 目录；若机器上还留有更早的 `novel-to-drama-script` 技能，一并删除——曾有分发事故是它抢触发所致）。你自己填过 key 的 `llm_nodes.json` 请留底放回。全部版本沿革见 `CHANGELOG.md`。

## 产线工作流（v1.5.4 引入·五阶接通·全产线工作流化）

多了一条**命名工作流 `dramacraft-pipeline`**：一条指令把 01 小说转剧本 → 02 导演规划 → 03 镜头组 → 逐集监督 → 会议 → 终验导出全串起来，
工人（写手/导演/镜头组/监督）由子代理担任，**确定性执法一条不退**（门族仍在 python 与 `validate.mjs` 里）。CLI `run_batch.py` 原样保留。

- **命令**（ZCode 里）：以 `dramacraft-pipeline` 起，参数 11 项；九阶段：盘点与原著转码 → **大纲预检 ＋ 读者会（写作之前）** → 剧本(12 门) → 导演规划(D 门族) → 镜头组(S 门族) → **全链终扫** → 监督 → 评审会 → 终验导出。
- **人只在四类节点被叫**：熔断升级 / 评审会 P0 / 监督红线 / 会议拍板；每问只给 2-3 个有限选项，等待人选不设超时。
- **起跑前三条**：① 引擎自己找得到（不要求拿技能目录当工作区，多份引擎同机时建议显式指认）；② `outlinePath` 与 `workdir` 必须在 ZCode 工作区内（工作区外的绝对路径会被世界读层直接拒），原著可以放工作区外；③ 原著 GBK/GB18030 不用预处理，工作流自己转 UTF-8 参考件。
- **装到哪**：把发布包里的 `.zcode/workflows/` 整片复制进你的工作区即可（详情见 `references/WORKFLOW.md` §15）。
- 引擎侧配套（两形态共用）：统一熔断 `breaker.py`、D 门族 `plan_lint.py`、S 门族 `shot_lint.py`、改稿失效链 `artifact_chain.py`、会议机读件 `meeting_pack.py`、五张规范卡 `skills_runtime/dramacraft_patch/spec_cards/`。

## v1.4.0 新增（创作纵深）

- **伏笔账本（埋了必须收）**：大纲 `episodes[].foreshadows` 登记、单集 `foreshadowsPaid` 认领兑现，账本为 append-only 事件流（并发零丢账、重跑即回滚）。
  **平时只提醒不拦**（缺证据/超期只在报告里 ⚠，绝不触发回炉——防为过闸硬塞假填坑）；**终验清零是硬闸**：交付前还有未回收伏笔就拒交付、点名清单、退出码 1，**12 道门全绿也不豁免**。放弃伏笔必须写明原因且经用户拍板。
- **命名与术语哨兵**：术语锁定三表（标准写法/禁用变体/替换记录）+ 泛化命名（小X/大X/老X）与同名不同人点名。单集命中 ⚠ 进回炉改名，终验残留 → 退出码 1；未提供人物/美术种子时显式「跳过」。附带结构雷同与句长节奏两项提示级数据。
- **AI 伪装打击全量包**：humanizer 24 类模式全量中文化入册（台词正则 23 条 + 动作行词表 144 条，**有字面义的词一律不收**——「对齐/打通/沉淀/底座」这类在正常动作行会误伤），每类配「改前→改后」中文示范；回炉指令命中新类别时自动携带示范，另加「写出人味」正向写作纪律（句长起伏/有立场犹疑/话留半句）。
- **写手上下文包同源**：原著章节参考 + 本集应回收 + 全剧未回收概览，**初稿与每一轮回炉共用**（同时修掉 v1.3.0「原文只在初稿、回炉即丢」的老毛病）。
- **续跑恢复卡**：启动即打印 4 行白话卡（剧名 / 已完成 x/y 集 / 未回收伏笔 n 条 / 下一步），数字全来自实扫磁盘与账本重放。
- **盘问军规与心理契约**：五条创作盘问军规（单模块一次收集、单会 ≤5 问、连问 ≤3 必给反馈、每问必有下游用途、拍板复述锚定）+ 评审开场三档心理契约（狠挑/均衡/鼓励，只调语气不调标准）+ 料不足粗稿四字段（`uncertainty`/`assumptions`/`upgrade_notes`/`rerun_hints`）。
- **大纲变更单模板**：`references/大纲变更单模板.md`，结构性改动留痕，配合缓存安检与账本回滚。
- **单集体检工具**：`python -m dramacraft.continuity_lint <workdir> --outline outline.json --art art.json`（白话报告，`--json` 落盘为三个独立顶层键，`validate.mjs` 的 12 门结构零改动）。

## v1.2.5 新增（灵感孵化选题会与单元剧模式）

- **编剧室单步压榨流（移植柠檬思考）**：
  - 一次一问，严禁并列轰炸；菜单制为主（2~3 互斥选项首位标 `[推荐]`），暗门自由发挥回锚校验，单向棘轮强制升级戏剧对抗；
  - 零散点子、金句、随笔自动打标入库至素材卡池 `<workdir>/inbox/cards.jsonl`（5 种类型），卡池已有的内容严禁重复盘问。
- **前提纪要预审门（P1~P9 确定性硬门）**：
  - 孵化纪要落盘 `<workdir>/state/premises/ep###.json`；
  - 强校验：P1四件套确权、P2不可逆绝境后果、P3单集角色≤4人且溯源、P4首尾纯物理动作禁文学病、P5离散标准档位（60s/90s/180s）、P6基调明确、P7卡池溯源、P8暗门回锚、P9明确授权。独立 CLI：`python -m dramacraft.premise_lint <ep###.json>`。
- **一键转译大纲与单元剧模式（Anthology）**：
  - `premise_to_outline.py` 将纪要一键转译为标准大纲（注入 `mode: "anthology"`），100% 自动通过 O1~O6 大纲门；
  - 单元剧模式下，`validate.mjs` 自动静默 X1 跨集回声告警（每集独立故事无角色交集属正常编排），直通 3 并发跑批！

## v1.2.1 新增（批量提速）

- **分集并发调度**：`run_batch.py` 默认开启 `--concurrency 3` 工作线程同时自愈生成不同集数（可传 `--concurrency 1` 降级为纯串行模式），大幅提速 60% 以上。
- **原子落盘保障**：单集缓存引入 `safe_write_json`，先写临时文件、flush+fsync 后原子换名，彻底杜绝断电或中断产生半截坏 JSON 污染断点。
- **并发安全共享状态**：`Ledger` 与 `gateway` 内部计费加锁，多线程 append 事件绝不混行，多线程并发撞闸立即广播熔断标并停止派发新调用。

## v1.2.0 新增（投产保障）

- **预算熔断**：`--max-calls` 设定模型调用上限（缺省自动按待跑集数装保险丝），防原地打转烧穿额度；触闸安全停批（退出码 3），已产出集数保全，原命令重跑自动续跑且不重复计费。
- **白话生产小结**：自动输出 `output/production_log.md`，每集状态、写作来源、回炉自愈次数一览无余，严格遵守白话收尾铁律，零技术黑话。状态只看本轮逐集安检结果（已验收在库 / 未交付 / 待处置，不拿汇总数猜）；来源栏有引擎生成记录才写模型，外部写件写「外部入库」，其余如实写「来源未记录」；模型调用次数标明是引擎账本口径，ZCode 子代理的调用不在其中。
- **缓存安检三查**：单集缓存注入 `_meta`（引擎版本、schemaVersion 2、大纲规范化哈希），断点检测三查防旧缓存带病通关；`--stale-cache`（ask 默认拒跑提示 / redo 清理重产 / trust 信任沿用）；汇编导出自动剥除 `_meta`。
- **结构化输出与自愈解析换代**：`validate.mjs` 支持 `--json` 输出 12 门结构化状态与命中明细，自愈解析彻底告别脆弱的文本行匹配状态机；无 `--json` 时终端输出比特级兼容。
- **词表命中体检工具**：`python -m dramacraft.vocab_stats` 汇总分析 `state/vocab_hits.jsonl`，产出高频命中 Top10 顽疾与零命中差集清单，提供规则扩充与精简依据，只读分析不私改词表。

## 退出码合同

| 退出码 | 状态 | 处置指引 |
|:---:|---|---|
| **0** | 成功交付 | 全部集数通过 12 道门终验，四件套工业交付物与生产记录已完整落盘 |
| **1** | 质量门未过 / 执行异常 | 达到自愈最大重试次数仍不合规；或**创作纵深终扫未过**（未回收伏笔、禁用称呼残留——12 门全绿也不豁免） |
| **2** | 预检违规拒跑 | 缺少必要参数、大纲 O1-O6 未过、**伏笔登记 F1 不合法**、逐集时长反塞或缓存安检过期未选策略 |
| **3** | 部分未完成（可续跑） | 预算熔断安全停批 / 回炉未收敛（escalate/abort 未交付集）/ 缺集防空；已完成集均已缓存，原命令重跑自动续跑且不重复计费 |

## 安装

1. 把本目录整体复制进你的技能目录（任选其一，跨运行时通用）：
   - `~/.agents/skills/dramacraft/`（Codex / Copilot CLI / Gemini / ZCode）
   - `~/.claude/skills/dramacraft/`（Claude Code）
2. 安装 Python 依赖：`pip install -r requirements.txt`
3. 确保本机有 Node.js ≥ 18（质量门 validate.mjs 用）。
4. 配置模型池：把 `llm_nodes.example.json` 复制为 `llm_nodes.json`，填入你自己的 API key。支持 OpenAI 兼容与 Anthropic 双协议、多节点自动故障切换。

## 使用

在支持技能（skill）的对话里直接说「**小说转短剧**」或输入 `/dramacraft`。引擎会**先问齐三件事**（单集时长/集数范围/导出格式）再动工——这是硬门禁，谁替你做主跳过这一步，就是在复刻事故。

- **轨道 A（协作轨）**：前 3 集基准在聊天里逐集打磨，大纲先过预审门，每集当场过 12 道门；
- **轨道 B（批量轨）**：长篇无人值守跑批，断点续跑，违规拍自动外科式回炉（最多 3 次）；
- **轨道 C（整条产线，v1.5.0）**：在 ZCode 里跑命名工作流 `dramacraft-pipeline`——小说 → 剧本 → 导演规划 → 镜头组 → 终扫 → 监督 → 会议 → 终验导出一条龙，
  工人是子代理、执法是引擎门族，人只在四类节点被叫（熔断升级 / 评审会 P0 / 监督红线 / 会议拍板）。起跑三条与参数见 `references/WORKFLOW.md` §15。

命令行直接跑：

```bash
cd <技能根目录>
python run_batch.py --input "你的小说.txt" --outline outline.json \
  --workdir workspace --target-seconds 150 --formats markdown csv json html
# 可选 --art art.json 让场景/道具对账实扫；缺 --outline/--target-seconds 会直接拒跑（参数须先与用户确认）
# 词表命中体检：python -m dramacraft.vocab_stats workspace
# 创作纵深体检：python -m dramacraft.continuity_lint workspace --outline outline.json --art art.json
```

大纲 `outline.json` 的写法与剧本 JSON 字段契约见 `references/WORKFLOW.md`；`fixtures/` 内有一套 3 集都市短剧满分样例（含 art.json）可对照。

## 交付物

`<workdir>/output/` 下交付物，全部以**人名**呈现台词说话人（经大纲人物表自动映射，缺人物表拒导）：

- `script.md` — **导演台本**：△动作行 + 台词 + 表演提示，可直接开拍
- `script_breakdown.csv` — **场记拆解表**：逐场逐句带估算时长，片场统筹用
- `script.json` — **机读底稿**：结构化剧本，喂下游分镜/生镜流程（已剥除 `_meta`，纯净契约）
- `script-report.html` — **验收报告页**：浏览器打开逐集看全剧
- `production_log.md` — **生产记录**：全剧各集写作来源、回炉自愈次数与模型调用小结（口径见上）

质检报告无论过不过都裸露逐集「估算时长 vs 目标时长」对照——时长是写出来的，不是凑出来的。

## 自检与体检

```bash
python run_batch.py --doctor      # 环境与依赖健康体检（可选 --probe-api 探测模型连通性）
python -m pytest tests/ -q        # 全绿；实测数字只记在 CHANGELOG 当版条目里（文档抄数必然过期）
node skills_runtime/dramacraft_patch/validate.mjs fixtures/sample_script.json --outline fixtures/sample_outline.json --art fixtures/sample_art.json   # 预期 12/12 全过且无「跳过」
```

## 目录结构

```
SKILL.md            # 技能门面（Agent 操作入口）
run_batch.py        # 批量跑批主控 CLI（参数硬闸+大纲预审门+预算熔断+缓存安检+并发调度+环境体检）
dramacraft/         # 引擎包：ingestor/gateway/self_healing/exporter/outline_lint/continuity_lint/ledger/vocab_stats/card_ledger/premise_lint/premise_to_outline/doctor
                    #   v1.5.0 新增：breaker（统一熔断）/plan_lint（D 门族）/shot_lint（S 门族）/artifact_chain（改稿失效链）/meeting_pack（会议机读件）
skills_runtime/     # 12 道质量门 CLI + 上游原生门与渲染器（勿动相对结构）+ spec_cards/（五张规范卡）
.zcode/workflows/   # dramacraft-pipeline.dwf.ts（命名工作流；复制这一片进你的工作区即可按名跑）
references/         # 字段契约、大纲六门、art.json 契约、台账与安检契约、P1~P9 纪要门禁、F/T 门族契约、执行纪律合同、大纲变更单模板、产线工作流合同、已知风险与挂账（按需查阅）
fixtures/           # 3 集基准样例（大纲/满分剧本/美术设定）
tests/              # 单测与端到端自检，自带播种不依赖环境残留；条数以 `python -m pytest tests -q` 实测为准
CHANGELOG.md        # 版本沿革
```

## 禁词与 AI 腔扩充

质量门词表可直接改：`skills_runtime/dramacraft_patch/banned_words.json`（文学病禁词 144 条）、`ai_tone_patterns.json`（AI 腔正则 23 条），改完立即生效。
AI 腔 24 类的「改前→改后」示范在 `skills_runtime/dramacraft_patch/ai_tone_examples.json`，回炉指令会自动引用命中的那一类；补充示范只改这个文件即可。
