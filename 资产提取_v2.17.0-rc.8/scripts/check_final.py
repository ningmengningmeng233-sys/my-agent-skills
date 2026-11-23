#!/usr/bin/env python3
"""Validate required sections in the final reverse-derived asset file."""
import argparse
import sys
from pathlib import Path

REQUIRED = [
    "## 一、项目基础信息表",
    "## 人物资产提取表",
    "## 场景资产提取表",
    "## 道具资产提取表",
    "## 服装妆造资产提取表",
    "## 六、分集/章节资产使用表",
    "## 七、导入资产总表",
    "## 八、文件夹结构建议",
    "## 衍生需求汇总",
    "## 合并统计",
    "## 待确认附录",
    "## 九、交付物说明",
]


def check_modules(text_or_path: str | Path) -> list[str]:
    """Return the list of required section headings missing from text or file."""
    path = Path(text_or_path)
    if path.exists():
        text = path.read_text(encoding="utf-8")
    else:
        text = str(text_or_path)
    return [m.removeprefix("## ") for m in REQUIRED if m not in text]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="反推文件模块齐全性校验")
    parser.add_argument("final", help="反推_最终.md 路径")
    args = parser.parse_args()
    try:
        text = Path(args.final).read_text(encoding="utf-8")
    except OSError as e:
        print(f"❌ 无法读取反推文件: {e}")
        return 1
    missing = check_modules(text)
    if missing:
        print(f"❌ 反推文件缺 {len(missing)} 个模块: {'、'.join(missing)}")
        return 1
    print(f"✅ 反推文件模块齐全（{len(REQUIRED)} 项全含）: {args.final}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
