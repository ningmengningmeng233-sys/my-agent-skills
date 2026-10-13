# 选择与类型对照表（Task 3）

这份文档回答三件事：**每个媒体类别靠哪条原始元数据判定、出处在哪一行**；**判不出来时怎么
处置**；**为什么绝不能用文件后缀/扩展名筛类别**。出处一律指仓库内捕获的原件副本
`work/upstream/`（tdl 0.20.4 依赖的 gotdgen 类型源码 + 本机 `tdl` 帮助原文），不是外部文档。
可重跑校验：`test_media.py::ClassificationSourceCitationTests` 会逐条读那份副本的对应行，
核对行号上确实写着引用的片段（40 条分类出处 + 14 条旗标出处，全部对账通过；`work/upstream/` 原件副本与该测试为开发侧资产，不随包，分类规则表可独立使用）。
除出处外，本文还回答两件用户会问的事：**旗标形状怎么判**（§六真值表）与**重复消息 ID 怎么去重**
（§六第二条）——两条都是产品行为，不只是代码注释。

## 一、类别 ↔ raw 判据 ↔ 出处

用户可选的九个内容类别里，附件类八个（`text` 不算附件）。判定顺序固定：先看消息级媒体
构造器，是 `messageMediaDocument` 时再看内层 `document` 的 `attributes` 列表。
**文件名、扩展名、MIME、标题、正文一律不参与判定。**

| 类别 | 判定条件（原始元数据） | 出处（file:行 · 该行原文片段） |
| --- | --- | --- |
| `image` | 媒体是 `messageMediaPhoto` | `media.go:137` `// Attached photo.`；`media.go:140` 结构体；`media.go:229` `return "messageMediaPhoto"`；字段 `photo` 见 `media.go:250` |
| `video` | `messageMediaDocument` + 属性 `documentAttributeVideo` | `media.go:887` `// Document (video, audio, voice, sticker, any media type except photo)`；`media.go:890` 结构体；`attributes.go:570` `// Defines a video`；`attributes.go:573` 结构体；`attributes.go:713` `return "documentAttributeVideo"`；字段行 `attributes.go:729-766`（`duration` 在 744） |
| `audio` | 属性 `documentAttributeAudio` 且**没有** voice 旗标 | `attributes.go:1033` `// Represents an audio file`；`attributes.go:1036` 结构体；`attributes.go:1148` `return "documentAttributeAudio"` |
| `voice` | 属性 `documentAttributeAudio` 且 voice 旗标为真 | `attributes.go:1042` `// Whether this is a voice message`；`attributes.go:1164` `SchemaName: "voice"`；`attributes.go:1258` `d.Voice = d.Flags.Has(10)` |
| `animation` | 属性 `documentAttributeAnimated` | `attributes.go:197` `// Defines an animated GIF`；`attributes.go:200` 结构体；`attributes.go:245` `return "documentAttributeAnimated"`；`attributes.go:258` `typ.Fields = []tdp.Field{}`（该构造器**没有任何 TL 字段**） |
| `sticker` | 属性 `documentAttributeSticker`（`documentAttributeHasStickers` 按推断同归贴纸，见第二节） | `attributes.go:299` `// Defines a sticker`；`attributes.go:302` 结构体；`attributes.go:393` `return "documentAttributeSticker"`；字段行 `attributes.go:409-422` |
| `document` | 上面都不命中，但有属性 `documentAttributeFilename`（「A simple document with a file name」） | `attributes.go:1372` 该行原文；`attributes.go:1375` 结构体；`attributes.go:1432` `return "documentAttributeFilename"`；`attributes.go:1448` `SchemaName: "file_name"`；大小取自 `document.go:193` `Size int64` / `document.go:350` `SchemaName: "size"` |
| `other` | 媒体是**已认识但不含可下载文件**的构造器：`messageMediaEmpty`（纯文字消息）、Geo/Contact/Venue/Game/Invoice/GeoLive/WebPage/Poll/Dice/Story/Giveaway/GiveawayResults/ToDo/VideoStream | `media.go:35` `// Empty constructor.`；`media.go:38` `type MessageMediaEmpty struct {`；全部构造器清单见 `media.go:5373` 起的 `MessageMediaClass` 注释块 |
| `unknown` | 判不出来：**没有媒体字段**、类型名不认识、`messageMediaUnsupported`、`documentEmpty`、属性列表形状不对、两个类别属性互相矛盾（如 Animated + Video）、属性节点认不出、`messageMediaPaidMedia`、`documentAttributeCustomEmoji` | `media.go:784` `// MessageMediaUnsupported represents TL type`；`media.go:788` 结构体；`media.go:4778`（付费媒体内层是 `ExtendedMediaClass`，本仓库未收录其定义）；`attributes.go:1609` `// Info about a custom emoji`；`document.go:181`（AccessHash 不是内容摘要，见第四节） |

### 消息级第二判据与相册

* `messageMediaDocument` 自带消息级旗标 `video`（`media.go:901`，schema 名 `media.go:1070`）、
  `round`（`media.go:903`）、`voice`（`media.go:905`，schema 名 `media.go:1080`，位号
  `media.go:1223-1225`）。只有当内层 `document` 没有任何类别属性时，才用它们定 `video`/`voice`
  ——这是第二判据，属推断，见第二节。
* 相册不扩选：同组消息按各自 message ID 分别计数，区间外的兄弟消息绝不进选择。后端
  `--group` 的原文作用是「auto detect grouped message and download all of them」
  （help:71），与合同冲突，**永不启用**。

## 二、哪些判定只是「合理推断」，哪些是「已验证事实」

* 已验证事实：上表里能在 `work/upstream/*.go` 指到行号的构造器名、字段名、旗标位号。
* 合理推断（必须带风险说话，不许当承诺）：
  1. `documentAttributeHasStickers` → `sticker`。出处只说「Whether the current document has
     stickers attached」（`attributes.go:1507`），没直说「这是贴纸」，据此归贴纸族。
  2. 属性缺失时用消息级 `video`/`round`/`voice` 旗标定 `video`/`voice`（`media.go:901-905`）。
     风险：GIF 若被上游打上 `video` 旗标而属性又被截断，会被算成视频；实际数据里属性通常齐全。
  3. `round video` 算 `video`（`media.go:903`）。
* 明确**不猜**、一律 `unknown` 的三类：
  1. `documentAttributeCustomEmoji`：九类里没有「自定义 emoji」这一档，出处也只写
     「Info about a custom emoji」（`attributes.go:1609`），不擅自塞进 sticker。
  2. `messageMediaPaidMedia`：内层是 `ExtendedMediaClass`（结构体 `media.go:4772`、字段
     `media.go:4783`），本仓库未收录其类型定义，无法判定内层文件类别。
  3. **未带构造器名且字段为空的属性节点**：`documentAttributeAnimated` 与
     `documentAttributeHasStickers` 都没有任何 TL 字段（`attributes.go:258`、
     `attributes.go:1568`），没有类型名时二者不可区分，判 `unknown` 而不是猜动画。

## 三、为什么不能用后缀 / 扩展名筛类别

本机 `tdl` 帮助原文自己写明，`-i/--include` 与 `-e/--exclude` 都是按**文件名**筛，不看 MIME：

```
  -e, --exclude strings   exclude the specified file extensions, and only judge by
                          file name, not file MIME. Example: -e png,jpg        （help:69）
  -i, --include strings   include the specified file extensions, and only judge by
                          file name, not file MIME. Example: -i mp4,mp3        （help:73）
```

而 Telegram 的类型信息不在后缀里（上面两段帮助原文都写着
`only judge by file name, not file MIME`，即「只按文件名判定，不看文件 MIME」）：

* 动画（GIF 语义）与视频**同为 `.mp4`**：`documentAttributeAnimated` 是「Defines an animated
  GIF」（`attributes.go:197`）却没有自己的容器；视频靠 `documentAttributeVideo`
  （`attributes.go:570`）。用 `-i mp4` 选「仅视频」必然把动画一起拉下来。
* 语音与普通音频**同为 `.ogg`**：唯一区分是 `documentAttributeAudio` 的 voice 旗标
  （`attributes.go:1042`、`1164`、`1258`）。用 `-i ogg` 选「仅语音」必然混进音乐。
* 文档与视频也可能同后缀（`documentAttributeFilename` 只是「A simple document with a file
  name」，`attributes.go:1372`），文件名还可能重名。

所以本技能的做法是：**本地按 raw 元数据分类 → 只把用户所选类别的消息写进索引 → 用
`tdl dl -f <该索引> -d <目录>`（help:68、help:70）下载**，`-i/-e` 一概不用。同时
`--skip-same`（help:78，按同名同尺寸跳过）也绝不启用，否则同名附件会被静默丢掉一个；
`--desc`（help:67，从新到旧下载）也不启用，索引顺序固定为消息 ID 升序以便续传。

## 四、附件验收：三步分开，退出码不算凭据

0. **下载源本身要先有凭据**（`--export-file` 的闸门，`media.accepted_export`）：只接受
   **本任务完成清单里验收过**的那份导出——清单（`manifest.json` 的段记录或 final 记录）里
   必须有一条 `sha256`/`size`/`count` 都与该文件一致的登记，登记身份必须等于本任务身份
   （peer 类型 + chat ID），登记区间与文件里的消息 ID 都必须落在任务已冻结的范围内（由
   `state.validate_export` 按任务边界复核）。**「文件存在」与「JSON 能解析」都不是凭据**；
   任一件不满足就拒绝，退出码 3（凭据/能力不足），且绝不启动下载子进程。
1. **候选索引生成** → `<状态根>/media/index-<peer身份>.json`（`kind` 为
   `telegram-archive-media-index`，`contains_full_text: false`）。纯媒体任务**不产出**全文
   阅读文档：没有 `.md/.markdown`，也没有 `*-messages.json`。索引保留下载定位所需的原始消息
   字段（`media` 子树、消息 ID、chat/dialog 标识），但按白名单投影，**正文 `text` 不写入**。
   内部定位索引 ≠ 用户所选全文 JSON，两者靠 `kind`、存放位置与字段范围区分。
2. **所选附件下载** → `tdl dl -f <索引> -d <保存目录>`，子进程一律参数列表调用，绝不拼
   shell。附件落在 `<destination>/media/<peer身份>/`，与状态目录分开。
3. **附件验收** → 按「候选消息 ↔ 输出文件」一一映射，核对存在性、已知大小与摘要。
   文件名锚点来自默认模板 `{{ .DialogID }}_{{ .MessageID }}_{{ filenamify .FileName }}`
   （help:80），即 `DialogID_MessageID_名字`；消息 ID 进文件名，同名附件互不覆盖。
   * 大小：只在内层 `document.size`（`document.go:193`/`document.go:350`）存在时核对；
     **照片没有可指认的大小字段**（本仓库未收录 `photo.go`），这类条目在验收里明确标注
     「依据较弱」（`size_unknown` / `weak_basis`），只靠存在性与 ID 映射。
   * 摘要：raw 元数据不含文件内容摘要（`document.go:181` 的 AccessHash 是「Check sum,
     dependent on document ID」，不是内容摘要），所以摘要只能与**上一轮本地验收记录**比，
     用来检出下载完成后被改动的附件；不能声称与 Telegram 原件一致。
   * 计数分开：`missing` / `size_mismatch` / `digest_mismatch` / `ambiguous`（一条消息对应
     多份文件）/ `inaccessible`（读不到）/ `unexpected`（非所选文件，只列出不删）/
     `unknown_type` / `not_downloadable` / `download_failed`。
   * **未处理完所选范围不得报整体完成**：还包括「还需向更早历史翻页」
     （`needs_earlier_pages`，见下节）这种一条附件都不缺的情形。

## 五、`last` 范围与 `matching_messages` 的扫描口径

`range.mode = last` 必须带 `n` 与 `count_unit`，两种计数对象不同，**不得互相降级**：

* `count_unit = matching_messages`（「最近 N 条视频」）：在**已冻结的快照上界**之下，按消息
  ID **由新到旧**扫；每条先分类，**类型确认属于所选类别才计数**；不够 N 条就继续向更早扫。
  动画不计入视频，纯文字不计入，`unknown` 也不计入（设计合同：无法识别的媒体不能计作已确认的
  视频）。本地一次拿到的导出载荷扫到底仍不够时，报 `needs_earlier_pages: true` 并让任务停在
  `partial`，等组合层向更早翻页后再补——**绝不重新探测上界**，也绝不为凑数把别的类别塞进来。
* `count_unit = messages`（「最近 N 条消息里的视频」）：先取最近 N 条消息，再在这 N 条里筛类别，
  结果可能少于 N 条。
* 工单里的合成案例（`test_media.py::LastRangeTests` 逐字命中）：最新五条为
  文字/视频/动画/视频/视频 时（消息 ID 依次为 5/4/3/2/1），最近 2 条视频选 **2 和 4**（扫过 4 条
  消息），最近 2 条消息里的视频只选扫描到的第 2 条（消息 ID 4，即最新那条视频）。

## 六、`unknown` 的处置：交回用户，不静默、不崩溃

`unknown` 不是可默选类别：既不能出现在 `contents`（`job.py` 已拒绝），也不能出现在
`select_media` 的 `selected` 入参（抛 ValueError，点名「unknown 不是可默选类别」）。扫描中
遇到的 `unknown` 消息 ID 通过 `select_media` 的第二个返回值与索引里的
`skipped.unknown_ids`、报告里的 `unknown.{ids, downloaded=false, user_decision_required,
choices}` 上报，验收计数里另有 `unknown_type` 一项。给用户的三个选项固定为：

1. **包含**——把该消息改成明确类别（例如按文档处理）后重跑；
2. **跳过**——保持当前选择，这些消息不下载；
3. **调整条件**——缩小范围或换类别，让未知项落在选择之外。

`unknown` 项绝不进下载索引，也绝不当作「已处理」，因此任务状态不会被它们推进到 `complete`。

### 旗标形状真值表（`media.flag_is_set`）

TL 旗标在 `--raw` JSON 里的编码形状未核对（见 §八第 1 条），因此**只有下表列出的形状**才算
判定结果，其余一律「歧义」——歧义不是 False，也不是某个类别：

| 输入形状 | 判定 | 含义 |
| --- | --- | --- |
| `True` / `1` / `"1"` / `"true"`（去首尾空白、大小写不敏感） | `True` | 置位 |
| `False` / `0` / `"0"` / `"false"` / `None` / **缺键** | `False` | 未置位（缺键就是没置位，不是歧义） |
| 其它任何类型或取值：`2`、`-1`、`1.5`、`{}`、`[]`、`"yes"`、`"voice"`… | `None` | **歧义** |

整数只对 `0/1` 给结论（`bool` 先于 `int` 判，避开 `isinstance(True, int)` 陷阱）；不做「猜成
某个类别」的降级。歧义的后果是**整条消息落 `unknown`** 并单独计数：属性级歧义记
`documentAttributeAudio.voice`，消息级 voice/video/round 先收集歧义清单再整条判 unknown；
`unknown.ids` 与 `ambiguous_flag_ids` 同时列出该消息 ID，`counts.unknown` 计入，绝不进下载载荷。
不变量：`ambiguous_flag_ids` 非空 ⇒ 该条类别必是 `unknown`（不存在「有歧义却仍出类别」的出口）。

### 计数前按「peer 身份 + 消息 ID」去重

`select_media` 在**计数与配额之前**去重，键是 peer 身份 + 消息 ID（shared-facts 裁决 1：边界
允许重叠，同一条不重复计数）：

- peer 身份优先取消息自带的 `chatId` / `peerId` / `dialogId`（含嵌套 dict 的
  `id`/`channelId`/`userId`/`chatId`；dict 里没有任何可用内层值时记 `opaque`），再退回载荷
  声明的身份；`bool` 值不算身份。
- 因此**跨 peer 的相同消息 ID 绝不互相抵消**：只有同一 peer 内的重复 ID 才只算一条。
- 去重发生在 `scanned/found/wanted/beyond_upper_bound_ids/` 与下载索引条目之前，所以重复 ID
  不再消耗配额、也不会造出「expected 2 但磁盘只有 1 个文件」的假 `complete`。
- 被去重的条数申报在 `duplicated_ids`（候选步骤里同名键），不静默。

## 七、留给后续任务读的结构

* `select_media(data, selected)` → `({kind, contains_full_text, messages, attachments[],
  counts, scanned, found, wanted, count_unit, range, snapshot_upper_bound,
  beyond_upper_bound_ids, unknown_ids, not_downloadable_ids, unidentified,
  history_exhausted, needs_earlier_pages, selected_categories, identity}, [unknown_id...])`。
  `attachments[]` 每条：`message_id / category / downloadable / file_name / expected_size /
  size_basis / weak_basis / document_id / has_access_hash / classification`（`classification`
  是这次判定用到的构造器名列表，便于复核出处）。
* 媒体完成状态：`state.record_output(<状态根>, "media", status, reason, evidence)`，
  `evidence` 含 `index_path / counters / digests / outstanding / weak_basis / unknown_ids /
  needs_earlier_pages / return_code_alone_is_not_proof / real_media_download_verified=false`。
  Task 4 的阅读版、Task 7 的分享副本、Task 8 的增量更新都从 `outputs.json` 的 `media` 条目
  与索引文件取数；`digests` 是下一轮验收的对比基线，`outstanding` 是补漏清单。
* 会话互斥：export / resume / download 三条产品路径都只经**同一份实现**取放锁——
  `state.acquire_session_lock` / `state.release_session_lock`（整任务一把，与另一动作互斥
  的锁名同为 `session-*`；`archive-*` 前缀留给 Task 5–8 的归档锁）。`run_download` 走
  `state.acquire_session_lock`，`run_export` 也走它，不再有内联的第二份三步实现。
* `lock_root` 的边界：它是执行者运行参数，**不得等于、位于或包住账号存储（登录会话）目录**
  ——包含关系用 `resolve()` + `relative_to` 判定（不用字符串前缀），结尾会被 Windows 静默
  剥掉的点或空格先按原始字符串拒绝。命中即「配置非法＝退出码 2」，在启动任何子进程之前拒绝，
  错误文字与 `session_mutex` 申报都**不回显该路径**（`lock_root` 报 `null`、
  `lock_scope` 报 `rejected`），会话目录位置不进任何输出。未配 `lock_root` 时跨 destination
  互斥不足照旧如实申报（`status=unavailable` + `reason` 点名要配）。
* 异常遗留的临时文件：`state.prune_stale_temp_files`（唯一一份实现）在**取锁成功后**对每个
  状态根调用，export、resume、download 三条路径同口径；只扫状态根、`tmp/`、`media/` 的顶层
  `*.part` / `*.part-<pid>`，过年龄闸门（默认 3600 秒）才删，逐条上报
  `removed/kept/skipped/needs_attention/ignored`。sidecar 缺正式文件时保留并申报，复认领
  语义不变；被锁拒绝的运行不做任何清理。
* 退出码沿用 `archive.py` 的口径：complete 0、partial/failed 1、配置非法 2、
  能力/依赖缺口（含会话被占、下载源未验收）3。`download` 的失败归类细则：
  **文字源（导出 JSON 或完成清单）在预检之后被删/被占读不出＝凭据不足 → 3**；跑在中途的
  其它 `OSError`（索引目录被占、写盘失败…）＝运行期失败 → 1；两者都**先写出 media 里程碑与
  补漏清单再返回**，异常一律不穿到 CLI 顶层。候选都还没生成时补漏明细无法确定，就照实在
  `acceptance.basis_note` 写「中断发生在候选生成之前，无法确定补漏明细：需整体重跑本任务」，
  里程碑带 `evidence.interrupted=true` 与空的 `outstanding`，绝不留一份「什么都没有」的状态。
  `render` 同一口径：凭据不足 3、发布失败 1（旧可用视图已回滚）、不同源的 `--media-dir` 2。

## 八、仍未验证的边界（不许当承诺）

1. 真实 `tdl chat export --raw` 的 JSON 拼写未核对：`--raw` 的帮助原文只说「export raw
   message struct of Telegram MTProto API」（help:167），本仓库只捕获了 gotdgen 类型定义
   （`TypeName()` 给 TL 构造器名、`TypeInfo()` 给 schema 字段名），没捕获 tdl 自己的编码器。
   因此分类同时接受 TL 构造器名与 Go 字段名两种拼写，认不出的一律 `unknown`；真实样本一旦
   到手，若拼写第三种，需要在此处补一条映射而不是猜。
2. `tdl dl -f` 是否接受「本地按类别过滤后的导出 JSON」未验证（help:70 只说 official client
   exported files），索引按原导出形状保留消息、只删非定位字段。
3. `--storage` 的**旗标形状本身已由帮助原文档化**（不是我们的推断）：
   `help:45` 原文 `--storage stringToString  storage options, format: type=driver,key1=value1,key2=value2. Available drivers: [legacy,bolt,file]`，
   同一条还写着本机默认值 `(default [path=...,type=bolt])` —— 所以 `type=bolt,path=...`
   是**已文档化的拼法**，`tdl_storage_option_mapping` 据此记 `available`。仍未核对的是
   **真实连接行为**（拿这个串去连本机会话会不会成功，本次 NOT_RUN），那部分不承诺。
4. ID 区间端点包含性仍记 `insufficient_source`（沿用 `state.ENDPOINT_NOTE`）：文档与帮助都
   没说 `-T id -i lower,upper` 哪端包含，媒体侧只在**已冻结上界**内工作，不重新探测。
5. 以上任何一条在本次静态与合成测试里通过，都**不代表**真实账号导出或真实媒体下载成功；
   对应能力在 `check` 与 `download` 报告里都以 `insufficient_source` 原文申报。
