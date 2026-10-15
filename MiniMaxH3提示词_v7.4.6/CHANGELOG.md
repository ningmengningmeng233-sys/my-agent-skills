# minimax-h3-craft 技能包变更日志（CHANGELOG）

## v7.4.4（2026-09-25，writing-skills 第二轮迭代：发现性收口落版 + 显示权威残留清零）

- **承接迭代2轮已提交实体**（`599ed6d`，当时版本行零改动）：新增 `_审查测试/test_skill_discovery_sdo.py` SDO 四判据微测；frontmatter description 触发词改「」可数原话（判据c 实测 2→13）并收紧 H3 平台限定分界；回补第1轮外移打断的 3 组正文守卫钉；跨项目交接声明逐字外移 `references/cross-project-handoff.md`。
- **显示权威对账 7.4.3→7.4.4**：`config/version.json`（唯一权威源）、`SKILL.md`（`metadata.version`、正文标题、pipeline-contract `skill_version`）同步提升；清掉残留 2 处时点标记——`AGENTS.md` 标题、`references/output-contract.md`「当前版本」行。本条为 CHANGELOG 销账动作。
- **不含**生成规则新增/放宽、不含模型能力清单变化、不含路由与时长政策改动。

## v7.4.3（2026-09-25，writing-skills 第一轮迭代：正文渐进披露外移 + 显示权威对账 + 代码瘦身）

- **SKILL.md 渐进披露外移（+40/−82 行）**：把正文里的长规范移进 11 个 `references/` 文件（`onboarding-card`、`onboarding-protocol`、`core-generation-rules`、`closure-gates`、`duration-contract`、`incremental-loading`、`long-term-loading-rules`、`model-mode-adjudication`、`output-contract`、`output-tiers`、`verification-hierarchy`），正文只留触发条件、路由与指针。外移是**搬移不是删除**：规则文本逐条进指针文件，正文指针全部经反查校验可解析。
- **显示权威对账**：`config/version.json`（本仓唯一权威源，`VersionSourceGuard` 读它）、`SKILL.md`（`metadata.version` 与标题）、`AGENTS.md` 标题、`references/output-contract.md` 的「当前版本」行同步到 7.4.3。此前存在 frontmatter 与 `config/version.json` 只改一处的漂移——本仓自带守卫当场把它判红，改齐后 `test_skill_structure_guards.py` **9 passed**。
- **代码瘦身（4 个脚本，净 −12 行）**：`result.py` −4、`rules_native_general.py` −6、`router.py` −1、`verification_protocol.py` −1，全部为零引用死语句；不动路由、时长政策与门禁口径。
- **不含**生成规则新增/放宽、不含模型能力清单变化。

## v7.4.1（2026-09-21，[Xs-Ys] 区间窗口本地约定加注）

- SKILL.md §4 核心生成规则新增第 3 条：`[Xs-Ys]` 区间窗口为**本地自创约定**，官方与社区均无对应语法；仅可作内部推演/台账记法，输出到外部系统（MiniMax 控制台、ComfyUI、第三方工具链）前必须转换为官方 `[Shot N] At MM:SS.mmm` 切点写法（细则见 `references/official-h3-grammar.md`）。
- 版本号同步：`config/version.json`（单一权威源）、`SKILL.md`（`version:` 与标题）、`AGENTS.md`。
- 不改变任何生成规则、路由与时长政策；发布门禁 11 套件/532 检查保持全绿。

## v7.4.0（2026-09-19，剧本时长忠实拆分与动态提示词组：时长合同、覆盖率门禁、原子渲染与诚实总门禁）

- **时长政策单一事实源（Task 2）**：`config/duration-policy.json`（clip_seconds 4—10、语速 3—4 字/秒、overage_review_ratio 0.2、selective padding 0.3—0.5s、四状态）+ `versioning.load_duration_policy()` 严格读盘零默认值（重复键、超深嵌套、非法 UTF-8 全部 Fail-Closed）。
- **剧本权威时长解析（Task 3）**：`script_duration.parse_script_duration()`——剧本级声明裁决（最早声明胜出、粘连词/表格/镜号前缀不算）、受支持写法（X秒/X分钟/X分钟Y秒/MM:SS/HH:MM:SS）、数字续写一律阻断不截断、内部时间码三条告警。
- **4—10 秒动态装箱（Task 4）**：`duration_planner.pack_beats()`——`Beat`/`ClipDraft` 冻结数据类、有效范围 = 政策 ∩ 模型能力、台词与动作并行计时（未识别类型一律相加）、换场强制分段、说话人/轮次软上限、三档切分风格重试（默认区间假阻断 0/1500）、Fail-Closed 异常家族；片段数由内容动态决定。
- **整集计划 Schema 与覆盖率门禁（Task 5）**：`templates/episode-plan.schema.json`（JSON Schema 2020-12）+ `episode_contract.validate_episode_plan()`——时长四档判定（Σ clip 为准、`PLANNED_SECONDS_MISMATCH` 申报分离）、`authoritative_target_seconds` 交叉核对（`SCRIPT_TARGET_MISMATCH`，反缩水不退化为自我申报）、逐字覆盖差分（gap/overlap/跨片分片相接）、声明层同类型重叠阻断、系统过渡双通道、诚实媒体字段矩阵（NOT_RUN/FAILED ⇒ null，计划值不得冒充实测）、形态门禁先于数值（`invalid_fields` 有界点名）、配置 Fail-Closed。
- **原子渲染四类整集交付物（Task 6）**：`render_episode_pack.render_episode_pack()`——`episode-timeline.md` / `prompts/GNNN.md`（与 `prompt_text` 字节精确，sha 绑定清单）/ `prompt-pack.md` / `duration-manifest.json`；每条 `prompt_text` 过真实 `audit_cli` 子进程（并发执行、逐条裁决、任一失败整集不交换）；未知 workflow 一律拒绝（不得靠 auto 降级审计通道）；目标兼容性双检 + backup/restore 原子交换；`REVIEW_REQUIRED` 只出审查摘要与机器清单；`action_beats_for_clip` 落实动作原文反查契约；`generate_output.py` 抽取 `create_staging_dir`/`atomic_swap`（行为保持），`git_head` 进程内缓存。
- **文档一致性与最小上下文路由（Task 7/8）**：运行时文档统一 4—10 口径（10—15s 黄金区间、"按比例压缩"、"SKILL.md §9b" 死指针全部清除，模型能力 15 秒与技能默认 10 秒持续区分）；`SKILL.md` §2.1 时长短合同 + §3 任务类型→初始参考集路由表（普通任务 ≤3 份，禁入清单明确）+ 长期规则（不加载整个 references、长命令写日志、阶段结束更新执行摘要、上下文 70%—80% 检查点、子代理最小上下文）；新增 `templates/execution-summary.md` 固定字段模板。
- **诚实的当前总门禁（Task 9）**：`_审查测试/run_all.py` 自动发现全部 `test_*.py`、计数从子套件输出动态求和、无计数 fail-closed、首行 `HISTORICAL_FULL_SUITE=UNAVAILABLE`（不冒充历史 316 项）；辅助检查纳入 `reference_graph`（新增机器可解析汇总行）、`validate_source_manifest`、`compileall`、上下文审计器。
- **端到端固定夹具验收（Task 10）**：`fixtures/episode-{30,60,90,120,120-over20}.json` 全链（解析→装箱→验证→渲染）——120 秒夹具动态 13 片段（≥12，组数不固定）、原问题反例 `[5,15,12,13,15,8]` 必须失败并报告少 52 秒、权威目标接线证明（私自下调申报目标被 `SCRIPT_TARGET_MISMATCH` 拦截）、真实媒体字段保持为空。
- **工程优化**：渲染器配置单次读盘（门禁与台账共用同一政策对象）、提示词审计并发化（裁决与顺序不变，套件提速约 26%）、`git_head` 进程内缓存。
- **未变（如实保持）**：人工评分表 0 份、原生 Agent 会话 0/30、未调用 live 模型、无真实视频——本版为 `local-stable` 候选交付，交付状态 `SOURCE_IMPLEMENTATION_PASS / RELEASE_NOT_RUN`，不构成 `full-closure`。

## v7.3.1（2026-09-14，非人工工程完善：判定说真话、证据可追溯、离线安全与文档口径）

- **人工评测聚合器（P0-1/P0-2）**：任一明确 `verdict=FAIL` 即阻断并退出 1（此前两份 FAIL 仍可能退出 0 并报 COVERED）；按 `(case_id, reviewer)` 唯一键校验，同一 reviewer 的两张表不再充数；五维分数缺失返回结构化错误而非异常栈；只有全部 case 由两名不同评审者五维齐全 PASS 才 `ACCEPTED`。
- **零原生会话（P0-3）**：正式结果目录 0/30 时 `REAL_AGENT_E2E` 如实报 `NOT_RUN`（不再因"未来要复核"报 `REVIEW_REQUIRED`），冒烟批次保留在隔离目录且不计入正式阈值。
- **证据索引覆盖外部原件（P0-4）**：索引逐 case 记录原件路径、字节数、SHA-256、run_id、source_commit、origin（视频另记授权），拒绝绝对路径、`..`、链接/目录联接逃逸、重复 case 与外来提交；`external_digest` 只作披露，**不能支撑 PASS 或 full-closure**；发布器读取报告时一次性取回状态、索引与 run_id 并逐字节复算。
- **发布清单绑定报告身份（P1-5）**：清单记录报告 run id、报告原始字节 SHA-256 与 basename（不含私有绝对路径）；报告在判定后被改一字节即拒绝发布；`--evidence-run-id` 必须与报告内 run id 一致；三镜像核验新增 `CLOSURE_REPORT_BOUND`。
- **离线下载逐跳安全（P1-6）**：逐跳校验 scheme/主机/解析地址（回环、私网、链路本地、保留、组播一律拒绝），限制跳数与体积，核对 `Content-Length` 与实收字节，`.part` 验证后原子替换；回环例外仅在显式 `--allow-test-hosts` 下存在，生产 `--base` 走同一受信 https 主机策略，避免 API key 误发。
- **文档口径（P1-7）**：公开 API 边界（supported/denied/not-listed）由 `config/provider-capabilities.json` 派生并写入能力矩阵与核对日志；SKILL.md 标注 `H3-Max-Turbo` 为内部条目、公开 V2 未列出；2026-09-14 联网复核确认公开模型表无 Turbo 变体、`H3-Max` 的 Ref2VA 官方明确不支持；一致性检查会在文档重新宣称 Turbo 公开支持或由通用 H3 推断 H3-Max Ref2VA 时失败。
- **工程收尾**：`run_all.py` 316/316、`build_release.py` dry-run 通过；新增离线套件 `test_render_download_security.py`（8 例）；修正人评套件在总表中按 0/0 计数、full-closure 拒绝测试依赖工作树干净两处缺陷。
- **未变（如实保持）**：人工评分表 0 份（`HUMAN_EVAL=NOT_RUN`）、原生 Agent 会话 0/30、未调用 live 模型、未下载真实视频；本版为 `local-stable` 候选交付，不构成 `full-closure`。

## v7.3.0（2026-09-13，门禁修复：六门禁判定契约、显式证据绑定与发布层级说真话）

- **六门禁判定契约（A01–A08）**：
  - 新增 `scripts/closure_gate_contract.py` 纯判定层：`command_gate / local_gate / agent_gate / render_gate / video_gate / verify_index / release_allowed`，状态机封闭（无证据=NOT_RUN；损坏/失败/矛盾=FAIL；机器过人工未覆盖=REVIEW_REQUIRED；机器+人工=PASS）；
  - `collect_closure_evidence.py` 重写：STATIC（9 套件 157 例）与 CLI（12 例）门禁由**本次执行**推导（rc/计数/起止/日志哈希全留档），LOCAL 门禁显式 `--local-run-dir` 并逐案例重算磁盘字节↔审计哈希绑定率，杜绝缺字段默认 1.0 与旧批次混入；
  - `run_agent_e2e.py` / `measure_e2e_metrics.py` 记录并透传 `source_commit`；
  - 冻结 `e2e/real_agent/case-manifest.json`（五模式各 5 + 边界 5，共 30 例）；`verify_transcripts.py` 增唯一 session_id、清单覆盖与技能 SHA 绑定；`agent_gate` 杜绝重复会话与部分复核充数（5 例冒烟如实保持 REVIEW_REQUIRED）；
  - `verify_render_evidence.py` 硬化：显式 run_id、task_id/模型/提示词 SHA 绑定、MP4 非零哈希一致、ffprobe 可解码、v2 逐模式人工表；
  - 新增 `e2e/video_reverse/verify_video_evidence.py`：合成夹具永不计入，5 个授权真实视频+逐项人工对照才有 PASS 路径；
  - 收集器产出 `evidence-index.json`（路径/字节/SHA/run_id/commit/type），报告记录 `evidence_index_sha256`；
- **发布器说真话（A07/A08）**：
  - 删除"字典序最新报告"隐式选择；`--apply` 必须显式 `--closure-report`/`--evidence-run-id` 并逐文件重算证据索引；
  - 新增 `--release-tier local-stable|full-closure`：full-closure 需六门禁全 PASS；local-stable 仅需本地三门禁且制品禁称闭环；无 tier/无报告的 `--apply` 一律拒绝；
  - staging 排除 `evidence/`；manifest 增记 `release_tier / closure_gates_source / closure_report / evidence_run_id` 与六门禁实际状态；
- **版本契约对齐（A09）**：
  - `config/version.json` → `7.3.0 / v7.3.0-closure` 与分支一致；修复 `versioning.load_version_info` 读取 `git_branch` 键名的潜伏缺陷（此前 enforce_git 恒判不匹配）；
  - 新增发布契约测试：分支与 release_name 不一致必须阻断发布。

- **输出安全与反伪造（第一批）**：
  - `generate_output.py`：安全 ID 校验（case_id/run_id）、realpath 路径逃逸防护、默认唯一 run 目录（`out/<run_id>/<case_id>`）不覆盖旧结果、显式 `--replace` 先校验后原子交换；失败永不删除旧结果；
  - 草稿组装取消全部伪造默认值：native-general 自然语言直传原话；drama 五项内容字段均为决定性信息，缺失即 NEEDS_INPUT（exit 3）补问清单，绝不编造人名/灯光/台词场景；
  - `run_agent_e2e.py`/`measure_e2e_metrics.py` 改按 run 目录组织并显式绑定；
- **输出质量合同与评测（第二批）**：
  - `analysis.json` 增四类信息（硬约束/素材用途/自主偏好/未知信息）与逐约束落点追溯，未落地硬约束即缺陷（exit 1）；
  - 独立盲测集 `e2e/blind`（10 例：五模式/连续动作/精确台词/信息不足/冲突要求/多素材），双检查"不能丢/不能凭空加"；盲测驱动 R-ZG03 动作词库扩充；
  - 人工评测脚手架：双评审五维评分 + 阻断缺陷词表 + 原始评分保留、分歧不平滑（`aggregate_human_eval.py`）；
- **文档与能力边界（第三批）**：
  - SKILL.md 层级计数/示例版本修正；能力文档再生加 `--check` 防漂移；新增外部核对记录 `references/official/external-verification-log.md`（2026-09-13 联网核对，无配置差异）；
  - 开发冒烟隔离至 `results_smoke/`，主 `results/` 专候冻结 30 例原生会话，试点协议入 README，门禁维持 REVIEW_REQUIRED；
- **离线接口与诚实发布（第四批）**：
  - v2 客户端 `--http-timeout`；假服务器补超时传输失败与 TERMINAL_FAILED 终态（8 例）；
  - 渲染 runner 重写：显式 `--prompts-dir`（缺合格 prompt 即阻断）、受检 git、https-only/回环白名单下载、尺寸上限、证据字段 task_id/model/prompt_sha256/sha256/url_host；
  - 视频证据包格式文档 + 破包必拒测试（夹具永不计入真实门禁）；
  - 终验 297/297 全绿；按新 HEAD 重新生成六门禁证据；local-stable 层级发布并独立核对三镜像。

## v7.2.0（2026-09-13，真实 Agent E2E 闭环、输出三文件物理隔离与指标度量门禁）

- **Native General 独立门禁（C-1）**：
  - `scripts/rules_native_general.py` 扩充为 R-ZG01~R-ZG05（结构分离、模板残留、主体动态、画质词、运镜合理性）；
  - `audit_cli.detect_channel` 在 auto 模式下识别通用中文并路由至 `native-general`，终结误调 drama 规则；
  - 新增 `_审查测试/test_native_general.py`（10 正例 + 10 反例 + 5 边界 + 5 路由，共 30 用例）并接入主门禁；
- **真实 Agent E2E 与三文件隔离（C-2/C-3）**：
  - 新增 `scripts/generate_output.py`：路由裁决 → 草稿组装 → 真实 `audit_cli` 子进程审计 → 输出 `prompt.txt` / `analysis.json` / `audit-report.json`；
  - 30 个黄金用例（`e2e/cases` + `e2e/expected`，T2VA/I2VA/FL2VA/L2VA/Ref2VA 各 5 + 路由边界 5），纯净提示词与审计报告物理分离；
- **指标度量门禁（C-4）**：
  - 新增 `scripts/measure_e2e_metrics.py` 与 `_审查测试/test_agent_e2e.py`：路由、素材、硬约束保留 100%，模板残留 0%，审计污染 0%，零崩溃，JSON 100%；
- **视频反向工程（C-5）**：
  - 新增 `scripts/video_probe.py`（ffprobe 优先、OpenCV 兜底、缺依赖诚实 `NOT_RUN`）与 `scripts/video_reverse_engineer.py`（观测/推断/建议分层，推断带置信度）及 `_审查测试/test_video_reverse_engineering.py`。

## v7.1.3（2026-09-12，发布构建器原子切换、回滚保障与 Fail-Closed 版本网关强化）

- **统一版本网关与绝对 Fail-Closed**：
  - 新增 `scripts/versioning.py` 统管版本读取与强校验，彻底移除所有 `7.1.1` 默认容错回退；
  - `audit_cli.py`、`run_all.py`、`build_release.py` 以及测试套件全线接入统一版本网关；
  - `config/version.json` 损坏或缺失时立即 Fail-Closed 终止进程；
- **发布构建器事务回滚与原子切换**：
  - 彻底封死发布后门，禁止 `--apply` 与 `--allow-dirty` 或 `--skip-prebuild-gates` 组合使用；
  - 深度安全校验目标路径，严格限定桌面三镜像路径白名单并防御路径穿越与符号链接逃逸；
  - 引入多目标完整暂存（`sibling-new`）与事务性原子备份切换（`sibling-backup`），任何异常触发即时回滚；
  - 结构化分层发布清单（`STATIC_TESTS`, `CLI_E2E`, `AGENT_E2E`, `MODEL_RENDER_E2E`）；
- **真实 CLI 入口测试与能力事实源对齐**：
  - 接入 `test_cli_entrypoints.py` 进程级真实测试；
  - 规范化 `config/model-capabilities.json` 来源元数据与一致性生成脚本。

## v7.1.2（2026-09-12，L2VA/FL2VA 严格路由重构、外部能力解耦与发布构建器 Fail-Closed 防护）

- **L2VA 与 FL2VA 严格首尾帧约束**：
  - 核心模式枚举规范化为 `t2va / i2va / fl2va / l2va / ref2va`，保留完整兼容别名；
  - 严格资产约束判定：I2VA 强制首帧；FL2VA 严格强制首帧与尾帧同时具备，缺失任一均失败；L2VA 强制尾帧；
  - `route_request()` 精准识别“仅尾帧”、“尾帧”路由至 `l2va`；
- **模型能力矩阵配置解耦**：
  - 新增 `config/model-capabilities.json` 作为唯一官方权威配置源，解耦硬编码，未证实特性标记 `UNVERIFIED`；
- **发布脚本 Fail-Closed 防护与 Staging 隔离**：
  - `scripts/build_release.py` 改造为绝对 Fail-Closed：Git SHA、分支、干净状态、主门禁任一不满足立即阻断；
  - 增加 `--dry-run` 与显式 `--apply`；目标目录强制 realpath 校验，杜绝意外删除；
  - 发布清单支持 staging 隔离，字段精确化为 `content_commit`、`source_branch`、`source_tree_clean` 等；
- **来源凭据分级与测试套件扩充**：
  - `scripts/validate_source_manifest.py` 细化状态码分类，诚实声明上游 commit 未验证；
  - 扩展四项全新自动化测试套件：`test_release_builder.py`、`test_version_consistency.py`、`test_source_provenance.py` 及新模式路由测试，总用例数大幅提升并由主门禁动态聚合。

## v7.1.1（2026-09-12，阶段 G 可信发布整改与稳定化闭环）

- **版本与Git规范化**：
  - 规范 Git 分支为 `v7.1.1-stabilization`，配置 `.gitignore` 排除 `__pycache__`、`*.pyc`、`*.log`、`*.tmp`；
  - 引入统一版本配置 `config/version.json`，全线统一版本标识为 `v7.1.1`；
- **发布脚本与交付物可信闭环**：
  - 重构 `scripts/build_release.py`：构建前强制运行完整主门禁（退出码0断言），获取真实 Git 完整 commit SHA；
  - 实施严格白名单机制与双向集合比对（`missing=0`、`extra=0`、`mismatch=0`），清理未授权额外文件；
- **严格根可达引用图拓扑**：
  - 彻底重写 `scripts/reference_graph.py` 为根节点（`SKILL.md`、`AGENTS.md`、`router.py`）BFS/DFS 拓扑遍历，支持严格 UTF-8 解码与循环孤儿精确识别；
- **官方来源清单与校验器**：
  - 补全 `references/official/source-manifest.json` 真实规范化 SHA-256 哈希；编写 `scripts/validate_source_manifest.py` 自动化校验；
- **动态测试统计与边界反例补齐**：
  - 测试套件输出动态结构化 JSON 统计，主门禁动态聚合真实用例总数，杜绝硬编码假数字；
  - 补齐 H3-Max-Turbo 首尾帧成功、非法 forced_profile 错误退出码、冲突路由、中文混入等边界测试；
  - 实事求是声明真实状态 `E2E_AGENT_TEST=NOT_RUN`。

## v7.1.0（2026-09-11，微内核规则重构、全确定性门禁闭环与五维路由交付）

- **P0 确定性缺陷清零**：
  - **P0-01/P0-02 时间轴重构**：彻底重写 `en_check_timeline()`，消灭不可达死代码；全线统一第一镜 `[Shot 1]` 严禁带时间戳，后续镜头必须递增携带 `At MM:SS.mmm,` 且杜绝跳号（R-E01、R-E02）；
  - **P0-03 四类标签资产闭环**：实现 `<Subject N>`、`<Picture N>`、`<Video N>`、`<Audio N>` 完整定义—使用生命周期闭环校验，未定义资产或跨章节首次声明即刻拦截（R-E04）；
  - **P0-04 保留标记类型分层**：严格区分视觉素材标记（`fully_preserved` 等）与音频标记（`reference`、`reuse` 等），杜绝跨介质违规混用（R-E05）；
  - **P0-05 任务类型去重**：summary 任务组合去重检查，禁止重复声明任务类型（R-E03）；
  - **P0-06 对白与标签闭环**：精准拦截未闭合 `<d>` 标签，严格检查语言头与配对（R-E06）；
  - **P0-07 静音策略支持**：正式支持全场静音 `overall_soundscape: N/A`（R-E07）及 `--audio-policy` 参数；
  - **P0-08 语义检验接入 Native 主门禁**：`semantic_lint.py` 的动作因果链与空间连续性检查直接接入 native 审计链路与退出码，阻断严重因果断裂（R-Z04）；
  - **P0-09~11 五维路由与能力阻断**：新增 `scripts/router.py`，区分 `native-drama-series` 与 `native-general`，并支持 H3 / H3-Max / H3-Max-Turbo 运行时能力校验与不支持模式硬阻断（如 H3-Max-Turbo + I2V 阻断）。
- **P1 假阳性根治与架构升级**：
  - **P1-01 图号属性有效性**：删除长度豁免，解析资产名称+分隔符+可观察属性，杜绝 `图1 = abcdefghij` 绕过（R-Z01）；
  - **P1-02/P1-03 模式风格精确匹配**：Mode A 末行必须精确匹配官方皮肤锁，追加内容即刻拦截（R-Z02）；真人与 CG 关键词同时出现即刻判定互斥冲突（R-Z03）；
  - **P1-07 孤儿文档清零**：创建 `scripts/reference_graph.py`，建立引用拓扑图，非活跃文件安全归档至 `references/legacy/`，活跃孤儿文档清零；
  - **P1-08 Markdown 围栏纠正**：输出模板严格采用四反引号包裹三反引号代码块，消灭渲染错位；
  - **P1-09 动态校验凭证**：审计器输出结构化 JSON 凭证与哈希签名，彻底消灭硬编码数字；
  - **P1-10 严格 WARN 策略**：支持 `--warn-policy allow|report|fail`；
  - **P1-11 严格编码错误**：默认 `errors="strict"`，拒绝非法字符静默替换。
- **P2 文档与事实源唯一化**：
  - 同步更新 `SKILL.md`（体积保持在 8 KiB 内）、`AGENTS.md`（消除旧范围锁冲突）；
  - 创建 `references/official/source-manifest.json` 记录官方权威快照哈希与追溯源。

## v7.0.0（2026-09-10，工业级双通道重构与 68/68 门禁闭环）
- 双通道架构与初始测试套件搭建。
