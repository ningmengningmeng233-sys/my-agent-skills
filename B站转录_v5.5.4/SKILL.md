---
name: B站自动转录
description: "当用户要转录B站视频、提取字幕或转文字时使用本技能：单链接（含 ?p=N 分P）、多P、收藏夹批量、断点续跑批次、补摘要、双TXT交付、校正转录文本、证据校正、归档补录、换机入库；用户报告 CC字幕为空、没有字幕、AI字幕不可用、退回 Whisper、HTTP Error 412、-352 风控限速、登录态失效批次暂停、超长音频显存不足本机不可转、搬迁后 venv 启动器静默烧队列 这类症状同样适用。CC字幕/AI字幕/faster-whisper 三级自动降级，自动写 SQLite 与 AI 摘要。不适用：给长篇专著/独立文档建索引库、带文件名行号引用的问答（归「知识库转录」技能）、通用视频下载。"
homepage: https://clawhub.ai/54lynnn/bilibili-transcript
author: 柠檬才不萌
license: Proprietary
metadata:
  version: "5.5.4"
  openclaw:
    emoji: 📼
    requires:
      bins:
        - yt-dlp
        - ffmpeg
        - curl
    install:
      - id: venv
        kind: shell
        command: cd {{SKILL_DIR}} && python3 -m venv .venv && .venv/bin/pip install faster-whisper requests python-dotenv yt-dlp
        label: Setup virtual env & install faster-whisper
---

# 📼 B站视频转录 v5.5.4

## 概述

把单链接或收藏夹里的 B站视频转成 TXT + SQLite 记录 + AI 摘要。
**边界：只转录，不索引**——索引交给「知识库转录」技能。核心原则：数据库是唯一事实源，
旧版本永不覆盖，重跑幂等，维护操作默认 dry-run 且改前自动备份。

## 速查

| 我要做什么 | 命令/入口（技能目录内、`.venv` 解释器执行） | 完成判据 |
|---|---|---|
| 开工前自检 | `scripts/doctor.py --json`（维护走 `scripts/maintenance.py doctor/verify`） | error 项全绿 |
| 转录单链接（含 ?p=N 分P） | `bash scripts/bilibili_transcript.sh "<URL>"` | 输出 `[BILI-DONE]` + 新增 `(bvid, page)` 记录 |
| 收藏夹批量 | `scripts/bilibili_scanner.py` 预览，再 `scripts/batch_transcribe.py`（首次 `--limit 3`） | `state/transcript_report.csv` + 成功/失败计数 |
| 批次暂停后恢复 | 浏览器登录后 `scripts/maintenance.py resume-batch`，重跑批量命令 | 从断点继续，已完成不重跑 |
| 补摘要 | `scripts/fill_summaries.py`（`--stats/--dry-run`）或 `scripts/generate_summary.py --bvid … --page …` | 摘要状态不再是占位符 |
| 归档补录 / schema 迁移 | `scripts/import_archive.py` / `scripts/migrations.py`（默认 dry-run，`--apply` 前自动备份） | 预览确认后落库 |
| 双文件交付 | `scripts/render_delivery.py --bvid … --page …` | 主文件 + 原始记录两 TXT |
| 证据校正 | `scripts/correct_with_evidence.py --video …`（默认 dry-run） | 生成新派生版本 |

## 运行前提

- `.env`：`FAV_MEDIA_ID`（批量必填）、`BILI_ARCHIVE_DIR`（归档落点，按需自定）
- `.env` 三项 LLM 接口 `OPENAI_API_KEY`/`SUMMARY_API_URL`/`SUMMARY_API_MODEL`
  （任何 OpenAI 兼容接口），供 AI 摘要与受约束校正使用；未配置时转录照常，摘要留占位符。
- 依赖装在 `.venv`，版本锁定见 `requirements.lock`（含 Windows CUDA 12 运行时；缺库时 Whisper 自动退回 CPU）
- 浏览器（Firefox/Edge/Chrome）已登录 B站

## 分支 0：开工前自检

```bash
.venv/Scripts/python.exe scripts/doctor.py --json
```

error 项必须全绿；warning 表示能力降级但可继续（缺 CUDA 运行库时 Whisper 自动退回 CPU）。
数据库检查会同时报出 schema 版本、记录数、作品数与版本数。
`ocr` 一项检查画面文字证据引擎（RapidOCR），坏掉时主转录照常、校正降级，只告警不拦流程。

维护类操作统一走一个入口（体检、批次恢复、僵尸回收、库与归档校验）：
`scripts/maintenance.py doctor --json` / `scripts/maintenance.py verify --json`。

## 分支 1：转录一个链接

```bash
bash scripts/bilibili_transcript.sh "https://www.bilibili.com/video/BVxxxxx/"
bash scripts/bilibili_transcript.sh "https://www.bilibili.com/video/BVxxxxx/?p=3"
```

1. 运行上面的命令（不写 `?p` = 第 1P）。
2. 完成判据：输出出现 `[BILI-DONE]`（写库成功后才输出）；SQLite 新增 `(bvid, page)` 记录；TXT 文件名带 `_P01` / `_P02` 分P标识。

- 字幕自动降级：CC 字幕 → AI 字幕 → faster-whisper（三级含义与运行参数见 `references/design-decisions.md`）
- 配了 LLM 接口时自动生成摘要；未配置或调用失败则留占位符，之后走分支 3
- 同一 BV 每个 P 独立记录、独立 TXT；重跑同 BV 同 P 覆盖，不同 P 互不影响

## 分支 2：收藏夹批量转录

1. 预检：`.env` 有 `FAV_MEDIA_ID`；`EXPAND_PAGES=true` 才逐P展开（false 只转每 BV 第 1P）。
2. 预览队列（只读）：`.venv/Scripts/python.exe scripts/bilibili_scanner.py`，
   看 `new_videos`（待转）与 `deleted_videos`（已失效，自动跳过）。
3. 执行（首次先小批验收）：`scripts/batch_transcribe.py --limit 3`，验收后去掉 `--limit` 跑全量。
4. 完成判据：`state/transcript_report.csv` 生成，打印成功/失败计数；把成功/失败标题汇报给用户。

- 默认双槽并行（`--workers 1` 可改为串行）；每条视频用独立临时目录
- 断点续传、归档落点、去重与报告 CSV 细则见 `references/ops-incidents.md`「批量运行细则」

### 长批次停机与恢复（要点）

**登录态失效**置 `paused_auth` 停整批；**风控**（`HTTP 412`/`-352`）退避两轮未解除置 `paused_risk`；
**超长音频默认快速失败**。恢复统一入口：浏览器登录 bilibili.com → `scripts/maintenance.py resume-batch`
→ 重跑批量（已完成不重跑；风控默认冷却 30 分钟）。断电续跑由看门狗负责，只续已有批次。
机理见 `references/ops-incidents.md`。

## 分支 3：补摘要与数据维护

```bash
.venv/Scripts/python.exe scripts/fill_summaries.py --stats   # 或 --dry-run；去掉参数即执行
.venv/Scripts/python.exe scripts/generate_summary.py --bvid BVxxxxx --page 3
```

- 旧库升级：`migrate_pages.py`（默认 dry-run，`--apply` 前自动备份 DB）
- 旧 P1 补 `cid/part_title`：`backfill_page_meta.py`（默认 dry-run）
- 数据库结构由脚本自动迁移，**不要手改 `.db`**；schema 当前为 v4，五张表清单与字段见 `references/architecture.md`

## 分支 4：版本化与补录（数据库是唯一事实源）

```bash
# 把归档 TXT 里「有文件但库中无记录」的作品补录进库（默认只预览，--apply 前自动备份）
.venv/Scripts/python.exe scripts/import_archive.py --dry-run --json   # 确认后换 --apply
# schema 迁移：建版本表与改动表，并把「校对版」作为派生版本入库
.venv/Scripts/python.exe scripts/migrations.py --dry-run --json      # 确认后换 --apply
```

- 元数据取自文件自身（不联网、不重转）、原始稿永不被覆盖、幂等与 `--apply` 前自动备份等细则见
  `references/ops-incidents.md`「版本化与补录细则」。

## 分支 4.5：转录写库的版本语义

主流水线第一步调用 `scripts/record_transcript.py`，一次转录 = 一个新版本：
`transcripts` 按 `(bvid, page)` 更新，版本表追加 `is_current=1` 的原始稿，**旧版本一律保留**（重转录可回溯）；
Whisper 分段侧车写入 `transcript_segments` 并产出 `quality_score`（CC／AI 字幕与历史归档为 NULL、
不回填，查询以 `IS NOT NULL` 限定）。细则与单独调用示例见 `references/ops-incidents.md`。

## 分支 5：双文件交付与证据受约束校正

```bash
# 从数据库渲染「主文件 + 原始记录」双文件
.venv/Scripts/python.exe scripts/render_delivery.py --bvid BVxxxxx --page 1 --json

# 用画面文字（硬字幕）证据做受约束校正，产出新的派生版本（默认 dry-run，确认后 --apply）
.venv/Scripts/python.exe scripts/correct_with_evidence.py --bvid BVxxxxx --video 本地视频.mp4 --dry-run --json
```

- **主文件**是阅读交付物：有校对版时交付校对版正文，并在头部写明交付说明；
- **原始记录**（`..._原始记录.txt`）是证据层：列出全部版本的类型、字数、内容哈希、时间与来源文件，
  并附各版本全文，用于追溯「谁改了什么」；
- 校正只允许在**有画面文字证据**的位置改动，证据不足则不改，校正失败时不写任何版本；
- `--download` 自行取视频、转录后自动校正开关与抽帧成本模型见 `references/ops-incidents.md`「证据校正细则」。

## 常见坑

- 现象：进程被杀后立刻重启，那条 `running` 任务没被回收。成因：回收窗口 2h5m，心跳太新被静默跳过。处置：显式 `maintenance.py recover-stale --seconds 60`；批次进程活着时**不要**手工回收（整份覆盖会冲掉改动）。
- 现象：把「没配 Cookie」当成登录态失效。成因：无 Cookie 只打印 `[WARN] 无可用Cookie` 并退回 Whisper，不暂停；只有 `paused_auth` 才需登录后 `resume-batch`。处置：看 `state/batch_state.json` 的批次状态再决定。
- 现象：搬迁/换机后队列被高速烧光、`.last_meta_error` 为 0 字节、reason 全是 network。成因：venv 的 pip 启动器烧死旧绝对路径、静默 exit=1，空 stderr 被归类成 network，风控退避与看门狗连环误判。处置：执行「搬迁/换机三件套」（见 `references/ops-incidents.md`）。
- 现象：超长音频在本机无声空磨数小时。成因：整段 Whisper 显存随时长线性增长。处置：默认快速失败并换机；确需本机跑再设 `BILI_ASR_CHUNKING=1`（有边界断词风险，需复核）。
- 现象：查质量分全是 NULL。成因：分段侧车只在 Whisper 路径产出且不回填历史。处置：查询用 `quality_score IS NOT NULL` 限定。
- 现象：把索引/问答需求按旧文档转介给 "knowledge-rag"，用户找不到落点。成因：转介目标写了本机不存在的技能名（死路由）。处置：转介前先核对技能真实存在——索引与带行号问答一律归「知识库转录」技能（`tests/test_skill_discovery.py` 锁住此判据）。

## 参考（按需加载，不常驻上下文）

- `references/SETUP.md` — 安装、依赖、浏览器 Cookie、可选定时任务
- `references/architecture.md` — 架构说明
- `references/bilibili-fav-api.md` — B站收藏夹 API
- `references/design-decisions.md` — Whisper 选型、分P改造决策记录
- `references/ops-incidents.md` — 长批次事故实录：风控/超长音频/看门狗/搬迁

## 完成标准

一次转录任务只有同时满足才算完成：**TXT 已渲染 + DB 已写入 + 摘要状态明确（已生成或占位符留待补）**。批量任务还必须生成 CSV 并汇报列表。涉及数据维护时另加两条：**改动前有备份**、**改动后 `doctor.py` 全绿且 `tests` 全过**（`python -m unittest discover -s tests -t .`）。
