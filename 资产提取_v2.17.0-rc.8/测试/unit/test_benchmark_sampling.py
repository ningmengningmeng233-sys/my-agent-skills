"""Repeated measurement must match the published zero-delay evidence basis."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_asset_compiler_benchmark as bench  # noqa: E402


def test_zero_delay_uses_repeated_local_measurements(tmp_path, monkeypatch):
    samples = {0.0: iter([8000, 2000]), 1000.0: iter([35000, 29000])}

    def measure(fixture, dest, delay_ms):
        return {"measured": True, "wall_ms": next(samples[delay_ms]),
                "text_calls": 27, "lib": str(dest)}

    monkeypatch.setattr(bench, "_measure_old_chain", measure)
    monkeypatch.setattr(bench, "_measure_new_chain", lambda *a, **k: {
        "measured": True, "wall_ms": 1000, "text_calls": 10,
        "lib": str(a[1])})
    runs = bench.measure_sweep(tmp_path, tmp_path, [0.0, 1000.0], 2)
    assert runs[0.0]["old"]["wall_ms"] == 2000
    assert runs[0.0]["old"]["repeats"] == 2
    assert runs[0.0]["old"]["wall_samples_ms"] == [8000, 2000]


def test_faster_sample_with_different_call_count_is_incomparable(tmp_path):
    samples = iter([(8000, 27), (2000, 28)])

    def measure(fixture, dest, delay_ms):
        wall, calls = next(samples)
        return {"measured": True, "wall_ms": wall, "text_calls": calls}

    run = bench.measure_repeated("old", measure, tmp_path, tmp_path, 0.0, 2)
    assert run["measured"] is False
    assert run["text_calls"] is None
    assert "uncomparable" in run
