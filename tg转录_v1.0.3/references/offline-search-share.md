# 离线整理：源适配、检索与分享

本文件按任务分段追加：Task 5 写「源适配」与共同归档 schema（本节，已冻结）；Task 6 追加
「本地检索」，Task 7 追加「分享副本」。追加时不要改写本节字段口径——Task 6/7/8 都建在它上面。

## 源适配（Task 5）

代码：`scripts/importing.py`；CLI：`archive.py import --job <任务配置.json>`。

### 边界

- **纯离线**：导入不登录、不联网、不启动任何子进程，也不执行 tdl。没有 `tdl` /
  `storage` / `namespace` 也必须完成（合同第 86 行）。测试里把
  `socket.*`、`subprocess.*`、`os.system/execv`、`urllib.request.urlopen`、
  `http.client.HTTPConnection` 全部钉死并断言零调用。
- `sources` 只能是用户**明确选定**的路径清单：不递归磁盘、不自动找文件、不接受通配。
  把**目录**或 `*.json` 这类通配串当来源传进来，一律按「来源文件不存在或不是文件」拒绝
  并单独进 `failed_sources`，该目录里哪怕放着可解析的真导出，一个都不进账（修复轮 1 的
  I-1 反例尺就钉在这里）。
- **原始文件只读**：每个来源在处理前后各取一次 SHA-256，报告里给
  `sha256_before` / `sha256_after` / `read_only_verified`；读期间变了就整源不采信。
- 导入声明固定为「**用户导入的本地快照，在线覆盖未验证**」。派生层不是在线完成凭据：
  `coverage.online_coverage` 恒为 `unverified`、`online_complete` 恒为 `false`、
  `online_completion_credential` 恒为 `null`；`render` / `download` / `export` 的完成闸
  照旧只认已验收导出的完成清单，导入文件送进去会被拒（退出码 3）。**不得**据此跳过
  后续在线导出。

### 结构依据（Desktop 侧只认公开文档列出的键）

- Telegram Desktop：`https://core.telegram.org/import-export`（Telegram Data Export
  Schema）。本节抄录的原文与该页一致：聊天对象列 `id` / `name` / `type` / `messages`；
  消息对象列 `id`、`type`（"The type is message for regular messages and service for
  service messages"）、`action`、`date`（"Message date as an ISO 8601 timestamp"）、
  `date_unixtime`（"Message date as a unix timestamp"）、`from` / `from_id`、
  `reply_to_message_id`、`forwarded_from`、`text` / `text_entities`、`photo`、`file`、
  `thumbnail`、`width`、`height`、`media_type`、`mime_type`、`duration_seconds`、`title`
  等；`text` 的规则是「没有实体时是字符串，否则是**按出现顺序**排列的 MessageEntity
  数组，`plain` 实体被摊平成普通字符串」。
- 因此：UTC 秒**只**取 `date_unixtime`；那条 `date` ISO 串官方未标注是本地时间还是 UTC，
  只原样存进 `display_time` 当展示串。
- 文档沉默的字段一律申报缺口、绝不使用也绝不丢弃：实测夹具里的 `caption`、`file_name`
  就落在 `gaps.unknown_fields`（`documented: false`）。文档列出但本模块不建立映射的键
  （服务消息类，如 `inviter`、`game_title`、`poll`，及 2026-10 复核补录的
  `live_location_period_seconds`、`gift_code`、`boost_peer_id`、`unclaimed`、
  `via_giveaway`、`giveaway_information`）同样进 `unknown_fields`，标注
  `documented: true`，供人复核——两类缺口在报告里必须分得开。
- 实体 `type` 取值集合也取自该页；不在表内的（夹具里的 `mystery_entity`）进
  `gaps.unknown_entities`，但它的文本节点**仍按原顺序并入正文**，不丢字。
- `text`（官方 schema 的权威纯串）与 `text_entities`（实体数组）**同时存在、拼出的正文又
  不一致**时**不整体替换**：拼接结果更长就用拼接，等长或更短都以权威纯串为准，即保住
  能保全内容的那一份；同时把差异作为缺口计入 `gaps.text_mismatch[]`（`message_id`、
  `identity_key`、两侧长度、两侧 SHA-256 前 16 位、`kept`、`note`，不落敏感原文）与
  `gaps.counts.text_mismatch`，并汇总进报告逐聊天条目 `text_mismatch` 与批次台账
  `text_mismatch_count`。未认得的实体节点形状是真实存在的（上一条），旧实现整体替换会
  吃掉纯串里的字且缺口计数不动——那是本条要绝掉的「静默丢正文」（修复轮 1 的 I-2）。
- Desktop `media_type` 的**取值**集合该页未列出，所以附件类别不按它猜：只认 `photo`
  字段（照片路径 → `image`）与 `mime_type` 的顶级类型（IANA：`image/*`→image、
  `video/*`→video、`audio/*`→audio、`application/*`→document），判不出来就 `unknown`；
  `media_type` 原样存进 `declared_media_type`。
- tdl 侧**没有**可指认的公开 JSON 字段表（Task 3 报告已把 `--raw` 的字段拼写记为
  `insufficient_source`）。所以 `tdl_json` 适配器只吃仓库里既有代码点名过的键：
  `state.MESSAGE_ID_KEYS`(`id`/`message_id`)、`state.DECLARED_CHAT_KEYS`
  (`chat_id`/`dialog_id`)、`reading.DATE_KEYS`(`date`/`timestamp`，**仅数值**按 UTC 秒)、
  `reading.TEXT_KEYS`(`text`/`message`)、`reading.SENDER_KEYS`、`reading.FILE_KEYS`、
  `media.CLASSIFICATION_INPUT_FIELDS`（附件节点，类别调 `media.classify`）；回复目标只认
  `reply_to_message_id` 这一种拼写。其它键（含 `mime_type`、`reply_to_id` 这类没核对过的
  写法）全部进 `gaps.unknown_fields` 让人看见，不静默丢、也不猜。

### 身份、时间与缺失标记

- 归档身份 = **peer 类型 + chat ID + 消息 ID**：`identity` 是 `"<peer_type>:<chat_id>"`
  （与 `job.chat_identity` 同口径），每条消息再带 `identity_key`
  = `"<peer_type>:<chat_id>#<message_id>"`。
- `peer_type` 与 `chat_id` 只能来自来源声明（Desktop 的 `chats[].id`、tdl 的
  `chat_id`/`dialog_id`）或用户给的 `metadata`；来源与 metadata 同时存在且不一致就拒绝
  （报「不符」）。`metadata.peer_type` 的合法取值仍限 `user`/`chat`/`channel`，不做别名
  猜测。**绝不**从聊天标题、文件名或正文猜身份：`identity_basis` 逐项记下出处，并恒定
  `guessed_from_name_or_title: false`；`display_name` 永不参与身份。
- 时间：`date_utc` 是 UTC 秒（整数），`time.display_timezone` 是用户给的展示时区标签
  （没给就是字符串 `"unknown"`，不默认北京时间）。Desktop 记
  `time.utc_field = "date_unixtime"`，tdl 记 `"date"`。
- 阅读版只接受可核对的 `UTC` / `±HH:MM`（Task 4 同一口径，不猜 tzdata）：用户给了
  `Asia/Shanghai` 这类命名时区时，归档里的 `display_timezone` 原样保留，阅读版退回 UTC
  渲染并在报告 `import_notes` 里申报。
- 正文、附件描述、发送者、回复目标、原始来源分别存储；源里没有的就显式标
  `text_missing` / `sender.missing` / `reply_to.missing` / `attachments_missing` /
  `time_missing`，汇总在 `gaps.missing_fields` 与 `gaps.counts`，**不编造**。
- 未知 schema → 拒绝并申报（错误消息里带上实际看到的顶层键），不猜、不默认为空聊天：
  `{"chats": []}` 这种「结构认识但一个聊天也没有」同样拒绝。
- 坏 JSON / 读不动的源：单独进 `failed_sources`（附原因），其余源照常入库，但整体报告
  `status` 至少降到 `partial`（CLI 退出码 1），失败源路径与原因都必须出现在报告里。

### CLI

```
PYTHONUTF8=1 python scripts/archive.py import --job job.json
```

`job.json` 最小形状（离线动作，不需要 tdl/会话）：

```json
{"action": "import", "source_format": "desktop_json",
 "sources": ["F:/某用户选定的目录/result.json"],
 "metadata": {"peer_type": "channel", "timezone": "+08:00"},
 "destination": "F:/备份/归档",
 "contents": ["text"], "outputs": ["markdown"], "cleaning": "none"}
```

- `source_format` 只接受 `tdl_json` / `desktop_json`。
- `metadata.chat_id` 是「一个文件里点名要哪个聊天」的选择器；不给就按文件声明的 chat ID
  入库（多聊天文件用 `iter_normalized_sources` 逐聊天处理，一次只装一个聊天）。
- 不选 `contents`/`outputs` 时只写派生归档与文本索引；选了 `text` + `markdown`/`json`
  才复用 Task 4 的 `reading.build_reading_pack` 出阅读版（不登录、不拉历史）。
- 选了任何附件类别时，另外产出一份 `media.SELECTION_KIND` 的**下载选择清单**
  （`actions.download_selection_path`），交给独立的在线 `download` 动作消费；
  导入本身一个字节都不下载。
- 退出码沿用既有口径：0 全部来源成功、1 部分或全部失败、2 配置非法（含越过用户冻结的
  范围）、3 能力缺口。

### 冻结的归档 schema（v1，供 Task 6/7/8 消费）

`normalize_source(path, source_format, metadata)` 返回一份可 JSON 序列化的归档 dict：

| 字段 | 含义 |
| --- | --- |
| `kind` / `version` | `"telegram-archive"` / `1`（schema 版本，Task 8 比对用） |
| `identity` / `peer_type` / `chat_id` | 归档身份（peer 类型 + chat ID） |
| `chat` | `peer_type`、`id`、`display_name`、`display_name_basis`(`metadata`/`source`/`missing`)、`locator`（导入源给不出 tdl 定位值时为 `null`） |
| `identity_basis` | `peer_type` / `chat_id` 的出处 + `guessed_from_name_or_title: false` + `display_name_never_used_for_identity` |
| `source` | `format`、`adapter`、`path`、`name`、`sha256_before`、`sha256_after`、`read_only_verified`、`batch_id`、`read_at`、`size` |
| `time` | `utc_field`、`source_time_basis`、`display_timezone` |
| `coverage` | `message_count`、`id_lower`、`id_upper`、`date_lower`、`date_upper`、`online_coverage`、`online_complete`、`online_completion_credential`、`cannot_replace_online_export`、`claim` |
| `messages[]` | `message_id`、`identity_key`、`date_utc`、`date_utc_basis`、`display_time`、`time_missing`、`text`、`text_missing`、`sender{name,id,missing,basis}`、`reply_to{message_id,missing}`、`forwarded_from{name,missing}`、`attachments[]`、`attachments_missing`、`service`、`action`、`gaps[]`、`source_ref{batch_id,path,index,format}` |
| `attachments[]` | `category`（`media.CATEGORIES` + `unknown`）、`category_basis`、`path`、`mime_type`、`declared_media_type`、`width`、`height`、`duration_seconds`、`thumbnail`、`title`、`node`（tdl 侧原样保留的分类输入节点，Desktop 侧为 `null`）、`source_field` |
| `gaps` | `unknown_fields[]`（`field`/`scope`/`source_format`/`documented`/`message_ids`/`note`）、`unknown_entities[]`、`missing_fields[]`、`text_mismatch[]`（`message_id`/`identity_key`/`text_length`/`text_entities_length`/`text_sha256`/`text_entities_sha256`/`kept`/`note`）、`counts{sender,time,text,reply_to,attachments,text_mismatch}`、`notes[]` |
| `claims` | `local_snapshot_only: true`、`online_full_export_verified: false`、`online_coverage: "unverified"`、`claim`、`read_only_source`、`network_used: false`、`login_used: false`、`media_downloaded: false` |
| `limitations` | 与本文件同口径的 7 条限制申报 |

派生存储（全部可重建，落在 `<destination>/derived/`）：

- `<identity-slug>-archive-<batch_id>.json`：上面那份归档 dict。
- `<identity-slug>-index-<batch_id>.json`：文本索引，形状与导出成品一致
  （`{"identity","peer_type","chat_id","range","snapshot","messages":[{"id","date","text",…}]}`，
  `date` 就是 `date_utc` 秒），`state.validate_export` 认得这个形状，Task 8 增量与
  Task 6 检索都从这里读。它**不是**在线完成凭据。
- `imported.json`：来源批次清单（`kind: "telegram-archive-import-batches"`、
  `version: 1`、`claim`、`processing_version`、`batches[]`）。
- `<identity>-download-selection.json`：仅当用户选了附件类别时产出。

`source_batches`（报告里与 `imported.json.batches[]` 同一形状）就是 Task 8 的增量对账单位，
每条含：`batch_id`（= `sha256(来源摘要|身份|schema 版本)` 前 16 位，同文件同聊天复导入
只替换不追加）、`identity`、`chat_id`、`peer_type`、`source_path`、`source_name`、
`source_format`、`source_sha256`、`source_sha256_after`、`read_only_verified`、
`processing_version`、`message_count`、`id_lower`、`id_upper`、`date_lower`、`date_upper`、
`display_timezone`、`utc_field`、`online_coverage`、`online_complete`、`claim`、
`missing_fields`、`unknown_field_count`、`unknown_entity_count`、`text_mismatch_count`、
`archive_path`、`text_index_path`、`online_completion`、`reading_built`、`imported_at`。

`import_sources(..., progress=...)` 的事件序列就是内存边界的可观测证据：
`source_started → (chat_normalized → chat_written) × N → source_finished`——上一个聊天
落盘之后才开始归一化下一个聊天，绝不把整批聊天同时攒在内存里最后才写
（标准库没有流式 JSON，所以边界是「一次一个聊天」，见 `limitations.stdlib_single_file`）。

### 本地导入 API（Task 6/7/8 直接用）

```python
importing.normalize_source(path, source_format, metadata) -> dict   # 单聊天归档（合同签名）
importing.iter_normalized_sources(path, source_format, metadata, select_chat=None)  # 逐聊天
importing.import_sources(sources, metadata, source_format=..., derived_dir=...,
                         batches_path=..., progress=..., per_chat=...) -> dict
importing.run_import(job) -> dict            # CLI import 的后端
importing.build_reading(archive, target, job) -> dict   # 复用 Task 4 阅读接口
importing.ARCHIVE_VERSION / BATCH_VERSION / ONLINE_COVERAGE / LOCAL_SNAPSHOT_CLAIM
# 归档锁（Task 5 起用，Task 7/8 复用同一份实现，不要再内联三步）：
state.acquire_archive_lock(root, *, resource=None, marker=None, owner=None) -> dict
state.release_archive_lock(handle) -> dict
state.archive_lock_marker(resource) -> str      # 与 catalog.archive_lock_marker 同一算法
```

### 来源批次台账的互斥（修复轮 1 的 I-3，Task 8 直接复用）

- `import_sources` 给了 `batches_path` 时，`imported.json` 的**整读 → 内存合并 → 整写**
  全程持**归档锁**：`state.acquire_archive_lock(状态根, resource=状态根)`——锁根与被锁资源
  都是台账所在的派生层目录 `<destination>/derived`（修复轮 2 的 N-2 同源口径，不再是
  「各锁自己那个文件」）→ 合并 → 由 `state.publish_text_files` **原子发布** →
  `state.release_archive_lock`。锁文件名用
  Task 2 预留的 `archive-*` 前缀；实现口径与会话锁同一份（`O_CREAT|O_EXCL` 原子占锁、
  不抢占、不自动破锁、`LockBusy` 随异常带出锁路径）。
- 加锁顺序固定「**先归档锁、后会话锁**」（`state.LOCK_ORDER = ("archive", "session")`，
  Task 2 已确立）。`import` 是离线动作：只持归档锁，不取会话锁，没有
  `storage`/`namespace` 也照样取得（归档锁按归档资源，不按账号存储）。
- 锁被占用：`state.LockBusy` 原样抛出 → CLI 按能力缺口**退出码 3** 并点名锁文件路径，
  **不静默跳过**、不覆盖对方正在写的台账；`-archive-*.json` 也不会被铺进派生目录。
- 并发两个 import 争同一份台账时，两份 `source_batches` 都在。用例
  `test_concurrent_imports_keep_both_batch_records` 把「并发」写成**代码自己撑开的确定窗口**：
  在真 import 的 `_load_batches` 缝隙里让另一方真跑一次导入，它必须被 `state.LockBusy` 挡下、
  既不写台账也不铺半成品，窗口放开后才完成。判据一次都不读墙钟（旧写法用栅栏超时 + 定时重试
  去赌撞锁，满载下期限先到期就翻红——按总控裁决那种尺不可信）；把取锁整段去掉的变异版与把锁
  资源改回台账文件的变异版下这条都变红。报告 `lock` 块记 `kind`/`marker`/`root`/
  `order`/`path`，没给 `batches_path`（不写台账）时为 `null`。
- 边界要说清（修复轮 2 已闭环的部分）：曾经 import 按台账路径取标记、`search` 按索引库路径
  取标记，锁分别落在 `<destination>/derived/locks/` 与 `<destination>/locks/`，两个锁名天然
  不同 → 「import 与 search 互斥」只剩个名字。现在**两侧都以同一个状态根
  （`<destination>/derived`）作为锁根与被锁资源**，锁文件完整路径逐字相同
  （`archive-<marker(状态根)>.lock`），细节见下面「本地检索」一节的归档锁口径与用例
  `test_import_and_index_resolve_the_same_archive_lock_file`。没配 `destination` 时索引侧
  退化成「索引库所在目录」，只与同样传该目录的一方互斥，`lock.root` 照实写。Task 8 的
  `update` 只要按同一个状态根取归档锁，就同时压住索引与台账（两侧算出的锁名已经一致），
  并继续遵守「先归档后会话」。

## 本地检索（Task 6 追加）

### 索引/查询（scripts/catalog.py）

只用 Python 标准库 `sqlite3` 建**可重建的派生索引**：原始归档与阅读版永远是底档，索引库
随时可以删掉重建；换索引库实例不影响归档可读。检索是**离线动作**，没有 tdl、没有登录、
没有 storage/namespace 也能用；不联网、不上传任何内容、不装中文分词包。

#### 怎么用

```bash
python scripts/archive.py search --job 检索配置.json --database 你选定的归档目录/catalog.db
#   只查阅读版时再加：--view reading --reading-pack 你选定的归档目录/阅读版/channel-1001
```

检索配置（`action` 必须是 `search`）：

| 字段 | 含义 |
| --- | --- |
| `sources` | **本次要建索引的归档清单**（用户明确选定的派生归档 JSON 绝对路径）。索引范围由用户选择：不递归磁盘、不自动全建，空清单直接拒绝 |
| `database` | 索引库路径（也可用 `--database` 给）；必须是非空绝对路径，不注入默认位置 |
| `query.view` | **必须显式写明** `raw` 或 `reading`，不默认、不猜 |
| `query.keyword` | 关键词，按字面匹配 |
| `query.chats` | 聊天过滤：`"peer_type:chat_id"` 字符串或 `{peer_type, chat_id}`；**不按显示名筛**（同名聊天会互串） |
| `query.since` / `query.until` | UTC 秒或带时区 ISO 日期；**起点含、终点不含**；时间缺失的消息不参与日期过滤 |
| `query.id_lower` / `query.id_upper` / `query.message_ids` | 消息 ID 过滤，区间**两端都含** |
| `query.order` / `query.limit` / `query.cursor` | `asc`（默认）/`desc`；每页 1–200 条（默认 20）；下一页把上一次返回的 `page.next_cursor` 传回来 |

#### 检索不再每次全量重索引（修复轮 1 的 I-1）

`search` 的检索路径**默认只读已提交视图**：不重写行、不取归档锁。只有在下列情况才重建
（并且只有重建才取归档锁、走事务发布）：

1. 本次归档算出的内容摘要与台账里的 `source_batches.content_sha256` 不一致——摘要覆盖的是
   **会写进视图的成员集合本身**（成员身份、正文散列、附件、阅读入口、聊天登记、批次登记值），
   所以正文/附件/阅读入口/聊天登记任一内容变化、该聊天当前的行其实来自另一批（把旧批次再灌
   一次也在这里被抓出来）、**同一批次里的等量成员替换**（`batch_id` 与条数都不变、只是把某条
   私密成员换成另一条——评审 N-1，收紧绝不被短路吃掉）、以及**选定的聊天范围与已提交清单
   不同**（新增或减少聊天、换视图）都算内容变了；
2. 索引库不存在或还没有内容；`schema_version` 与当前实现不符时**自动从原档整批重建**并保持
   查询可用（修复轮 1 申报的「版本不符就拒绝读写」偏差已按总控裁决改掉：索引是可重建派生层，
   报告里 `schema.rebuilt_from` 记下它是从哪个版本重建的，不必再人手删库）。只有**手上没有
   原档**的只读路径（`search_archive` / `database_status`）与结构损坏（缺表）才退回拒绝并
   如实申报原因；
3. 调用方显式要求重建：API 参数 `catalog.index_archive(database, archives, view,
   rebuild=True, lock_root=None)`，或 CLI `search --rebuild`（`archive._cmd_search`
   透传，收尾轮已接线；重建理由记进 `reindex_reason`）。

判定结果一律可读，不靠猜：索引报告里带 `reindexed`（true/false）与 `reindex_reason`
（为什么重建、或为什么没重建），`database_status()` 的每个视图 `coverage` 都带该批次的
`content_sha256`。本机 10 万条合成归档实测：全量重建 5.8–7.3 秒，同范围第二次调用（短路）
2.0 秒且零写入，纯检索 0.12 秒——短路的成本主要是「把本次归档读一遍算摘要」，这正是
判定所必需的。真正的增量合并（只搬新增的那几条）仍归 Task 8。

覆盖用例：`test_unchanged_scope_second_index_writes_no_rows_and_needs_no_archive_lock`
（第二次零重写 + 别人持锁也照样能查 + 真要重建时会被同一把锁挡下）、
`test_content_digest_change_forces_a_rebuild`、`test_scope_and_view_changes_force_a_rebuild`、
`test_republishing_an_old_batch_is_not_short_circuited`、
`test_reading_membership_tightening_is_not_short_circuited_away`（阅读版收紧绝不许被短路吃掉）、
`test_equal_size_membership_swap_in_one_batch_still_rebuilds`（评审 N-1 决定性用例：同
`batch_id`、同条数、把某条私密成员换成另一条 → reading 视图必须换人；删掉「过滤后重算摘要」
的变异版上这条必须变红）、
`test_stale_schema_version_rebuilds_from_the_archives`、
`test_stale_schema_without_any_archive_is_a_declined_read_with_a_reason`、
`test_stale_schema_with_unverifiable_archives_still_refuses`（schema 版本处置三条）、
`test_explicit_rebuild_rewrites_rows_even_when_digests_match`、
`test_cli_second_search_short_circuits_the_reindex`。

#### 视图区别（用户可见承诺）

- `raw`＝原始正文：包含阅读版**过滤掉**的内容（广告、模板压缩掉的重复长文等）。
- `reading`＝**成员取自阅读版、正文取自原档**（评审 I-5 的措辞纠正：不写成「阅读版正文」，
  那比实现强）。视图里**有哪些消息**严格取自阅读版分篇里真实出现的消息 ID：被阅读版过滤掉的
  **内容**不会成为这个视图的成员，查 `reading` 时**绝不返回**它们，也**不在片段里泄漏**它们的
  一个字。**后果要说清**：成员行的正文取自原始归档那一条，因此换行、模板压缩等**排版细节以
  原档为准**，可能与阅读版分篇里的呈现不同——本视图保证的是「能读到什么以阅读版为准」，
  **不宣称等同阅读版逐字排版**（代码侧同一份申报见索引报告的 `limitations.reading_membership`）。
- 两个视图同库不同表主键（`view` 是主键的一部分），互不串；查哪一个由 `query.view` 决定。
- 每条结果都带 `entries`：`archive` / `text_index` / `source` 是归档定位，`reading` 是阅读版
  分篇位置。**没有阅读入口时 `reading` 返回 `null`**，只给归档定位——不生成虚构链接，
  阅读入口只登记磁盘上真实存在、且确实含该消息的分篇文件。入口查询带视图谓词
  （只取 `view='reading'` 登记的那批路径），别的视图塞进来的同键路径不会串进来；
  `raw` 结果指向真实存在的阅读入口是**定位特性**，入口本身不含正文，不算内容泄漏
  （用例 `test_reading_entry_lookup_only_uses_reading_view_paths`）。
- 想搜私密原文就用 `raw`，只想看能对外阅读的内容就用 `reading`；两者都要**建过一次**索引
  （`search` 先按 `sources` 选定的范围核对内容摘要：已经建过且没变就只读已提交视图，
  不再重写；变了或没建过才建）。

#### 关键词怎么被处理（不会把通配符当代码）

- 匹配走 `instr()` 的**参数化比较**：`%`、`_`、单引号 `'`、反斜杠 `\`、中文一律当普通字符，
  因此「查 `%`」只命中正文里真有 `%` 的消息，不会命中全部内容；正文里长得像 SQL 的串
  （例如 `'; DROP TABLE messages; --`）也只被当文本比较，**不会被执行**。
- 大小写只对 ASCII 折叠（查 `q3` 命中 `Q3`），报告里如实写 `case_folding="ascii-only"`；
  要严格区分就传 `query.case_sensitive=true`。
- **未启用 FTS、未装中文分词包、未接任何外部搜索服务**：报告里
  `fts_enabled=false`、`chinese_tokenizer_installed=false`、`external_search_service=false`，
  不宣称全文分词能力。中文靠字面子串匹配，正确性优先。
- 片段长度上限 160 字符（命中位置优先起算），并给出 `text_length` 与 `snippet_truncated`。

#### 分页与身份

排序键固定为 `(date_sort, identity, message_id)`：`date_sort` 是 UTC 秒，时间缺失的取哨兵
`-1`（升序时排在最前）。同秒、同 ID 也靠身份与 ID 定序，因此跨页**不重复、不遗漏**；
游标里带查询条件的指纹，换了关键词/聊天/日期/排序/页长都会拒绝复用旧游标（避免串页）。
身份口径与 Task 5 一致：`identity = <peer_type>:<chat_id>`、`identity_key =
<peer_type>:<chat_id>#<message_id>`。两个同名聊天持有相同 message ID 时，检索与结果归属
各自独立，绝不互串。

#### 索引库结构（schema 版本 2）

| 表 | 装什么 |
| --- | --- |
| `meta` | `schema_version`（当前 2；落后/被篡改的版本由 `ensure_schema` **自动整批重建**——派生层不拒绝读写，只有缺表这类结构损坏才拒绝、不做半修）、`archive_version`（归档 schema v1）、`claim`、后端与匹配口径、`updated_at` |
| `chats` | 聊天身份：`identity` 主键、`peer_type`、`chat_id`、显示名与其出处、`identity_basis`、定位值、来源格式与路径 |
| `messages` | **当前视图**：主键 `(view, identity, message_id)`，正文、`date_utc`/`date_sort`/`time_missing`、发送者、回复、服务消息、`identity_key`、`text_sha256`、所属批次与归档路径 |
| `revisions` | **历史版本**：当前视图内容被不同内容取代（`content_changed`）或从批次里消失（`removed_from_batch`）时追加一行，带旧正文、`previous_batch_id` 与 `new_batch_id`。`new_batch_id` 只对 `content_changed` 记「实际取代它的那一批」；消息从批次里消失时**没有**取代它的那一批，该列记 `NULL`。「谁在什么时候把这条删掉了」的完整删除证据台账属 **Task 8**，本轮只保证收紧/删除路径不崩、台账可读 |
| `attachments` | 附件映射：`(view, identity, message_id, position)` 唯一，类别/路径/MIME/尺寸/时长 |
| `source_batches` | **来源/覆盖清单**：`batch_id`、来源绝对路径与 SHA-256、`processing_version`、消息数、ID/日期上下界、展示时区、`online_coverage`（恒 `unverified`）、`online_complete`（恒 0）、`claim`、归档与文本索引路径、本次索引内容摘要 |
| `reading_paths` | 可选阅读入口：`(view, identity, message_id, path)`，路径来自阅读版分篇的真实位置。查询**带 `view` 谓词**（只认 `view='reading'` 登记的那批行，别的视图塞进来的同键路径不会串进结果）；`raw` 的结果同样允许指向这条真实阅读入口——那是定位特性，入口本身不含正文 |
| `stage_*` | 与正式表同构的缓冲表，事务内使用，发布即清空 |

身份不足（缺 `identity`/`peer_type`+`chat_id`/`batch_id`、`identity_key` 与身份不符、归档
`version` 不认识）一律**拒绝并提示补 metadata**，不把文件名或显示名猜成账号/聊天身份。

#### 事务、可见性与损坏处理

- 写入：`BEGIN IMMEDIATE` → 写 `stage_*` → 一次事务内整批替换该视图 → `COMMIT`。
  任何一步失败都 `ROLLBACK`：**旧的已提交视图继续可查**，不出现「更新到一半」的数据。
- **视图收紧（消息从批次里消失）不会让检索失效**：同聊天少一条、整聊天清空、阅读版保留
  清单变严这三种形状都在同一事务里同步收紧当前视图——被移除的正文从视图里消失（查不到、
  片段里也不留），`revisions` 逐条追加一行且 `new_batch_id` 为 `NULL`，`search` 仍返回
  退出码 0。覆盖用例：`test_removed_message_records_a_null_revision_instead_of_crashing`、
  `test_emptied_chat_is_tightened_without_index_error`、
  `test_reading_view_tightening_drops_the_removed_body_from_the_view`、
  `test_cli_search_survives_a_shrunk_archive`。「谁在何时按什么水位删除」的完整删除证据
  台账属 Task 8，本层只保证收紧可用 + 台账可读。
- 读取：检索用独立连接，只读**已提交**视图；库处于 WAL 模式时读者不阻塞写者，反之亦然。
- `schema_version` 与实现不符：**带原档就自动重建**（`index_archive` → `ensure_schema`
  丢弃旧版本派生表、按本次选定范围重新发布，原档只读不动）。原档缺失或不可验证时不猜，
  报 `CatalogError` / `ValueError` 并申报「按选定范围从原始归档重建」；CLI 按**能力缺口退出码 3**。
- 结构被破坏（缺表、没有版本记录）：不半修、不猜，报 `CatalogError` 并申报「删掉索引库后
  从原始归档重建」；CLI 按**能力缺口退出码 3**。
- 索引是派生层：删库 → 重建 → 结果与删前一致；原始归档文件全程只读（摘要不变）。

#### 互斥与退出码

`search`（含其索引写入）、`import`、`share`、`update` 的组合按**归档锁**互斥，锁文件名
前缀 `archive-`（`session-*` 是会话锁，两者不同名、不互相顶替）；在线动作另持会话锁。
加锁顺序固定「先归档锁、后会话锁」，释放顺序相反。锁被占用时报退出码 3 并说明是谁的锁，
不伪装已完成。

**归档锁的口径只有一份实现，而且锁根与锁资源同源**（修复轮 1 的 I-4 + 修复轮 2 的 N-2）：
marker 与取放都在 `state.py` 里（`state.archive_lock_marker` / `state.acquire_archive_lock` /
`state.release_archive_lock`），`catalog` 不自带第二份 marker。但只统一 marker 算法还不够：
曾经 import 锁 `imported.json`、索引侧锁 `catalog.db`，**锁根与锁资源不同源**，两个锁名天然
不同（实测 `53f561d0…` vs `807c0403…`），import 与 search/rebuild 之间其实不互斥。
现在的口径：**两侧都以同一个状态根（派生层目录 `<destination>/derived`）作为锁根，也把它
作为被锁的资源**，锁文件完整路径逐字相同——`archive-<marker(状态根)>.lock` 落在
`<状态根>/locks/` 下。锁根仍由配置里的 `destination` 与既有的派生层目录常量推出，不新造
第三套路径规则；`lock_root` 那个配置字段是**会话锁**的执行者参数，与归档锁不是一回事，
两者不混用。没配 `destination` 时索引侧退化成「索引库所在目录」，只与同样传该目录的一方
互斥，报告里 `lock.root` 照实写。
用例把这件事钉在**行为**上，不是钉在「调了同一个函数」上：
`test_import_and_index_resolve_the_same_archive_lock_file`（真实 import 与真实 index/rebuild
各自取锁时记录到的锁文件完整路径逐字相同）、
`test_a_real_import_locks_out_an_index_rebuild`（在真 import 的真持锁窗口里发起 index/rebuild
→ `LockBusy`，撞的就是 import 实际持有的那个锁文件；放锁后同一动作必须成功）、
`test_an_index_rebuild_locks_out_a_real_import`（反向：索引侧先持锁 → 真 import 被挡在门外）、
`test_cli_search_is_locked_out_by_the_archive_lock_only`（CLI 口径：别的进程持归档锁且本次要
重建 → 退出码 3）、`test_the_archive_lock_protects_the_state_root_not_the_ledger_file`
（import 侧锁根＝被锁资源＝状态根，且**不等于**锁台账文件本身）、
`test_catalog_reuses_the_state_archive_lock_helpers`、
`test_archive_lock_mutex_is_real_in_both_directions`。把两侧锁资源改回各自那个文件的变异版
（MUT-N2）下这 5 条全部变红。调用方已经持有同一把
归档锁时（同进程重入，例如 Task 8 的 update），索引写入沿用它的锁、不重复占用、也不另起
一把；**别的进程**持有时一律抛 `LockBusy`，不静默跳过、不自动破锁。

**只有需要重建时才取归档锁**（修复轮 2 的 CLI 取锁裁决）：`catalog` 这一层的只读检索与摘要
短路都不取锁（`search_archive` 全程只用读连接），CLI 的 `search` 组合层也不再在检索前无条件
独占归档锁——取锁动作由 `catalog.index_archive` 在真的要重建时完成。所以「两个终端各查各的」
在 CLI 上不再把后到者顶成退出码 3：同范围第二次检索不取锁、零重写，只有范围/成员/版本变化
要重建时才会撞锁（用例 `test_cli_search_takes_no_archive_lock_when_nothing_to_rebuild`，
索引报告与 CLI 报告都用 `lock.taken` 交代这一次到底取没取锁）。

其余口径：0 检索成功（命中 0 条也算成功）、1 运行期失败、2 选择/配置/查询非法
（在碰任何文件之前拒绝，不会留下半截索引库）。

#### 供 Task 8 增量使用（本任务只提供接口，不含增量判断）

```python
catalog.connect(database)                      # 连接（WAL、显式事务）
catalog.begin(conn)                            # BEGIN IMMEDIATE
catalog.stage_archive(conn, view, archive)     # 一份归档写进 staging（校验在此，不动当前视图）
catalog.publish_staged(conn, view)             # 同一事务内换成当前视图；内容变了才追加 revisions
catalog.commit(conn) / catalog.abort(conn)     # 提交切换 / 回滚维持旧视图
catalog.index_archive(database, archives, view, rebuild=False, lock_root=None)
                                                 # 合同入口：先比内容摘要，一致就只读短路；
                                                 # 要重建才取归档锁并一次事务发布
catalog.search_archive(database, query)           # 合同入口（只读已提交视图，不建、不取锁）
catalog.database_status(database)              # 版本、各视图消息数、批次数、修订数、日志模式、
                                                 # 每批 content_sha256（短路的可读凭据）
catalog.list_revisions(database, identity=…, message_id=…, view=…)  # 回溯历史版本
catalog.archive_lock_marker(database)          # 就是 state.archive_lock_marker 本身（同一函数
                                                 # 对象，不再自带第二份口径）
catalog.acquire_index_lock(lock_root, database) # 复用 state.acquire_archive_lock；调用方
catalog.release_index_lock(handle)              # 已持有时沿用同一把，不重复占用
```

「更新失败不切换当前版本」的机制即 `stage → publish → commit` 三段：只 `stage` 不
`publish`，或 `publish` 后不 `commit`（异常时 `abort`），检索侧看到的仍是旧的已提交视图；
水位推进交给 Task 8 用 `source_batches`（`batch_id` + 来源 SHA-256 + 上下界）对账。

#### 申报边界

以上全部基于**合成数据 + 临时目录**：索引与检索可用，不代表真实在线导出已验证；
索引覆盖范围永远是「本地已导入的快照」，`online_coverage` 恒为 `unverified`，
不能据此跳过后续在线导出。

## 分享副本（Task 7 追加）

<!-- 本节由 Task 7 追加：分享副本另建独立目标，不指向 raw 派生层，也不指向私有原档。 -->

分享是**显式模式**：源数据、输出位置、脱敏字段、是否带附件都要由用户给全，缺任何一项一律按
配置非法拒绝（退出码 2），不注入默认值、不猜。「以为脱敏了其实没有」比少一个功能更危险，所以
选项是**严格白名单**——打错字的键（如 `fieldz`、`telephon`）直接报错，绝不静默忽略。分享只读
归档、另建独立目录：原始归档与本地私有阅读版一个字节都不动（`unchanged.*` 前后 SHA-256 比对为证）。

### 怎么用

```bash
python scripts/archive.py share --job 分享配置.json
```

分享配置（`action` 必须是 `share`，且 `sources` 只放**一份**用户选定的派生归档 JSON 绝对路径）：

| 字段 | 含义 |
| --- | --- |
| `destination` | 私有归档位置：化名映射表与全量私有报告只落在这里，**不进分享包** |
| `share.target` | 分享输出目录（绝对路径）；与 `destination`/阅读版/所选附件目录**两个方向**都不得相等或互相包含（按解析后的目录形态判定，含 junction/符号链接、同盘同父、尾随点与空格），否则把私有层和分享包混在一起，在任何文件写出之前直接拒绝（退出码 2） |
| `share.fields` | 要脱敏的字段子集：`phone`/`username`/`sender_name`/`chat_name`/`forwarded_name`/`other`；可给空列表（=一项都不脱敏，只剩下面的不变量） |
| `share.values` | 用户自填的其它敏感值（如 `{"other":["雷霆计划"]}`），必须是**非空字符串列表**，数字/None 一律拒绝 |
| `share.with_attachments` | 是否带附件：必须显式布尔，不默认带也不默认不带 |
| `share.attachment_root` | 带附件时用户选定的附件目录绝对路径；带附件还必须逐项给 `attachments`（`{message_id,index}`，不接受整目录开关） |
| `share.text_view` | `raw`（原始正文，须申报）或 `reading`（只复用显式清洗后的阅读正文，给 `reading` 就必须给 `cleaning`） |
| `proxy` | 运行期可选；分享包与报告里只保留去掉认证信息的形态（`socks5://host:port`），`username:password` 绝不写出 |

### 文字与文件名脱敏的覆盖面

- 选定字段的值既替换**元数据字段**（发送者显示名、会话名、转发者名），也替换**正文里出现的同一
  些值**（含大小写变体、HTML 转义后的形态）。
- **同一份副本里同一个人化名一致**：以发送者 ID 为锚（缺 ID 才用规范化名字），消息标题行写成
  `## 消息 <ID> · <化名> · <时间>`；同一发送者得到同一个「参与者-NN」，不同发送者不同。会话名走
  「会话-NN」，转发者走「转发者-NN」。
- **化名映射表只在私有 `destination`**（`share-alias-<分享指纹>.json`），绝不进分享目录；分享目录里
  既没有映射表，也没有任何文件名含 `alias`/`mapping`。拿到分享包的人无法据此还原真名。
- **不变量（与 `fields` 是否选中无关）**：分享目录里绝不会出现——会话/登录文件、代理凭据
  （含 `--proxy` 里的 `username:password`）、完整原始 JSON（`source_ref`/`attachments_missing`/
  `messageMediaPhoto` 这类原始节点键都不写出）、私有来源绝对路径、未经选择的附件。正文里出现的绝对
  路径形态（盘符/UNC/根路径）一律隐去并计入 `path_scrubbed`；**含空格的路径整条一起隐**（中间段
  允许空格、末段止于空格），同一句里路径之后的文字照原样保留。
- **被用户逐项选中的附件也要过身份判定**：文件名可以改，所以判定同时看**路径形态**（`.session`、
  `.dtmp`、`.env`、密钥/口令库、`imported.json`、`share-alias*`、`locks/` 目录、`tdl|telegram`
  的 storage/session）与**内容结构**（SQLite 数据库头、`user:password@host` 凭据串、化名映射表
  /导入台账/全量私有报告的 `kind` 字段、带原始节点键的完整 raw）。命中即**拒绝落盘**，写进
  `refused_sensitive_artefacts` + `pending_decisions`（`kind=sensitive_artefact_refused`）+
  `uncovered`（点名这是什么），状态转 `partial`；申报文案只说形态，绝不回写口令明文。
  化名映射表是否在包里由**构建后遍历分享目录**算出（`claims.alias_map_in_share_package`、
  `mapping.in_share_package`），不是硬编码；若目录里本来就躺着映射表形态的文件，申报为 `true`
  并追加 `alias_map_present` 待办——本模块不删用户目录里的任何文件。
- **附件文件名也过同一套脱敏**：清洗后同名不互相覆盖（追加 `-2` 序号并计入
  `attachments_name_conflicts`）；**只差大小写的名字按 Windows 的有效路径算同一个落点**，
  同样追加序号；未选的附件一个都不复制；解析后越出所选附件目录（`..`、绝对路径外指、
  符号链接/junction 式指向）一律拒绝落盘并计入 `refused` / `pending_decisions`，状态转
  `partial`。附件先落盘、再渲染链接，落盘后逐个核对：没落到磁盘的不计入 `attachments_copied`、
  不生成指向不存在文件的链接，改为写 `attachment_not_landed` + `partial`。
- **链接只指向分享目录内真实存在的文件**：`verify_share_links` 遍历每个 `.md` 的每个链接，解码后
  必须仍在目录内且真实存在；`..`、盘符/UNC/根路径、`://` 外链、断链一律抛错。用户正文里手写的
  `[文本](目标)`、`scheme:`/`www.` 裸 URL、HTML 标签，以及会被渲染器自动识别的可点形态，
  在渲染前就地降级成 `[外部链接已移除]` 等中性串：带 `/` 或 `:端口` 的裸域名
  （`evil.example/path`，含尾点 FQDN `evil.example./path`）、不带 `/` 的裸域（只认已知
  后缀如 `bit.ly`、`evil.example`——未知后缀如 `evil.corp` 仍可点，这是为不误吞文件名有意
  收窄的代价）、裸邮箱（`a@evil.example`）、`host:port`（带点的 host 认 `host:8080`，单标签
  host 必须带 `/` 路径如 `host:8080/a`）。口径要说清：`verify_share_links` 只解析
  `[..](..)` 语法，上面这一面靠的是渲染前的字符串中和，不是链接校验；`report:2024`、
  `ID:12345`、`v2:2020`、`会议记录.png`、`v1.2.txt`、`12:30`、`16:9` 这类普通词不受影响
  （有对应用例守着不被吞）。

### 文字脱敏 ≠ 媒体完全脱敏（用户可见承诺）

分享目录的 `index.md` 明写这条边界，`report.json` 逐条申报**未覆盖范围（`uncovered`）**：

- 图片/视频/音频**内嵌信息没做 OCR、声音转写/声纹、EXIF/metadata 剥离**时，按附件类别逐条标
  「未覆盖」；文件内容本身（如文档正文）也没做文字脱敏，只做了文件名脱敏。
- `claims.media_fully_redacted` 恒为 `false`、`claims.full_anonymity_claimed` 恒为 `false`、
  `claims.text_only_masking` 恒为 `true`。绝不会把「文字字段已脱敏」冒充成「附件完全脱敏」，
  报告里也不出现「已完全脱敏 / 完全匿名 / 绝无泄漏」这类话。
- 用户要求「完全脱敏」而这些处理条件无法兑现时，原媒体**不放进分享包**（`shared_media` 为空），
  改为写入 `pending_decisions`、状态 `partial`，等用户另行选择处理方式；不默许把原媒体塞进分享包。

### 离线动作与互斥（复用唯一一把归档锁）

- 分享属**离线动作**：没有 tdl、未登录、禁网也能完成（`runtime.offline_action=true`、
  `claims.{tdl_required,network_used,login_used}=false`）。测试里 `socket`/`subprocess`/
  `os.system`/`builtins.exec` 全部被桩住，一旦触碰即失败。
- 写分享包与私有映射表/台账期间的读-改-写**复用 `state` 的那一份归档锁实现**：
  `state.acquire_archive_lock(private_root, resource=mapping)`，marker 由
  `state.archive_lock_marker` 计算，本模块不自建第二份、不再自己算 marker。加锁序固定
  「先归档锁、后会话锁」（`report.lock.order = state.LOCK_ORDER`），与在线动作互斥；锁忙时
  `LockBusy` 原样抛出，CLI 归退出码 3（能力/依赖缺口），成品完成后不残留 `archive-*` 锁文件。

### 构建被打断时目录里写着什么

`report.json` 在分享目录一建成就**先占位**（`status=incomplete`、`complete=false`、
`headline` 明写「这不是一份成品，请勿转发」），落盘与链接校验阶段抛异常时再覆写成带具体阶段的同一
形态，并清掉本次的 `*.part` 残留，原始异常照原样上抛（CLI 按运行期失败退出，不会静默）。
所以半成品目录**任何时刻**都读得到「不完整」这条机器可判的申报，而不是只剩看着像成品的
`index.md`/`parts/`。这不是断点续传：被打断的目录请整份删掉重跑。

### 报告与计数

`create_share_copy(archive, target, options) -> dict` 返回一份可机读报告（`kind=
telegram-archive-share-report`）：`counts`（`messages_in/shared`、`messages_filtered_out`、按字段
的 `redacted`、`path_scrubbed`、`attachments_selected/copied/not_selected/name_conflicts/
refused_outside_scope/refused_sensitive/not_landed/missing_on_disk`、`links`）、
`unselected_fields`、`uncovered`、`pending_decisions`、`refused`、`refused_sensitive_artefacts`、
`attachments_not_landed`、`claims`、`mapping`、`lock`、`runtime`、`unchanged`、`limitations`。
分享目录内的 `report.json` 是**公开裁剪版**：私有绝对路径与身份（`channel:<id>`）一律隐去——
整份报告每个字符串都过一次绝对路径 masker（不再只认串首是绝对路径的值），映射表只标「留在私有侧」
并注明该字段由构建后遍历算出；全量报告另存私有目录。不以空报告宣称完全匿名。

API（`scripts/sharing.py`）：

```python
sharing.create_share_copy(archive, target, options)  # 合同入口，签名逐字 (archive,target,options)
sharing.verify_share_links(target)                   # 遍历 .md 链接，越界/断链即 ValueError
sharing.resolve_within(root, value)                  # 解析到所选目录内，越界即 ValueError
sharing.mapping_path(target, private_root)           # 化名映射表落点（只在私有目录）
sharing.run_share(job)                               # CLI share 后端
```

### 覆盖用例与边界

- 覆盖在 `work/telegram-archive-tests/test_sharing.py`（77 条）：显式模式、字段/正文双覆盖、化名
  一致、映射表只在私有、遍历分享目录全部文件逐串检查、附件文件名脱敏与同名不覆盖、越界/junction/
  符号链接式指向拒绝、完全脱敏无法兑现则待决策、链接只在目录内、未覆盖逐能力申报、离线、复用
  `state` 归档锁、原档/阅读版前后 SHA-256 不变、CLI 冒烟；修复轮 1 追加：选中的附件本身是会话库/
  登录密钥/凭据配置/台账/映射表时按内容与结构拒绝入包、目录包含双向判定（含相等于尾随点与
  junction 形态）、只差大小写的目标名不互相覆盖、清洗后只差尾随点/空格的目标名也不互相覆盖、附件没落盘不计 copied 且不生成断链、带 `/`
  或端口的裸域与裸邮箱与带路径的 `host:port` 不可点、含空格私有路径整条隐去、构建被打断仍留可机读的未完成申报；
  修复轮 2 追加：重跑同一目标的陈旧分篇/附件/残留对账（`stale_share_files`）、同目录不同写法共用
  同一份化名映射（`share_id` 按目录形态算）、同名落同文件时按内容哈希验载荷、无 scheme 的
  `user:pass@host` 与无扩展名 session 同样拒收、不带 `/` 的裸域只认已知后缀、UNC/POSIX/止于带
  空格目录名的路径整条隐去、`.txt`/`.md` 别名映射同样申报、硬中断扫尾进 `finally`。
- **真实媒体脱敏效果不由合成文字测试代替**：这里的遍历断言只证明「选定文字与文件名没有泄漏」，
  **不证明**图片像素、声轨或 EXIF 里没有可识别信息。合成数据 + 临时目录，不碰真实归档、不联网。
