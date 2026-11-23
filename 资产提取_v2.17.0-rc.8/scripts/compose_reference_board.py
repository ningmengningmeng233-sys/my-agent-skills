#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""人物参考板拼版（规格 6.2，工单03-Task7）。

**只由已通过的图片自动拼版，不调用任何生成模型**：
- 只接受 ACCEPTED / READY_AS_REFERENCE 的产物；
- 固定画布与确定性排版（同一组输入必得同一字节）；
- 任一输入图片哈希变化（过期）则旧拼版失效，需重新合成。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from typing import Iterable, Sequence

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))
sys.path.insert(0, str(SKILL_DIR / "scripts" / "engine"))

from engine.visual_schema import VisualPlan, VisualTask, load_visual_plan  # noqa: E402

BOARD_SUBDIR = ("视觉资产", "拼版")
CELL_WIDTH = 768
CELL_HEIGHT = 1152
GAP = 24
BACKGROUND = (184, 184, 184)  # 与资产提示词的素灰背景一致


class BoardError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}：{detail}" if detail else code)


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def compose_person_board(accepted_images: Sequence[Path], output: Path) -> Path:
    """把已通过图片横向排成一张参考板（Pillow 本地合成，不调用生成模型）。"""
    images = [Path(p) for p in accepted_images if Path(p).is_file()]
    if not images:
        raise BoardError("NO_ACCEPTED_IMAGES", "拼版只接受已通过图片，当前没有可用输入")
    try:
        from PIL import Image
    except ImportError as e:  # pragma: no cover - 精简安装
        raise BoardError("PILLOW_REQUIRED",
                         "拼版需要 Pillow：pip install \"Pillow>=10.0,<12\"") from e

    canvas_w = CELL_WIDTH * len(images) + GAP * (len(images) + 1)
    canvas_h = CELL_HEIGHT + GAP * 2
    canvas = Image.new("RGB", (canvas_w, canvas_h), BACKGROUND)
    for index, path in enumerate(images):
        with Image.open(path) as img:
            tile = img.convert("RGB")
            tile.thumbnail((CELL_WIDTH, CELL_HEIGHT), Image.LANCZOS)
            left = GAP + index * (CELL_WIDTH + GAP) + (CELL_WIDTH - tile.width) // 2
            top = GAP + (CELL_HEIGHT - tile.height) // 2
            canvas.paste(tile, (left, top))
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    canvas.save(tmp, format="PNG", optimize=False, compress_level=6)
    os.replace(tmp, target)
    return target


def _board_meta_path(lib: Path, task_id: str) -> Path:
    return Path(lib) / BOARD_SUBDIR[0] / BOARD_SUBDIR[1] / f"{task_id}.toml"


def board_is_fresh(lib: Path, task_id: str) -> bool:
    """拼版是否仍然新鲜：清单存在且每个输入哈希与当前产物一致。"""
    meta = _board_meta_path(Path(lib), task_id)
    if not meta.is_file():
        return False
    import re

    text = meta.read_text(encoding="utf-8")
    stated = set(re.findall(r'"([0-9a-f]{64})"', text))
    plan = load_visual_plan(Path(lib) / "视觉资产" / "计划.toml")
    task = plan.by_id(task_id)
    if task is None:
        return False
    # 以磁盘真实字节为准：任一输入被替换/篡改即判定过期
    current = [_sha256(Path(lib) / a.image) for a in task.attempts if a.image
               and (Path(lib) / a.image).is_file()]
    return bool(stated) and bool(current) and all(h in stated for h in current)


def board_from_task(lib: Path, task_id: str, plan: VisualPlan | None = None) -> Path:
    """由任务已通过的产物合成参考板（要求该任务本身已通过）。"""
    lib = Path(lib)
    plan = plan or load_visual_plan(lib / "视觉资产" / "计划.toml")
    task = plan.by_id(task_id)
    if task is None:
        raise BoardError("UNKNOWN_TASK", f"计划中没有任务 {task_id}")
    if not task.is_accepted:
        raise BoardError("TASK_NOT_ACCEPTED",
                         f"任务 {task_id} 状态为 {task.status}（未通过不得拼版）")
    images = [lib / a.image for a in task.attempts if a.image]
    output = lib / BOARD_SUBDIR[0] / BOARD_SUBDIR[1] / f"{task_id}.png"
    compose_person_board(images, output)
    hashes = [a.image_sha256 for a in task.attempts if a.image_sha256]
    lines = [
        "# 拼版清单（由已通过图片本地合成；输入哈希变化即失效）",
        f'task_id = "{task_id}"',
        f'board = "{BOARD_SUBDIR[0]}/{BOARD_SUBDIR[1]}/{task_id}.png"',
        "input_hashes = [" + ", ".join(f'"{h}"' for h in hashes) + "]",
        "generated_by = \"compose_reference_board.py（Pillow 本地合成，未调用生成模型）\"",
        "",
    ]
    _board_meta_path(lib, task_id).write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="由已通过图片合成人物参考板（本地拼版）")
    parser.add_argument("--lib", required=True)
    parser.add_argument("--task", required=True, help="任务 ID（必须已通过）")
    parser.add_argument("--images", nargs="*", help="直接指定图片（默认取该任务已通过产物）")
    args = parser.parse_args()
    lib = Path(args.lib)
    try:
        if args.images:
            out = compose_person_board([Path(p) for p in args.images],
                                       lib / BOARD_SUBDIR[0] / BOARD_SUBDIR[1]
                                       / f"{args.task}.png")
        else:
            out = board_from_task(lib, args.task)
    except BoardError as e:
        print(f"❌ 拼版失败（{e.code}）：{e.detail}")
        return 1
    print(f"✅ 参考板已生成: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


_ = (Iterable, VisualTask)  # 保持 typing/契约导入
