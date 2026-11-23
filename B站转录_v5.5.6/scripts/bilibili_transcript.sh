#!/bin/bash
# B站视频字幕智能获取脚本 v5.5.0（分P与加速支持）
# 功能：CC字幕 → AI字幕 → Whisper 转录（三级降级）
# 分P：URL 中 ?p=N 指定分P（默认第1P）；DB 唯一键 (bvid, page)；文件名带 _PNN
# 支持：WSL Chromium/Edge Cookie、多语言AI字幕、GPU加速、音频优化

VIDEO_URL="$1"
export PYTHONIOENCODING=utf-8
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# 优先使用技能自带的 .venv（python/yt-dlp/whisper）与 ffmpeg，避免解析到 anaconda 旧版
VENV_BIN="$SCRIPT_DIR/../.venv/Scripts"
[ -d "$VENV_BIN" ] || VENV_BIN="$SCRIPT_DIR/../.venv/bin"
export PATH="$VENV_BIN:$PATH"
PY_BIN="$VENV_BIN/python.exe"
[ -x "$PY_BIN" ] || PY_BIN="$VENV_BIN/python"
[ -x "$PY_BIN" ] || PY_BIN="python"
PY_SCRIPT_DIR="$(cygpath -m "$SCRIPT_DIR" 2>/dev/null || echo "$SCRIPT_DIR")"
OUTPUT_DIR="${BILI_OUTPUT_DIR:-${2:-$SCRIPT_DIR/../output}}"
if command -v cygpath >/dev/null 2>&1; then
    OUTPUT_DIR="$(cygpath -u "$OUTPUT_DIR")"
fi
mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR_WIN="$(cygpath -m "$OUTPUT_DIR" 2>/dev/null || echo "$OUTPUT_DIR")"
BROWSER_TYPE="${3:-chromium}"

CLEANUP_DIR="$OUTPUT_DIR"
LOG_DIR="$SCRIPT_DIR/../state/logs"
LOG_FILE="$LOG_DIR/transcript.log"
mkdir -p "$LOG_DIR"

shell_log() {
    local level="${1:-INFO}"
    local message="$2"
    local ts
    ts=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$ts] [$level] [bilibili_transcript.sh] $message" >> "$LOG_FILE"
}

cleanup_temp() {
    rm -f "$CLEANUP_DIR"/bilibili_subtitle*.srt "$CLEANUP_DIR"/bilibili_ai_subtitle*.srt \
          "$CLEANUP_DIR"/bilibili_audio*.mp3 "$CLEANUP_DIR"/bilibili_audio*.m4a \
          "$CLEANUP_DIR"/bilibili_audio*.webm "$CLEANUP_DIR"/bilibili_audio*.opus \
          "$CLEANUP_DIR"/bilibili_audio*.mp4 \
          "$CLEANUP_DIR"/bilibili_audio*.wav "$CLEANUP_DIR"/bilibili_audio*.txt \
          "$CLEANUP_DIR"/.qwen_transcript.txt
}
trap cleanup_temp EXIT

if [ -z "$VIDEO_URL" ]; then
    echo "用法: $0 <B站视频链接> [输出目录] [浏览器类型:chromium|edge|firefox]"
    echo "分P: 链接中带 ?p=N 指定分P，如 https://www.bilibili.com/video/BVxxxxx/?p=3"
    exit 1
fi

# ===== 解析 BV 号与分P（v5.3） =====
BVID=$(echo "$VIDEO_URL" | grep -oP 'BV[a-zA-Z0-9]+' | head -1)
PAGE_RAW=$(echo "$VIDEO_URL" | grep -oP '[?&]p=\K[0-9]+' | head -1)
PAGE=1
if [ -n "$PAGE_RAW" ]; then
    case "$PAGE_RAW" in
        *[!0-9]*) PAGE=1 ;;
        *) PAGE=$((10#$PAGE_RAW)); [ "$PAGE" -lt 1 ] && PAGE=1 ;;
    esac
fi

# 规范化 URL：所有 yt-dlp 调用统一使用（无 p 参数默认第1P）
if [ -n "$BVID" ]; then
    if [ "$PAGE" -gt 1 ]; then
        VIDEO_URL_NORMALIZED="https://www.bilibili.com/video/${BVID}/?p=${PAGE}"
    else
        VIDEO_URL_NORMALIZED="https://www.bilibili.com/video/${BVID}/"
    fi
else
    VIDEO_URL_NORMALIZED="$VIDEO_URL"
fi

echo "🔍 正在获取视频信息..."
[ "$PAGE" -gt 1 ] && echo "📚 目标分P: 第 ${PAGE}P"

# ===== 检测浏览器Cookie =====
echo "🔍 检测浏览器Cookie..."

COOKIE_ARGS=()

detect_cookie() {
    local browser="$1"
    local label="$2"
    local test_out
    test_out=$(yt-dlp --list-subs --cookies-from-browser "$browser" "$VIDEO_URL_NORMALIZED" 2>&1 | head -20)
    # 只有真正提取成功（无 ERROR/412）才认为 Cookie 有效，避免把报错行前的
    # "Extracting URL" 误判为成功，导致带上过期 Cookie 触发 B站风控
    if echo "$test_out" | grep -q "Extracting" && ! echo "$test_out" | grep -qE "ERROR|412|Unable to download"; then
        echo "   [OK] 使用 $label Cookie"
        COOKIE_ARGS=(--cookies-from-browser "$browser")
        return 0
    fi
    # 失败原因也留痕：412 与「读不到 Cookie」对后续排查是两件事。
    shell_log "WARN" "Cookie 探测失败(${label}): $(printf '%s' "$test_out" | grep -aE 'ERROR|412' | head -1)"
    return 1
}

case "$BROWSER_TYPE" in
    edge)     detect_cookie "edge" "Windows Edge" || true ;;
    chrome)   detect_cookie "chrome" "Chrome" || true ;;
    firefox)  detect_cookie "firefox" "Firefox" || true ;;
    *)
        # 本机实测：Firefox 可读 cookie；Edge 常被浏览器锁定、Chrome 有 DPAPI 解密问题
        detect_cookie "firefox" "Firefox" || \
        detect_cookie "edge" "Windows Edge" || \
        detect_cookie "chrome" "Chrome" || true
        ;;
esac

if [ ${#COOKIE_ARGS[@]} -eq 0 ]; then
    echo "   [WARN] 无可用Cookie，B站AI字幕可能无法获取"
    echo "   [TIP] 请先用 Chrome/Edge 登录 bilibili.com（yt-dlp 会自动读取浏览器 Cookie）"
fi
echo ""

# ===== 获取视频元数据（当前分P） =====
# yt-dlp 的失败原因全在 stderr 里（HTTP 412／风控／网络），原先 2>/dev/null 把它丢掉，
# 批次层只能看到「无法获取视频信息」，无法区分风控与普通网络故障，90 多条任务各自在
# 30 秒内把重试撞完，反而把风控窗口撑得更久。这里把 stderr 留档，并给出机器可读原因。
META_ERR_FILE="$LOG_DIR/.last_meta_error"

fetch_video_info() {
    local attempt out
    : > "$META_ERR_FILE"
    for attempt in 1 2; do
        out=$(yt-dlp "${COOKIE_ARGS[@]}" --dump-json "$VIDEO_URL_NORMALIZED" \
              2>"$META_ERR_FILE" | head -1)
        if [ -n "$out" ]; then printf '%s' "$out"; return 0; fi
        # Cookie 过期本身也会 412，去掉 Cookie 再试一次，以区分「Cookie 坏了」与「链接坏了」。
        out=$(yt-dlp --dump-json "$VIDEO_URL_NORMALIZED" 2>"$META_ERR_FILE" | head -1)
        if [ -n "$out" ]; then printf '%s' "$out"; return 0; fi
        if [ "$attempt" -lt 2 ]; then
            shell_log "WARN" "元数据拉取失败（第 ${attempt} 次），退避 10 秒后重试"
            sleep 10
        fi
    done
    return 1
}

VIDEO_INFO=$(fetch_video_info)

if [ -z "$VIDEO_INFO" ]; then
    ERR_TEXT="$(head -20 "$META_ERR_FILE" 2>/dev/null)"
    if printf '%s' "$ERR_TEXT" | grep -qaE 'HTTP Error 412|Precondition Failed|风控|请求过于频繁|"code":-412|"code":-352'; then
        META_FAIL_REASON="risk_control"
        echo "❌ 无法获取视频信息：疑似 B站风控/限流（HTTP 412）"
    else
        META_FAIL_REASON="network"
        echo "❌ 无法获取视频信息：网络异常或链接不可用"
    fi
    echo "[BILI-META-FAIL] reason=${META_FAIL_REASON} url=${VIDEO_URL_NORMALIZED}"
    printf '%s\n' "$ERR_TEXT" | grep -aE 'ERROR|HTTP Error|风控|code' | head -5 | sed 's/^/   ↳ /'
    shell_log "ERROR" "元数据拉取失败(${META_FAIL_REASON}): $(printf '%s' "$ERR_TEXT" | tr '\n' ' ' | cut -c1-500)"
    exit 1
fi

TITLE=$(echo "$VIDEO_INFO" | "$PY_BIN" -c "import sys, json; print(json.load(sys.stdin).get('title', '未知标题'))")
AUTHOR=$(echo "$VIDEO_INFO" | "$PY_BIN" -c "import sys, json; print(json.load(sys.stdin).get('uploader', '未知作者'))")
UPLOAD_DATE=$(echo "$VIDEO_INFO" | "$PY_BIN" -c "import sys, json; print(json.load(sys.stdin).get('upload_date', '未知时间'))")
DURATION_SEC=$(echo "$VIDEO_INFO" | "$PY_BIN" -c "import sys, json; print(json.load(sys.stdin).get('duration', 0))")
DURATION=$(echo "$VIDEO_INFO" | "$PY_BIN" -c "import sys, json; d=json.load(sys.stdin).get('duration', 0); print(f'{int(d//60)}分{int(d%60)}秒')")

if [ "$UPLOAD_DATE" != "未知时间" ]; then
    UPLOAD_DATE_FORMATTED=$(echo "$UPLOAD_DATE" | sed 's/\(....\)\(..\)\(..\)/\1-\2-\3/')
else
    UPLOAD_DATE_FORMATTED="$UPLOAD_DATE"
fi

# ===== 获取分P元数据（cid / part / 总标题 / 总P数） =====
CID=""
PART_TITLE=""
TOTAL_PAGES=1
MAIN_TITLE=""
if [ -n "$BVID" ]; then
    PAGE_META=$("$PY_BIN" -c '
import json, sys, urllib.request
bvid = sys.argv[1]
page = int(sys.argv[2])
out = {"cid": None, "part": "", "title": "", "total_pages": 1}
try:
    req = urllib.request.Request("https://api.bilibili.com/x/web-interface/view?bvid=" + bvid,
                                 headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") == 0:
        d = data.get("data") or {}
        out["title"] = d.get("title") or ""
        pages = d.get("pages") or []
        out["total_pages"] = len(pages)
        for p in pages:
            if p.get("page") == page:
                out["cid"] = p.get("cid")
                out["part"] = p.get("part") or ""
                break
except Exception:
    pass
print(json.dumps(out, ensure_ascii=False))
' "$BVID" "$PAGE" 2>/dev/null)
    MAIN_TITLE=$(echo "$PAGE_META" | "$PY_BIN" -c "import sys,json; print(json.load(sys.stdin).get('title') or '')" 2>/dev/null)
    CID=$(echo "$PAGE_META" | "$PY_BIN" -c "import sys,json; c=json.load(sys.stdin).get('cid'); print(c if c else '')" 2>/dev/null)
    PART_TITLE=$(echo "$PAGE_META" | "$PY_BIN" -c "import sys,json; print(json.load(sys.stdin).get('part') or '')" 2>/dev/null)
    TOTAL_PAGES=$(echo "$PAGE_META" | "$PY_BIN" -c "import sys,json; print(json.load(sys.stdin).get('total_pages') or 1)" 2>/dev/null)
fi

# 总标题以 view API 为准（多P时 yt-dlp 的 title 常带 "- P2 分P标题" 后缀）
if [ -n "$MAIN_TITLE" ]; then
    TITLE="$MAIN_TITLE"
fi

echo "📹 视频: $TITLE"
if [ "$TOTAL_PAGES" -gt 1 ] 2>/dev/null; then
    echo "📚 分P: 第 ${PAGE}P / 共 ${TOTAL_PAGES}P${PART_TITLE:+（$PART_TITLE）}"
fi
echo "👤 作者: $AUTHOR"
echo "📅 发布: $UPLOAD_DATE_FORMATTED"
echo "⏱️  时长: $DURATION"
shell_log "INFO" "开始转录: $VIDEO_URL | P$PAGE/${TOTAL_PAGES}P | $TITLE | $AUTHOR"

# ===== 检查字幕 =====
echo ""
echo "🔍 正在检查字幕..."
SUB_CHECK=$(yt-dlp "${COOKIE_ARGS[@]}" --list-subs "$VIDEO_URL_NORMALIZED" 2>&1)

HAS_CC_SUBS=false
CC_SUB_LANG=""
CC_SUB_LANG=$(echo "$SUB_CHECK" | awk '!/danmaku/ && !/ai-/ && /^[[:space:]]*(zh-CN|zh-TW|zh-Hans|zh-Hant|en|ja|ko|es|ar|pt|de|fr)($|[-[:space:]])/ {print $1; exit}')
if [ -n "$CC_SUB_LANG" ]; then
    HAS_CC_SUBS=true
fi

HAS_AI_SUBS=false
AI_LANG=""
for lang in "ai-zh" "ai-en" "ai-ja" "ai-kr" "ai-th" "ai-id" "ai-vi"; do
    if echo "$SUB_CHECK" | grep -q "$lang"; then
        HAS_AI_SUBS=true
        AI_LANG="$lang"
        break
    fi
done

TRANSCRIPT_SOURCE=""
TRANSCRIPT_TEXT=""

# 第1级：人工CC字幕
if [ "$HAS_CC_SUBS" = true ]; then
    echo "✅ 发现人工CC字幕（$CC_SUB_LANG），优先下载..."

    yt-dlp "${COOKIE_ARGS[@]}" --skip-download --write-subs --sub-langs "$CC_SUB_LANG" --convert-subs srt \
        -o "${OUTPUT_DIR}/bilibili_subtitle.%(ext)s" "$VIDEO_URL_NORMALIZED" 2>&1

    SUB_FILE=$(find "$OUTPUT_DIR" -maxdepth 1 -name "bilibili_subtitle*.srt" -type f 2>/dev/null | head -1)

    if [ -n "$SUB_FILE" ] && [ -s "$SUB_FILE" ]; then
        echo "✅ CC字幕下载成功"
        TRANSCRIPT_SOURCE="B站CC字幕"
        shell_log "SUCCESS" "使用CC字幕 ($CC_SUB_LANG)"
        TRANSCRIPT_TEXT=$(sed '/^[0-9]\{1,\}:[0-9][0-9]:[0-9][0-9]/d' "$SUB_FILE" | sed '/^[0-9]*$/d' | sed '/^$/d')
    else
        echo "⚠️  CC字幕下载失败..."
        HAS_CC_SUBS=false
    fi
fi

# 第2级：AI字幕
if [ -z "$TRANSCRIPT_TEXT" ] && [ "$HAS_AI_SUBS" = true ]; then
    echo "✅ 发现AI字幕（$AI_LANG），正在下载..."

    yt-dlp "${COOKIE_ARGS[@]}" --skip-download --write-subs --write-auto-subs --sub-langs "$AI_LANG" --convert-subs srt \
        -o "${OUTPUT_DIR}/bilibili_ai_subtitle.%(ext)s" "$VIDEO_URL_NORMALIZED" 2>&1

    SUB_FILE=$(find "$OUTPUT_DIR" -maxdepth 1 -name "bilibili_ai_subtitle*.srt" -type f 2>/dev/null | head -1)

    if [ -n "$SUB_FILE" ] && [ -s "$SUB_FILE" ]; then
        echo "✅ AI字幕下载成功"
        TRANSCRIPT_SOURCE="B站AI字幕 ($AI_LANG)"
        shell_log "SUCCESS" "使用AI字幕 ($AI_LANG)"
        TRANSCRIPT_TEXT=$(sed '/^[0-9]\{1,\}:[0-9][0-9]:[0-9][0-9]/d' "$SUB_FILE" | sed '/^[0-9]*$/d' | sed '/^$/d')
    else
        echo "⚠️  AI字幕下载失败..."
        HAS_AI_SUBS=false
    fi
fi

# 第2.5级：兜底 - 尝试直接下载AI字幕（解决 yt-dlp 列表检测不到的 AI 字幕）
if [ -z "$TRANSCRIPT_TEXT" ]; then
    echo "🔍 尝试直接下载 AI 字幕（兜底）..."
    for try_lang in "ai-zh" "ai-en" "ai-ja"; do
        yt-dlp "${COOKIE_ARGS[@]}" --skip-download --write-subs --write-auto-subs --sub-langs "$try_lang" --convert-subs srt \
            -o "${OUTPUT_DIR}/bilibili_ai_subtitle.%(ext)s" "$VIDEO_URL_NORMALIZED" 2>/dev/null
        SUB_FILE=$(find "$OUTPUT_DIR" -maxdepth 1 -name "bilibili_ai_subtitle*.srt" -type f 2>/dev/null | head -1)
        if [ -n "$SUB_FILE" ] && [ -s "$SUB_FILE" ]; then
            echo "✅ 兜底成功！AI字幕已下载（$try_lang）"
            TRANSCRIPT_SOURCE="B站AI字幕 ($try_lang)"
            shell_log "SUCCESS" "兜底AI字幕 ($try_lang)"
            TRANSCRIPT_TEXT=$(sed '/^[0-9]\{1,\}:[0-9][0-9]:[0-9][0-9]/d' "$SUB_FILE" | sed '/^[0-9]*$/d' | sed '/^$/d')
            break
        fi
    done
fi

# 第3级：Whisper 本地语音转文字
# 有独显且显存≥6GB → medium；有独显但显存<6GB → small
# 无独显且视频≤30分钟 → base；无独显且视频>30分钟 → tiny
if [ -z "$TRANSCRIPT_TEXT" ]; then
    echo "🎤 未发现字幕，使用 Whisper 本地语音转文字..."
    echo "⏳ 这可能需要一些时间，请耐心等待..."

    # 检测 CTranslate2 的 CUDA float16 支持（无需安装 PyTorch）
    HAS_CUDA=false
    GPU_VRAM_MB=0
    if "$PY_BIN" -c "import faster_whisper" 2>/dev/null && \
       "$PY_BIN" "$SCRIPT_DIR/fw_whisper_main.py" --probe 2>/dev/null | grep -q '^cuda$'; then
        HAS_CUDA=true
        echo "   ✅ GPU加速可用（CUDA）"
        # 检测显存
        if command -v nvidia-smi &>/dev/null; then
            GPU_VRAM_MB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -dc '0-9')
            GPU_VRAM_MB=${GPU_VRAM_MB:-0}
            echo "   💾 显存: ${GPU_VRAM_MB}MB"
        fi
    elif ! "$PY_BIN" -c "import faster_whisper" 2>/dev/null && \
         "$PY_BIN" -c "import torch; assert torch.cuda.is_available()" 2>/dev/null; then
        HAS_CUDA=true
    fi

    USE_FASTER=false
    if "$PY_BIN" -c "import faster_whisper" 2>/dev/null; then
        USE_FASTER=true
    fi

    # 根据 GPU/显存选择 Whisper 模型（必须在下载之前定下来：预算闸门要用它）
    #   GPU+显存≥6GB → medium | GPU+显存<6GB → small（显存未知也退回 small）
    #   CPU+≤30分钟 → base     | CPU+>30分钟 → tiny
    DURATION_INT=${DURATION_SEC%%.*}
    DURATION_INT=${DURATION_INT:-0}
    WHISPER_MODEL="base"
    if [ "$HAS_CUDA" = true ]; then
        if [ "${GPU_VRAM_MB:-0}" -ge 6144 ] 2>/dev/null; then
            WHISPER_MODEL="medium"
            echo "   📐 显存 ${GPU_VRAM_MB}MB ≥ 6GB → 使用 medium 模型"
        else
            WHISPER_MODEL="small"
            echo "   📐 显存不足 6GB 或未知 → 使用 small 模型"
        fi
    elif [ "$DURATION_INT" -gt 1800 ]; then
        WHISPER_MODEL="tiny"
        echo "   📐 长视频(>30分钟)+CPU → 使用 tiny 模型（避免等待过久）"
    else
        WHISPER_MODEL="base"
        echo "   📐 CPU模式 → 使用 base 模型"
    fi
    WHISPER_MODEL="${BILI_WHISPER_MODEL:-$WHISPER_MODEL}"

    # 下载前先过预算闸门：超阈值条目几秒内失败，而不是先下完上百 MB 再拒绝
    # （判定与执行器同一套函数，口径不会分叉；BILI_ASR_CHUNKING=1 时自动放行）。
    if [ "${DURATION_INT:-0}" -gt 0 ] 2>/dev/null; then
        BUDGET_ARGS=(--seconds "$DURATION_INT" --model "$WHISPER_MODEL")
        [ "$HAS_CUDA" = true ] && BUDGET_ARGS+=(--device cuda)
        if BUDGET_OUT=$("$PY_BIN" "$SCRIPT_DIR/asr_budget.py" "${BUDGET_ARGS[@]}" 2>&1); then
            echo "   📐 $BUDGET_OUT"
        else
            echo "$BUDGET_OUT"
            shell_log "ERROR" "超出转写预算，跳过下载: ${DURATION_INT}s / $WHISPER_MODEL"
            exit 1
        fi
    fi

    # 下载音频（只下载当前分P）
    echo "   ⬇️ 下载音频..."
    if [ "$USE_FASTER" = true ]; then
        # PyAV 可直接解码原音频，省掉 mp3 与 WAV 的两次转码。
        yt-dlp "${COOKIE_ARGS[@]}" -f bestaudio -o "${OUTPUT_DIR}/bilibili_audio.%(ext)s" "$VIDEO_URL_NORMALIZED" 2>&1 || \
        yt-dlp -f bestaudio -o "${OUTPUT_DIR}/bilibili_audio.%(ext)s" "$VIDEO_URL_NORMALIZED" 2>&1
    else
        yt-dlp "${COOKIE_ARGS[@]}" -x --audio-format mp3 -o "${OUTPUT_DIR}/bilibili_audio.%(ext)s" "$VIDEO_URL_NORMALIZED" 2>&1 || \
        yt-dlp -x --audio-format mp3 -o "${OUTPUT_DIR}/bilibili_audio.%(ext)s" "$VIDEO_URL_NORMALIZED" 2>&1
    fi

    AUDIO_FILE=$(find "$OUTPUT_DIR" -maxdepth 1 -type f \( -name 'bilibili_audio.mp3' -o -name 'bilibili_audio.m4a' -o -name 'bilibili_audio.webm' -o -name 'bilibili_audio.opus' -o -name 'bilibili_audio.mp4' \) 2>/dev/null | head -1)

    if [ -z "$AUDIO_FILE" ]; then
        echo "❌ 音频下载失败"
        exit 1
    fi

    if [ "$USE_FASTER" != true ]; then
        # 原生 Whisper 继续使用 16kHz 单声道 WAV。
        echo "   🔄 音频格式优化（16kHz 单声道）..."
        WAV_FILE="${OUTPUT_DIR}/bilibili_audio.wav"
        ffmpeg -y -i "$AUDIO_FILE" -ar 16000 -ac 1 "$WAV_FILE" 2>/dev/null
        if [ -f "$WAV_FILE" ] && [ -s "$WAV_FILE" ]; then
            AUDIO_FILE="$WAV_FILE"
            echo "   ✅ 音频已优化"
        fi
    fi

    # faster-whisper 优先；保留原生 Whisper 作为未安装时的兼容后备。
    if [ "$USE_FASTER" != true ]; then
        WHISPER_BIN="$VENV_BIN/whisper.exe"
        [ -x "$WHISPER_BIN" ] || WHISPER_BIN="$VENV_BIN/whisper"
        [ -x "$WHISPER_BIN" ] || WHISPER_BIN="whisper"
        if ! command -v "$WHISPER_BIN" &>/dev/null; then
            echo "❌ 未安装转录引擎；请查看 references/SETUP.md"
            exit 1
        fi
    fi

    # 检测视频语言（判断是否为中文内容）
    WHISPER_LANG=""
    if echo "$TITLE" | "$PY_BIN" -c "import sys; s=sys.stdin.read(); sys.exit(0 if any('\u4e00'<=c<='\u9fff' for c in s) else 1)"; then
        WHISPER_LANG="zh"
        echo "   🌐 检测到中文标题，指定 --language zh 提高准确率"
    fi

    echo "   🎤 开始语音转文字（模型: $WHISPER_MODEL）..."
    RUNNER_JSON=""
    if [ "$USE_FASTER" = true ]; then
        # 多轮转写：首轮不达标时按质量证据换参数重试（最多三轮），取历史最佳轮。
        # BILI_WHISPER_ROUNDS=1 可关闭多轮，回到「一轮定结果」的旧行为。
        WHISPER_ROUNDS="${BILI_WHISPER_ROUNDS:-3}"
        RUNNER_STATE="${OUTPUT_DIR}/.asr_runner.json"
        # stdout 是 JSON（留作解析），stderr 是进度/告警（直接透出，不混入状态文件）
        RUNNER_JSON=$(BILI_WHISPER_MODEL="$WHISPER_MODEL" "$PY_BIN" "$SCRIPT_DIR/asr_runner.py" \
            --audio "$AUDIO_FILE" --output-dir "$OUTPUT_DIR" \
            --model "$WHISPER_MODEL" \
            --max-rounds "$WHISPER_ROUNDS" \
            ${WHISPER_LANG:+--language "$WHISPER_LANG"} --json 2>"${OUTPUT_DIR}/.asr_runner.stderr")
        RUNNER_RC=$?
        [ -s "${OUTPUT_DIR}/.asr_runner.stderr" ] && tail -20 "${OUTPUT_DIR}/.asr_runner.stderr"
        if [ "$RUNNER_RC" -eq 1 ]; then
            # 执行器整体失败（模型加载/推理异常）：退回单轮脚本，避免比改造前更差
            echo "   ⚠️  多轮执行器失败，退回单轮转写..."
            echo "$RUNNER_JSON" > "$RUNNER_STATE"
            WHISPER_ARGS=("$AUDIO_FILE" --model "$WHISPER_MODEL" --output_format txt --output_dir "$OUTPUT_DIR")
            [ -n "$WHISPER_LANG" ] && WHISPER_ARGS+=(--language "$WHISPER_LANG")
            "$PY_BIN" "$SCRIPT_DIR/fw_whisper_main.py" "${WHISPER_ARGS[@]}" 2>&1 || { echo "❌ faster-whisper 运行失败"; exit 1; }
            RUNNER_JSON=""
        elif [ "$RUNNER_RC" -eq 3 ]; then
            # 正常跑完但没有语音：再跑一遍不会有不同结果，交给下面的空文件判定收尾
            echo "   ℹ️  未识别到语音内容"
            echo "$RUNNER_JSON" > "$RUNNER_STATE"
        else
            echo "$RUNNER_JSON" > "$RUNNER_STATE"
            RUNNER_ROUNDS=$("$PY_BIN" -c "import json,sys; print(json.load(open(sys.argv[1]))['rounds'])" "$RUNNER_STATE" 2>/dev/null || echo "?")
            RUNNER_SCORE=$("$PY_BIN" -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('score'))" "$RUNNER_STATE" 2>/dev/null || echo "?")
            echo "   📊 转写轮次: ${RUNNER_ROUNDS}，质量分: ${RUNNER_SCORE}"
            if [ "$RUNNER_RC" -eq 2 ]; then
                echo "   ⚠️  质量分未达标，结果仍交付，但不标为通过（原文与分歧见原始记录）"
            fi
        fi
    else
        WHISPER_ARGS=("$AUDIO_FILE" --model "$WHISPER_MODEL" --output_format txt --output_dir "$OUTPUT_DIR")
        if [ -n "$WHISPER_LANG" ]; then
            WHISPER_ARGS+=(--language "$WHISPER_LANG")
        fi
        "$WHISPER_BIN" "${WHISPER_ARGS[@]}" 2>&1 || { echo "❌ Whisper 运行失败"; exit 1; }
    fi

    TXT_FILE="${OUTPUT_DIR}/bilibili_audio.txt"
    if [ -n "${RUNNER_STATE:-}" ] && [ -s "$RUNNER_STATE" ]; then
        # AT-01：以执行器 JSON 报告的实际落盘路径为准，约定路径仅作兜底。
        RUNNER_TXT=$("$PY_BIN" -c "import json,sys; print(json.load(open(sys.argv[1])).get('txt',''))" "$RUNNER_STATE" 2>/dev/null)
        [ -n "$RUNNER_TXT" ] && TXT_FILE="$RUNNER_TXT"
    fi
    if [ ! -f "$TXT_FILE" ]; then
        TXT_FILE=$(find "$OUTPUT_DIR" -maxdepth 1 -name "*bilibili_audio*.txt" -type f 2>/dev/null | head -1)
    fi

    if [ -n "$TXT_FILE" ] && [ -s "$TXT_FILE" ]; then
        echo "✅ Whisper 转写完成（待写库）"
        TRANSCRIPT_SOURCE="Whisper $WHISPER_MODEL 模型"
        if [ "$USE_FASTER" = true ]; then
            TRANSCRIPT_SOURCE="faster-whisper $WHISPER_MODEL 模型"
        fi
        if [ "$HAS_CUDA" = true ]; then
            TRANSCRIPT_SOURCE="$TRANSCRIPT_SOURCE（GPU加速）"
        fi
        shell_log "SUCCESS" "使用Whisper $WHISPER_MODEL 转录完成"
        TRANSCRIPT_TEXT=$(cat "$TXT_FILE")
        # 分段侧车：执行器与 TXT 同目录同名，供质量评分与证据对齐使用
        SEGMENTS_FILE="${TXT_FILE%.txt}.segments.json"
        if [ -n "${RUNNER_STATE:-}" ] && [ -s "$RUNNER_STATE" ]; then
            # AT-01：同 TXT，以执行器 JSON 报告的实际侧车路径为准。
            RUNNER_SEG=$("$PY_BIN" -c "import json,sys; print(json.load(open(sys.argv[1])).get('segments_file',''))" "$RUNNER_STATE" 2>/dev/null)
            [ -n "$RUNNER_SEG" ] && SEGMENTS_FILE="$RUNNER_SEG"
        fi
        [ -f "$SEGMENTS_FILE" ] || SEGMENTS_FILE=""
    else
        echo "❌ Whisper 转录失败"
        # 清理临时文件
        rm -f "$WAV_FILE" "$AUDIO_FILE"
        exit 1
    fi
fi

# 繁体转简体
if command -v opencc >/dev/null 2>&1; then
    echo "🔄 正在转换为简体字..."
    TRANSCRIPT_TEXT_SIMPLIFIED=$(echo "$TRANSCRIPT_TEXT" | opencc -c tw2s)
else
    TRANSCRIPT_TEXT_SIMPLIFIED="$TRANSCRIPT_TEXT"
fi

# 按发布年月组织输出目录
PUB_YEAR=$(echo "$UPLOAD_DATE_FORMATTED" | cut -d'-' -f1)
PUB_MONTH=$(echo "$UPLOAD_DATE_FORMATTED" | cut -d'-' -f2)
if [ -n "$PUB_YEAR" ] && [ "$PUB_YEAR" != "未知时间" ]; then
    OUTPUT_DIR="${OUTPUT_DIR}/${PUB_YEAR}/${PUB_MONTH}"
    mkdir -p "$OUTPUT_DIR"
fi

echo "📝 正在写入数据库..."

# ===== 第一步：写入SQLite（DB是数据源；唯一键 bvid+page） =====
if [ -n "$BVID" ]; then
    # 将元数据写入临时 JSON 文件（避免 shell 字符串拼接导致注入）
    META_JSON=$(mktemp /tmp/bili_meta_XXXXXX.json)
    META_JSON_WIN=$(cygpath -m "$META_JSON" 2>/dev/null || echo "$META_JSON")
    "$PY_BIN" -c "
import json, sys
data = {
    'bvid': sys.argv[1], 'page': int(sys.argv[2]),
    'cid': int(sys.argv[3]) if sys.argv[3].isdigit() else None,
    'part_title': sys.argv[4], 'url': sys.argv[5],
    'title': sys.argv[6], 'author': sys.argv[7],
    'duration': sys.argv[8], 'upload_date': sys.argv[9],
    'transcript_source': sys.argv[10]
}
with open(sys.argv[11], 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False)
" "$BVID" "$PAGE" "$CID" "$PART_TITLE" "$VIDEO_URL_NORMALIZED" "$TITLE" "$AUTHOR" "$DURATION" "$UPLOAD_DATE_FORMATTED" "$TRANSCRIPT_SOURCE" "$META_JSON_WIN" 2>/dev/null

    # 写入库行 + 追加原始稿版本（+ 分段与质量分）；旧版本一律保留，不做覆盖
    TEXT_TMP=$(mktemp /tmp/bili_text_XXXXXX.txt)
    TEXT_TMP_WIN=$(cygpath -m "$TEXT_TMP" 2>/dev/null || echo "$TEXT_TMP")
    printf '%s' "$TRANSCRIPT_TEXT_SIMPLIFIED" > "$TEXT_TMP"
    SEGMENTS_ARG=()
    if [ -n "$SEGMENTS_FILE" ]; then
        SEGMENTS_WIN=$(cygpath -m "$SEGMENTS_FILE" 2>/dev/null || echo "$SEGMENTS_FILE")
        SEGMENTS_ARG=(--segments "$SEGMENTS_WIN")
    fi
    "$PY_BIN" "$SCRIPT_DIR/record_transcript.py"         --bvid "$BVID" --page "$PAGE"         --meta "$META_JSON_WIN" --text-file "$TEXT_TMP_WIN"         "${SEGMENTS_ARG[@]}" 2>&1
    RECORD_RC=$?
    if [ "$RECORD_RC" -ne 0 ]; then
        echo "[BILI-DB-FAIL] reason=record_failed rc=$RECORD_RC bvid=$BVID page=$PAGE"
        echo "❌ 写库失败（rc=$RECORD_RC），转录稿已保留：$TXT_FILE"
        [ -n "$SEGMENTS_FILE" ] && echo "   分段侧车已保留：$SEGMENTS_FILE"
        echo "   待写正文已保留：$TEXT_TMP"
        shell_log "ERROR" "写库失败(rc=$RECORD_RC)，稿件保留: $BVID P$PAGE txt=$TXT_FILE"
        rm -f "$META_JSON"
        exit 1
    fi
    shell_log "INFO" "已写入数据库: $BVID P$PAGE"
    rm -f "$META_JSON" "$TEXT_TMP" "$TXT_FILE" "$SEGMENTS_FILE"
fi

# ===== 第二步：生成AI摘要（有API key时，从DB读取） =====
if [ -n "$OPENAI_API_KEY" ] || [ -f "$SCRIPT_DIR/../.env" ]; then
    echo ""
    echo "🤖 正在生成AI摘要..."
    # 仅在未设置API key时才从.env加载（尊重已存在的环境变量）
    if [ -z "$OPENAI_API_KEY" ] && [ -f "$SCRIPT_DIR/../.env" ]; then
        set -a; . "$SCRIPT_DIR/../.env" 2>/dev/null || true; set +a
    fi
    if [ -n "$BVID" ]; then
        "$PY_BIN" "$SCRIPT_DIR/generate_summary.py" --bvid "$BVID" --page "$PAGE" 2>&1
        shell_log "INFO" "AI摘要已触发: $BVID P$PAGE"
    fi
fi

# ===== 第三步：从DB渲染TXT（TXT是展示层；文件名带 _PNN） =====
if [ -n "$BVID" ]; then
    RENDERED=$("$PY_BIN" -c "
import sys; sys.path.insert(0, '$PY_SCRIPT_DIR')
from transcript_db import TranscriptDB
with TranscriptDB() as db:
    path = db.render_txt('$BVID', page=$PAGE, output_dir='$OUTPUT_DIR_WIN')
    if path: print(path)
" 2>/dev/null)
    [ -n "$RENDERED" ] && OUTPUT_FILE="$RENDERED"
fi

# ===== 第四步（可选）：画面文字证据受约束校正 =====
# 需要下载视频抽帧 OCR，耗时较长，故默认关闭；在 .env 设 BILI_EVIDENCE_CORRECTION=true 开启
EVIDENCE_CORRECTION="${BILI_EVIDENCE_CORRECTION:-}"
if [ -z "$EVIDENCE_CORRECTION" ] && [ -f "$SCRIPT_DIR/../.env" ]; then
    EVIDENCE_CORRECTION=$(grep -E '^BILI_EVIDENCE_CORRECTION=' "$SCRIPT_DIR/../.env"         | tail -1 | cut -d= -f2- | tr -d '"' | tr 'A-Z' 'a-z')
fi
if [ -n "$BVID" ] && [ "$EVIDENCE_CORRECTION" = "true" ]; then
    echo ""
    echo "🔎 正在做画面文字证据校正（可选阶段）..."
    if CORRECTION_OUT=$("$PY_BIN" "$SCRIPT_DIR/correct_with_evidence.py"             --bvid "$BVID" --page "$PAGE" --download --apply --json 2>&1); then
        echo "$CORRECTION_OUT"
        # 校正产出了新版本，故重渲染一次，让交付文件反映"最优文本"（有校对版则交付校对版）
        echo "🔁 校正已产出新版本，重渲染交付文件..."
        "$PY_BIN" "$SCRIPT_DIR/render_delivery.py" --bvid "$BVID" --page "$PAGE"             --out "$OUTPUT_DIR" --json 2>&1             || echo "   ⚠️ 重渲染失败，可用 render_delivery.py 手工重建"
    else
        echo "$CORRECTION_OUT"
        echo "   ⚠️ 证据校正未完成；原稿与既有版本不受影响"
    fi
fi

echo ""
if [ -n "$BVID" ]; then
    # 写库成功才输出机器可读的完成标记（批量层唯一成功判据）；写库失败早已 exit 1。
    echo "[BILI-DONE] bvid=$BVID page=$PAGE file=${OUTPUT_FILE:-}"
    shell_log "SUCCESS" "转录流程完成: $BVID P$PAGE ($TRANSCRIPT_SOURCE)"
else
    echo "✅ 转录完成（未写库：未能从链接解析 BV 号）"
    shell_log "SUCCESS" "转录流程完成（未写库）: $VIDEO_URL_NORMALIZED"
fi
if [ -n "$OUTPUT_FILE" ]; then
    echo "📄 文件已保存: $OUTPUT_FILE"
    echo "$OUTPUT_FILE"
elif [ -n "$BVID" ]; then
    echo "📄 转录全文已写入数据库"
else
    echo "📄 转录正文见本次输出（未写库）"
fi
