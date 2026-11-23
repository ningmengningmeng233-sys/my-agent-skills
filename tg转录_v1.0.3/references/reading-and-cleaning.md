# 阅读版与可恢复清洗（Task 4）

这份文档回答四件事：**Markdown 阅读版怎么分节与拆分**、**同名聊天与特殊字符标题怎么消歧并
编码链接**、**清洗规则凭什么命中、正负样本各是什么**、**过滤后计数怎么守恒、怎么逐条撤销**。
成品代码是 `scripts/reading.py`（接口
`build_reading_pack(data: list[dict], target: Path, options: dict) -> dict`），CLI 入口是
`archive.py render`。可重跑校验：
`PYTHONUTF8=1 python -m unittest discover -s work/telegram-archive-tests -p test_reading.py -v`（开发侧命令，`work/` 测试目录不随包）。

**边界先说清楚**：本模块与全部测试只用**合成数据**（TemporaryDirectory 里的假 JSON），不联网、
不登录、不起子进程、不读真实归档。因此这里证明的是本地构建与清洗逻辑，**不代表任何真实导出
或真实附件下载已通过**；阅读版与清洗记录都是派生视图，**不得当作「该聊天全部在线历史已导出」
的证据**，`claims.online_coverage_verified` 恒为 `false`、`claims.real_data_verified` 恒为 `false`
（即在线覆盖**未验证**）。

## 一、分节与拆分规则

设计上位依据点名的旧缺陷是「大群仅按年拆分，单年巨量不会拆，仍生成难以打开的 Markdown」。
新版按四步收口：

1. **分节**：文件内按**日**分节（`## 2026-03-05 周四`），节内逐条消息保留时间、消息 ID、正文
   与附件标记；没有消息日期字段的条目单独归到「时间缺失」节，不猜时间。
2. **什么时候拆**：`条数超 split_trigger_messages` **OR** `正文体积超 max_bytes_per_file`
   **OR** `消息跨年月边界`——**任一**超限即拆。只看条数不行：长文刷屏时条数很少也会撑出打不开
   的巨文件（修复轮 1 前实测默认阈值产出 12 MB 单文件，就是这条）。
3. **怎么拆**：先按**年月**分组（`2026-01`、`2026-02`……月份由消息时间戳按展示时区算出），
   **同一年不再是一个文件**；每个年月内再按 `max_messages_per_file`（条数）与
   `max_bytes_per_file`（UTF-8 字节）双阈值滚动分篇，两个阈值谁先到谁生效。
4. **按渲染后的整篇体积收敛**：页眉、按日分节标题与同聊天互链同样占字节，所以最后一遍拿
   **渲染后的整篇**体积与上限比：超了就二分该篇，直到全部收进上限。单条正文本身超限的只能
   独占一篇（不截断、不丢消息），记进 `counts.oversized_parts`。测试是**遍历断言**：默认阈值
   下每个输出文件都不许超过字节上限，不是抽查。

| 阈值 | 默认值 | 取值理由 |
| --- | --- | --- |
| `split_trigger_messages` | 5000 | 小群一份文件读完，不制造翻页负担；**只是三个触发条件之一**，字节与年月边界同样能单独触发 |
| `max_messages_per_file` | 5000 | 与 tdl 分段宽度同量级，单篇可在编辑器里稳定打开、单次重做成本可控 |
| `max_bytes_per_file` | 1000000 | 约 1 MB UTF-8：长文刷屏时条数很少也会撑爆文件，字节上限兜住「打不开」这一条；页眉与互链也算在内 |

三条阈值都可显式覆盖，必须是正整数，`0`、字符串一律 `ValueError`（不猜默认值）。实际生效值
逐次写进构建报告 `thresholds`。单条消息本身超字节上限时**独占一篇、绝不截断或丢弃**，这类
篇数记在 `counts.oversized_parts`。

分篇之间必须有互链（同聊天其它分篇 + 返回目录），总目录 `目录.md` 必须链到每一个分篇；
发布前逐条遍历本次成品的所有 Markdown 链接，解码后目标要么在本次计划文件集合内、要么磁盘上
真存在，否则整批不发布。

## 二、命名消歧与链接编码

旧实现按显示名命名，同名聊天互相覆盖、标题里的空格/井号/括号让链接失效。新版：

- **身份进路径，显示名只用于人读**。分篇一律落在 `parts/<peer 类型>-<chat ID>/`，例如
  `channel:123` → `parts/channel-123/`，`chat:123` → `parts/chat-123/`。同名群、同显示名的
  不同 peer 类型、同 ID 的不同 peer 类型都各占各的目录，**不可能互相覆盖**。
- **文件名** = `安全化显示名__<年月|全篇>[-p序号].md`。显示名只做文件系统非法字符替换
  （`\ / : * ? " < > |` 与控制字符换 `_`）、剥掉 Windows 会静默删除的尾随点/空格、限长 60；
  显示名为空时退回 `未命名-<身份>`，绝不编造群名。
- **链接编码**：所有链接目标按 URL **percent-encoding** 逐段编码（空格 → `%20`、`#` → `%23`、
  `(` → `%28`、`)` → `%29`），**文件名本身不改写**——所以磁盘上是 `项目 组 (测试) #1__2026-03.md`，
  链接里是 `...%E9%A1%B9%E7%9B%AE%20%E7%BB%84%20%28...%29%20%231__2026-03.md`。测试逐文件遍历
  每条链接并解码回真实路径断言目标存在，不是抽查字符串。
- **一律不写外部链接**：链接里出现 `://` 直接判构建失败；附件链接只指向本次所选媒体目录内的
  文件，越出该目录立即拒绝（发生在写盘之前，所以旧视图不会被半成品覆盖）。
- **发布是原子且可回滚的**：全部文件先在内存出齐并校验，再交给 `state.publish_text_files`
  —— 全仓**唯一一份**原子发布实现（阅读版自己不再写第二份，`_publish` 已删除）。侧车
  `<名>.part-<pid>` 在 `open` **之前**就登记，所以写入阶段失败也清得掉；`os.replace` 逐条进行
  并留旧字节备份，任何一步失败**整批回滚**：旧文件复原、本次新建的文件删掉、残留侧车清掉、
  本次新建的空目录撤掉——不留「新分篇 + 旧目录」的混合视图，也不残留 `.part`。回滚自身失败
  不掩盖原始异常：遗留项写进阅读里程碑的 `reason` 与 `evidence.rollback_problems` 申报。
  硬崩溃遗留的 `.part-*` 由 `state.prune_stale_temp_files`（扩展 `extra_dirs` 扫描面）回收，
  结果写进 `report["temp_files"]`——全仓只有这一套临时文件治理，不再新造第三套。
- 重建只替换**本聊天**目录里的分篇，并清理同一聊天被取代的旧分篇（不链、不留孤儿）；
  其它聊天的文件、总目录与清单在正文没变时逐字节不变。
- 清单 `reading-pack.json` 的分篇条目只记不随正文变化的稳定字段（路径/身份/标题/period/part/
  parts/messages）；逐文件的 `bytes` 与 `sha256` 出现在**本次构建报告** `report["files"]` 里，由
  调用方核对，不写进共享清单。

## 三、清洗是显式选项

`cleaning` 只接受 `none` 或 `ads`，两者都必须**显式选择**（Task 1 的 `validate_job` 在有文字
输出时已强制；本模块再校验一次，其它取值和 `None` 一律 `ValueError`）。`template_compression`
是**独立开关，默认关闭**，必须是布尔值。清洗只作用于**所选文字阅读输出**（`scope` =
`markdown_text_output`），不碰原始 JSON，也不碰媒体下载。

### 广告判定：显式信号 + 营销语境「组合」命中

单关键词命中不算广告——旧实现就是靠「社工库」这类词单点匹配，把正常讨论也删了。新版要求
**同时**命中广告信号与营销语境，且反诈骗/风险提示语境直接豁免：

| 类别 | 判据 |
| --- | --- |
| 广告信号（A，命中其一） | 代查隐私数据（`代查/可查/承接` + `身份证/户籍/开房/社工库`）、接单招徕（接单/全天接单/代理招商）、引流联系方式（加微信/QQ/`t.me/`）、广告标签（`#广告`/`#推广`/`#机场` 独立成词）、成人招嫖、充值返利（首充/返利/免费试用） |
| 营销语境（B，命中其一） | 价格/价目（价格、价目、USDT、元/次）、下单/客服（下单、客服、成交、秒回）、促销动作（点我、点击、注册、试用、私聊、进群、扫码） |
| 豁免（C，命中即保留） | 警惕/谨防/防范/诈骗/骗局/被骗/举报/曝光/违法/已被抓/别信/切勿/骗子 |

判定式：`C 命中 → 保留`；否则 `A ∧ B → 过滤`；只命中 A 或只命中 B → 保留。

正样本（必须被过滤，规则名写进记录）：

- `代查身份证/户籍/开房记录，社工库实时数据，全天接单，价格见目录链接，加我微信详聊`
- `#广告 新上线机场节点，点我免费试用三天，首充返利 50%，客服 24 小时在线下单`
- `https://t.me/xxx_bot 同城约见上门服务，价目表见图，下单秒回复，代理招商中`

负样本（必须原样保留，逐条断言正文仍在）：

- `请警惕社工库诈骗！本群已有人因为查户籍被骗了 800 元，遇到这类私信请直接举报`
- `有人问能不能查户籍？答案是这是违法的，新闻里那个卖数据的团伙已经被抓了`
- `开房记录这种东西别信群里发的，全是骗局，我已经举报过三个号了`

另有两类负样本：只带广告标签没有营销语境（`#广告 栏最近被人塞满了，大家举报了吗`）、以及
同一条正常发言重复多次（`同意，这个方案我也觉得可以` ×8）——默认与 `cleaning=ads` 下都全数
保留。

### 模板压缩

只有 `template_compression=True` 才生效：同聊天内**归一化后**长度 > 20 字符、完全相同且出现
≥ 3 次的正文，保留首条、压缩其余，并逐条记录。短重复（如「短重复」）与 2 次重复都不动。
默认关闭时 `counts.template_compressed` 恒为 0。

## 四、计数守恒与可恢复

等式（写进报告 `conservation.formula` / `conservation.text`、清单、每篇页眉与总目录）：

```text
原始数 = 保留数 + 广告过滤数 + 显式模板压缩数
original = kept + ads_filtered + template_compressed
```

- `原始数` 是**去重后**的输入条数（边界重叠按消息 ID 去重，`counts.duplicated_ids` 单记），
  重复 ID 不重复计数；缺消息 ID 或非正整数 ID 直接报错，不补号。
- 每条过滤记录包含：`rule`（规则名，广告形如 `ads:代查隐私数据`，压缩固定
  `template_compression`）、`message_id`（原消息 ID）、`original_text`（**原文逐字**）、
  `matched_signals` / `matched_context`、`chat_identity`、`date_utc` / `display_time`、`undo`。
- 报告 `kept_ids` 与 `filtered` 里的消息 ID 互斥，`kept_ids` 升序，`len(kept_ids)` 必须等于
  `counts.kept`。
- **不声称零误删**：`claims.zero_false_positive = false`，`false_positive_note` 原文写着
  「不声称零误删：规则命中不等于确证广告……」。

撤销（可恢复）流程：阅读版是显示层，清洗从不写回原始 JSON——

1. 直接按 `cleaning=none`（并关掉 `template_compression`）重跑 `build_reading_pack`，即还原
   未清洗的阅读版（测试 `test_undoing_cleaning_reproduces_the_original_text` 逐条比对被过滤
   消息的**渲染后可见文本**都回来了——正文按不可信内容转义，比对的是还原转义后的文本——
   且 `kept_ids` 等于输入全部 ID）。本轮没有过滤记录时，**上一轮本聊天的
   `清洗记录/<身份>.json` 会被删掉**并记进 `report["removed"]`：留着一份还在宣称旧计数的记录
   等于说谎；别人家聊天的记录一律不碰。
2. 或按 `清洗记录/<身份>.json` 逐条用 `original_text` 覆盖回对应消息 ID 的正文。

记录文件形状：`kind = telegram-archive-reading-cleaning`、`identity`、`counts`（四项计数）、
`conservation_formula`、`false_positive_note`、`records[]`（每条含 `undo`）。

**原档只读**：给出 `options["source"]["path"]` 时记录构建前后 SHA-256 并核对一致
（`source.sha256_before/after`、`source.unchanged`、`claims.original_files_modified=false`）；
测试另用 SHA-256 直接断言原始 JSON 与清洗前一致。

## 五、时间、发送者与附件

- **展示时区**显式写入：页眉、总目录与清单都写 `展示时区: +08:00`，取值只接受 `UTC` 或
  `±HH:MM` 固定偏移；命名时区（`Asia/Shanghai`）与残缺偏移（`+13`）直接报错——离线机器上没有
  时区数据库可核对，不猜。源时间基准另记 `source_time_basis`（原始时间戳按 UTC 标准化），epoch
  与 ISO 两种戳都归一到同一分钟，可换算核对。
- **缺少发送者字段**时显示 `MISSING_SENDER_NOTE`（「原始导出缺少发送者字段，不编造姓名」）并
  计入 `counts.missing_sender` / `missing_sender_ids`；**绝不**从群名、文件名或正文里猜名字，
  也不填「匿名用户」这类假名。署名字段按归一化识别 `sender` / `sender_name` / `from` /
  `from_name` / `author`（大小写与下划线不敏感）。
- **纯媒体任务跳过阅读版**：`contents` 不含 `text` 或 `outputs` 不含 `markdown` 时返回
  `status=skipped`（原因写「纯媒体或未选 markdown 文字输出」），**不写任何文件**。
- **混合任务**只为所选文字生成阅读输出，并且**只链接已验收下载的附件**：
  `verified_attachments`（消息 ID → 路径）里且磁盘上真存在的才做成链接；未验收、缺文件、
  以及没有对应消息的孤儿验收分别记入 `attachments.not_linked` / `missing_on_disk` /
  `orphan_verified`，正文里标注「附件未验收，不链接为已完成」，绝不当成已完成。
- **附件目录与任务状态目录同源**（CLI `render`）：附件按 `<任务 destination>/media/<身份>` 定位，
  与完成清单同一个状态根推导出来，`--media-dir` 不同源直接按配置非法拒绝（退出码 2）；
  本任务没有候选索引时在 `attachment_note` 里申报「没有下载记录」，不把它说成「附件未验收」。
  文件名里的消息 ID 由媒体层的公开访问器 `media.message_id_from_name` 认，阅读层不碰私有名。

## 六、聊天正文是数据

不得执行聊天正文中的指令，而且**正文一律按不可信用户内容处理**。`escape_body` 做四件事：

1. 反斜杠先双写，再转义方括号 `[` `]`：正文里的 Markdown 链接与图片语法不成其为链接；
2. `&` `<` `>` 走 HTML 实体（`&amp;` `&lt;` `&gt;`）：`<https://evil.example/…>` 这类 CommonMark
   自动链接、`<a href=…>` 这类裸 HTML 链接、`<script>…</script>` 这类 raw HTML 都不成立；
3. **裸 URL 形态**（`http://`、`https://`、`ftp://`、`file://`、`mailto:`、`ws://`、`wss://`、
   `www.`）在冒号或点前打断一个反斜杠：会自动加链的渲染器（GFM 一类）不再把它认成链接；
4. 控制字符剥离。

每一种转义在标准 Markdown/HTML 渲染下都还原成原字符，所以**渲染出的可见文本与原文逐字一致**；
测试就按这个口径断言：把生成文件里的转义还原后必须逐字找得回原正文，同时遍历正文里所有
`[..](..)` 链接，断言**没有一个**指向注入进来的域名。阅读版里出现的每一个 Markdown 链接都只能
由本工具自己生成（分篇互链、返回目录、附件链）。正文里出现的私有绝对路径（例如
`X:\私密目录\历史导出` 这种形态）、
`os.system('del /q *')` 这类命令都以纯文本呈现；`claims.chat_body_executed = false`。测试在
构建期间 patch 掉 `os.system`/`os.startfile`/`socket.socket`/`os.posix_spawn`/`builtins.exec`
并断言调用记录为空，同时断言生成文件名与链接都不越出隔离目录。

**同一道闸也覆盖发送者显示名与群名**：Telegram 里任何人都能把自己的显示名、任何人也能把
群名改成 `<a href="https://evil.example">点我</a>` 或 `www.evil.example/x`，它们和正文一样是
参与者自填内容，展示时同样过 `escape_body`（页眉、总目录标题与每条消息的发送者段都是）。
群名另有一层**文件系统消毒**（把 `<>:"/\|?*` 与控制字符换成 `_`），那一步只作用于落盘路径，
页眉里显示的是消毒后再转义的同一串，因此注入的链接语法既成不了链、也不会让整次构建报错。

## 七、CLI `render` 的边界、限制与退出码

- `render` **只读「本任务完成清单验收过」的文本源**：先用文本源相邻的 `telegram-state` 定位
  任务状态目录（只认它登记的最终成品路径，不递归磁盘、不自动找文件），再走
  `media.accepted_export` 的凭据闸（摘要 + 身份 + 冻结区间）。未验收、伪造、身份不符、
  **文本源被删/读不出**（凭据不足）一律 `status=unavailable`，归退出码 **3**；配置里 `action`
  与子命令不一致、`--job` 文件不存在、`--media-dir` 与任务不同源归 **2**；发布中途失败
  `status=failed` 归 **1**（旧可用视图已回滚保留，原因写进阅读里程碑）；成功出阅读版归 **0**，
  且**不启动任何子进程、不碰网络**。体积口径全仓一套：清单登记的 `size`/`sha256` 就是实际落盘
  字节，报告里不再出现任何「换行口径调和」式的依据字段。
- **限制：`render` 一次只吃一个聊天**。一次调用只读**一份**已验收的文本源、只出该聊天的阅读版；
  配置里给多个 `chats` 会被按配置非法拒绝（退出码 2），**请逐个调用**（一次一个聊天）——阅读版清单
  `reading-pack.json` 跨聊天合并、分篇落在 `parts/<peer 类型>-<chat ID>/`，天然隔离不会互相覆盖。
  这条限制同样写进 `report["limitations"]` 与阅读里程碑的 `evidence.limitations`，Task 8 的
  完成判据要按「多聊天 = 多次调用」来数。
- **限制：附件目录与任务状态目录同源**。附件一律按 `<任务 destination>/media/<身份>` 定位，
  与完成清单同一个状态根推导；显式传入的 `--media-dir` 与它不同源时按**配置非法拒绝**
  （退出码 2 并申报两条路径），**不再**出现「已验收附件被静默标成未验收、却照样报 complete」。
  本任务没有候选索引时 `report["attachment_note"]` 如实写「没有下载记录」，与「附件未验收」分开
  申报；里程碑的 `evidence.attachment_index` 为 `null` 就是这个意思。
- **限制：`cleaning=none` 重跑会删掉本聊天上一轮的 `清洗记录/<身份>.json`**（见 §四），
  需要保留历史过滤记录时请把阅读版输出到别的目录。
- 成功后用 `state.record_output(root, "reading", "complete", evidence=…)` 登记阅读里程碑，
  `status` 的三类完成状态里 `outputs.reading` 才从 pending 变 complete；`evidence` 缺内容会被
  `record_output` 拒收，防止用「文件存在」冒充完成。发布失败走 `status=failed` 的里程碑，
  带 `evidence.rollback_problems` 与 `partial_publish_attempted`。
- 消息 ID 逐条保留在正文里（`- **06:02:33** #777777 张三：…`），下游按 `kept_ids` 与正文里的
  `#<id>` 双向核对；测试遍历断言正文含每条消息 ID。

## 八、供 Task 5–8 消费的结构

`build_reading_pack` 返回的报告（`kind = telegram-archive-reading-pack`、`version = 1`）字段：

| 字段 | 含义 / 消费者 |
| --- | --- |
| `status`、`reason`、`text_selected`、`scope` | `complete`/`skipped`（+ CLI 侧 `unavailable`）；Task 5 归档清单与 Task 8 汇报按此判定阅读版是否该存在 |
| `target`、`identity`、`chat`、`index` | 阅读版根目录、`peer:ID` 身份、总目录文件名 `目录.md` |
| `display_timezone`、`source_time_basis`、`generated_at` | 展示与源时间口径；Task 7 搜索索引要按同一口径解析时间 |
| `cleaning`、`template_compression`、`thresholds` | 本次显式选择；Task 6 导入快照必须原样带上，不得改写 |
| `counts`、`conservation` | 八个计数 + `holds`/`formula`/`text`；守恒不成立即 `holds=false` |
| `files[]` | 每篇 `path/identity/title/period/part/parts/messages/bytes/sha256`（逐文件摘要只在这里给） |
| `records_path`、`filtered[]`、`kept_ids`、`missing_sender_ids` | 清洗记录相对路径、逐条过滤记录（含原文与 `undo`）、保留与缺字段的消息 ID；Task 7 检索与 Task 8 复核用 |
| `attachments` | `linked[]`（含 `exists`）/`not_linked[]`/`missing_on_disk[]`/`orphan_verified[]`，Task 5 附件核对的唯一依据 |
| `source`、`claims`、`removed`、`linked_index_entries` | 文本源 SHA-256 前后与是否改动；申报口径；本次清理掉的旧分篇与旧清洗记录；总目录应链接的分篇清单 |
| `limitations`（+ CLI 侧 `attachment_root`/`attachment_note`/`temp_files`/`milestone`） | 对用户可见的限制申报（一次一个聊天、正文按不可信内容转义、附件目录同源）；附件目录落点与「没找到索引 vs 未验收」的区分；回收的孤儿 `.part` 报告；写进 outputs.json 的阅读里程碑 |

`reading-pack.json`（清单，跨聊天合并）：`kind/version/generated_at/index/display_timezone/
source_time_basis/thresholds/conservation/chats{identity:{…,counts,files,records,attachments,
thresholds,display_timezone,source}}/claims`；`claims.real_data_verified` 恒为 `false`。
