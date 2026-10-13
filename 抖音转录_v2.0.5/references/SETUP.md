# 首次安装（SETUP）

`<SKILL_DIR>` 指本 skill 所在目录（例如 `C:\…\skills\douyin-auto-transcript`）。
以下命令都在该目录用 PowerShell 执行。

## 1. 虚拟环境与 Python 依赖

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.lock
```

Python 最低且目标版本都是 **3.12**。`requirements.lock` 固定：
`faster-whisper`、`python-dotenv`、`requests`、`rapidocr==3.9.2`、
`onnxruntime==1.30.0`、`sherpa-onnx==1.13.8`、Windows 专用的
`nvidia-cublas-cu12` / `nvidia-cudnn-cu12`，以及上游抖音采集器
`douyin-ingest @ v0.3.0`（不得跟随上游 main）。

如果本机走了失效代理导致 pip 报错，先在当前会话临时清掉代理变量再安装：

```powershell
$env:ALL_PROXY=''; $env:HTTP_PROXY=''; $env:HTTPS_PROXY=''
```

## 2. Playwright Chromium（上游采集器要用）

```powershell
.venv\Scripts\python.exe -m playwright install chromium
```

## 3. FFmpeg / FFprobe（系统依赖，不自动安装）

```powershell
winget install --id Gyan.FFmpeg -e
```

抽帧、音频分析、增强与人声分离都依赖它；装完重开终端确认 `ffprobe -version` 可执行。

## 4. 模型下载与哈希校验

模型不进 Git，全部放在 `state/models/`，并逐文件按 SHA-256 校验。

```powershell
# OCR 模型随 rapidocr wheel 分发：复制到 state/models/ocr 并核对哈希
.venv\Scripts\python.exe scripts\model_assets.py --sync

# 查看缺哪些模型、官方下载地址与期望哈希
.venv\Scripts\python.exe scripts\model_assets.py --json
```

需要手动下载的大模型（清单见 `references/model-manifest.json`）：

| 模型 | 用途 | 相对路径 |
|---|---|---|
| `UVR-MDX-NET-Voc_FT.onnx` | 强背景音乐时的人声分离 | `state/models/sherpa/uvr/` |
| `sherpa-onnx-pyannote-segmentation-3-0.tar.bz2`（解包取 `model.onnx`） | 说话人分割 | `state/models/sherpa/segmentation/` |
| `3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx` | 中文声纹 embedding | `state/models/sherpa/embedding/` |

下载后重新跑 `scripts\model_assets.py --json`，确认 `broken_required=0`
且不再出现在 `download_plan` 中。缺少可选模型只会让对应能力降级
（人声分离 / 说话人分离 / 画面文字），原音转录不受影响。

## 5. 摘要与校正配置（可选，但强烈建议）

```powershell
copy .env.example .env
```

编辑 `.env` 填 `OPENAI_API_KEY`；`SUMMARY_API_URL`、`SUMMARY_API_MODEL`
默认指向 OpenAI 兼容接口，可换成本机或第三方兼容端点（Chat Completions 协议）。
没有密钥也能用：转录照常完成，摘要与校正标记为待补，之后用
`scripts\maintenance.py fill-ai` 补跑。`.env` 永不提交 Git、永不出现在日志与 TXT。

## 6. 体检

```powershell
.venv\Scripts\python.exe scripts\doctor.py --json
```

所有 `severity=error` 的项必须 `ok=true`；`severity=warning`（CUDA 不可用、
可选模型缺失、摘要未配置）只代表相应能力降级。GPU 加速无需安装 CUDA Toolkit：
lock 文件里的 `nvidia-*` wheel 自带运行时，只要机器有 NVIDIA 驱动即可自动启用
（显存 ≥6GB → medium/float16，<6GB → small/float16）。

## 7. 数据库迁移（v1 → v2，只做一次）

```powershell
# 先备份再迁移：脚本自己用 SQLite backup API 生成带时间戳的副本
.venv\Scripts\python.exe scripts\migrations.py --json
```

输出里的 `backup` 是备份路径（`.db/backups/transcripts-before-v2-<时间>.db`），
`migrated`/`skipped` 表示本次迁移与跳过的记录数，`integrity=ok` 才算成功。
迁移幂等，可以重复执行；v1 的 `transcripts` 表原样保留，不会删除任何内容。

**回滚**：迁移只新增 v2 表，不改动 v1 数据；若要回退，直接用备份覆盖
`.db/transcripts.db`（先关闭所有进程），并删除 `output/` 里的 v2 文件即可。
迁移前的备份副本请保留一个发布周期。

## 8. 首次登录（可选，仅匿名访问被拒时）

1. 正常跑一条链接；若返回 `status=login_required`，说明需要一次抖音扫码。
2. 用可见浏览器重跑同一条命令并加 `--visible`：

```powershell
.venv\Scripts\python.exe scripts\douyin_transcript.py "<链接>" --visible --json
```

3. 在弹出的 Chromium 窗口里用手机抖音 App 扫码。登录态只由上游写在
   `state/ingest/`（本 Skill 专属目录，已被 .gitignore 排除）。
4. 之后无需重复扫码；失效时再走一遍本流程。**登录态文件绝不导出、阅读或转发**，
   其内容不得出现在日志、TXT、数据库或任何回复中；也不得要求用户提供凭据文本。

批量任务遇到认证失效会暂停批次，扫码后继续即可：

```powershell
.venv\Scripts\python.exe scripts\batch_transcribe.py --resume "<batch_id>" --json
```

## 目录说明

| 路径 | 内容 | 可否提交 Git |
|---|---|---|
| `.db\transcripts.db` | 唯一事实源 | 否 |
| `.db\backups\` | 迁移前的数据库副本 | 否 |
| `output\YYYY\MM\*.txt` | 渲染出的主文件与原始记录文件 | 否 |
| `state/ingest/` | 抖音登录态（敏感） | 否 |
| `state\jobs\` | 每次任务的临时媒体（成功/失败后删除） | 否 |
| `state\models\` | Whisper 权重与 OCR/Sherpa 模型 | 否 |
| `state\logs\transcript.log` | 已脱敏运行日志 | 否 |
| `state\gpu.lock` | GPU 推理串行锁文件 | 否 |
| `.env` | 摘要/校正 API 密钥 | 否 |
