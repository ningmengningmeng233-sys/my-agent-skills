#!/usr/bin/env python3
"""Small Whisper-CLI-compatible entry point backed by faster-whisper."""

import argparse
import json
from contextlib import contextmanager
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from asr_chunk import (  # noqa: E402
    cpu_budget_error,
    max_chunk_seconds,
    plan_report,
    probe_duration,
    transcribe_chunked,
    vram_refusal,
)


def write_segments_sidecar(txt_path: Path, segments: list, info, model: str,
                          device: str, compute_type: str) -> Path:
    """把逐段时间轴写出到与 TXT 同名的 `.segments.json`。

    质量评分需要每段的起止时间与置信度；TXT 只有纯文本，无法复原这些信息，
    因此转写时一并落盘，由 scripts/record_transcript.py 入库。
    """
    sidecar = txt_path.with_suffix(".segments.json")
    payload = {
        "model": model,
        "device": device,
        "compute_type": compute_type,
        "language": getattr(info, "language", "") or "",
        "language_probability": float(getattr(info, "language_probability", 0.0) or 0.0),
        "duration": float(getattr(info, "duration", 0.0) or 0.0),
        "segments": segments,
    }
    sidecar.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return sidecar


def _nvidia_bin_dirs(site_packages: Path) -> list[Path]:
    """pip 安装的 nvidia-* 运行时 wheel 把 DLL 放在 site-packages/nvidia/<pkg>/bin。"""
    root = Path(site_packages) / "nvidia"
    return sorted(d for d in root.glob("*/bin") if d.is_dir())


_registered_dll_dirs: list[object] = []


def _register_nvidia_dll_dirs(site_packages: Path | None = None) -> None:
    """把 nvidia wheel 的 bin 目录加入 DLL 搜索路径（Windows）。"""
    add_dll = getattr(os, "add_dll_directory", None)
    if add_dll is None:
        return
    if site_packages is None:
        site_packages = Path(sys.prefix) / "Lib" / "site-packages"
    for bin_dir in _nvidia_bin_dirs(Path(site_packages)):
        try:
            _registered_dll_dirs.append(add_dll(str(bin_dir)))
        except OSError:
            continue


def _cuda_runtime_loadable() -> bool:
    """cublas/cudnn 运行时 DLL 是否能真正加载。

    CTranslate2 的 float16 能力声明不代表运行时 DLL 存在；只信它会在加载模型时
    报 cublas64_12.dll is not found 而整条流水线失败。
    """
    import ctypes

    _register_nvidia_dll_dirs()
    try:
        ctypes.CDLL("cublas64_12.dll")
    except OSError:
        return False
    for name in ("cudnn64_9.dll", "cudnn_ops64_9.dll"):
        try:
            ctypes.CDLL(name)
            return True
        except OSError:
            continue
    return False


def cuda_available() -> bool:
    try:
        import ctranslate2

        if "float16" not in ctranslate2.get_supported_compute_types("cuda"):
            return False
    except Exception:
        return False
    return _cuda_runtime_loadable()


@contextmanager
def gpu_slot():
    """Serialize model loading/inference while a second batch slot downloads."""
    lock_path = Path(os.environ.get("BILI_GPU_LOCK") or Path(__file__).resolve().parent.parent / "state" / "gpu.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        lock.seek(0, 2)
        if lock.tell() == 0:
            lock.write(b"\0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.25)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


@contextmanager
def no_slot():
    yield


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="faster-whisper TXT shim")
    parser.add_argument("audio", nargs="?")
    parser.add_argument("--model", default="small")
    parser.add_argument("--output_format", choices=["txt"], default="txt")
    parser.add_argument("--output_dir", default=".")
    parser.add_argument("--language")
    parser.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    parser.add_argument("--probe", action="store_true", help="print cuda or cpu")
    args = parser.parse_args(argv)
    if args.probe:
        print("cuda" if cuda_available() else "cpu")
        return 0
    if not args.audio:
        parser.error("audio is required")

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("faster-whisper 未安装；请按 references/SETUP.md 安装", file=sys.stderr)
        return 2

    device = "cuda" if args.device != "cpu" and cuda_available() else "cpu"
    if args.device == "cuda" and device != "cuda":
        print("CUDA float16 不可用", file=sys.stderr)
        return 2
    compute_type = "float16" if device == "cuda" else "int8"
    attempts = [(device, compute_type)]
    if device == "cuda" and args.device == "auto":
        # cuda_available() 只反映 CTranslate2 报告的计算能力，不代表 cublas/cudnn
        # 运行库真的能加载；加载失败时自动退回 CPU，而不是让整条流水线失败。
        attempts.append(("cpu", "int8"))
    duration = probe_duration(args.audio)
    if duration > 0:
        print(plan_report(duration, model=args.model, device=device,
                          chunk_seconds=max_chunk_seconds()),
              file=sys.stderr, flush=True)
    last_error = None
    for index, (device, compute_type) in enumerate(attempts):
        if index:
            print(f"CUDA 不可用（{last_error}），自动改用 cpu/int8", file=sys.stderr)
        if duration > 0:
            refusal = vram_refusal(duration, model=args.model, device=device) \
                or cpu_budget_error(duration, model=args.model, device=device)
            if refusal:
                # 默认策略是快速失败：超阈值音频立刻判本机不可转，
                # 而不是回退 CPU 无声空磨数小时。
                last_error = refusal
                print(f"⛔ {refusal}", file=sys.stderr, flush=True)
                continue
        print(f"faster-whisper: {device}/{compute_type}, model={args.model}", flush=True)
        try:
            with gpu_slot() if device == "cuda" else no_slot():
                model = WhisperModel(args.model, device=device, compute_type=compute_type)
                segments, info = transcribe_chunked(
                    model, args.audio, model_name=args.model, device=device,
                    log=lambda message: print(message, file=sys.stderr, flush=True),
                    language=args.language, beam_size=5)
                output = Path(args.output_dir) / (Path(args.audio).stem + ".txt")
                output.parent.mkdir(parents=True, exist_ok=True)
                # Consume the generator: inference happens during iteration.
                collected = []
                with output.open("w", encoding="utf-8") as stream:
                    for segment in segments:
                        stream.write(segment.text.strip() + "\n")
                        collected.append({
                            "start": float(segment.start),
                            "end": float(segment.end),
                            "text": segment.text.strip(),
                            "avg_logprob": float(getattr(segment, "avg_logprob", 0.0) or 0.0),
                            "no_speech_prob": float(getattr(segment, "no_speech_prob", 0.0) or 0.0),
                        })
                if not output.stat().st_size:
                    output.unlink()
                    print("空转录结果", file=sys.stderr)
                    return 1
                write_segments_sidecar(output, collected, info, args.model, device,
                                       compute_type)
                print(output)
                return 0
        except Exception as exc:
            last_error = exc
    print(f"faster-whisper 转录失败: {last_error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
