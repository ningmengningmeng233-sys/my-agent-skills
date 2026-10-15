#!/bin/bash
# bilibili-auto-transcript 便捷入口
# 用法: bash transcribe.sh "<B站视频链接>" [输出目录]  （多P: 链接带 ?p=N）
# 作用: 优先使用技能自己的虚拟环境，再调用主脚本

SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_BIN="$SKILL_DIR/.venv/Scripts"
[ -d "$VENV_BIN" ] || VENV_BIN="$SKILL_DIR/.venv/bin"
export PATH="$VENV_BIN:$PATH"
export PYTHONIOENCODING=utf-8

if [ -z "$1" ]; then
    echo "用法: bash transcribe.sh <B站视频链接>   （多P: 链接带 ?p=N）"
    exit 1
fi

bash "$SKILL_DIR/scripts/bilibili_transcript.sh" "$@"
