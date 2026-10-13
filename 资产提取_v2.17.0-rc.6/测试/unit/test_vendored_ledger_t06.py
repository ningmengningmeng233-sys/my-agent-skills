"""The independent package must emit records without a sibling schemas repo."""
import json
from pathlib import Path


def test_vendored_ledger_writes_with_no_sibling_dependency(tmp_path, monkeypatch):
    from engine import perf_ledger
    root = Path(__file__).resolve().parents[2]
    monkeypatch.delenv("UNIFIED_LEDGER_DISABLE", raising=False)
    monkeypatch.setenv("UNIFIED_LEDGER_PY", str(root / "vendor/schemas/ledger.py"))
    out = tmp_path / "unified.jsonl"
    monkeypatch.setenv("UNIFIED_LEDGER_PATH", str(out))
    perf_ledger.append(tmp_path, {
        "step": "vendor-test", "node": "mock", "elapsed_ms": 1,
        "prompt_tokens": 7, "completion_tokens": 3, "error": "",
    })
    assert out.is_file()
    row = json.loads(out.read_text(encoding="utf-8"))
    assert row["stage"] == "vendor-test"
    assert row["tokens_in"] == 7
