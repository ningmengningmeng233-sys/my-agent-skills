#!/usr/bin/env python3
"""转写前的预算闸门（给 bash 用的薄封装）。

为什么要有这一层：shell 在**下载音频之前**就从 `--dump-json` 拿到了时长，
把闸门放在下载之前，超阈值条目几秒内就能失败；否则要先下完 150 MB 才拒绝，
每条白花一两分钟（实测 6 小时分P 重试 3 次 ≈ 6 分钟）。

判定复用 `asr_chunk` 的同一套函数，保证 shell 闸门与执行器闸门口径一致。

退出码：0 = 放行；3 = 拒绝（原因已打印）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from asr_chunk import (  # noqa: E402
    chunking_enabled,
    cpu_budget_error,
    max_chunk_seconds,
    plan_report,
    vram_refusal,
)

RC_OK = 0
RC_REFUSED = 3


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="转写前的显存/耗时预算闸门")
    parser.add_argument("--seconds", type=float, required=True,
                        help="音频时长（秒），来自视频元数据")
    parser.add_argument("--model", default="medium")
    parser.add_argument("--device", choices=["cuda", "cpu"], default=None,
                        help="留空则按 CUDA 是否可用自动判断")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    device = args.device
    if device is None:
        from fw_whisper_main import cuda_available

        device = "cuda" if cuda_available() else "cpu"

    refusal = vram_refusal(args.seconds, model=args.model, device=device) \
        or cpu_budget_error(args.seconds, model=args.model, device=device)
    plan = plan_report(args.seconds, model=args.model, device=device,
                       chunk_seconds=max_chunk_seconds())
    payload = {"ok": not refusal, "device": device, "model": args.model,
               "seconds": float(args.seconds), "plan": plan,
               "chunking": chunking_enabled(), "reason": refusal}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    elif refusal:
        print(plan)
        print(f"⛔ {refusal}")
    else:
        print(plan)
    return RC_REFUSED if refusal else RC_OK


if __name__ == "__main__":
    sys.exit(main())
