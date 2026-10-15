#!/usr/bin/env bash
# 一键入口（加速改法工单 T9）：只做编排，不改各步骤脚本行为。
# 用法：run_pipeline.sh --lib <资产库> --input <小说文件> [--genre 古风] [--style 仙侠奇幻风]
#       [--dry-run] [--force]
# 任一步骤失败 → 立即停止，打印原因码与失败步骤名；再跑同一条命令即可续跑
# （断点跳过逻辑由各步骤脚本自带：IndexDB 指纹 / --skip-existing）。
set -u

LIB=""
INPUT=""
GENRE="古风"
STYLE="仙侠奇幻风"
DRY_RUN=0
FORCE_FLAG=""
NOVEL_OVERRIDE=""
AUTHOR_OVERRIDE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --lib) LIB="$2"; shift 2 ;;
    --input) INPUT="$2"; shift 2 ;;
    --genre) GENRE="$2"; shift 2 ;;
    --style) STYLE="$2"; shift 2 ;;
    --novel) NOVEL_OVERRIDE="$2"; shift 2 ;;
    --author) AUTHOR_OVERRIDE="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --force) FORCE_FLAG="--force-retry"; shift ;;
    *) echo "未知参数: $1"; exit 2 ;;
  esac
done

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python}"

if [[ $DRY_RUN -eq 1 ]]; then
  echo "【dry-run】一键链路将按以下顺序执行（不出网、不调用模型、不落账）："
  echo "  0. 初始化      scripts/maintenance.py init（状态文件已存在时自动跳过）"
  echo "  1. 拆分        scripts/split_segments.py"
  echo "  2. 登记        scripts/scan_registry.py"
  echo "  3. 提取        scripts/extract_segments.py"
  echo "  4. 聚合        scripts/aggregate.py"
  echo "  5. 对账        scripts/scan_registry.py --include-claims"
  echo "  6. 服饰        scripts/prepare_costume_input.py"
  echo "  7. 描述        scripts/generate_prompts.py --enrich"
  echo "  8. 基础提示词  scripts/generate_prompts.py --base"
  echo "  9. 衍生提示词  scripts/generate_prompts.py --deriv"
  echo " 10. verify      scripts/maintenance.py verify"
  exit 0
fi

[[ -n $LIB ]] || { echo "缺少 --lib <资产库>"; exit 2; }
[[ -n $INPUT ]] || { echo "缺少 --input <小说文件>"; exit 2; }
[[ -f $INPUT ]] || { echo "输入文件不存在: $INPUT"; exit 2; }

NOVEL="${NOVEL_OVERRIDE:-$(basename "$INPUT")}"
NOVEL="${NOVEL%.*}"
AUTHOR="${AUTHOR_OVERRIDE:-${ASSET_AUTHOR:-佚名}}"
LIB="$(cd "$LIB" 2>/dev/null && pwd || echo "$LIB")"
mkdir -p "$LIB"
export ASSET_LIB_DIR="$LIB"

run_step() {
  local num="$1" name="$2"; shift 2
  echo "── [步骤 $num] $name"
  "$@"
  local rc=$?
  if [[ $rc -ne 0 ]]; then
    echo ""
    echo "❌ 失败步骤: $name（原因码 rc=$rc）"
    echo "   再跑同一条命令即可续跑：已完成步骤会按断点自动跳过。"
    exit "$rc"
  fi
}

if [[ ! -f "$LIB/.start-state.json" ]]; then
  run_step 0 "初始化"   "$PY" scripts/maintenance.py init --lib "$LIB" --input-source "$INPUT" --genre "$GENRE" --style "$STYLE"
fi
run_step 1 "拆分"      "$PY" scripts/split_segments.py "$INPUT" --novel "$NOVEL" --author "$AUTHOR" --lib "$LIB" --overlap 1
run_step 2 "登记"      "$PY" scripts/scan_registry.py "$LIB/原文段/$NOVEL-$AUTHOR" --lib "$LIB"
run_step 3 "提取"      "$PY" scripts/extract_segments.py "$LIB/原文段/$NOVEL-$AUTHOR" --lib "$LIB" $FORCE_FLAG
run_step 4 "聚合"      "$PY" scripts/aggregate.py "$LIB/段产物/$NOVEL-$AUTHOR" "$LIB/原文段/$NOVEL-$AUTHOR" --lib "$LIB"
run_step 5 "对账"      "$PY" scripts/scan_registry.py --include-claims "$LIB/.process/登记表对账报告.md" --lib "$LIB"
run_step 6 "服饰"      "$PY" scripts/prepare_costume_input.py "$LIB/反推_最终.md" --genre "$GENRE" --lib "$LIB"
run_step 7 "描述"      "$PY" scripts/generate_prompts.py --enrich "$LIB/反推_最终.md" --genre "$GENRE" --lib "$LIB" --style "$STYLE" --yes
run_step 8 "基础提示词" "$PY" scripts/generate_prompts.py --base "$LIB/反推_最终.md" --genre "$GENRE" --lib "$LIB" --style "$STYLE" --yes --view-mode full
run_step 9 "衍生提示词" "$PY" scripts/generate_prompts.py --init-schedule "$LIB/反推_最终.md" --genre "$GENRE" --lib "$LIB"
run_step 9 "衍生提示词" "$PY" scripts/generate_prompts.py --deriv --genre "$GENRE" --lib "$LIB" --style "$STYLE" --yes --view-mode full
run_step 10 "verify"   "$PY" scripts/maintenance.py verify --lib "$LIB" --style "$STYLE"

echo "✅ 全部步骤完成：$LIB"
