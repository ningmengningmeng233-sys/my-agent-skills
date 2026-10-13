# 故障处置（按错误码与症状）

先跑体检拿全局结论：

```powershell
.venv\Scripts\python.exe scripts\doctor.py --json
```

每条错误都按三段看：**系统自动做了什么 / 你需要做什么 / 数据是否安全**。

约定：`重下` = 需要重新从抖音获取媒体；`重转` = 需要重新跑语音识别；
`重建` = 只从 SQLite 重渲染交付文件（不下载、不识别）。

## 按 JSON `error.code`

| code | 系统自动做什么 | 你需要做什么 | 是否重下/重转 | 数据是否安全 |
|---|---|---|---|---|
| `invalid_input` | 直接拒绝，不访问抖音 | 重发完整分享文案或作品链接；多条改用批量入口 | 否 | 安全，无任何写入 |
| `input_encoding_error` | 拒绝该文件，不解析 | 把清单另存为 UTF-8 或 UTF-8 BOM 后重试 | 否 | 安全 |
| `batch_confirmation_required` | 建好批次但不领取任何 job | 把预览数量与预估时间告知用户，得到同意后加 `--confirm` 或 `--resume <batch_id>` | 否 | 安全，job 已在库中 |
| `login_required` | 暂停整个批次（`paused_auth`），后续 job 不再领取 | 用 `--visible` 打开专用 Chromium 扫码，然后 `--resume "<batch_id>"` | 否 | 安全，已完成项不重跑 |
| `video_unavailable` | 只终结这一条，不盲目重试 | 在抖音 App 里确认作品是否被删/私密/下架 | 否 | 安全，其他条目继续 |
| `platform_rate_limited` | 已按 1 秒/3 秒退避重试 3 次 | 过一会儿再跑；避免短时间大量提交 | 否 | 安全 |
| `ingest_timeout` | 已重试 3 次 | 检查网络与代理；稍后重试 | 否 | 安全 |
| `ingest_contract_changed` | 立即可疑：不重试，保留安全日志 | 先跑 `doctor.py` 确认 `douyin-ingest` 仍是 v0.3.0；若上游已升级，需按 `references/SETUP.md` 重新验收再改锁定版本 | 否 | 安全 |
| `media_download_failed` | 丢弃半成品，只禁用依赖视频的能力（OCR） | 通常无需处理；持续出现检查网络 | 否（原音转录继续） | 安全 |
| `audio_invalid` | 该条标记失败并清理媒体 | 确认 `ffprobe -version` 可用；作品确实无音轨时属正常 | 否 | 安全 |
| `video_invalid` | 只禁用 OCR，其余继续 | 无需处理 | 否 | 安全 |
| `audio_enhancement_failed` | 放弃增强音轨，退回原音 | 无需处理；若日志反复出现检查 FFmpeg | 否 | 安全 |
| `asr_failed` | 最多三轮后仍失败才终结；已尝试其他音轨/参数 | 看 `state\logs\transcript.log` 的档位与错误；确认磁盘空间；纯音乐视频本就没有口播 | 重转（`--force`） | 安全，不覆盖历史版本 |
| `ocr_failed` | 局部降级，主流程继续，`degradations` 记录 | 无需处理；必要时 `model_assets.py --json` 看模型是否缺失 | 否 | 安全 |
| `diarization_failed` | 局部降级，不输出说话人标签 | 无需处理；缺模型时按 SETUP 补齐 | 否 | 安全 |
| `correction_pending` | 交付原始稿，主文件标注“校正版待补” | 网络恢复后 `maintenance.py fill-ai --json` | 否（只补 AI） | 安全 |
| `summary_pending` | 交付原始稿，摘要位置显示待补提示 | 同上，用 `fill-ai` 补 | 否（只补 AI） | 安全 |
| `database_failed` | 不报告成功；旧版本保持不变 | 确认没有其它进程占用 `.db\`；不要手工删库 | 否 | 安全，必要时用 `.db\backups\` 副本 |
| `publish_failed` | 回滚文件，旧 current 版本与旧文件保持不变 | 检查磁盘空间与输出目录权限；修好后用 `maintenance.py repair-artifacts --json` | 否（只重建） | 安全 |
| `artifact_missing` | 判定“文件缺失或哈希不符” | 跑 `maintenance.py repair-artifacts --json` 从数据库重建两个文件 | 否（只重建） | 安全，原稿在库里 |

## 复用判定：缺 TXT 时到底会发生什么

v2 起交付是**两个文件**（主文件 + `_原始记录.txt`），复用判定按实测文件状态决定：

| 情况 | `reuse_action` | 实际动作 |
|---|---|---|
| 两个文件都在且哈希一致、AI 已完成 | `reuse` | 直接复用，不下载不转写 |
| 原稿在库里但文件缺失/被外部改动 | `render_only` | **只从 SQLite 重新渲染**两个文件，不下载不转写 |
| 原稿在、校正或摘要待补 | `fill_ai` | 只补 AI 内容并重渲染 |
| 质量未达标且管线签名落后 | `reprocess` | 需要显式重处理（`reprocess-review` + `--resume --force`） |
| 没有可用版本 | `fetch` | 正常采集流程 |


## 登录 / 浏览器

- 首次运行报浏览器缺失：
  `.venv\Scripts\python.exe -m playwright install chromium`
- 扫码后仍 `login_required`：登录态可能过期，但**不要手工解析或复制**
  `state\ingest\` 里的文件；直接用 `--visible` 重跑再扫一次。删除该目录
  只会丢失登录态，不影响转录历史库。
- 绝不要把 `state\ingest\`、`.env`、Cookie 文本发给任何人或粘贴到对话里。

## 网络与代理

- pip / `douyin-ingest` 报 `ProxyError` 或连不上 GitHub：当前 shell 有失效代理。
  在会话里临时清除 `$env:ALL_PROXY=''`，或指向确实在运行的代理端口。
- 摘要或校正接口失败不阻断转录：会留在 `summary_status=pending` /
  `correction_status=pending`，网络恢复后跑
  `scripts\maintenance.py fill-ai --json` 补。

## GPU 与模型

- 两个任务同时跑不会互踩：CUDA 推理串行（`state\gpu.lock`）。
  若怀疑死锁，确认没有残留转录进程后删除 `state\gpu.lock`。
- GPU 需要 NVIDIA 驱动 + CUDA 12 运行时（cublas/cudnn DLL）。本 Skill 通过
  `requirements.lock` 里的 `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` wheel 提供
  运行时，无需安装 CUDA Toolkit。只装驱动不装本仓库依赖时会自动回退 CPU。
- 档位自适配：GPU 显存 ≥6GB → `medium/float16`；<6GB → `small/float16`；
  CPU 且 ≤30 分钟 → `base/int8`；CPU 长视频 → `tiny/int8`；
  `$env:DOUYIN_WHISPER_MODEL='small'` 可强制覆盖。日志会输出
  `📐 语音识别档位` 说明选择原因。
- `asr_oom`（显存不足）会自动降级到 CPU 并重跑一次，`degradations` 里能查到。
- 模型判据：`model_assets.py --json` 的 `states` 全部 `ok`，
  或只缺可选模型（warning，对应能力降级）。

## 中断与恢复

```powershell
# 把心跳过期的 job 放回队列
.venv\Scripts\python.exe scripts\maintenance.py recover-interrupted --json
# 然后继续批次
.venv\Scripts\python.exe scripts\batch_transcribe.py --resume "<batch_id>" --json
```

## 验收自检（一条链三步）

```powershell
.venv\Scripts\python.exe scripts\douyin_transcript.py "<链接>" --json         # 期望 completed
.venv\Scripts\python.exe scripts\douyin_transcript.py "<链接>" --json         # 期望 reused，无新下载
.venv\Scripts\python.exe scripts\douyin_transcript.py "<链接>" --force --json  # 期望新的 completed
```

成功后 `state\jobs\` 里对应 job 目录不应残留任何 `.mp4/.m4a/.mp3/.wav/.jpg`。

## 仍解决不了时

收集并安全分享：`state\logs\transcript.log`（已脱敏）、`doctor.py --json` 输出、
失败的 `error.code`、`maintenance.py repair-artifacts --json` 的结果。
不要分享 `.env`、`state\ingest\` 或数据库文件本身。
