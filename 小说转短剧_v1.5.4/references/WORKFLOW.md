# DramaCraft 数据契约与质量门细则

> 按需查阅，不需要通读。SKILL.md 是操作入口，本文是字段级事实源。

## 一、outline.json 契约（大纲 = 分集生成的唯一事实源）

```json
{
  "title": "剧名",
  "totalEpisodes": 3,
  "characters": [
    { "id": "C01", "name": "林晨", "role": "protagonist",
      "desire": "核心欲望", "obstacle": "核心障碍", "voiceStyle": "口吻一句话画像" }
  ],
  "beats": [ { "episode": 1, "type": "家族羞辱" } ],
  "episodes": [
    { "ep": 1, "targetSeconds": 90,
      "hook": "开场钩子的物理画面", "cliff": "结尾悬崖的物理画面",
      "beatsClaimed": ["家族羞辱", "身份初显"] }
  ]
}
```

- `characters` 是角色唯一登记表：剧本里所有 `speaker`/`characters` 必须引用 `id`（C01 式），大纲外的 id 会被 G8 拦。
- `voiceStyle` 会被注入分集生成提示词，是对抗"全员共用一种腔调"（去个性化漂移）的锚。
- 人物核心三角 **≤4 人**：超出即一致性漂移高风险，须先与用户确认取舍。

## 二、剧本 ep###.json 契约（beat schema）

```json
{
  "ep": 1, "targetSeconds": 90,
  "hook": "…", "cliff": "…", "hookBeat": [1, 1],
  "beatsClaimed": ["家族羞辱"],
  "scenes": [
    { "sceneId": "S01", "location": "顾家大厅·夜",
      "characters": ["C01", "C03"],
      "flow": [
        { "type": "action", "action": "顾浩端着热茶猛地泼在地上，皮鞋踹在林晨后背。" },
        { "type": "dialogue", "speaker": "C03",
          "delivery": "踩着林晨手背，冷笑晃杯", "line": "让你倒杯茶都倒不好！" }
      ] }
  ]
}
```

硬规则（对应质量门）：
- `action` = 单一可拍物理动词句，禁引号台词混入（G7）、禁抽象评价词（G11）。
- `line` ≤35 字（G2），且不得命中"不是…而是…"等 38 个 AI 辩论腔模式（G12）。
- `delivery` 承载潜台词与表演提示——台词只说三成，剩下七成写在这里。
- `hookBeat: [场, 拍]` 必须指向真实节拍且全集前 3 拍内兑现（G5）。
- `speaker` 可写 `"VO"`（画外音），否则必须在本场 `characters` 里（G3）。
- `action` ⇄ `dialogue` 交替推进节拍流，避免整场干说或整场干打。
- `targetSeconds` **全剧统一**（来自用户确认的业务参数）：`run_batch.py` 缺 `--target-seconds`/`--outline` 或大纲逐集目标与统一值冲突一律拒跑。
- `speaker` 在机读剧本里存编号（G3/G8 校验根基）；人类可读交付层由 exporter 按大纲 characters 自动映射成**人名**，缺人物表拒导人类可读格式（v1.0.1）。

## 三、12 道确定性质量门（validate.mjs 输出顺序）

| # | 门 id | 判定 |
|---|--------|------|
| 1 | duration | 估算秒数 ∈ target ±15%；估算 = 台词字数/4.5 + 动作拍×2.5。无论过否都逐集裸露「估 vs 目标」对照；逐集目标不统一打 ⚠ 告警（反塞凑门指纹） |
| 2 | line-length | 单句台词 ≤35 字 |
| 3 | speaker | 说话人在本场人物表内或标 VO |
| 4 | hook-cliff | 每集 hook 与 cliff 都落在纸面 |
| 5 | hook-open | hookBeat 认领且在前 3 拍兑现 |
| 6 | has-action | 每场 ≥1 个 action 节拍 |
| 7 | action-prose | 动作行禁含引号台词 |
| 8 | beats-claimed | 大纲本集 beats 全部认领（需 --outline） |
| 9 | refs-characters | 角色 id 全部对账大纲（需 --outline） |
| 10 | refs-scenes | 场景/光照/道具对账美术设定（`--art` 提供 art.json 时实扫，未提供则跳过并如实注明） |
| 11 | literature-sickness | action 零容忍禁词表 `dramacraft_patch/banned_words.json` |
| 12 | ai-tone-removal | line 零容忍辩论/翻案腔正则 `dramacraft_patch/ai_tone_patterns.json` |

两个词表可直接扩充：禁词按行加进 JSON 数组；AI 腔加 `{name, pattern}` 正则对象，改完立即生效（validate.mjs 与 self_healing.py 共用同一词表，无第二副本）。

### 3.1 大纲预审门 O1-O6（v1.1.0，`python -m dramacraft.outline_lint <outline.json>`）

12 道门保护剧本，这六门保护**改编的根**——大纲烂了后面全白搭。run_batch 加载大纲即自动过闸，违规 exit 2 拒跑：

| # | 判定 |
|---|------|
| O1 | title 非空 |
| O2 | characters 非空；id 唯一；每角色 name/desire/obstacle/voiceStyle 四字段齐（人设四字段不齐=纸片人预警） |
| O3 | 集号为正且不重复；**targetSeconds 全剧统一**（逐集乱填是反塞凑门的指纹） |
| O4 | 每集 hook 与 cliff 非空 |
| O5 | 每条 beat 有 type 且挂在存在的集上（孤儿节拍永不会被认领） |
| O6 | 逐集 beatsClaimed 与大纲 beats 双向对账：不漏认领、不虚报 |

### 3.2 art.json 契约与门 10 实扫（v1.1.0）

`validate.mjs --art <art.json>` / `run_batch.py --art <art.json>`；结构：`{"scenes":[{"id","name","lighting":[{"state","note"}]}],"props":[{"id","name"}]}`。剧本每场 `sceneId` 必须在 `scenes` 登记；场次标了 `lighting` 必须在对应场景的 state 列表里；场次 `props` 引用的 id 必须在 `props` 里。样例见 `fixtures/sample_art.json`。不给 art.json 则该门如实标注跳过，不装作通过。

### 3.3 跨集回声告警 X1（告警级，不计 12 门、不否决交付）

validate.mjs 报告尾部输出：上一集末场人物 ∩ 下一集首场人物 = ∅ → ⚠「上一集悬念开场无人接住」。刻意换线转场可忽略，否则补承接。

### 3.4 结构化质量门输出（v1.2.0，`validate.mjs --json <report.json>`）

除终端报告外，支持通过 `--json` 输出机器可读结构化报告：
```json
{
  "gates": [{"id": "duration", "label": "...", "ok": true, "detail": "..."}],
  "hits": [{"gate": "literature-sickness", "term": "若有所思", "ep": 1, "scene": "S01", "snippet": "..."}],
  "warnings": ["..."]
}
```
自愈循环自动读取此结构提取违规项，淘汰脆弱的纯文本行正则解析。

### 3.5 前提纪要预审门 P1-P9 与素材卡池（v1.2.5）

当使用零散素材或孵化单元剧时，前置执行选题会纪要预审门（`python -m dramacraft.premise_lint <ep###.json>`）：

| # | 门 id | 判定规则 |
|---|--------|---------|
| P1 | four-completeness | 核心四件套 desire/obstacle/twist/cost 必须全部非空，且标注 decided_by 来源 |
| P2 | irreversible-cost | desire 必须声明具体不可逆后果（irreversible_consequence），杜绝无病呻吟 |
| P3 | cast-limit | 单集核心角色池 ≤ 4 人，且有据可查 |
| P4 | physical-hook-cliff | hook 与 cliff 禁含文学病形容词（若有所思/神色复杂等），必须为纯物理动作 |
| P5 | standard-duration | target_seconds 必须为标准档位（60s / 90s / 180s） |
| P6 | tone-ending-enum | tone 与 ending_type 必须选自受控枚举 |
| P7 | cards-traceable | cards_used 中的卡片在 `inbox/cards.jsonl` 中存在且非 spent 状态 |
| P8 | custom-anchors | 存在 user_custom 时，custom_anchors 必须非空且被四件套收纳 |
| P9 | explicit-verdict | 纪要必须具备 verdict == "pass"（用户明确点头授权） |

### 3.6 单元剧模式与 X1 告警静默（mode: "anthology"）

大纲声明 `"mode": "anthology"` 时：
- `validate.mjs` 自动静默 X1 跨集回声告警（单元剧各单元独立，前后集人物无交集属正常编排）；
- 可通过 `dramacraft.premise_to_outline.convert_premises_to_outline(premises)`（Python 内调用，无命令行入口）把全通 P1~P9 的 premise 列表一键转译为标准大纲，100% 自动通过 O1~O6 大纲门。

## 四、自愈循环（self_healing.py）

生成 → `node validate.mjs --json` → 结构化解析 `gates` 违规项与 `hits` → 把违规"集-场-拍"坐标打包成定向修改指令 → 仅回炉违规拍、保持合格部分 → 最多 3 次 → 仍失败抛错停批。**不是重写全集，是外科手术。**

## 五、网关与预算熔断（gateway.py）

- 节点池三级发现链（`--nodes` 显式指定优先）：环境变量 `DRAMACRAFT_NODES` → 技能根 `llm_nodes.json` → `~/.agents/skills/bilibili-auto-transcript/llm_nodes.json`。新用户拿模板 `llm_nodes.example.json` 填自己的 API key 即可，多节点自动 failover。
- 双协议自适应：OpenAI `/chat/completions` 与 Anthropic `/v1/messages`。
- 截断检测（finish_reason ≠ stop 或长度异常）与 429/529/超时均触发**顺序 failover 到下一节点**，不重试同一节点烧额度。
- **调用计数与预算熔断**：`gateway` 对每一次网络尝试（含 failover 失败尝试）严格计费。`--max-calls` 触顶立即抛出 `BudgetExhausted`，由 `run_batch` 捕获后安全退出（退出码 3），已落盘集数安全保全，原命令重跑自动续跑且不重复计费。

## 六、导出四件套（exporter.py）

| 格式 | 文件 | 用途 |
|------|------|------|
| markdown | script.md | 拍摄本：`△` 动作行 + 台词 + 潜台词括注 |
| csv | script_breakdown.csv | 拆解表：集/场/拍/时长估算，片场统筹用 |
| json | script.json | 全量结构化剧本，下游（分镜/生图）消费（聚合时自动剥除 `_meta` 元数据） |
| html | script-report.html | 交互式报告（调 novel-script.mjs render） |

`--formats markdown csv` 可任选子集，输出目录 `<workdir>/output/`。

## 七、生成台账与白话小结（ledger.py）

- 运行期在 `<workdir>/state/ledger.jsonl` 中记录不可变事件流（`run_start`、`llm_call`、`episode_generated`、`gate_fail`、`episode_cached`、`stale_cache`、`budget_stop`、`run_finish`）。
- 每次跑批（无论是正常完成、预算熔断还是中断）均自动提炼生成 `<workdir>/output/production_log.md` 白话生产小结，清晰展示各集状态、执笔节点与回炉自愈次数。

## 八、缓存安检与版本契约（_meta）

- 单集缓存 `ep###.json` 落盘时注入元数据：
  ```json
  "_meta": {
    "engineVersion": "1.2.0",
    "schemaVersion": 2,
    "outlineHash": "md5前12位",
    "gatesPassedAt": "2026-09-21T12:00:00+08:00"
  }
  ```
- 断点检测自动执行三查（有 _meta、schemaVersion ≥ 2、outlineHash 匹配）。
- `--stale-cache` 三档策略：`ask`（默认，退出码 2 拒跑并点名失效集数与原因）、`redo`（自动清理失效集数重跑）、`trust`（信任放行并标记 legacy）。
- 聚合导出到 `output/script.json` 时自动剥除 `_meta`，下游分镜与生图纯净消费。

## 九、词表命中体检（vocab_stats.py）

质量门拦截时的词表命中自动追加至 `state/vocab_hits.jsonl`。
执行 `python -m dramacraft.vocab_stats <workspace>` 产出分析报告：
1. **高频命中 Top10**：模型最常踩坑的词条/正则，提供该不该扩充规则的依据；
2. **零命中清单**：现有词表中从未被拦截的规则差集，提供该不该裁撤冗余规则的候选；
3. **只读审计**：绝对不自动改写或删除词表文件，确保规则稳定。

## 十、分集并发调度与原子落盘（v1.2.1）

- 线程池并发：`run_batch.py` 默认启动 `--concurrency 3` 工作线程同时自愈生成不同集数（设为 1 时降级为纯串行模式）。
- 原子文件写入（`safe_write_json`）：先写入临时文件 `.tmp.{pid}.{tid}`，`flush + fsync` 完毕后再原子 `os.replace` 换名，彻底杜绝断电或中断写出半截坏 JSON 污染断点。
- 线程安全保障：
  - `Ledger` 内置线程锁，多线程 append-only 写入 `state/ledger.jsonl` 绝不混行；
  - `LLMGateway` 内置原子计费与熔断标志位 `is_exhausted`，多线程并发撞闸立即停止派发新请求，防止超额计费；
  - `state/vocab_hits.jsonl` 写入受独立锁保护。

## 十一、ingestor 输入自适应规则

- 目录：按 `.txt/.md/.text` 收集，文件名自然数排序（第2章 < 第10章）。
- 单文件：优先按章节标题正则切分；识别不到章节则整篇一块。
- 无结构节选：按段落切，超 `chunk_size`(默认3000字) 的段落做强制滑窗切片。

## 十一、创作纵深门：伏笔账本（F 族）与命名节奏（T 族）

> 模块：`dramacraft/continuity_lint.py`（纯标准库，与 O/P/X 一样独立成家——**不占用 validate.mjs 的 `gates` 恒 12 数组**）。
> 轮子血统：伏笔台账协议照搬 `work研究` novel-writing 技能；术语锁定三表与命名禁令照搬 fbs `workflow-s0.md` S0-Init 模板；
> 结构雷同与句长节奏指标照搬 fbs `quality-check.md` §5.1 B2-C / B2-A / S6。

### 11.1 伏笔账本 = append-only 事件流

账本落在 `<workdir>/state/foreshadow_log.jsonl`，**不存「当前状态文件」**——彻底消灭读-改-写竞态；
当前状态一律由 `replay()` 重放派生：

```json
{"ts":"…","event":"plant","ep":3,"id":"F001","text":"玉佩夜里发烫——身世线索","payoffPlannedEp":8,"registeredBy":"outline"}
{"ts":"…","event":"pay","ep":8,"id":"F001","evidence":"第8集第2场：玉佩特写+台词认领"}
{"ts":"…","event":"abandon","ep":8,"id":"F001","abandonReason":"情节线砍掉，用户确认放弃","decidedBy":"user"}
```

**重放即回滚**：`pay` 事件仅当其认领集数 ∈ 当前有效缓存集才被采信；`--stale-cache redo` 删掉某集缓存后，
该集认领自动作废、涉及伏笔自然回 `open`，无需任何快照镜像。`plant` / `abandon` 是登记与决策记录，
与某集缓存存不存在无关，一律采信（否则伏笔债务会凭空消失、F3 漏账）。

### 11.2 三处登记入口与一处兑现认领

| 入口 | 契约字段 | 说明 |
|---|---|---|
| 大纲 | `episodes[].foreshadows: [{id?, text, payoffInEp}]` | 加载即幂等入册（F1 通过后）；缺 `id` 自动编 `F001/F002…` |
| 选题会纪要 | `premises[].foreshadows[]` | `premise_to_outline` 原样带入大纲，溯源标记 `registeredBy: "topic-meeting"` |
| 人工 CLI | `python -m dramacraft.continuity_lint <workdir>` 报告 + 手工 `emit_event` | 轨道 A 补登记 |
| **兑现认领** | `ep###.json` → `foreshadowsPaid: [{id, evidence}]` | 自愈通过后合成 `pay` 事件；`evidence` 必须是「第几集第几场」，缺证据只 ⚠ 不合成 |

### 11.3 F 门族判据与级别

| 门 | 判据 | 级别与挂点 |
|---|---|---|
| **F1** 登记合法 | `text` 非空；`payoffInEp ≥ plantedInEp` 且 **∈ 大纲集号集合**（单元剧允许同集自收）；ID 唯一 | `_enforce_outline_gate` 内与 O 门同点同时机 → **exit 2**（`--validate-only` 有 `--outline` 时同样生效） |
| **F2** 认领对账 | 认领 ID 必须在账本；`evidence` 非空；认领集早于埋设 → 不入账；晚于计划 >1 集 → ⚠ 但按兑现采信；超期未填 ⚠ 点名 | 单集落盘后跑 → **⚠ 警告级，绝不进回炉指令**（防模型为过闸硬塞假填坑） |
| **F3** 终验清零 | 未回收必须清零；`abandoned` 须有原因且 `decidedBy == "user"` | 终验序硬闸 → **exit 1 点名，全绿不豁免** |

**终验序**（v1.4.0 定案）：12 道门汇编终验通过 → `final_foreshadow_sweep + final_naming_sweep` 同场终扫 → 导出四件套。
任一步未过即 exit 1，不产交付物。

### 11.4 T 门族判据与级别

| 门 | 判据 | 级别与兜底 |
|---|---|---|
| **T1** 术语锁定 | 三表=标准写法（人物名/别名 + 美术场景道具名 + 显式 `terms[].standard`）/ 禁用变体（`forbiddenNames` + `terms[].forbidden`）/ 替换记录（命中清单即替换指令）；成稿扫描命中即点名标准写法 | 单集 ⚠ **进回炉**（改名是确定性修补，无凑闸风险）；终验残留 → exit 1 |
| **T2** 命名冲突 | 泛化命名「小X/大X/老X」未登记点名；人物表内部同名不同人点名；`forbiddenNames` 命中 | 同 T1；另设 `T2-注册` advisory（恒 pass）：人物表自身登记泛化命名时提示一次 |
| **T3** 结构雷同 | 相邻 ≥2 集节拍骨架同构（先剥「第N集_」类型前缀再比对） | ⚠ 警告级，不进回炉 |
| **T4** 句长节奏 | 动作行句长变异系数 + 破折号密度 | 提示级（样本小易误报），数据全产不设闸 |

**种子双缺 → 显式裸露**：既无人物表也无美术件时输出「跳过（未提供人物/美术种子）」，不误报也不静默（同 G10 `--art` 跳过先例）。

### 11.5 结构化输出键合同

```bash
python -m dramacraft.continuity_lint <workdir> [--outline outline.json] [--art art.json] [--json report.json]
```

`--json` 落盘为**三个独立顶层键**，每项为 `{id, label, ok, detail}`：

```json
{ "foreshadow_gates": [{"id":"F3","label":"终验未回收清零（放弃须有原因且经用户拍板）","ok":false,"detail":"…"}],
  "naming_gates":     [{"id":"T1","label":"术语锁定（标准写法 / 禁用变体）","ok":true,"detail":""}],
  "rhythm":           [{"id":"T4","label":"动作行句长节奏与标点纪律（提示级）","ok":true,"detail":"…"}] }
```

**不含 `gates` 键**——`validate.mjs --json` 的 12 门数组结构与键名零改动，既有解析向后兼容。

## 十二、执行纪律合同（硬条款）

1. **「技能已加载 ≠ 门禁已运行」**：轨道 A 任何宣称「通过」都必须附**本次实测**的质检输出摘要；没有实测输出＝流程未启动，禁止宣称阶段完成（这是本工程记录在案的架构盲区：技能只会把规范文本放进上下文，不会替你跑任何一条命令）。
2. **跳步降级链**：门禁跑不起来（环境缺件）→ 先跑 `python run_batch.py --doctor` 修好再进下一阶段；**聊天内自检不得替代机器门禁**，也不得作为「已通过」的依据。
3. **JSON 落盘回读**：质检结果一律 `--json <path>` 写文件 → 由 Python 读文件，**禁止解析终端混流**（Windows/PowerShell 宿主下 stdout 可能被 CLIXML 包装或截断）。

## 十三、工程合同

- **状态宣告四要素**：阶段切换的宣告必须含 `旧→新` / `原因` / `门禁出口` / `下一步`——`production_log.md` 的每一行都按此渲染，不许只写「已通过」。
- **实测数字优先**：恢复卡、生产小结、时长对照一律取台账与本次实扫值，**禁模型估算上台面**。
- **编排三纪律**：星型中转（所有跨角色信息流经主控，禁互相直连）/ 主控不代写（专业产出由对应角色给出后才采信）/ 磁盘台账为唯一真值（与任何记忆镜像冲突时以磁盘为准）。

## 十四、跑批操作备忘

```bash
cd <技能根>   # 即本技能 SKILL.md 所在目录

# 全量批量（默认 3 并发提速 + 12 道门自愈 + 终验 + 导出）
python run_batch.py --input "小说.txt" --outline outline.json --workdir workspace --target-seconds 150

# 纯串行模式（若只想单线程跑）
python run_batch.py --input "小说.txt" --outline outline.json --workdir workspace --target-seconds 150 --concurrency 1

# 断点续跑：直接重复上一条命令，state/episodes/ 已有的集自动跳过
# 只重导出不生成：python run_batch.py --outline outline.json --assemble-only
# 单集过门：node skills_runtime/dramacraft_patch/validate.mjs workspace/state/episodes/ep001.json --outline outline.json
# 创作纵深体检：python -m dramacraft.continuity_lint workspace --outline outline.json --art art.json
# 词表命中体检：python -m dramacraft.vocab_stats workspace
```

## 十五、产线工作流（v1.5.0）

一条命名工作流把 01→03 全链串起来；工人是子代理，执法是脚本。两形态共用同一套门族与熔断。

### 15.1 两形态边界

| 形态 | 入口 | 吃什么额度 | 适合 |
|---|---|---|---|
| 工作流 | ZCode 里跑 `dramacraft-pipeline` | 本机会话额度（子代理） | 整条产线无人值守跑批；人只在四类节点被叫 |
| CLI | `python run_batch.py …` | 模型节点池额度（`llm_nodes.json`） | 只产 01 剧本；单点重跑、人工盯稿、断点续跑 |

两形态**共用**：`validate.mjs` 12 门、`dramacraft/plan_lint.py`（D 门族 H1-H6/W1）、`dramacraft/shot_lint.py`（S 门族 SG1-SG7/W2）、
`dramacraft/breaker.py`（统一熔断）、`dramacraft/artifact_chain.py`（改稿下游失效链 + 越权快照）、`dramacraft/ledger.py`（台账）。

### 15.2 工人与规范卡（`skills_runtime/dramacraft_patch/spec_cards/`）

| 工人 | 作业规范卡 | 交出的在库件 |
|---|---|---|
| 写手 | 技能既有写作纪律（12 门 + 创作纵深） | `state/episodes/epNNN.json` |
| 导演 | 导演规划规范卡.md（原件：ToonFlow 导演规划） | `state/plans/epNNN.json` |
| 镜头组 | 镜头组规范卡.md（原件：Omni 剧本转镜头组 v1.2.0） | `state/shotgroups/epNNN.json` |
| 监督 | 监督规范卡·成片层.md（原件：ToonFlow 监督层） | `state/supervision/epNNN.json` |
| 会议 | 会议机制卡.md（原件：fbs session-protocols v2.1.2） | `state/meetings/*.md` |

派活时**整卡交给工人**（卡头有原件出处与行号，可回溯）；规则只在这五张卡与代码常量里，别在别处复述第二份。
大纲层那张卡（监督规范卡·大纲层.md）**不用于逐集终审**，只用于大纲预检与策略评审。

### 15.3 机读合同（工作流只按这个读）

- **门族 CLI**：`python -m dramacraft.plan_lint <plan> --script <剧本> --json <报告>`、`python -m dramacraft.shot_lint <镜头组> --plan <plan> --script <剧本> --target-seconds N --json <报告>`。
  退出码 0=全绿 / 2=有硬伤 / 1=用法错或输入不可用（**不落报告**）；判定一律「退出码 + 报告文件 json 可解析」双读，**禁解析 stdout**。
- **熔断**：`python -m dramacraft.breaker --workdir W --ep N --round R --gate-report <本轮门报告> [--cap 3] --json <报告>`；
  stdout 只有裸词 `pass|continue|escalate|abort`，真值在报告 `{verdict, unresolved, new, resolved, …}`。
  轮次语义：**round 1 = 一次新尝试的开头**（会清掉更早那次的残留）；`--round 0`、`--cap 0` 都是用法错。
- **失效链**：`python -m dramacraft.artifact_chain check|emit|snapshot|verify …`；链条 `script → plan → shots → supervision`，
  上游内容一变下游即失效（指纹对账，不是时间戳）；`snapshot/verify` 给越权硬闸用（exit 5 = 有文件被改动）。
- **会议闭环**：读者会每条关切必须有决议行，`python -m dramacraft.meeting_pack check-closure --json <报告>` exit 4 = 有未闭环。

### 15.4 分批与集数上限

单次运行 **≤40 集**：`report` 有 256 条硬顶，按「每集只报里程碑态（写作/规划/镜头/质检/交付/阻塞/待人判）」倒推，
65 集的四阶段全报会撞顶（260 > 256）。**批次运行**只出批次验收报告、不盖全剧交付戳；
**全剧终验**要求大纲全部集数在库（缺集即红并列缺号，CLI 侧同规则 exit 2 且不出交付物）。

### 15.5 人在哪四类节点被叫

熔断升级（连续两轮阻塞项不缩小）、评审会 P0、监督红线命中、会议拍板（心理契约/方向二选一）。
每问只给 2-3 个有限选项；**等待人选不设超时**，其余环节按会议机制卡的超时行为收束。

### 15.6 起跑前三条（2026-09-22 装包实验实测，缺一条就跑不动）

1. **引擎自己找得到**：工作流**不要求**把技能目录当工作区。开工第一步探测引擎根，候选序＝显式 `engineRoot` → 当前目录能 import 到的 `dramacraft` 包 → `~/.agents/skills/dramacraft` → `~/.claude/skills/dramacraft` → 当前目录；
   命中条件是该根下同时有 `run_batch.py` 与 `dramacraft/ingestor.py`。都找不到会当场报白话错并停，不会跑出半成品。
   同一台机器上有多份引擎（开发仓库 + 已装技能）时，**显式传 `engineRoot` 最稳**——不传就按上面的顺序命中第一份，可能不是你以为的那份。
2. **路径要在 ZCode 工作区内**：工作流读大纲、写产物、读门报告全走工作区内的世界读层，**传工作区外的绝对路径会被直接拒**（`path '…' is outside the workspace`）。
   ⇒ `outlinePath` 与 `workdir` 必须是工作区内（相对或工作区内绝对）；**原著可以放工作区外任意位置**，工作流会把它转成 UTF-8 参考件写进 `workdir/state/source/novel-utf8.md` 再给写手读。
3. **原著编码不用管**：GBK / GB18030 的真书（例如 1MB 的《特战先驱全集》，自动切出 343 章 / 531,420 字）与 UTF-8 一视同仁；
   转码失败会退回"直接读原文件"并把退出码写进日志，不会静默降级。

九个阶段的实际顺序（读者会在写作之前，关切接受的部分直接进写手提示词；全链终扫在送监督之前）：
盘点与转码 → **大纲预检 O1-O6 ＋ 读者会** → 逐集剧本(12 门) → 逐集导演规划(D 门族) → 逐集镜头组(S 门族) →
**全链终扫（伏笔/命名/指纹）** → 逐集监督 → 评审会 → 终验导出（交付戳：读者会未闭环＝「待人确认」）。

### 15.7 保存与按名跑（三端分发）

- 正式版随技能包三端分发，落点是**工作区** `.zcode/workflows/dramacraft-pipeline.dwf.ts`（`SaveWorkflow scope=project`）：
  仓库 `.zcode/workflows/` → `~/.agents/skills/dramacraft/` → 发布包目录（`Desktop\自制skill\小说转短剧_vX.Y.Z\`），三处 MD5 必须逐字节一致。
- 跑法：ZCode 里以 `saved:{name:"dramacraft-pipeline"}` 起（工作区里能看到它才算装好）；参数 11 项见工作流声明。
- **AmendWorkflow 不继承参数**：改脚本＝重开一次运行，必须重新把 `novelPath / outlinePath / workdir` 传齐（脚本对缺参数会当场报错并给出这句提示）。
