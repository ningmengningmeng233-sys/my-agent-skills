# 设计决策

## Whisper vs Qwen3-ASR

v4.x 用 Qwen3-ASR，v5.0 换回 Whisper：

1. Qwen3-ASR 是 LLM 做语音转文字，整段音频送进大模型推理，1.7B 权重显存占用大、推理慢；Whisper 是纯语音识别，30 秒分段做声学特征识别
2. Qwen3-ASR-1.7B 需要 4-6GB 显存；Whisper 可选 tiny/base/small/medium（tiny 仅 39MB）
3. `pip install openai-whisper` 一行安装；Qwen3-ASR 需从 HuggingFace 下载 2-5GB 权重，国内网络易失败
4. Whisper 是三级降级的最后兜底，大多数视频走 CC/AI 字幕，不值得背 LLM 级模型

## v5.3 分P改造决策（2026-08-19）

- **唯一键 `(bvid, page)`**：同一 BV 每个 P 独立记录；`cid` 作为稳定引用；旧记录自动视为 `page=1`
- **去重键**：第 1P 写裸 `BVxxx`（兼容旧文件），其余 P 写 `BVxxx_PN`
- **文件名**：`..._BVxxx_PNN_分P标题.txt`；目录保持按年月
- **`EXPAND_PAGES`**：默认 false，用户显式开启才逐P展开，避免批量任务量爆炸
- **旧 TXT 不重命名**：避免 knowledge-rag 把旧文件当新文件重索引
- **`GENERATE_SUMMARY_PER_PAGE` 不实现**：需要省钱时清空 `OPENAI_API_KEY`，转录照常、摘要留占位
- **Cookie 探测顺序**：firefox → edge → chrome（本机实测只有 Firefox 可用）
- **失败要出声**：pagelist 失败 stderr 告警；-404 视频跳过并标 `deleted_videos`
- **单执行点**：`D:\桌面\deepseek skills\bilibili-auto-transcript`；桌面 v5.2.0 副本只读

## 字幕自动降级与运行参数（SKILL.md 分支 1 挪入）

- 字幕自动降级：CC 字幕（B站官方/UP主上传的字幕） → AI 字幕 → faster-whisper（CTranslate2 CUDA float16 优先；无 GPU 时 CPU int8）
- 超长音频的分段转写开关与显存预算变量（`BILI_ASR_CHUNKING` 等）见 `references/ops-incidents.md`「超长音频」一节。
