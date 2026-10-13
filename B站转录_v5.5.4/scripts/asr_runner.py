#!/usr/bin/env python3
"""单轮 ASR 的执行、质量评分与多轮决策。

为什么独立成模块：B站 的 ASR 编排原本在 `bilibili_transcript.sh` 里，
模型一次性选定、失败即整条退出。质量层（`quality.py`）早已实现了
「按证据逐轮换参数」的决策算法，但 bash 无法调用它，于是成了死代码。
把编排下沉到这里，bash 只负责调用与报告，决策逻辑才真正接上流水线。

契约（供 bash 调用）：
    退出码 0 = 已达标；2 = 跑通但质量分不足（结果可用）；
    1 = 执行器没跑起来（值得退回单轮重试）；3 = 无语音（重跑无意义）。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from asr_types import AsrAttemptResult, AsrStrategy  # noqa: E402
from asr_chunk import (  # noqa: E402
    cpu_budget_error,
    max_chunk_seconds,
    plan_report,
    probe_duration,
    transcribe_chunked,
    vram_refusal,
)
from quality import (  # noqa: E402
    AsrAssets,
    HardwareBudget,
    QualityReport,
    evaluate_quality,
    next_strategy,
)

# 与流水线一致：最多三轮，清晰首轮即停。
MAX_ROUNDS = 3
_VAD_RELAXED = {"threshold": 0.35, "min_silence_duration_ms": 500,
                "min_speech_duration_ms": 100}

# 逐段诊断阈值（沿用抖音版口径，使两仓库的质量分可比）
AVG_LOGPROB_THRESHOLD = -1.0
WORD_PROBABILITY_THRESHOLD = 0.5
REPETITION_RATIO = 0.6
REPETITION_MIN_WORDS = 4
NO_SPEECH_THRESHOLD = 0.6


def normalize_segment(segment) -> dict:
    """把 faster-whisper 的一段结果转成带诊断的字典。

    质量层的 `repetitive` 判定读的是 `review_reasons`，而 TXT 只有纯文本；
    若不在这里打标，重复输出永远检不出来，第 2 轮的相应修复分支也就永不触发。
    """
    words = []
    for word in getattr(segment, "words", None) or []:
        token = str(getattr(word, "word", "") or "").strip()
        probability = getattr(word, "probability", None)
        if token:
            words.append((token, float(probability)
                          if isinstance(probability, (int, float)) else 1.0))

    avg_logprob = float(getattr(segment, "avg_logprob", 0.0) or 0.0)
    no_speech_prob = float(getattr(segment, "no_speech_prob", 0.0) or 0.0)
    low_confidence_words = [token for token, prob in words
                            if prob < WORD_PROBABILITY_THRESHOLD]

    reasons = []
    if avg_logprob < AVG_LOGPROB_THRESHOLD:
        reasons.append("low_avg_logprob")
    if low_confidence_words:
        reasons.append("low_confidence_words")
    if len(words) >= REPETITION_MIN_WORDS:
        counts = Counter(token for token, _ in words)
        if counts and counts.most_common(1)[0][1] / len(words) >= REPETITION_RATIO:
            reasons.append("repetitive")
    if no_speech_prob > NO_SPEECH_THRESHOLD:
        reasons.append("high_silence_prob")

    return {
        "start": round(float(getattr(segment, "start", 0.0) or 0.0), 2),
        "end": round(float(getattr(segment, "end", 0.0) or 0.0), 2),
        "text": str(getattr(segment, "text", "") or "").strip(),
        "avg_logprob": round(avg_logprob, 4),
        "no_speech_prob": round(no_speech_prob, 4),
        "low_confidence_words": low_confidence_words,
        "needs_review": bool(reasons),
        "review_reasons": reasons,
    }


def _default_max_model(device: str, vram_gb: float | None) -> str:
    """模型档位上限：GPU 按显存给到 medium，CPU 封顶 base。

    CPU 上 medium 的单轮耗时是 base 的数倍，把它作为重试上限会让「自动重试」
    比原样失败更慢；这里与抖音版同一口径。
    """
    if device != "cuda":
        return "base"
    return "medium" if (vram_gb or 0) >= 6 else "small"


def _vram_gb() -> float | None:
    try:
        import subprocess

        output = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        return float(output.stdout.strip().splitlines()[0]) / 1024.0
    except Exception:  # noqa: BLE001 无 nvidia-smi／无 GPU 都走不到这里
        return None


def _default_loader(model_name: str, *, device: str, compute_type: str):
    """惰性加载 faster-whisper 模型；导入失败时抛错交由调用方降级。"""
    from faster_whisper import WhisperModel

    from fw_whisper_main import gpu_slot, no_slot

    class _Model:
        def __init__(self):
            self._slot = gpu_slot() if device == "cuda" else no_slot()
            self._model = None
            self._slot.__enter__()
            try:
                self._model = WhisperModel(model_name, device=device,
                                           compute_type=compute_type)
            except Exception:
                self._slot.__exit__(None, None, None)
                raise

        def transcribe(self, audio, **kwargs):
            return self._model.transcribe(audio, **kwargs)

        def close(self):
            self._slot.__exit__(None, None, None)

    return _Model()


def _segments_payload(audio: Path, segments: list, info, strategy) -> dict:
    return {
        "model": strategy.model,
        "device": strategy.device,
        "compute_type": strategy.compute_type,
        "language": getattr(info, "language", "") or "",
        "language_probability": float(getattr(info, "language_probability", 0.0) or 0.0),
        "duration": float(getattr(info, "duration", 0.0) or 0.0),
        "segments": segments,
    }


def _write_results(output_dir: Path, audio: Path, segments: list, info,
                   strategy: AsrStrategy) -> tuple[Path | None, Path | None]:
    """写出 TXT 与分段侧车。

    空结果不落盘：留一个只有换行符的 TXT 会让 bash 的 `-s` 判定为成功，
    从而把「没识别出任何内容」当成正常交付（改造前的单轮脚本正是先删空文件再报错）。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    txt = output_dir / (Path(audio).stem + ".txt")
    if not segments:
        txt.unlink(missing_ok=True)
        txt.with_suffix(".segments.json").unlink(missing_ok=True)
        return None, None
    txt.write_text("\n".join(str(s.get("text", "")).strip() for s in segments) + "\n",
                   encoding="utf-8")
    sidecar = txt.with_suffix(".segments.json")
    sidecar.write_text(
        json.dumps(_segments_payload(audio, segments, info, strategy),
                   ensure_ascii=False),
        encoding="utf-8")
    return txt, sidecar


def run_once(audio: Path, output_dir: Path, *, model, model_name: str,
             device: str, compute_type: str, language: str | None = None,
             condition_on_previous_text: bool | None = None,
             repetition_penalty: float | None = None,
             no_repeat_ngram_size: int | None = None,
             vad_relaxed: bool = False, attempt_no: int = 1) -> dict:
    """执行一轮转写并评分；任何推理异常都由调用方捕获。"""
    strategy = AsrStrategy(
        model=model_name, device=device, compute_type=compute_type,
        language=language, condition_on_previous_text=condition_on_previous_text,
        repetition_penalty=repetition_penalty,
        no_repeat_ngram_size=no_repeat_ngram_size,
        vad_parameters=dict(_VAD_RELAXED) if vad_relaxed else None)

    kwargs = {"language": language, "beam_size": strategy.beam_size,
              "word_timestamps": True}
    if condition_on_previous_text is not None:
        kwargs["condition_on_previous_text"] = condition_on_previous_text
    if repetition_penalty is not None:
        kwargs["repetition_penalty"] = repetition_penalty
    if no_repeat_ngram_size is not None:
        kwargs["no_repeat_ngram_size"] = no_repeat_ngram_size
    if strategy.vad_parameters:
        kwargs["vad_parameters"] = strategy.vad_parameters

    raw, info = transcribe_chunked(
        model, str(audio), model_name=model_name, device=device,
        log=lambda message: sys.stderr.write(message + "\n"), **kwargs)
    segments = [normalize_segment(seg) for seg in raw]

    attempt = AsrAttemptResult(
        attempt_no=attempt_no, audio_variant=strategy.audio_variant,
        strategy=strategy,
        transcript_text="".join(s["text"] for s in segments),
        segments=segments, error_code="" if segments else "empty_output")
    report = evaluate_quality(attempt)

    txt, sidecar = _write_results(Path(output_dir), Path(audio), segments, info,
                                 strategy)
    return {"status": "ok" if segments else "empty", "txt": txt,
            "segments_file": sidecar, "segments": segments,
            "score": float(report.score), "passed": bool(report.passed),
            "report": report, "attempt": attempt}


def _assets(overrides: dict | None) -> AsrAssets:
    """本轮可用音轨；B站 无人声分离，默认只有原音。"""
    variants = dict((overrides or {}).get("variants") or {"original": True})
    models = tuple((overrides or {}).get("available_models")
                   or ("base", "small", "medium", "large-v3"))
    return AsrAssets(variants=variants, available_models=models)


def run_rounds(audio: Path, output_dir: Path, *, loader, initial: dict,
               max_rounds: int = MAX_ROUNDS, assets: dict | None = None,
               expected_language: str = "zh",
               analysis=None) -> dict:
    """最多 `max_rounds` 轮；每轮只改一个主要变量，取历史最佳轮为结果。

    `loader` 负责按 (model, device, compute_type) 构造模型，便于测试注入；
    生产路径传 `_default_loader`。
    """
    audio = Path(audio)
    if not audio.is_file():
        return {"status": "failed", "rounds": 0, "error": "audio_missing",
                "selected": None, "history": []}

    budget = HardwareBudget(
        device=initial.get("device", "cpu"),
        compute_type=initial.get("compute_type", "int8"),
        max_model=initial.get("max_model", "medium"),
        vram_gb=initial.get("vram_gb"))
    strategy = AsrStrategy(
        model=initial.get("model", "small"),
        device=initial.get("device", "cpu"),
        compute_type=initial.get("compute_type", "int8"),
        language=initial.get("language") or None)
    pool = _assets(assets)

    history: list[tuple[AsrAttemptResult, QualityReport]] = []
    outputs: list[dict] = []
    model = None
    try:
        for round_no in range(1, max(1, max_rounds) + 1):
            model = loader(strategy.model, device=strategy.device,
                           compute_type=strategy.compute_type)
            try:
                result = run_once(
                    audio, output_dir, model=model, model_name=strategy.model,
                    device=strategy.device, compute_type=strategy.compute_type,
                    language=strategy.language,
                    condition_on_previous_text=strategy.condition_on_previous_text,
                    repetition_penalty=strategy.repetition_penalty,
                    no_repeat_ngram_size=strategy.no_repeat_ngram_size,
                    vad_relaxed=strategy.vad_parameters is not None,
                    attempt_no=round_no)
            finally:
                close = getattr(model, "close", None)
                if callable(close):
                    close()
            history.append((result["attempt"], result["report"]))
            outputs.append(result)
            if result["passed"]:
                break
            following = next_strategy(history, pool, budget,
                                      expected_language=expected_language)
            if following is None:
                break
            strategy = following
    except Exception as exc:  # noqa: BLE001 加载/推理失败都不该抛穿编排
        if not history:
            return {"status": "failed", "rounds": 0,
                    "error": type(exc).__name__, "selected": None, "history": []}
        sys.stderr.write(f"后续轮次失败，沿用已有结果：{type(exc).__name__}\n")

    if not history:
        return {"status": "failed", "rounds": 0, "error": "no_rounds",
                "selected": None, "history": []}

    # 空结果绝不作为「最佳轮」：它会让整条流水线把「什么都没识别出」当成功交付。
    usable = [i for i, result in enumerate(outputs) if result["segments"]]
    if not usable:
        return {"status": "failed", "rounds": len(history),
                "error": "empty_output", "selected": None,
                "history": [{"round": i + 1, "score": float(rep.score),
                             "passed": bool(rep.passed)}
                            for i, (att, rep) in enumerate(history)]}

    best_index = max(
        usable, key=lambda i: (not history[i][0].hard_failures,
                               history[i][1].score))
    best_attempt, best_report = history[best_index]
    selected = dict(outputs[best_index])
    selected["round"] = best_index + 1
    return {"status": "ok", "rounds": len(history), "selected": selected,
            "score": float(best_report.score), "passed": bool(best_report.passed),
            "history": [{"round": i + 1, "score": float(rep.score),
                         "passed": bool(rep.passed),
                         "strategy": att.strategy.as_record()}
                        for i, (att, rep) in enumerate(history)]}


def _parse_bool(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="单轮/多轮 ASR 执行器")
    parser.add_argument("--audio", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--model", default="small")
    parser.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    parser.add_argument("--language", default="")
    parser.add_argument("--max-rounds", type=int, default=MAX_ROUNDS)
    parser.add_argument("--max-model", default="",
                        help="重试的模型上限；留空则按设备自动（CPU 封顶 base）")
    parser.add_argument("--condition-on-previous-text")
    parser.add_argument("--repetition-penalty", type=float)
    parser.add_argument("--no-repeat-ngram-size", type=int)
    parser.add_argument("--vad-relaxed", action="store_true")
    parser.add_argument("--single-round", action="store_true",
                        help="只跑一轮（等同于接入前的行为）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from fw_whisper_main import cuda_available

    device = "cuda" if args.device != "cpu" and cuda_available() else "cpu"
    compute_type = "float16" if device == "cuda" else "int8"
    max_model = args.max_model or _default_max_model(
        device, _vram_gb() if device == "cuda" else None)
    initial = {"model": args.model, "device": device,
               "compute_type": compute_type, "language": args.language or None,
               "max_model": max_model}

    audio = Path(args.audio)
    duration = probe_duration(audio)
    if duration > 0:
        sys.stderr.write(
            plan_report(duration, model=args.model, device=device,
                        chunk_seconds=max_chunk_seconds()) + "\n")
        refusal = vram_refusal(duration, model=args.model, device=device) \
            or cpu_budget_error(duration, model=args.model, device=device)
        if refusal:
            # 默认策略是快速失败：超阈值音频立刻判本机不可转，而不是先 OOM
            # 再退回 CPU 空磨数小时（实测白烧近 3 小时）。
            sys.stderr.write(f"⛔ {refusal}\n")
            return RC_UNRUNNABLE
    if args.single_round:
        try:
            model = _default_loader(args.model, device=device,
                                    compute_type=compute_type)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"模型加载失败：{type(exc).__name__}\n")
            return RC_UNRUNNABLE
        try:
            result = run_once(
                audio, Path(args.output_dir), model=model, model_name=args.model,
                device=device, compute_type=compute_type,
                language=args.language or None,
                condition_on_previous_text=(
                    _parse_bool(args.condition_on_previous_text)
                    if args.condition_on_previous_text is not None else None),
                repetition_penalty=args.repetition_penalty,
                no_repeat_ngram_size=args.no_repeat_ngram_size,
                vad_relaxed=args.vad_relaxed)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"转写失败：{type(exc).__name__}\n")
            return RC_UNRUNNABLE
        finally:
            close = getattr(model, "close", None)
            if callable(close):
                close()
        has_result = bool(result["txt"])
        outcome = {"status": "ok" if has_result else "failed",
                   "error": "" if has_result else "empty_output",
                   "rounds": 1,
                   "selected": ({"txt": str(result["txt"]),
                                 "score": result["score"],
                                 "passed": result["passed"], "round": 1,
                                 "segments_file": (str(result["segments_file"])
                                                   if result["segments_file"] else "")}
                                if has_result else None),
                   "score": result["score"], "passed": result["passed"],
                   "history": []}
    else:
        outcome = run_rounds(audio, Path(args.output_dir),
                             loader=_default_loader, initial=initial,
                             max_rounds=args.max_rounds,
                             expected_language=args.language or "zh")

    if args.json:
        selected = outcome.get("selected") or {}
        print(json.dumps({
            "status": outcome["status"], "rounds": outcome["rounds"],
            "score": outcome.get("score"), "passed": outcome.get("passed"),
            "txt": str(selected.get("txt") or ""),
            "segments_file": str(selected.get("segments_file") or ""),
            "selected_round": selected.get("round"),
            "history": outcome.get("history", []),
        }, ensure_ascii=False))
    return exit_code(outcome)


# 退出码语义（bash 据此决定是否值得退回单轮重跑）
RC_OK = 0               # 已达标，直接可用
RC_NEEDS_REVIEW = 2     # 跑通但质量分不足，结果仍可用
RC_UNRUNNABLE = 1       # 执行器没能跑起来（加载/推理异常）——退回单轮有意义
RC_NO_SPEECH = 3        # 正常跑完但没有语音——再跑一遍不会有不同结果


def exit_code(outcome: dict) -> int:
    if outcome.get("status") == "ok":
        return RC_OK if outcome.get("passed") else RC_NEEDS_REVIEW
    if outcome.get("error") == "empty_output":
        return RC_NO_SPEECH
    return RC_UNRUNNABLE


if __name__ == "__main__":
    sys.exit(main())
