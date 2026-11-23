# 长批次运维事故与机理详解（从 SKILL.md 挪出）

本文收录批量转录在真实机器上踩过的事故与对应机理（2026-09-18 / 09-21 实测记录），
供故障复核时按需加载；日常操作只看 SKILL.md 即可。

## 登录态失效：暂停而非空跑

批量遇到「需要登录」这类**登录态失效**时，会停下整批而不是继续跑剩下的几百条
（继续跑只会撞更多风控）。此时 `state/batch_state.json` 的批次状态为 `paused_auth`，
未跑的任务原样保留：

```bash
# 1) 先在浏览器登录 bilibili.com（yt-dlp 会自动读 Cookie）
# 2) 恢复批次：auth_required 条目转回待跑，已完成的不重跑
.venv/Scripts/python.exe scripts/maintenance.py resume-batch
# 3) 重新执行批量，从断点继续
.venv/Scripts/python.exe scripts/batch_transcribe.py
```

注意区分：**没配 Cookie** 时脚本只打印 `[WARN] 无可用Cookie` 并退回 Whisper 正常出稿，
这不算登录态失效，不会暂停批次。

## B站风控：整批退避，而不是连续撞墙

`HTTP Error 412`／`-412`／`-352`／「风控」属于**限速窗口**，与登录态失效不同：等一段时间
多半自己就好。批量的处理顺序是

1. 命中风控 → 整批退避 10 分钟，再重试**同一条**（`BILI_RISK_COOLDOWNS` 可改，逗号分隔秒数）；
2. 仍未解除 → 再退避 20 分钟重试一次；
3. 两轮都没解除 → 批次置为 `paused_risk` 并退出，不再领取任何任务。
   重跑同一命令时，若退避已满 `BILI_RISK_RESUME_COOLDOWN`（默认 30 分钟）会**自动继续**，
   否则提示还需等待多久；也可确认已登录后手动 `maintenance.py resume-batch`。

脚本层（`bilibili_transcript.sh`）拉元数据时会把 yt-dlp 的 stderr 留档到
`state/logs/.last_meta_error`，并输出机器可读的 `[BILI-META-FAIL] reason=risk_control|network`——
原因被吞掉过一次，结果就是 90 多条任务各自在 30 秒内把重试撞完。

## 超长音频：默认快速失败（拍板结论），分段是可选项

faster-whisper 对整个音频数组一次性算 log-mel 声谱图，需求随时长线性增长：
**需显存 ≈ 音频分钟数 × 0.018 GiB + 模型常驻**（实测锚点：420 分钟报
`Unable to allocate 7.55 GiB for an array with shape (1, 2520906, 201)`）。

**默认策略是快速失败**：超阈值直接判「本机不可转」并说明原因，不切段、不退回 CPU 空磨。
实测 6 小时音频在加载模型之前就退出（1.8 秒），完事在清单里换机：

```
⛔ 本机不可转：6小时0分 音频需约 8.1 GiB 显存，当前空闲仅 5.7 GiB（阈值 5.5 GiB）
   ——请关掉占用显存的程序后重试，或按「无法转录清单」换机；
   如确要本机跑完，可设 BILI_ASR_CHUNKING=1 启用分段转写（切段有边界断词风险，需自行复核）
```

| 变量 | 默认 | 作用 |
|------|------|------|
| `BILI_ASR_CHUNKING` | 关 | 置 `1` 启用分段转写（切段+时间轴平移拼接）；默认关闭 |
| `BILI_ASR_MEMORY_BUDGET_GB` | GPU 取空闲显存的 85%，CPU 取 8 | 预算，超出即快速失败（开了分段则改为切段依据） |
| `BILI_ASR_MAX_CHUNK_SECONDS` | 1800 | 开启分段后的单段上限（秒） |
| `BILI_ASR_CPU_BUDGET_SECONDS` | 10800 | CPU 路径耗时预算；预估超过即快速失败，不再无声空磨数小时 |

分段转写的实测（2026-09-18，供复核时判断取舍）：10 分钟真实音频切 6 段，136 段结果
时间轴严格递增、末段结束 600.06s 对得上音频 600.1s；6 小时真实音频切 12–13 段，
整段需 21.1 GiB 的峰值被压到 3 GiB 以内，首段 19 分钟跑完且无 OOM。
本机 medium + beam5 + 逐词时间戳的实测速率约 **0.7 倍实时**（27.7 分钟音频用 19 分钟），
所以 6 小时音频即使分段也要约 4 小时，且过程中 GPU 约 81℃、受功耗墙限制。

## 进程被杀后的僵尸条目回收

进程被杀（断电、关终端）后留下的 `running` 条目，会在下次启动时按心跳超时自动回收重跑，
也可手动触发：

```bash
.venv/Scripts/python.exe scripts/maintenance.py recover-stale
```

⚠️ 两个坑（2026-09-18 实测）：回收窗口是 **2h5m**，所以「刚被杀就重启」时那条条目心跳
还很新、**不会被回收**，会静默跳过——此时必须显式 `recover-stale --seconds 60`。
反过来，**批次进程还活着时不要手工回收**：长驻脚本持有内存状态、每次写回都是整份覆盖，
手工改的会被冲掉。

## 断电/意外中断：看门狗自动续跑

2026-09-18 电脑 14:37 意外断电，机器 14:39 就起来了，但没人拉起批次，白等 27 分钟才被
手工续上；当晚又出现两次**静默死亡**（日志停在「开始转录」某行，无异常回溯、无应用错误
事件、无驱动重置、机器整晚没重启），16:14 与 21:00 各一次，都是看门狗自己发现并续跑的。
`scripts/watchdog_resume.py` 把那一步自动化，由计划任务在**登录时**与**每 5 分钟**调用：

- **只续跑已存在的批次**——没有状态文件、批次已跑完、或处于等登录的 `paused_auth`
  一律不动，绝不自作主张开一批新的；
- 判定前先确认**没有任何批次进程存活**（命令行匹配 `batch_transcribe`，venv 转发壳与
  其子进程都会命中），确认已死才回收僵尸条目、再以独立进程续跑；
  风控暂停要等冷却满 30 分钟才续，避免刚暂停就被拉起来白撞一轮；
- 存活判定**不看状态文件新鲜度**：单条长音频转写期间状态可以几十分钟不写一次，
  用新鲜度当存活信号会把正在跑的批次判成僵尸，拉起来就是重复干活；
- 判定集中在 `decide()`，34 项单测覆盖（拉起路径用替身脚本验证，不碰真实批次）；
- **续跑频率上限**：一小时内最多续跑 3 次。每次续跑都会让批次重扫一遍收藏夹（打 B站
  API），所以「反复死→反复拉→毫无进展」的崩溃循环必须停下来等人看，不能一直空烧；
- 续跑时把**死前最后输出**一并记进日志，事后不必再翻旧日志找「死在哪一步」；
- 判定与动作追加写入 `state/logs/watchdog.log`。

```bash
# 注册（注册到当前用户，不需要管理员；换机后重跑一次脚本即可）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/register_watchdog.ps1
# 查看 / 卸载
Get-ScheduledTaskInfo -TaskName BilibiliTranscriptWatchdog
Unregister-ScheduledTask -TaskName BilibiliTranscriptWatchdog
```

**刻意不做网络探测**：断网时拉起批次只会让条目各失败 3 次（失败条目下次开批会被重排，
不丢数据），而误判「没网」却会让批次一直不恢复——后者代价大得多。

## 搬迁技能目录后：venv 启动器会静默死亡（2026-09-21 实测事故）

技能目录被移动后（本机 `Desktop\B站转录` → `.agents\skills\bilibili-auto-transcript`），
`python.exe` 照常工作（相对定位），但 `.venv\Scripts` 下 **pip console-script 启动器
（yt-dlp.exe 等 23 个）全部静默 exit=1**——exe 里烧死了安装时的绝对路径，找不到解释器，
stdout/stderr 一个字节都不吐。症状组合拳：每条任务 `BILI-META-FAIL reason=network`、
留档 `.last_meta_error` 为 **0 字节**、而手工拉同一 URL 完全正常（系统 PATH 上的另一份
yt-dlp 是健康的）、烧队列约 2 条/分钟。空 stderr 会被归类成 network，风控退避不触发，
看门狗还会把烧完的队列再拉起来烧——三连击。

修复不依赖 pip 索引（本机 pip 到 PyPI/镜像站当时都取不到包）：坏 exe 改名
`yt-dlp.exe.broken-stale-path`，在 `.venv/Scripts` 写两行 shim `yt-dlp.cmd`
转发 `"%~dp0python.exe" -m yt_dlp %*`——模块本体在 site-packages，不受搬迁影响。
验证：`yt-dlp.cmd --version` 有版本号 + `--dump-json` 拉一条健康视频出 JSON。

**凡是移动技能目录或换机，三件套都要重做**：重建启动器（上述 shim 或联网后
`pip install --force-reinstall`）、重跑 `register_watchdog.ps1`（计划任务同样烧死旧
绝对路径，不重注册就是每天一次静默 no-op）、以及全库 grep 旧路径。

## 转录写库的版本语义与质量分口径

主流水线第一步调用 `scripts/record_transcript.py`，一次转录 = 一个新版本：

- `transcripts` 行更新为本次结果（唯一键 `(bvid, page)`）；
- `transcript_versions` 追加 `kind='raw'`、`is_current=1` 的新版本，该键其它版本置 `is_current=0`；
- **旧版本一律保留**，不删除不覆盖，因此重转录可回溯；
- 若 Whisper 产出了分段侧车（`<stem>.segments.json`），同时写入 `transcript_segments`，
  并由分段与指标算出 `quality_score` 写在该版本上。

> **质量分的覆盖口径**：`quality_score` 依赖分段侧车，而侧车只在 Whisper 转写路径产出。
> 走 CC／AI 字幕的转录没有分段，因而没有质量分（值为 NULL，状态 `no_segments`）。
> 历史归档也没有侧车（归档只保留纯文本正文），**因此质量分只对启用分段侧车之后的新转录生效，不会回填**——
> 回填需重下音频并重跑 Whisper，代价远高于收益。查询高分作品时请以 `quality_score IS NOT NULL` 限定范围。

单独调用（补跑某条、或手工指定正文）：

```bash
.venv/Scripts/python.exe scripts/record_transcript.py --bvid BVxxx --page 1     --meta meta.json --text-file text.txt [--segments x.segments.json] --json
```

## 批量运行细则

- 断点续传、失败重试、标题校验自动完成；扫描前读取既有 `state/processed_videos.txt`；
- 去重文件 `state/processed_videos.txt`：第 1P 写裸 `BVxxx`，其余 P 写 `BVxxx_PN`；
- 报告 CSV（`state/transcript_report.csv`）含 `bvid/page/cid/part_title` 分P列；
- 归档落点由 `BILI_ARCHIVE_DIR` 决定（在 `.env` 里按需设置），
  成功后在库中同时写入 `transcript_file` 指向归档路径；未设置该变量时归档到技能目录下的 `转录文件/已转录`；

## 版本化与补录细则（SKILL.md 分支 4 挪入）

- 元数据与正文都取自文件自身（文件名/头部链接/所在文件夹 → BV，`_Pnn`/分P序号 → 分P），**不联网、不重转**；
- 「原始稿」是 v1 且永不被覆盖，校对版作为 v2、v3… 派生版本保存；改动逐条记入 `correction_changes`；
- 两个入口都幂等、都默认 dry-run、都在 `--apply` 前自动备份数据库到 `.db/backups/`。

## 证据校正细则（SKILL.md 分支 5 挪入）

- `--download` 让入口自行用 yt-dlp 取视频（仅视频流、≤720p、用完即删）；
- **转录后的自动校正**由 `.env` 的 `BILI_EVIDENCE_CORRECTION=true` 开启（默认关）。
- 校正只允许在**有画面文字证据**的位置改动（同音字/错字、专有名词），标点与分段可无证据补齐；
  证据不足则不改，校正失败时不写任何版本。

## 证据校正的抽帧成本模型

抽帧成本 = 底帧（时长 × `BILI_OCR_BASE_FPS`）＋ 场景帧 ＋ 字幕带加密帧（受 `BILI_OCR_MAX_FRAMES` 约束）；
实测 9 分钟全程硬字幕视频约 1400 帧、25 分钟以上，长视频先用 `BILI_OCR_BASE_FPS` 收敛。
