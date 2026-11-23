# Scan-Script / Cronjob / Agent 三体架构

这是 bilibili-auto-transcript 使用的核心架构模式，适用于任何需要**定时扫描外部源 → 发现增量 → 由 AI 处理并通知**的任务。

## 架构示意

```
定时触发 (cronjob / 系统定时器)
    ↓
扫描脚本 (bilibili_scanner.py)
    → 调用外部 API 获取数据
    → 读取本地已处理记录 (processed_videos.txt)
    → 发现增量 (取差集)
    → stdout 输出新条目 (结构化文本)
    ↓ stdout
AI Agent（加载 skill）
    → 读取 stdout 中的新条目
    → 对每个新条目执行处理流程
    → 更新已处理记录
    → 通知用户
```

## 三个组件

### 1. 扫描脚本 (Scanner)
- 职责：只做"发现"，不做"处理"
- 执行方式：快速（通常 <1 秒），通过 cronjob 定时触发
- 输出格式：结构化的纯文本标记，方便 agent 解析
  - `NO_NEW_ITEMS` / `ALL_CAUGHT_UP` — 无增量，agent 不做事
  - `NEW_ITEMS:N` + 逐条详细信息 — 有增量，agent 启动处理
- 状态维护：维护一个 `processed_xxx.txt` 文件，每行一个唯一 ID

### 2. 定时触发器 (Cronjob/Scheduler)
- OpenClaw 内置 cronjob：
  - `no_agent=False`（默认）：script stdout 注入 prompt，agent 按 prompt 处理
  - `script=bilibili_scanner.py`（相对路径解析到 `~/.openclaw/workspace/skills/bilibili-auto-transcript/scripts/`）
  - `skills=[...]`：加载相关 skill 提供上下文
  - 无增量时 agent 只回复一句"无新内容"，不打扰用户
- 外部定时器：
  - 用 Linux systemd timer、crontab 等触发扫描

### 3. AI 处理器 (Agent)
- 职责：处理新条目（分析、转换、生成、通知）
- 输入：扫描脚本的 stdout
- 输出：最终交付给用户的结果（文件、消息、摘要等）
- skill 提供处理步骤的完整定义

## 适用场景

这个模式适用于：
- 监控网站/API 的新内容（博客、新闻、社交平台）
- 定时检查文件系统变化
- 定时抓取数据并生成报告
- 任何"定期巡检 → 发现增量 → AI 分析 → 通知"的工作流

## 关键设计原则

1. **扫描脚本必须快** — 它只是 API 调用 + 集合比对，不应包含耗时操作
2. **增量记录用唯一 ID** — 用数据集的原生 ID（如 avid），不用文件名或标题，避免重名/改名导致重复处理
3. **不重复不遗漏** — 处理完立即记录 ID，agent 中途失败也不会漏掉
4. **无增量时静默** — 用户只在有变化时才收到消息
5. **自愈** — 脚本/agent 处理失败不阻塞后续扫描，下次运行时重试发现

---

# 追加：v5.5.0 的数据结构与入口（2026-09-16）

## 数据库 schema v3

| 表 | 作用 |
|---|---|
| `transcripts` | 作品当前记录，唯一键 `(bvid, page)`。**入库文本是原始稿**，本表不被派生版本覆盖 |
| `transcript_versions` | 版本层：`v1` 为原始稿（`is_current=1`），`v2+` 为派生版本（校对版／证据校正版） |
| `correction_changes` | 逐条校正改动：原文、改后、证据（JSON）、置信度。**没有证据就没有改动记录** |
| `schema_migrations` | 已应用的 schema 版本，使迁移幂等 |

数据流是单向的：**库 → 文件**。`render_delivery.py` 从库重建「主文件 + 原始记录」，
`import_archive.py` 是唯一的反向通道（把归档 TXT 补录回库，用于库损坏后的重建）。

## 入口一览

| 脚本 | 作用 | 是否有写操作 |
|---|---|---|
| `doctor.py` | 11 项开工自检 | 否（只探测与建目录） |
| `bilibili_transcript.sh` | 单条转录（CC→AI 字幕→Whisper 三级降级） | 写库、写 output、归档 |
| `batch_transcribe.py` | 收藏夹批量（串行/双槽） | 同上 |
| `import_archive.py` | 归档 TXT 补录进库 | 默认预览，`--apply` 才写，写前备份 |
| `migrations.py` | schema 迁移与版本回填 | 同上 |
| `render_delivery.py` | 双文件交付渲染 | 写文件，不改库内容 |
| `correct_with_evidence.py` | 画面文字证据受约束校正 | 默认预览，`--apply` 追加派生版本 |

## 证据受约束校正的两个适配（与抖音版的差异）

1. **BV 与分P的判定链**：文件名 → 头部「B站链接」→ 所在文件夹名；页号：文件名 `_Pnn` → 头部「分P序号」→ 默认 1。
2. **证据挂接依据**：B站 的库不保存分段时间轴，故用 `align_evidence_by_text`
   按窗口相似度把硬字幕挂到文本块上（证据强度阈值与抖音版一致）。

## 两条不可动摇的不变量

1. **原始稿永不被覆盖**：任何校正都只追加派生版本，`transcripts.transcript_text` 与 `v1` 保持原样。
2. **写库前必备份**：`import_archive.py` 与 `migrations.py` 在 `--apply` 前自动备份到 `.db/backups/`，备份失败即中止。

## 追加：v5.5.0 分段层与写库路径（2026-09-16 批次 P5/P6）

### schema v4 增补

| 表／列 | 作用 |
|---|---|
| `transcript_segments` | 每段的起止时间、文本、`avg_logprob`、`no_speech_prob`，归属到具体版本 |
| `transcript_versions.quality_score` | 该版本的质量分（由分段与指标算出；无分段时为 NULL） |
| `transcript_versions.metrics_json` | 该版本的指标（duration／segment_count／speech_ratio／language…） |

> 质量分只覆盖 Whisper 转写路径：CC／AI 字幕没有分段侧车，历史归档也只留纯文本正文，
> 故该列对 2026-09 之前入库的记录与全部字幕来源记录为 NULL，且**不做回填**（回填需重跑 Whisper）。

### 「一次转录 = 一个新版本」

写库路径从「内联更新一行」改为 `scripts/record_transcript.py`：

```
transcripts 行          ← 本次结果（唯一键 bvid+page）
transcript_versions     ← 追加 kind='raw'、is_current=1；旧版本一律保留
transcript_segments     ← 该版本的分段（若 Whisper 产出了侧车）
quality_score/metrics   ← 该版本的评分与指标
```

分段侧车由 `scripts/fw_whisper_main.py` 与 TXT 同目录同名写出（`<stem>.segments.json`）。

### 可选第四步：画面文字证据校正

`bilibili_transcript.sh` 的第四步由 `.env` 的 `BILI_EVIDENCE_CORRECTION=true` 开启；
入口 `scripts/correct_with_evidence.py` 支持 `--download`（yt-dlp 取仅视频流，用完即删）。
抽帧成本与三个旋钮见 `SKILL.md` 分支 5 与 `.env` 注释；该阶段任何失败都不影响转录结果。

