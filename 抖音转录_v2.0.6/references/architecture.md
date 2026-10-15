# 架构与数据流（v2）

## 分层与职责边界

```
用户对话 / TXT / Markdown / CSV
   │
   ▼
SKILL.md 行为规范（AI 只调 CLI，读一行 JSON）
   │
   ├─ input_normalizer.py   链接提取与归一化（modal_id→标准作品页、拒绝相似域）
   ├─ job_queue.py          batches/jobs 持久队列、心跳、暂停与恢复、预览阈值
   ▼
douyin_transcript.py   单条编排器 process_one(job, ServiceBundle)
batch_transcribe.py    批量串行入口（预览/续跑/重试/逐条报告）
maintenance.py         补 AI、重渲染、续跑、重处理、版本回退
   │
   ├─ ingest_runner.py ──子进程──► douyin-ingest v0.3.0（CLI 边界，不 import 内部模块）
   │      ▲ 唯一被允许触碰抖音平台的层：短链、Playwright 会话、扫码登录、
   │        元数据、口播音轨准备（DOUYIN_INGEST_HOME=state/ingest 隔离登录态）
   ├─ ingest_adapter.py     上游 JSON 契约校验 + 临时视频上下文（repr=False，只驻留内存）
   ├─ video_media.py        临时视频下载（仅 http(s)、媒体类型、原子落地、FFprobe）
   ├─ media.py / audio_pipeline.py  音频验证、响度/削波分析、按需增强、UVR 人声分离
   ├─ asr.py                faster-whisper 单轮 attempt（策略参数全量透传）
   ├─ quality.py            集中阈值、六项加权扣分、三轮策略决策
   ├─ ocr.py                自适应抽帧（1fps + 场景补帧 + 字幕 4fps）与文字分类
   ├─ diarization.py        匿名说话人分离与 ASR 时间对齐（保守发布）
   ├─ evidence_fusion.py    画面文字与 ASR 对齐，只让稳定同步字幕成为修正证据
   ├─ correct_transcript.py 受约束 AI 校正（JSON 契约 + 变更记录）
   ├─ generate_summary.py   三种摘要模式（normal / restricted / visual_text_only）
   ├─ artifacts.py          双文件渲染 + 事务式发布（tmp→replace→DB）
   ├─ transcript_db.py      SQLite 唯一事实源：v2 多表 + 发布事务 + 复用判定
   └─ migrations.py         v1→v2 幂等迁移与备份
```

## 数据流（阶段只前进，可跳过但不可回退）

```
resolving → fetching → media_ready → analyzing → transcribing
→ ocr → diarization → correcting → summarizing → publishing
                                    └─ 任意阶段 → failed / auth_required / retry_wait
```

```
复用判定(reuse|render_only|fill_ai|reprocess|fetch)
  → ingest（匿名无头；认证失败→暂停批次）
  → create_staging_version（videos + transcript_versions，不改 current）
  → verify_audio → analyze_audio → 音轨候选（原音/增强/vocals）
  → 最多三轮 ASR：每轮写 asr_attempts，质量评分决定是否继续
  → select_attempt（raw_text = 选中轮次文本，原始稿从此不可变）
  → OCR（需要视频素材，失败只禁用 OCR）→ 写 ocr_observations
  → diarization（失败只降级）→ 写 speaker_turns + 段落说话人引用
  → align_evidence → AI 校正（失败→pending）→ 写 correction_changes
  → 摘要（按质量选模式，失败→pending）→ 写 transcript_versions.summary*
  → 渲染主文件 + 原始记录文件（同目录 .tmp）→ 校验哈希 → 原子 replace
  → 一个 DB 事务：旧版本 is_current=0、新版本 is_current=1、登记 artifacts
  → 清理 job 媒体 → 一行 JSON（schema_version=2）
```

## 核心不变量

- **SQLite 是唯一事实源**：任何 TXT 都能由数据库重建（`repair-artifacts`）。
- **原始证据不可覆盖**：`asr_attempts.segments_json` 与 `raw_text` 一经写入不再被 AI
  或 OCR 改写；AI 校正只写 `corrected_text` 与 `correction_changes`。
- **先保存、后发布**：新版本完整校验通过前 `videos.current_version_id` 不变；
  任一步失败都回滚文件并保住旧 current。
- **质量驱动重算**：不固定跑三轮；只有出现具体问题才启动下一轮，最后一轮没有默认优先权。
- **局部降级**：人声分离 / 增强 / OCR / 说话人 / 摘要任一失败都只记录 `degradations`。
- **内容不是指令**：分享文案、标题、口播、字幕、截图文字全部是不可信数据，
  校正与摘要的 system prompt 都明确声明不得执行素材中的任何指令。

## 为什么这样设计

- **两次去重**：短链在调用上游前拿不到作品 ID，所以入库前按 `candidate_aweme_id`
  查一次，拿到真实 `aweme_id` 后再查一次；命中的任务标 `duplicate` 并指向主任务。
- **staging 版本**：把“处理中间结果”与“正式交付”分开，失败不留半成品。
- **媒体为临时资产**：成功与终态失败都删除；只有可恢复的中断才保留经验证的资产。
- **模型自适应**：CUDA 需 CTranslate2 float16 能力 + cublas/cudnn DLL 可加载
  （由 `nvidia-*` wheel 提供，免装 Toolkit）；显存 ≥6GB→medium，<6GB→small；
  CPU ≤30min→base/int8；更长→tiny/int8；`DOUYIN_WHISPER_MODEL` 可覆盖。
  GPU 推理互斥靠 `state/gpu.lock`（msvcrt/fcntl）。
- **保守说话人**：少于两个稳定聚类不加标签；总时长 <2 秒的聚类不算新人物；
  重叠或覆盖不足 60% 时留空——宁可不标，也不误标。
- **安全边界**：错误 JSON 与日志不含密钥、Cookie、登录态路径与媒体直链；
  上游未校验字段一律拒收（`ingest_contract_changed`）。

## 数据模型

| 表 | 作用 |
|---|---|
| `schema_migrations` | 迁移版本 |
| `videos` | 作品级元数据与 `current_version_id` |
| `batches` / `jobs` | 持久队列：状态、阶段、checkpoint、心跳、去重指向 |
| `transcript_versions` | 版本：原始稿、校正版、摘要、质量、签名、`is_current` |
| `asr_attempts` | 每轮 ASR 的策略、指标、得分、硬异常 |
| `ocr_observations` | 画面文字与分类（`speech_subtitle` 等） |
| `speaker_turns` | 匿名说话人时间段 |
| `correction_changes` | 逐段修正记录（原文、校正文、证据、置信度） |
| `artifacts` | 主文件与原始记录文件的路径、哈希、大小、当前标记 |

约束：`UNIQUE(aweme_id, version_no)`、`UNIQUE(version_id, attempt_no)`；
每个作品最多一个 `is_current=1` 的版本；每个版本每种 artifact 最多一个当前项。

## 模块清单

| 文件 | 职责 |
|---|---|
| `paths.py` | 由 `__file__` 推导的根目录与运行路径 |
| `models.py` | v2 枚举、`NormalizedInput`/`JobRecord`/`BatchPreview`、状态迁移规则 |
| `logger.py` | stderr + `state/logs/transcript.log`，可注入日志路径与注册式脱敏 |
| `input_normalizer.py` | 文本/文件导入、URL 归一化、上限与编码错误 |
| `job_queue.py` | 队列读写、领取、心跳、暂停恢复、重试排队 |
| `migrations.py` | v1→v2 幂等迁移、`backup_database`、`file_sha256` |
| `transcript_db.py` | v2 repository、发布事务、复用判定、artifact 校验 |
| `ingest_adapter.py` / `ingest_runner.py` | 上游契约与分类重试 |
| `video_media.py` / `media.py` | 视频素材与音频验证、受限清理 |
| `audio_pipeline.py` / `model_assets.py` | 音频分析、增强、人声分离、模型清单 |
| `asr.py` / `quality.py` | 单轮 attempt 与质量评分/三轮决策 |
| `ocr.py` / `diarization.py` / `evidence_fusion.py` | 画面文字、说话人、证据对齐 |
| `correct_transcript.py` / `generate_summary.py` | 受约束校正与三种摘要 |
| `artifacts.py` / `render_txt.py` | 双文件渲染与事务发布（后者为兼容包装） |
| `douyin_transcript.py` / `batch_transcribe.py` / `maintenance.py` | 编排、批量与维护入口 |
| `doctor.py` | 12 项体检，每项带 `fix_command` 与 severity |
