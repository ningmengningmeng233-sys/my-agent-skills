# 在线导出与恢复（环境检查 → 清单 → 导出 → 验收/合并 → 续跑 → 状态）

代码权威：`scripts/archive.py`、`job.py`、`preflight.py`、`state.py`、`media.py`。本文件
引用实现时**按函数名/常量名**指（不行号），引用后端时按你本机 `tdl --help`
（0.20.4 版、commit 9d7d49e 基准）输出的行号指，下文记作 `help:N`；tdl 版本不同时
行号可能漂移，以你本机输出为准。每句话都能指回这两处之一；指不到的，一律
落在最后一节「未验证与限制」，不写成能力。

入口 `SKILL.md` 的模式路由把 `export` / `resume` / `status` / `download` 指到本文件。

## 何时加载本文件

要动在线面就加载它：读聊天清单、跑 `tdl chat export`、分段导出、验收合并、断点续跑、
补媒体、查完成状态、排障恢复。

不在本文件（按模式另加载，别一次全读）：

- 离线整理 / 本地检索 / 分享副本 → `offline-search-share.md`
- 阅读版生成与广告清洗 → `reading-and-cleaning.md`
- 媒体类别判据、附件三步验收细则、`last` / `matching_messages` 计数口径 → `selection-and-types.md`
- 增量更新与常用方案 → `incremental-and-presets.md`

## 1. 任何在线动作之前：环境与能力检查

`archive.py check --job <任务配置.json>`（`archive._cmd_check`）→ 内部走
`preflight.inspect_environment(config, action, probe_session)`。

- **纯本地**：不执行 tdl、不查 PATH、不创建目录、不触碰会话文件、不联网
  （`preflight.py` 模块 docstring）。实现只做 `os.path.isfile(tdl)`（`_check_tdl`）、
  `os.path.isdir(storage)`（`_check_session_storage`）、`os.path.isdir(destination 的上级)`
  （`_check_output_path`）这类存在性判断。
- 报告形状：`{action, probe_session, session_probed, local_checks_only, capabilities[]}`，
  每条能力是 `capability / status / reason / affected_action` 四元组（`preflight._entry`），
  status 只有 `preflight.ALLOWED_STATUS` 五种取值
  `available / unavailable / unsupported / insufficient_source / unchecked`。
  **没有总结论字段**：局部未知不被一句「整体可用」覆盖，逐条读。
- `check` 另由 CLI 追加两条申报（`archive._cmd_check` 的 `observations`）：
  `tdl_id_range_endpoints` 恒为 `insufficient_source`（原因即 `state.ENDPOINT_NOTE`，见 §4），
  以及 `session_mutex_across_destinations`（= `state.MUTEX_CAPABILITY`，见 §6）。
- **`check` 的退出码只有 0 与 2**：选择合同不过（`job_selection` 非 available）或
  `lock_root` 指向账号存储目录（`state.LOCK_SCOPE_REJECTED`）→ 2；其余一律 0。
  **能力缺口不用退出码表达**：`tdl_executable` 是 `unavailable` 时 `check` 照样返 0 ——
  读 JSON 里的 `capabilities[]`，别看码。
- `tdl_version_and_parameters`（`preflight._check_tdl_parameters`）、`text_export` 与各类
  `media_<类别>`（`_check_content_capabilities`）在本次构建里恒为 `unchecked`：代码没跑过
  tdl，就不宣称版本与参数兼容。另有 `署名显示`（同一函数，选了 `text` 才出现）恒为
  `insufficient_source`：发送者字段未按真实导出 schema 核对，缺发送者时阅读版只记缺失、
  不编造姓名（`reading.MISSING_SENDER_NOTE` / `missing_sender` 口径）。

**会话探测是另一件事**：`--probe-session` 只是「请求」，它自己的帮助原文就写着
「请求会话探测（本 CLI 不联网，只如实标注未验证）」（`archive.build_parser`）。对离线
action 传它，`inspect_environment` 直接 `ValueError` → 退出码 2；对在线 action 传它，
CLI **也没有传入任何 `session_probe` 实现**，于是 `preflight._check_session_login` 落
`unchecked` + 原因「已请求会话探测但未提供探测实现；未联网，不宣称登录有效」，
`session_probed=false`、`local_checks_only=true`。也就是说：**本构建里没有任何联网探测被
执行过**，纯本地检查绝不等于登录有效。真实登录由用户在交互式终端自己完成
（设计方案第 120 行）。

## 2. 聊天清单：不在本 CLI 里

`archive.py` **没有**清单子命令——已注册的动作只有 `archive.COMMANDS` 里那 12 个
（`validate / check / export / status / resume / download / render / import / search /
share / update / preset`；`update` / `preset` 的用法归 `incremental-and-presets.md`）。读取当前
清单由执行者直接调后端，旗标面以捕获副本为准：`tdl chat ls -o json`，可选 `-f/--filter`
（`help:186-195`）。顶层命令表里**没有 `export`，也没有 `tools`**（`help:8-32`、`50-54`）；
导出与清单都在 `chat` 组下（`help:96-105`）。

把选中的项按 `job.validate_job` 的合同写进 `chats`（`job._check_chats`）：每项 `peer_type`
（只 `job.PEER_TYPES` 三个：`user/chat/channel`）、正整数 `id`、`display_name`（仅人读，
不参与身份隔离）、`locator`（当前 tdl 可用定位值，合同原文就写着「不臆造格式」）。
`locator` 会被导出模板代入 `-c/--chat`，而 `help:162` 的原文只有「chat id or domain. If not
specified, 'Saved Messages' will be used」——**私聊/基本群/超级群/频道各自的定位写法本次未对
真实账号核对**，不臆造；同一 `peer_type + id` 重复会被拒。用户选「全部在线聊天」时，把
**当前清单**的 ID 固化成 `chats` 快照，不默认全账号（工单「配置与跨任务合同」第 78 行）。

## 3. 冻结快照上界与任务身份

- 分段上界只取**本次配置里已冻结的值**，代码绝不现场探测（`state._declared_bounds`）：
  `range.mode=id` 时可退到 `range.upper`，其余模式（all/time/last）**必须**显式给
  `snapshot.upper_bound`（正整数，由 `job._check_optional_maps` 把形状），否则抛错
  「非 ID 范围必须显式给出 snapshot.upper_bound（恢复与导出都不得重新探测上界）」；
  `range.upper` 越过冻结上界同样直接拒（「不得扩大范围」）。
- 任务身份 = peer 类型 + chat ID（`job.chat_identity`），状态根
  `<destination>/telegram-state/<身份 slug>`（`state.task_state_root` + `identity_slug`），
  显示名不参与路径与隔离。
- `task.json` 记下身份、配置摘要、冻结上界、段宽与整份分段计划（`state.init_task`）。复用
  旧任务时三道闸：身份不符拒、上界不符拒（「恢复旧任务沿用原上界，不重新探测扩大范围」）、
  摘要不符拒（「请建立新任务，不复用旧完成状态」）。摘要由 `job.job_fingerprint` 算，输入
  是 `_digest_payload`：chats/range/contents/outputs/cleaning/template_compression/
  destination/sources/`snapshot.upper_bound`/tdl/storage/namespace 与**去掉认证信息的**
  proxy（`job._safe_proxy`）；会话目录与命名空间的原值不进摘要，也不进
  `job.job_summary` 的输出。
- 计划闸独立于摘要闸：段宽是运行参数、不在摘要里，但续跑时 `state.plan_task_segments`
  会按冻结身份与上界重算段计划比对，变了就拒复用（「分段计划与任务冻结的计划不一致」）。
- **选择变了就开新任务**（改 `contents`/`outputs`/范围/清洗就会让摘要不一致而自然生效），
  别指望旧完成状态能续上新选择。换 `destination` 等于换一套状态根。

## 4. 分段、逐段验收与合并

导出走 `archive.py export`（`archive._run_online` → `state.run_export`）。

**分段**：`state.plan_segments(chat_id, lower, upper, width)` 把 `[lower, upper]`（任务侧按
合同**两端包含**，`state.ID_ENDPOINT_SEMANTICS = "inclusive"`）按 `segment_width` 切片，
相邻段**边界重叠一个 ID**（下一段 `lower` = 上一段 `upper`）。非 `id` 模式的下界是
`state.PLAN_LOWER_BOUND = 1`——**分段轴是消息 ID，不是日期**。段宽未配时默认
`state.DEFAULT_SEGMENT_WIDTH = 5000`，配了就必须 ≥1（`job.RUNTIME_INTEGER_MINIMUM`）。

**端点包含性（不得翻案的一条）**：`-T id` / `-T time` 哪端包含，`help:154-184` 与官方文档
都没说，本次也没对真实账号核对 → `state.ID_ENDPOINT_EVIDENCE = "unknown"`，能力记
`insufficient_source`（`state.ENDPOINT_NOTE`、`archive._cmd_check` 的
`tdl_id_range_endpoints`）。防御是重叠段边界 + 按 `peer_type + chat_id + message_id` 去重
（`state.merge_completed` 的 dedup key 用 `task["identity"] + 消息 ID`）。
**任何文档与报告都不得写「后端端点包含/不包含」。**

**命令从哪来**：仓库内**没有** `tdl chat export` 的参数拼装实现。导出命令由执行者的
`--command-template`（一个「非空字符串 JSON 数组」文件）提供，`archive._template_builder`
的替换面固定为 `archive.PLACEHOLDER_NAMES`：
`{tdl} {chat_id} {destination} {display_name} {locator} {lower} {namespace} {output}
{peer_type} {segment_id} {upper}`。未知占位符、非数组、空数组、占位符在本次配置里没有值，
全部按配置非法在启动子进程前拒（退出码 2）。**没有 `{start}` / `{end}` / `{n}` 占位符**，
`--all` / `--raw` / `--with-content` / `-f/--filter` / `--reply` / `--topic`
（`help:161-171`）也得由模板自己写死——代码不会替你把日期窗口或内容开关传给后端。
仓库测试夹具用的形状是
`["{tdl}","chat","export","-c","{locator}","-T","id","-i","{lower},{upper}","-o","{output}"]`
（`work/telegram-archive-tests/test_state.py` 的 `REAL_EXPORT_TEMPLATE`），对着**假后端**跑；
它由 `test_state.BackendFlagSurfaceTests` 逐字核对回 `help` 的 `tdl chat export` 段，并断言
不出现 `--id-from` / `--id-to`。缺 `--command-template` 就**一个子进程都不启动**，直接
`unavailable` + `insufficient_source` 申报（`state.run_export` 的
`_refused_report(..., "insufficient_source", ... ENDPOINT_NOTE)`）→ 退出码 3。

**逐段落盘**（`state._attempt_segment` + `state.commit_segment`）：
`tmp/<segment_id>.json.part` → 子进程（`state._argument_list` 只收参数列表、`_subprocess_runner`
用 `subprocess.run(list(argv), capture_output=True)`，**绝不拼 shell**）→ 退出码为 0 →
临时文件确实存在 → `validate_export` 解析 → `os.replace` 进 `segments/<segment_id>.json` →
完成清单登记 `sha256 / size / count / 区间 / 身份`。「退出码 0 但后端没写出导出文件」不是
完成凭据（失败记录原文就这么写）。
`state.validate_export` 只认两种形状：**消息数组**或**含 `messages` 数组的映射**，消息 ID
取 `state.MESSAGE_ID_KEYS`（`id` / `message_id`），逐条必须落在段区间内；文件自带
`state.DECLARED_CHAT_KEYS`（`chat_id` / `dialog_id`）时必须与任务一致。零条是合法结果
（空范围，返回 `empty: true`），但完成凭据由清单记录提供，不是「文件存在」。

**缺段绝不发布成品**：`state.merge_completed` 先要求段都在冻结计划内、计划段全齐、同一
peer 身份，再逐段过一次 `state.scan_completed`，任一不过就抛错且不创建目标。成品路径
`<destination>/telegram-state/<身份>-messages.json`（`state.final_target` +
`state.FINAL_SUFFIX`），size/SHA-256 按**实际落盘字节**登记；合并去重**只在同一 peer 身份
内**按消息 ID 生效。目标已存在且不是本任务已验收发布的成品 → 拒覆盖用户文件。
只有选了 `json` 且无未验收段时 `run_export` 才尝试合并；没选 `json` 时段索引全部验收也算
`text_index` complete（`state._index_entry` 的原因文案区分这两种）。

**崩溃/重启后怎么复用已验收段**：完成凭据五件齐全才算数——清单在册、身份一致、区间一致、
文件仍可解析且逐条 ID 在界内、SHA-256 与条数一致（`state._rejection_reason`；
「文件存在」或「能解析」单独都不算，`report["pending"][].reason` 会写明是哪一条不过）。
已验收段直接跳过，不重导。「段文件已落盘但清单没发布」这个窗口由
`state.reclaim_orphan_segments` 在取锁后复认领：判定一项不放松，复认领**只补清单记录、
绝不改段文件字节**。取锁成功后还会清掉超龄（`state.STALE_PART_MAX_AGE_SECONDS`，默认
3600 秒）的 `*.part` / `*.part-<pid>`，只扫状态根、`tmp/`、`media/` 顶层，逐条上报
`removed / kept / skipped / needs_attention / ignored`；被锁拒绝的运行不做任何清理
（`state.prune_stale_temp_files`，在 `run_export` 取锁后调用）。

## 5. 续跑与补媒体

- **续跑 = `resume`**，与 `export` 同一个处理函数（`archive.COMMANDS` 里两者都指向
  `archive._run_online`），但配置里的 `action` 必须写 `resume`：`export / resume / download
  / render / import / search / share` 都比对 `action` 与子命令，不一致按配置非法拒；
  `validate` 与 `status` **不比对**（`status` 只用配置推状态目录）。任务身份摘要按
  `export` 归一（`state.export_fingerprint`），所以两者指向同一个任务。恢复**复用原配置**：
  原冻结上界、原段计划、原完成清单；不重新探测、不扩大范围，要扩大就新建任务。
- **补媒体 = `download`，独立在线动作**：`archive.py download --job <配置>
  --export-file <本任务已验收的导出 JSON>`，`--export-file` 必填，不自动找文件、不递归
  磁盘（`archive._cmd_download`）。文字源要先过 `media.accepted_export` 的凭据闸（摘要 +
  身份 + 登记区间落在任务冻结范围内），未验收 / 他人任务 / 旧记录缺区间字段一律拒
  → `unavailable`（归 3）。后端命令由 `media.download_argv` 生成，固定为
  `tdl dl -f <索引> -d <目录> --template <media.TDL_DEFAULT_TEMPLATE> -n <ns>
  --storage type=bolt,path=<storage> [--proxy <url>]`：**绝不带 `-i`/`-e`**
  （`help:69`、`help:73` 自证「only judge by file name, not file MIME」，用了就会把动画混进
  视频、语音混进音频）、**绝不带 `--group`**（`help:71` 会自动扩相册）、不带 `--skip-same`、
  不带 `--desc`。候选索引 / 下载 / 验收三步分开、退出码 0 单独不成立、`tdl dl -f` 是否接受
  本地过滤后的索引仍未核对（`media.CAPABILITIES` 里 `tdl_dl_export_file_schema` 等条目记
  `insufficient_source`）——细则见 `selection-and-types.md` 第四节。

## 6. 会话互斥、重试与失败分类

- **一把会话锁覆盖整个任务**（不是每聊天一把，见 `state.acquire_session_lock` 的
  docstring），标记是 `state.account_marker`＝`sha256("storage|namespace")` 前 16 位，
  会话目录与命名空间原值不进锁文件名也不进锁内容。锁文件
  `<lock_root>/locks/session-<标记>.lock`（`state.LOCK_SUBDIR` + `state.LOCK_PREFIXES`），
  `state.acquire_lock` 用 `O_CREAT|O_EXCL` 原子占，**不抢占、不超时自动破锁、不写会话目录**。
  `export / resume / download` 三条产品路径都只经同一份 helper 取放
  （`state.acquire_session_lock` / `state.release_session_lock`）。
- 锁根优先级：显式参数 > 执行者配置 `lock_root` > 未配置时用 `destination`
  （`state._lock_root_choice` / `state.resolve_lock_root` / `state.default_lock_root`）。
  **未配 `lock_root` 时，同一账号存储换不同 `destination` 的两个任务互相看不见，跨保存目录
  不互斥**——这条缺口必须由 `session_mutex_across_destinations` 申报（`status=unavailable`
  + `state.MUTEX_GAP_REASON` 点名要配），不许沉默（`state.mutex_declaration` /
  `state.session_mutex_report`）。
- `lock_root` 与账号存储（登录会话）目录**相同、在其内或包住它** = 配置非法，启动任何子
  进程之前拒（退出码 2）；包含关系用 `state.inside_session_storage` 的 `resolve()` +
  `relative_to` 判定（不用字符串前缀），错误文字与互斥申报都**不回显该路径**
  （`lock_root` 报 `null`、`lock_scope` 报 `state.LOCK_SCOPE_REJECTED`）。
  结尾会被 Windows 静默剥掉的点或空格也先按原样拒（`state._trimmable_by_windows`）。
- 锁被占 → 不并发写同一会话：整任务归 `unavailable`（退出码 3），`state.LockBusy` 随异常
  带出锁文件路径，陈旧锁由**人**确认后处理，代码不自动删（`state.run_export` 的
  `_refused_report(..., "lock_busy", ...)`）。
- 取锁成功后才重置本轮诊断，且只清本任务名下的记录（`state._reset_run_diagnostics`）；
  被拒的运行不写。释放锁失败记 `lock_release` 诊断并明写「不伪装已释放」
  （`state.release_lock` 只认自己的 nonce，凭据不符就保留他人的锁）。
- 加锁顺序固定「先归档锁、后会话锁」（`state.LOCK_ORDER`）；`session-*` 是会话锁名、
  `archive-*` 是归档锁名（`state.acquire_archive_lock`），两把锁绝不同名、绝不互替。
- **失败分类与重试**（`state.classify_failure` + `state._attempt_segment`）：限流
  （`state._RATE_LIMIT_MARKER` 即 `flood_wait_<N>`，或文本含 `flood_wait`）与网络类
  （`state._NETWORK_MARKERS`）属 `state.RETRYABLE_KINDS` → 可重试，次数取配置 `retries`
  （默认 0，即不重试）；限流按后端给的秒数等，单次封顶
  `state.FLOOD_WAIT_CAP_SECONDS = 30` 秒，读不到数字就等 1 秒（`state.flood_wait_seconds`）。
  授权/会话类（`state._AUTHORIZATION_MARKERS`：`unauthorized`、`auth_key_invalid`、
  `session_terminated` 等）属 `state.STOPPING_KINDS` → **停整轮**：`stopped` 一旦出现，
  同一 job 的后续聊天不再启动，各标 `unavailable` 并点名前序身份（聚合报告另带
  `stopped_early.after/skipped` 清单），不进重试循环、不逐个刷同样的失败。
  后端可执行文件起不来（`FileNotFoundError`）归 `insufficient_source` → 3，不伪装成跑坏了
  一段。
- **`EOF` / `pong missed` 只是观察到的连接故障**（它们在 `_NETWORK_MARKERS` 里，因而参与
  有限重试），**不能据此归因为机场、代理或 Telegram 风控**（设计方案第 112 行）。分段降低
  的是失败重做成本，不承诺根治网络。子进程失败文本截断到 400 字符用于分类
  （`state._subprocess_runner`）。

## 7. 状态与完成判据

`archive.py status --root <状态目录>` 或 `--job <配置>`（用 `--job` 时配置只能含一个聊天，
`archive._cmd_status`）。**status 只读**：不启动、不恢复任何子进程，也不写状态文件，
报告里 `no_child_process_started: true`（`state.build_status`）。

报告字段：`state_root / no_child_process_started / session_mutex / task / segments
{planned, verified, pending, items[]} / final / outputs / complete / status / diagnostics`。
**三类输出分开列**（`state._output_entries`）：

- `text_index`（`state._index_entry`）：`evidence` 给 `planned / verified / missing /
  segment_reasons`；缺段就是 `partial`，原因原文是「已验收 n/m 段，缺段不发布正式成品」；
  能力类诊断（`state.CAPABILITY_KINDS`：`insufficient_source` / `lock_busy` /
  `lock_release`）优先，直接把 `text_index` 压成 `unavailable`——`build_status` 里明写
  降级成 partial 会把「本轮什么都没启动/没收口」伪装成「跑坏了可续」。
- `reading`：取 `outputs.json` 的阅读里程碑（`render` 经 `state.record_output` 登记）。
- `media`：取自媒体里程碑（`download` 经 `state.record_output` 登记）。
  未选中的输出 `counts_toward_completion=false`，不参与整体完成；登记为 `complete` 但没有
  验收凭据的一律降回 `partial`（`state._milestone`；`state.record_output` 在写入时就拒收
  无凭据的 complete）。
- `final`（`state._final_entry`）：`selected / published / trusted / path / sha256 / count /
  reason`。**final 存在不等于全量完成**：仍有未验收段、或成品摘要与清单不一致（文件被改过），
  `trusted=false`。没有 `task.json` 时三类输出一律 `unavailable`，原因原文
  「没有任务快照 task.json：不猜本次选择了哪些输出，也不猜完成状态」。

**整体 `complete` 只在所有计入完成的输出都 complete 时才成立**（`state.build_status` 的
`counting` + `complete`）。`export` / `resume` 打出的就是同一份状态 JSON，外加 `written_at`
（`state._write_status_file`）、`lock`、`temp_files`；多聊天聚合成
`{kind: telegram-archive-task, status, complete, session_mutex, state_roots, tasks[]}`，
聚合口径是先 `unavailable`、再 `failed`、再「全部 complete」，否则 `partial`
（`state._aggregate_reports`）。

**进度与 ETA**：各聊天状态报告带 `progress`（`state._progress_record`）：
`completed_samples / completed_sample_seconds`（本轮实际完成的段耗时，只覆盖本轮新
完成的段，不含复用的已验收段）、`remaining`（尚待补的段数）、`eta_seconds`（样本≥2
时按样本均值×剩余段数，否则 `"unknown"`，见 `state.MIN_PROGRESS_SAMPLES_FOR_ETA`）、
`window`（`time` 窗口的过滤计数，`last` / `all` / `id` 模式为 `null`）。
`status` 读出上一次 run 写下的 progress（只读 `status.json`，不写任何文件；尚无样本
时按无样本申报，`eta` 照样标未知）。要估时间只能由执行者按已验收段另算，**无样本时
写未知**；不得用文件体积或最大消息 ID 当进度凭据（消息 ID 不连续不代表漏导，最大 ID
不等于消息数，设计方案第 110 行）。

## 8. 退出码（`archive.py` 实测口径）

`0` 所选输出全部验收；`1` partial / failed（导出路径下意味着本轮跑过子进程、成功段与诊断
都已保留；`status` 单独读出一个非 complete 状态同样是 1，即便它没启动任何子进程）；
`2` 选择或配置非法，在启动任何子进程前拒绝；`3` 能力或依赖缺口（导出参数未核对、会话锁被占、
后端起不来、锁未能释放、下载源未通过验收闸）。常量见 `archive.EXIT_COMPLETE /
EXIT_INCOMPLETE / EXIT_REJECTED / EXIT_CAPABILITY`，映射见 `archive._report_exit` 与
`archive.main`。细则：

- 不带子命令（只打帮助）与未知子命令 → 2；`--help` 自身 → 0（`archive.main`）。
- handler 抛出的 `ValueError` 归 2（选择或配置非法）、`OSError` 归 1（运行期 IO
  异常：状态文件读不出、落盘失败、`--job` 指向不存在的文件等，不是「配置非法」）；
  `state.LockBusy` 归 3；其它未预期异常归 1 并打一行说明。
  `render` / `download` / `import` / `search` / `share` 另有 `archive._guarded` 兜底，运行期异常归 1 且只打纯文本、绝不与报告 JSON 混发
  （兜底返 `None`，调用方判 `None` 后返 1）。
- `render` 的 `skipped`（纯媒体选择）也归 0（`archive._cmd_render`）。

## 9. 恢复动作清单（按现象取用）

| 现象 | 做什么 | 凭据 |
|---|---|---|
| 中断/重启后要续 | 用**原任务配置**跑 `resume --command-template <原模板>`（续跑也非得带模板：缺了整轮在取锁前就按 3 拒，**不会**只做合并发布） | 已验收段被 `state.scan_completed` 复用，不重导 |
| 段文件在、清单没登记 | 同上，取锁后自动复认领 | `state.reclaim_orphan_segments` |
| 报告说「已完成段不再合法 / 摘要不一致」 | 该段会被重新导出，其余保留；成品不发布 | `state._rejection_reason` |
| 想扩大范围或换内容 | **新建任务**，别改旧配置硬续 | `state.init_task` 三道闸 + `state.plan_task_segments` |
| 「同一账号存储已有归档任务在运行」 | 读报告里的锁文件路径，确认对方结束后再试；代码不自动破锁 | `state.LockBusy` |
| 锁文件残留但确认无人持有 | 人工核对锁内容里的 `process_pid` / `acquired_at` 后删该 `session-*.lock` | `state.acquire_lock` 写入的 holder |
| 缺 `--command-template` 就 3 | 补模板；模板只能写 `help` 里存在的旗标与 `archive.PLACEHOLDER_NAMES` 里的占位符 | `archive._template_builder` |
| 缺段但有人误以为完成 | 以 `status` 为准：`complete` 为假、`final.trusted` 为假 | `state.build_status` / `state._final_entry` |

## 10. 未验证与限制（不许当承诺）

1. **本次没有做任何真实账号验证**：真实文字导出、真实媒体下载一律 `NOT_RUN`（工单「成果可
   验证性分层」第 4 层：未执行时写 NOT_RUN）。假后端 / 假 CLI 只证明**本地调度与状态处理**，
   不证明 Telegram 在线兼容性。
2. `-T id` / `-T time` 的**端点包含性未知**（`insufficient_source`），防御靠重叠段 + peer 内
   去重；本文件不写「包含」也不写「不包含」。
3. **真实 `tdl chat export` 的输出形状未核对**：`state.validate_export` 只认「消息数组」或
   「含 `messages` 数组的映射」两种本地形状，模块 docstring 明写「不臆造 tdl 导出 schema」。
   第一次真实导出可能因形状不符而验收不过——那时按报告调整模板（`--all` /
   `--with-content` / `--raw`，`help:161-171`），**不要改判定闸门放宽凭据**。
4. **`time` 模式的日期窗口由本地二次校验兑现**：分段仍按消息 ID 轴
   `[1, 冻结上界]` 切（`state._declared_bounds` + `state.plan_segments`），但提交前按
   UTC 窗口过滤消息——起点包含、终点不包含，与 `job` 规范化的半开口径一致
   （`state._time_window` + `state._enforce_time_window`）：窗外消息剔除，缺日期的
   保留并计数；日期键与 `reading.DATE_KEYS`（`date` / `timestamp`）同口径，数值按
   UTC 秒、无时区标记的字符串按 UTC。过滤计数写入各聊天 `progress.window`。
   `last` / `all` / `id` 模式没有日期窗口，本地不设闸。模板也没有 `{start}` /
   `{end}` 占位符，日期→消息的筛选仍须由执行者在模板里用 `-T time` + `-i`
   （`help:165`、`170`，原文只说「input data, depends on export type」）或
   `-f/--filter`（`help:163`）自己传，本仓库未验证 `-i` 在 `-T time` 下的具体编码
   方式与这些取值的真实行为。
5. `--storage` 在**下载侧固定拼 `type=bolt,path=<storage>`**（`media.download_argv`；格式与
   驱动清单见 `help:45`），换 `legacy` / `file` 驱动需要改代码，不在本参考的承诺面；
   真实连接行为仍未核对。导出侧的 `--storage` / `-n` 怎么写完全由模板决定，代码不注入。
6. `--probe-session` **不联网**：本构建没有会话探测实现，`session_login` 恒 `unchecked`
   （§1）。要判断登录是否有效，只有用户自己在交互式终端里做。
7. 权限/会话失效停**整轮**：后续聊天标 `unavailable` 并点名前序身份（§6），聚合报告
   另带 `stopped_early.after/skipped` 清单，不会伪装整体完成。
8. 范围完成只表示「该 tdl 版本 + 当时账号可访问的数据导出完成」，不声称包含已删除或无权
   访问的消息；不可访问、查询失败、空聊天三者分开，不混为一谈（设计方案第 110 行）。
9. `check` 的退出码没有 3（§1）：能力缺口要读 JSON 的 `capabilities[]`。
10. 本文件不写任何本机真实路径；示例位置一律写 `<保存目录>` / `<账号存储目录>` 这类占位。
