#!/bin/bash
# 转录索引 · 一键建索引（增量自动）
# 用法: bash build.sh [--force|--dry-run]
set -u
cd "$(dirname "$0")" || exit 1
if [[ -x ".venv/Scripts/python.exe" ]]; then
  PY=".venv/Scripts/python.exe"
elif [[ -x ".venv/bin/python" ]]; then
  PY=".venv/bin/python"
else
  echo "✗ 未找到技能虚拟环境 Python：.venv/Scripts/python.exe 或 .venv/bin/python" >&2
  exit 1
fi
CONFIG_DIR="${TRANSCRIPT_INDEX_CONFIG_DIR:-$HOME/.config/转录索引}"
if [[ ! -f "$CONFIG_DIR/.env" && ! -f "${APPDATA:-}/转录索引/.env" && ! -f ".env" ]]; then
  echo "✗ 未找到转录索引配置，请创建用户私有 .env（见 .env.example）" >&2
  exit 1
fi
FORCE=""
MODE=""
if [ "${1:-}" = "--force" ]; then FORCE="--force"; MODE="--force"; fi
if [ "${1:-}" = "--dry-run" ]; then MODE="--dry-run"; fi
echo "== ① 校对（规则：繁转简+断行合并+术语纠错） =="
"$PY" scripts/校对.py $MODE || exit 1
if [ "$MODE" = "--dry-run" ]; then
  echo "== 预览结束：未写入索引、台账、向量或源文件 =="
  exit 0
fi
echo "== ② 切条目（DeepSeek LLM，断点续跑） =="
CUT_OUT=$("$PY" scripts/切条目.py $FORCE)
CUT_STATUS=$?
echo "$CUT_OUT"
NEED_VECTOR=1
if [[ $CUT_STATUS -eq 2 ]]; then
  echo "== 切条目全部失败，跳过建向量（索引未变） ==" >&2
  NEED_VECTOR=0
elif echo "$CUT_OUT" | grep -qE "无新文件|强制模式，无匹配文件"; then
  echo "== 无新增/变更，跳过建向量（优化 C） =="
  NEED_VECTOR=0
elif [[ $CUT_STATUS -ne 0 ]]; then
  echo "✗ 切条目阶段失败" >&2
  NEED_VECTOR=0
fi
if [[ $NEED_VECTOR -eq 1 ]]; then
  echo "== ③ 建向量（本地 bge，全量幂等） =="
  "$PY" scripts/建向量.py || exit 1
fi
echo "== ④ 数据完整性体检（只读） =="
CHECK_OUT=$("$PY" scripts/_检查行号引用.py --json 2>/dev/null)
CHECK_RC=$?
if [[ $CHECK_RC -eq 0 && -n "$CHECK_OUT" ]]; then
  echo "$CHECK_OUT" | "$PY" -c "import sys,json;d=json.loads(sys.stdin.read().splitlines()[-1]);print(f\"  未索引 {d['unindexed_count']} | 越界 {d['out_of_bounds']} | 孤儿 {d['orphan_count']} | 干净 {d['clean_files']}/{d['idx_count']}\")"
else
  echo "  ⚠ 体检脚本执行失败"
fi
SNAP_DEST=$("$PY" -c "import sys;sys.path.insert(0,'scripts');import common;print(common.SNAPSHOT_DEST or '')" 2>/dev/null)
if [[ -n "$SNAP_DEST" ]]; then
  echo "== ⑤ 快照同步（SNAPSHOT_DEST 已配置） =="
  "$PY" scripts/_同步快照.py "$SNAP_DEST" || echo "  ⚠ 快照同步失败"
else
  echo "== ⑤ 快照同步：未配置 SNAPSHOT_DEST，跳过 =="
fi
echo "== 完成 =="
