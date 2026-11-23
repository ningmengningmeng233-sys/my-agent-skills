# 安装与运行前提

> 面向第一次拿到本技能的人。全部命令在技能根目录执行。

## 1. 前置条件

| 项 | 说明 |
|---|---|
| Python | 3.10 或以上（`doctor.py` 会检查） |
| FFmpeg | 必须，含 `ffprobe`。Windows 可用 `winget install Gyan.FFmpeg` |
| yt-dlp | 必须。装进 `.venv` 即可，无需全局安装 |
| 磁盘 | 首次运行会下载 Whisper 模型（数百 MB 起），预留 2 GB 以上 |
| 浏览器 | 已登录 B站（Firefox / Edge / Chrome 任一），用于取字幕 |

## 2. 建立虚拟环境并安装依赖

```bash
# Linux / macOS
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
```

```bat
:: Windows
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock
```

`requirements.lock` 固定了实测可用的版本：Whisper（faster-whisper + CTranslate2）、
OCR（RapidOCR + ONNX Runtime，CPU 推理）、以及 Windows 专用的 CUDA 12 运行库
（带 `sys_platform == "win32"` 标记，Linux/macOS 上自动跳过）。

**只想要最小可用**（不含画面文字 OCR）：

```bash
.venv/bin/python -m pip install faster-whisper requests python-dotenv yt-dlp
```

**GPU 说明**：CTranslate2 检测到 CUDA float16 可用时走 GPU，否则自动用 CPU int8（慢但可用）。
无需安装 PyTorch。CUDA 12 的 cuBLAS/cuDNN 运行时由 `requirements.lock` 里的
`nvidia-*-cu12` 提供（仅 Windows）；若机器是 CUDA 12 + cuDNN 8，可固定
`ctranslate2==4.4.0`（见 [faster-whisper 官方说明](https://github.com/SYSTRAN/faster-whisper#gpu)）。
查看实际走哪条路：

```bash
.venv/Scripts/python.exe scripts/fw_whisper_main.py --probe   # 输出 cuda 或 cpu
```

## 3. 配置

```bash
cp .env.example .env
```

| 键 | 必填 | 说明 |
|----|------|------|
| `FAV_MEDIA_ID` | 批量转录时必填 | 收藏夹 URL 中 `fid=` 后面的数字 |
| `EXPAND_PAGES` | 否 | `true` 时收藏夹里的多P视频逐P展开；默认 `false` 只转第 1P |
| `BILI_ARCHIVE_DIR` | 否 | 归档根目录；不设则归档到技能目录下的 `转录文件/已转录` |
| `BILI_WHISPER_MODEL` | 否 | 覆盖自动选择的 Whisper 模型档位 |
| `OPENAI_API_KEY` | 否 | 配置后才生成 AI 摘要与受约束校正 |
| `SUMMARY_API_URL` | 否 | 默认 `https://api.openai.com/v1/chat/completions` |
| `SUMMARY_API_MODEL` | 否 | 默认 `gpt-4o-mini` |

摘要与校正走任何 **OpenAI 兼容** 接口：把 `SUMMARY_API_URL` 指向你的端点、
`SUMMARY_API_MODEL` 填模型名即可。未配置时**转录照常**，只是摘要留占位符。

## 4. 自检

```bash
.venv/Scripts/python.exe scripts/doctor.py          # Windows
.venv/bin/python scripts/doctor.py                  # Linux/macOS
```

`error` 级必须全绿；`warning` 表示某项能力降级但可继续（例如没有 GPU 就走 CPU、
没配摘要接口就留占位符）。会一并报出数据库 schema 版本、记录数与归档目录可写性。

跑测试确认环境完整：

```bash
.venv/Scripts/python.exe -m unittest discover -s tests -t .
```

## 5. 浏览器 Cookie

B站 AI 字幕需要登录态。脚本按 `firefox → edge → chrome` 顺序自动探测第一个可用的浏览器。

- **Firefox**：通常最省事，Cookie 库不常被占用
- **Edge**：浏览器运行时 Cookie 库会被锁定，失败会自动尝试下一个
- **Chrome**：部分环境有 DPAPI 解密问题，失败会自动跳过

三者都拿不到时脚本会打印 `[WARN] 无可用Cookie`，此时仍会退回 Whisper 本地转写，
不影响出稿。

## 6. 可选：定时任务

1. **Windows 任务计划程序**：定时运行 `batch_transcribe.py`（例如每 6 小时）
   与 `fill_summaries.py`（每 12 小时），「起始位置」设为技能目录。
   也可用 `scripts/register_watchdog.ps1` 注册看门狗：意外中断后回收僵尸任务并续跑。
2. **openclaw cron**（Linux/macOS）：

```bash
openclaw cron add --name bilibili-auto-transcribe --every 21600000 \
  --message "cd <skill_dir> && .venv/bin/python scripts/bilibili_scanner.py；有新视频则跑 batch_transcribe.py 并汇报结果"
```

## 7. 故障排查

| 现象 | 处理 |
|---|---|
| 拿不到字幕 | 确认浏览器已登录 B站；看脚本打印的 Cookie 探测结果 |
| 首次运行很慢 | 正在下载 Whisper 模型，属正常；检查磁盘与网络 |
| `CUDA 不可用` | 缺 cuBLAS/cuDNN 运行库，会自动退回 CPU，或按 §2 处理 |
| GPU 上 OOM | 长音频会先做显存预算判断并快速失败；也可设 `BILI_WHISPER_MODEL=small` |
| 收藏夹有失效视频 | 扫描器自动跳过并记入 `deleted_videos`，无需处理 |
| 摘要留占位符 | 未配 `OPENAI_API_KEY`／`SUMMARY_API_URL`／`SUMMARY_API_MODEL` |
