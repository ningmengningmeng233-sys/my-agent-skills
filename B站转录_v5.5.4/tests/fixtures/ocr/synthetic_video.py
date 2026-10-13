"""生成 OCR 测试用的合成视频（帧由 PIL 画，视频由 FFmpeg 合成）。

产物只写临时目录，绝不提交二进制视频；详见本目录 README.md。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

WIDTH, HEIGHT, FPS, SECONDS = 640, 360, 8, 6
# 只接受能渲染中文的字体：拉丁字体（如 DejaVu）会让识别结果变成乱码
FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)
SUBTITLE_FIRST = "端午粽子"
SUBTITLE_SECOND = "不分南北"
STATIC_LINE = "参数 2026-06-19 1.2 GB"

_AVAILABLE = shutil.which("ffmpeg") is not None


def ffmpeg_available() -> bool:
    return _AVAILABLE


def font_available() -> bool:
    return any(Path(item).is_file() for item in FONT_CANDIDATES)


def _font(size: int):
    from PIL import ImageFont

    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return None


def _frame(index: int):
    from PIL import Image, ImageDraw

    second = index / FPS
    dark = second < 3.0
    background = (18, 18, 24) if dark else (238, 238, 240)
    ink = (255, 255, 255) if dark else (24, 24, 28)
    image = Image.new("RGB", (WIDTH, HEIGHT), background)
    draw = ImageDraw.Draw(image)
    subtitle = SUBTITLE_FIRST if dark else SUBTITLE_SECOND
    draw.text((WIDTH // 2 - 72, int(HEIGHT * 0.80)), subtitle,
              fill=ink, font=_font(34))
    if not dark:
        draw.text((40, 150), STATIC_LINE, fill=ink, font=_font(22))
    return image


def build_synthetic_video(target_dir: Path, *, keep_frames: bool = False) -> Path:
    """画帧并合成 6 秒 640x360 视频；返回 mp4 路径。"""
    if not _AVAILABLE:
        raise RuntimeError("需要系统 FFmpeg 才能生成合成视频")
    target_dir = Path(target_dir)
    frames_dir = target_dir / "png"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for index in range(SECONDS * FPS):
        _frame(index).save(frames_dir / f"f{index:04d}.png")
    video = target_dir / "synthetic.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-framerate", str(FPS), "-i", str(frames_dir / "f%04d.png"),
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "1", str(video)],
        check=True, capture_output=True)
    if not keep_frames:
        shutil.rmtree(frames_dir, ignore_errors=True)
    return video
