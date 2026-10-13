# 📼 Bilibili Auto Transcript v5.5.4

B站视频转录与收藏夹批量处理：CC字幕 → AI字幕 → Whisper 三级降级，自动写 SQLite 与 AI 摘要，支持多P（`?p=N`）。

## 快速开始

```bash
# 单链接（多P：URL 带 ?p=N）
bash scripts/bilibili_transcript.sh "https://www.bilibili.com/video/BVxxxxx/?p=3"

# 收藏夹批量（EXPAND_PAGES=true 时逐P展开；--limit N 小批验收）
.venv/Scripts/python.exe scripts/batch_transcribe.py --limit 3
.venv/Scripts/python.exe scripts/batch_transcribe.py

# 补摘要 / 统计
.venv/Scripts/python.exe scripts/fill_summaries.py --stats
```

Windows 用 `.venv\Scripts\python.exe`；Linux/macOS 用 `.venv/bin/python3`。

## 功能

- 三级字幕降级，faster-whisper 优先用 CTranslate2 CUDA float16 / GPU small，CPU int8 自动回退
- 批量默认双槽、每任务独立工作目录，成功后归档到 `转录文件/已转录/年/月`
- 收藏夹扫描、断点续传、失败重试、标题校验、CSV 报告
- 多P支持：DB 唯一键 `(bvid, page)`，文件带 `_P01/_P02`，批量可逐P展开
- AI 摘要；失效视频自动跳过

## 配置

`cp .env.example .env`：`FAV_MEDIA_ID`（批量必填）、`EXPAND_PAGES`。可用 `BILI_ARCHIVE_DIR` 自定归档根目录，`BILI_WHISPER_MODEL` 自定模型。

AI 摘要与受约束校正走 `.env` 的三项：`OPENAI_API_KEY`、`SUMMARY_API_URL`、
`SUMMARY_API_MODEL`（任何 OpenAI 兼容接口都可用）。未配置时转录照常，摘要留占位符。

## 文档

- `SKILL.md` — agent 执行步骤（分支：单链接 / 批量 / 补摘要）
- `references/SETUP.md` — 安装、Cookie、可选定时任务
- `references/design-decisions.md` — Whisper 选型与 v5.3 分P决策
- `references/architecture.md`、`references/bilibili-fav-api.md`

## 结构

```
scripts/
  bilibili_transcript.sh   核心转录引擎（CC→AI→Whisper）
  bilibili_scanner.py      收藏夹扫描（分P展开、404跳过）
  batch_transcribe.py      批量调度（--limit、断点续传、CSV）
  watchdog_resume.py       看门狗：意外中断后回收僵尸并自动续跑（配 register_watchdog.ps1）
  register_watchdog.ps1    注册看门狗计划任务（登录时 + 每 15 分钟，注册到当前用户）
  asr_chunk.py             显存/耗时预算与可选分段转写（超阈值默认快速失败）
  generate_summary.py      单条摘要（--bvid/--page）
  fill_summaries.py        批量补摘要
  correction.py            受约束校正（画面文字证据）
  transcript_db.py         SQLite（(bvid,page) 唯一键、自动迁移）
  migrate_pages.py         v5.2→v5.3 迁移助手
  backfill_page_meta.py    旧 P1 补 cid/part_title
```

## 分发给他人

本仓库的**已跟踪文件**即全部可分发内容；个人数据（`.env`、`.db/`、`output/`、`state/`、
`llm_nodes.json`、`.venv/`）都在 `.gitignore` 里，不会被带出。要产出一个干净的压缩包：

```bash
mkdir -p dist
git archive --format=zip -o dist/bilibili-auto-transcript-5.5.4.zip HEAD \
  -- SKILL.md README.md requirements.lock .env.example .gitattributes .gitignore \
     .githooks scripts references tests
```

- 用 `git archive` 而不是手打包，是因为它**只导出已跟踪文件**，不会把本机的库、产物、密钥捎出去。
- 显式列出路径可顺带排除 `文档/`（项目内部的设计与交付记录，不属于技能本体）。
- 接收方按 `references/SETUP.md` 建 `.venv`、装 `requirements.lock`、`cp .env.example .env`
  即可使用；`scripts/doctor.py` 会告诉他缺什么。

## 许可

MIT
