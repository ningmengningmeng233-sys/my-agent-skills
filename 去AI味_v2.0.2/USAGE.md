# 使用说明（USAGE）

本文件是发布包的组成部分，面向安装与使用者。技能机器 ID 是 `lieflat-less-ai-tone`
（中文显示名：中文去 AI 味）；授权方式为 MIT，第三方声明见 `THIRD_PARTY_NOTICES.md`。

## 安装

1. 校验包完整性：`release-manifest.json` 逐文件记录字节数与 SHA-256；发布附件中的
   `SHA256SUMS.txt`（含 ZIP 自身哈希行）可用 `sha256sum -c SHA256SUMS.txt` 复核。
2. 把本目录整体放入 Agent 宿主的技能目录，**目录名必须是 `lieflat-less-ai-tone`**
   （调用与闸门认机器 ID）。常见位置：`~/.agents/skills/`、`~/.claude/skills/`、
   `~/.codex/skills/`、`~/.qoder/skills/`。
3. 运行依赖：Python ≥ 3.12（发布验证环境 3.12）与 Node ≥ 20.18.0（Markdown 结构
   候选走 `runtime/textlint/` 桥）。v2 通路需要第三方库，分两把锁：默认只装轻量
   核心锁 `requirements-v2.lock`（状态机、任务、提交与 Jev 通路）；相似度/向量
   扩展按需把 `requirements-v2-similarity.lock` 叠装进同一目标目录（绝不单独
   安装，见「依赖准备与撤销」第 2 步）。「装了什么 / 没装什么 / 什么被停用」由
   `python -B -X utf8 -c "import sys;sys.path.insert(0,'scripts');import runtime_contract,json;print(json.dumps(runtime_contract.capability_report(),ensure_ascii=False,indent=2))"`
   一次读出（含可选锁逐组在场状态与缺锁指引）。不隐式联网：向量模型只从钉定的本地快照读（`local_files_only=True`），
   可选中文 NLP 组件（HanLP/LTP）不捆绑、不自动下载。会读环境变量：宿主与工具
   路径发现用 `PATH`，可选覆盖项以 `LIEFLAT_` 开头；不写全局配置。
4. 验收（可选但建议）：用发布附件 `receiver_acceptance.py` 对解压目录做逐文件哈希、
   脚本加载与提交门烟测；或用 `python scripts/validate-rewrite.py BEFORE AFTER` 对
   任意一对文本做机械验证烟测（No-Op 判 `NO-OP` 且退出码 0）。

## 使用方式

由宿主代理按技能文档执行；触发词：去 AI 味 / AI 味太重 / 一股模型腔 / 像机器人写的 /
太模板化，或指向文风编辑的「说人话」，或点名本技能。整本 AI 小说直接给整本，不必预拆。

- **四种输出模式**：Rewrite（默认，交付单一改写终稿）；Audit（只审不改，输出命中与
  建议）；File（就地改文件，必须经 `scripts/file_commit.py` 生产提交门）；Embedded
  （只输出纯改写正文）。
- **三档力度**：保守（in-place，默认技术文档）；平衡（balanced，默认短文与整本小说）；
  深度（rebuild，仅在你明确说「深度/推倒重写」时启用，不自动升档）。
- **NO-OP 是正常结果**：没有可靠病灶时原样返回，不为显示效果强行修改。

## v2 任务通路（v2.0.0-stable 目标包身份，发布通过仍 false）

- 入口：`scripts/v2_session.handle_request(job_dir, event, spec)` 按状态给下一句中文
  提示；「处理方式 → Jev → 任务摘要」逐项确认前不写正文，催促与推荐值都不算回答。
- 整本与多文件任务、按章预览与逐项接受、提交与回滚：`scripts/v2_task.py`，状态与
  账本记进现有 job 的 manifest/state/lock/events，不建第二任务库。
- 反馈（喜恶）：`scripts/v2_preferences.apply_feedback(job_dir, event, scope=...)`，
  默认项目作用域；词汇喜恶不作为 AI 证据，偏好一变向量缓存按新版本重算。
- 可选中文 NLP：`scripts/v2_nlp.py` 默认 `none`；HanLP/LTP 未捆绑，许可与能力分别
  披露（条款文本只写在 `THIRD_PARTY_NOTICES.md`）。
- 能力披露：`runtime_contract.capability_report()` 一次给出运行件数、精确依赖锁、
  可选后端可用性与被停用的能力面。
- 阶段名升到 `stable` 只声明「这就是将交付那份字节的身份」，**不是发布通过**：真实 Jev
  调用、宿主自然触发、整本真实稿效果与人工校准都未运行或未闭合时，终态仍报开放，
  高风险自动提交保持关闭；经预览逐项接受的通路可用。判定入口是
  `run_v2_quality.final_release_gate`（只吃带身份的四项读数，不吃布尔常量）。

## 依赖准备与撤销（干净候选 ≠ 准备后的运行副本）

发行候选**不带**依赖树：Python 第三方件不捆绑，`runtime/textlint/` 也不带 `node_modules`。
缺依赖时相关通路显式停止并点名缺什么，不会回空结果冒充扫过。准备一律落在**你自己指定的
一份运行副本**里：不动安装目录、不写全局环境、不装进系统 site-packages、不借别的树的解析器。

### 0) 先设三个变量（后面每条命令都从它们取路径，不用逐行手改）

路径一律用环境变量交给 Python，**不拼进 `-c` 里的字符串字面量**：反斜杠、空格与中文路径
拼进字面量会被转义规则吃掉，那是文档该背的错，不该让你手修命令。

POSIX（bash / zsh）：

```sh
Cand="$PWD/lieflat-candidate"    # 干净候选根：与 SKILL.md 同层的那棵树
Run="$PWD/lieflat-run-copy"      # 运行副本：留一个还不存在的目录名
Py=python3.12                    # 先 "$Py" -V 确认 ≥ 3.12
```

PowerShell：

```powershell
$Cand = Join-Path (Get-Location) 'lieflat-candidate'
$Run  = Join-Path (Get-Location) 'lieflat-run-copy'
$Py   = (Get-Command python).Source    # 先 & $Py -V 确认 ≥ 3.12
$env:CAND = $Cand
$env:RUN = $Run
```

### 1) 造运行副本并按 Node 锁准备（Node ≥ 20.18.0）

```sh
CAND="$Cand" RUN="$Run" "$Py" -B -X utf8 -c "import os,sys,json;sys.path.insert(0,os.path.join(os.environ['CAND'],'scripts'));import v2_structure as s;print(json.dumps(s.prepare_runtime_copy(os.environ['CAND'],os.environ['RUN']),ensure_ascii=False,indent=2))"
```

```powershell
& $Py -B -X utf8 -c "import os,sys,json;sys.path.insert(0,os.path.join(os.environ['CAND'],'scripts'));import v2_structure as s;print(json.dumps(s.prepare_runtime_copy(os.environ['CAND'],os.environ['RUN']),ensure_ascii=False,indent=2))"
```

这一步复制候选（排除 `node_modules`、`__pycache__`、`*.lieflat-work`、`.git`），然后在副本
里跑 `npm ci --no-audit --no-fund`，返回 `clean_candidate` / `run_copy` 两个路径、两侧锁摘要
（应相同）、`resolved_modules`、`dependency_versions` 与 `run_copy_files_added`（一次实测在
副本内新增 5201 个依赖文件）。目标目录已存在就拒绝覆盖；没有 npm、或装完仍不就绪就抛错并
保持「能力未准备」，不静默降级成空候选。

### 2) Python 依赖装进这个副本自己的 `.deps`（默认核心锁，按需叠装相似度锁）

默认装核心锁（v2 状态机、入口、提交门、偏好反馈、缓存与 Jev 通路，含 numpy）：

```sh
"$Py" -m pip install -r "$Run/requirements-v2.lock" --target "$Run/.deps"
```

```powershell
& $Py -m pip install -r "$Run/requirements-v2.lock" --target "$Run/.deps"
```

锁钉 `transitions` @ git `bd42b38`（0.9.4）与 `six` 1.17.0。

按需再叠装相似度锁（模板近重复、语义检索与真实向量模型）：叠进**同一** `.deps`
（绝不单独安装——交集传递依赖只在 core 锁里，单装扩展锁起不来）：

```sh
"$Py" -m pip install -r "$Run/requirements-v2-similarity.lock" --target "$Run/.deps"
```

```powershell
& $Py -m pip install -r "$Run/requirements-v2-similarity.lock" --target "$Run/.deps"
```

没装扩展就导入 `v2_similarity` 或创建 `v2_embeddings.BGEProvider` 时，明确报缺的
模块名及相似度锁安装命令（`ModuleNotFoundError`，不返回空结果）；缓存与项目反馈仍可只用核心依赖。`capability_report()`
的 `optional_locks` 逐组报在场与否并给上面这条叠装命令。

### 3) 实际加载：`PYTHONPATH` 只给这个副本的 `.deps`

```sh
PYTHONPATH="$Run/.deps" RUN="$Run" "$Py" -B -X utf8 -c "import os,sys;sys.path.insert(0,os.path.join(os.environ['RUN'],'scripts'));import v2_session;print('ok')"
```

```powershell
$env:PYTHONPATH = "$Run/.deps"
& $Py -B -X utf8 -c "import os,sys;sys.path.insert(0,os.path.join(os.environ['RUN'],'scripts'));import v2_session;print('ok')"
```

不给 `PYTHONPATH` 时，**一个没装过这两个件的解释器**会报
`ModuleNotFoundError: No module named 'transitions'`，这是预期停点，不是脚本缺陷。
如果你的解释器本来就装过（本仓的开发 venv 正是这种），这条负对照看不到报错——此时以
**解析落点**为准：`transitions.__file__` 必须指向这份副本的 `.deps`，指向别处就不算就绪。

### 4) 结构扫描就绪判定（只认副本树内解析）

```sh
RUN="$Run" "$Py" -B -X utf8 -c "import os,sys,json;sys.path.insert(0,os.path.join(os.environ['RUN'],'scripts'));import v2_structure as s;print(json.dumps(s.preflight(bridge_dir=os.path.join(os.environ['RUN'],'runtime','textlint')),ensure_ascii=False,indent=2))"
```

```powershell
& $Py -B -X utf8 -c "import os,sys,json;sys.path.insert(0,os.path.join(os.environ['RUN'],'scripts'));import v2_structure as s;print(json.dumps(s.preflight(bridge_dir=os.path.join(os.environ['RUN'],'runtime','textlint')),ensure_ascii=False,indent=2))"
```

`ready` 为假时，返回值里的 `prepare_command` 就是这条副本该跑的 `npm ci --prefix`，缺依赖时
`call_bridge` / `scan_structure` 抛的错里也原样带着它——它不是抄在文档里的固定字符串。
`resolved_outside_tree` 非空即判本树未就绪：借别的树（包括开发工作树）不算通过。

### 5) 干净候选 ≠ 准备后的运行副本

两份东西分开登记：代码与锁的身份相同，副本里多出来的依赖件另列（第 1 步的
`run_copy_files_added`），不把准备后的运行副本写成「零漂移」。

### 6) 撤销（先核归属；本轮只登记，不代删）

- 依赖件只有两处：`$Run/.deps` 与 `$Run/runtime/textlint/node_modules`，连副本一起撤就是 `$Run`
  这一个目录。删之前先确认 `$Run` 指向你这次建的副本。
- 不在撤销范围内、也不会被代删：安装目录里那份技能、稿件、`.lieflat-work/` 任务状态、`.bak` 备份。

## File 模式：v2 任务存在时只有一条成功通路

- **v2 任务已经建立（有 job、有摘要确认）**：写正文只允许走
  `scripts/v2_task.commit_task` → `v2_preview.commit_verified`（内部仍调用同一道
  机械写者），拿真实预览 bundle、真实候选与真实验证报告，成功后当场落
  逐文件 `CommitRecord`。**没有 bundle 就停在 REVIEW／暂停，正文一字节不写**，
  也不许手搓 bundle、验证报告或 `CommitRecord` 让账本变绿。
- **`scripts/file_commit.py` 独立 CLI**：v1 时代的兼容入口，用于「没有 v2 任务、
  只想安全改一个文件」的场合。它保证机械验证、唯一备份、哈希竞争、原子替换与回滚，
  只到"文件安全落地"为止，**不认识 v2 job，也就不会写 v2 的账**。
- **别把独立 CLI 当成 v2 的第二条成功通路**：走它落盘后 v2 job 仍停在 READY、
  `CommitRecord` 为空是**预期结果**，不是丢单，也不算这次 v2 交付完成。事后拿原来
  那份授权去补交会被源哈希与 bundle 一致性门拒掉（稿件不再被改动），恢复入口也不会
  认领没有意图依据的文件。采证时同理：看到 `COMMITTED` 与正确备份只能记「机械安全
  通过」，任务级提交要另看登记。
- **本库拦不住宿主**：宿主仍有 Write/Bash 能力，可以直接改文件。这里做的是不给旁路
  盖章，不是沙箱；真发生了绕过，照实记成 FAIL。

## 剧情敏感条目的第二次语义复核（缺证据就停 REVIEW）

- **谁定的规则**：`scripts/novel_contracts.py` 那张类别表（`relationship`／`viewpoint`／
  `time`／`world-rule`／`secret`／`foreshadowing`／`character-state`）里 `certainty="certain"`
  的条目，必须有**绑定本次交付**的逐条复核事实才可能被当作确定事实。没有证据、判定是
  `failed`/`unknown`、或出处对不上 → 维持 REVIEW。这是**正确拒绝**，不是要绕开的故障。
- **唯一入口**：评审器除 `review(...)` 外多提供一条
  `review_claim(claim, *, original, candidate, context, ledger, stage)`，逐条回答
  `{"verdict": "confirmed|failed|unknown", "note", "independence": "same_host|external",
  "routing", "artifact"}`；再由
  `scripts/v2_verify.second_review_facts(reviewer, original=…, candidate=…, ledger=…,
  stage=…, task=spec, judge_epoch=session.judge_epoch)` 产出事实。评审器没有这条能力就
  返回**空事实**（那是「没复核过」，不是「复核通过」）——不代它编造，也不整本交 ID 冒充。
- **判词文档**（宿主第二遍的生产端）：逐条判词落盘成
  `{"schema": "lieflat-v2-second-review-verdicts-1", "verdicts": [一条一判]}`，由
  `scripts/v2_verify.facts_from_verdict_document(文档, original=…, candidate=…, ledger=…,
  stage=…, task=spec, judge_epoch=…)` 接成上面那些事实；`claims_to_review(ledger)` 给待核清单。
  文档里没有这条、或判 `confirmed` 却没写 `note`，一律记 `unknown`——空依据不算复核过。
- **怎么接**：事实连同 `judge_epoch` 交给 `stage_outcomes`／`verify_stages`／
  `verify_with_recheck`／`write_evidence` 的 `second_review=` 参数。投影**按阶段各算一次**：
  clean 的复核放行不了新的 polished，润色失败仍只保留已验证的 clean。
- **谁在核**（三层各核各的，任何一层不过都零触碰）：
  ① `stage_outcomes` 内把合格事实投影给现有 `novel_validation`／`novel_review.validate_review`
  （用的还是那一份底层合同，不新建校验器）；
  ② `v2_preview.commit_verified` 在记提交意图**之前**核**出处**——`rule_version`、
  `prompt_version`、`model_version`、`judge_provider` 与当前规格一致，`judge_epoch` 与当前
  裁判纪元一致，不符抛 `second_review_provenance_mismatch`；
  ③ `novel_output.publish` 复验核**字节绑定**——判定 `confirmed`、`bytes_bindings` 里的源稿与
  候选磁盘摘要就是这次交付、`ledger_sha256` 对得上当前账本、出处字段齐备；缺哪条就按
  `plot_sensitive_needs_second_review` 拒，并在证据里点名是哪条 claim。
- **独立性如实记**：同一个宿主模型再走一遍是允许的，但要标 `independence="same_host"`；
  自称外部裁判必须交出可证的 `routing` 标识，否则不认。Jev 不是必需条件。
- **三条不许**：把 `world-rule` 之类的敏感类别改成 `name` 骗绿；只填 `claim_id` 列表冒充复核；
  手搓 manifest／bundle／`CommitRecord`。证据导出与重载后事实与出处必须仍在——
  `v2-protection-manifest.json` 的 `second_semantic_review` 就是发布复验读的那一份。

## 能力边界

- 不输出「AI 率 / AIGC 概率」，不承诺规避任何 AI 检测器，不制造虚假人味。
- 机械检查通过不等于语义复核通过；改动必须能回指原文病灶与规则依据。
- 编码：整本小说通道（`scripts/text_source.py` / `scripts/novel_output.py`）支持
  UTF-8、UTF-8 BOM、带 BOM 的 UTF-16 与 GB18030；`scripts/file_commit.py` 单独
  CLI 仅支持 UTF-8 / UTF-8 BOM——GBK 原稿会得到 ERROR 且文件保持原样，请先转码
  或改走小说通道。BOM 与 CRLF/LF 原样保留，混合换行统一需显式授权。
- 交给提交门的候选必须是**不含 BOM 的文本**：目标带 BOM 时由门补一层，候选自己再带一个
  就会叠成双 BOM，所以候选首字符是 BOM 直接判 `BLOCKED`、零写入、不留备份；CLI 读候选文件
  按 `utf-8-sig`，文件自带 BOM 会当编码元数据剥掉。

## 工作区与隐私

- 默认在本地处理整条链路，编辑、验证与写盘都不需要网络。会触网的只有两处，且都不读
  稿件正文：① 按锁准备依赖（`pip` / `npm`，见「依赖准备与撤销」）从公共源拉取钉定的
  包；② 外部裁判 Jev（`scripts/v2_jev.py`）——仅在你**明确启用**、凭据校验通过后才
  发起调用，未启用时这条路径不存在。本库没有把正文发往任何远端的代码，但也没有做过
  对宿主模型与网关的端到端数据流核验，因此这里不声称「私有稿绝不外传」——那取决于
  你所用的宿主与渠道。
- 运行会在**工作目录**（不是技能目录）留下：`.<原名>.lieflat-work/`（任务状态、
  事件与候选）、提交前的 `原名.bak` 备份、少量 `.tmp` 探针文件。`events.jsonl`
  只记时间、任务号、事件与判定码，不写正文。
- 判定详情与控制台回显可能包含**原稿片段（≤40 字）与绝对路径**（备份路径、任务
  目录、`--out-marker` 回执）；宿主会话记录会留存这些回显，请在意的场合自行注意。
- 任务时间戳以 UTC 记录在任务 JSON 内，不进文件名。

## 备份、恢复与卸载

- File 覆盖提交前在同目录生成唯一 `.bak`（撞名递增 `.bak1`、`.bak2`…），备份不会被
  后续提交覆盖；还原用备份覆盖回原文件即可。
- 整本任务中断后重跑：按 `state.json` 里记录的交付名与哈希幂等恢复，不产生第二份
  副本；候选与源稿哈希不符时拒绝旧结果提交。
- 卸载 = 删除技能目录本身；工作区里的 `.lieflat-work/`、`.bak` 与另存交付文件按需
  自行清理，技能不代删。
