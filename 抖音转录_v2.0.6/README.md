# 🎬 抖音视频转录 v2.0.4

抖音单作品/批量转录：**douyin-ingest v0.3.0 采集 → faster-whisper 最多三轮自适应 ASR →
自适应 OCR → 匿名说话人分离 → 证据融合与受约束 AI 校正 → 三段式摘要 →
「主文件 + 原始记录文件」双 TXT 原子发布**。

事实源是 SQLite，TXT 随时可重建；成功后删除全部视频、音频、分离音轨与帧图。
原始 ASR 稿永不被 AI 或 OCR 覆盖，AI 校正只作为可追溯的派生版本。

## 怎么用：三种给它链接的方式

### 1）直接对 AI 说（最省事）

在对话里粘贴抖音的分享文案或链接，然后说“转录”：

```
8.88 复制打开抖音，看看【奶龙的作品】端午就看我奶龙大侠赛龙舟 https://v.douyin.com/xxxx/
```

AI 会自动：识别并规范化链接 → 去重 → （超过 10 条先给你预览）→ 串行处理 →
逐条汇报标题、作品 ID、状态与 TXT 路径。

### 2）命令行单条

```powershell
.venv\Scripts\python.exe scripts\douyin_transcript.py "<分享文案或链接>" --json
```

### 3）批量（可多条链接，也可用文件）

```powershell
# 一条消息里的多条链接
.venv\Scripts\python.exe scripts\batch_transcribe.py --text "<粘贴整段分享文案>" --json

# 清单文件：TXT / Markdown / CSV 都行，每行一个链接也可以
.venv\Scripts\python.exe scripts\batch_transcribe.py --file "抖音清单.txt" --json

# 新增超过 10 条或预计超过 30 分钟时，入口会先返回预览（退出码 2），确认后加 --confirm
.venv\Scripts\python.exe scripts\batch_transcribe.py --file "抖音清单.txt" --confirm --json
```

接受的链接形式：

| 形式 | 例子 | 说明 |
|---|---|---|
| 完整分享文案 | `8.88 复制打开抖音… https://v.douyin.com/abc/` | 自动挑出链接 |
| 短链 | `https://v.douyin.com/abc/` | 由上游展开 |
| 标准作品页 | `https://www.douyin.com/video/7652684072266845450` | 直接取 ID |
| 带 `modal_id` 的页面 | `https://www.douyin.com/user/self?...&modal_id=765…` | 自动转成标准作品页（**不会**把原 URL 传给上游） |
| 清单文件 | TXT / Markdown / CSV（列名 `url`/`link`/`douyin_url`/`分享链接`） | 单文件 ≤10 MiB、≤10 000 条 |

不支持：账号主页、收藏夹页、合集、直播、图文，也不绕过验证码或权限——这些会被上游如实拒绝。

## 首次运行

```powershell
.venv\Scripts\python.exe scripts\doctor.py --json     # 12 项体检，核心项必须全绿
```

- 若某条链接返回 `login_required`：需要一次扫码。用 `--visible` 重跑同一条命令，
  在弹出的**专用 Chromium** 里用抖音 App 扫码；之后无需重复。
  批量任务遇到认证失效会暂停批次，扫码后 `--resume "<batch_id>"` 继续，已完成项不重跑。
- 未配摘要密钥也能用：转录照常交付，摘要/校正标记为待补，之后
  `scripts\maintenance.py fill-ai --json` 补齐。

## 常用维护

```powershell
.venv\Scripts\python.exe scripts\maintenance.py repair-artifacts --json   # 文件丢了？从数据库重建
.venv\Scripts\python.exe scripts\maintenance.py fill-ai --json           # 补校正与摘要
.venv\Scripts\python.exe scripts\maintenance.py recover-interrupted --json
.venv\Scripts\python.exe scripts\maintenance.py reprocess-review --json
.venv\Scripts\python.exe scripts\batch_transcribe.py --resume "<batch_id>" --json
.venv\Scripts\python.exe scripts\batch_transcribe.py --retry-failed "<batch_id>" --json
```

## 交付物

- 主文件：`output\YYYY\MM\作者_标题_作品ID.txt`（元数据 + 摘要 + 带时间戳与匿名说话人的校正版）
- 原始记录：`..._原始记录.txt`（选中 ASR 轮次、原始时间戳稿、OCR 分类、重要修正、疑点、降级说明）
- `needs_review` 会在主文件顶部醒目标记；无旁白只有画面文字时用“画面文字记录”，不谎称语音转录。

## 安装与依赖

Python 3.12 venv + `requirements.lock`（含 `douyin-ingest v0.3.0`、`faster-whisper`、
`rapidocr`、`onnxruntime`、`sherpa-onnx`）、Playwright Chromium、系统 FFmpeg/FFprobe、
以及 `state/models/` 下的模型（OCR 随 wheel 分发，可用 `model_assets.py --sync` 复制；
UVR 与说话人模型按 `references/model-manifest.json` 下载并校验 SHA-256）。
详见 `references/SETUP.md`；内部结构与不变量见 `references/architecture.md`；
错误码处置见 `references/troubleshooting.md`；验收证据见 `references/acceptance-matrix.md`。

## 自检

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -t .    # 695 项
```
