"""Exercise installation grouping against actual non-similarity call paths."""
from pathlib import Path
import json
import re
import subprocess
import sys

import pytest
import runtime_contract as rc

ROOT = Path(__file__).resolve().parents[1]

def test_numpy_is_core_for_feedback_and_judge_cache():
    assert rc.EXTERNAL_DEPENDENCIES["numpy"]["optional_group"] is None
    assert re.search(r"(?m)^numpy==", (ROOT / "requirements-v2.lock").read_text())
    assert not re.search(r"(?m)^numpy==", (ROOT / "requirements-v2-similarity.lock").read_text())

def test_complete_report_reads_both_dependency_locks():
    report = rc.capability_report(ROOT)
    assert all(e["locked_version"] for e in report["external_dependencies"]), report

def test_normalized_lock_names_and_conflicts(tmp_path):
    (tmp_path / "requirements-v2.lock").write_text("huggingface-hub==1.33.0\n", encoding="utf-8")
    (tmp_path / "requirements-v2-similarity.lock").write_text("", encoding="utf-8")
    report = rc.capability_report(tmp_path)
    hf = next(e for e in report["external_dependencies"] if e["import"] == "huggingface_hub")
    assert hf["locked_version"] == "1.33.0"
    (tmp_path / "requirements-v2-similarity.lock").write_text("huggingface_hub==0.23.0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="冲突"):
        rc.capability_report(tmp_path)

@pytest.mark.parametrize("module,missing", [("v2_similarity", "datasets"), ("v2_embeddings", "huggingface_hub")])
def test_missing_optional_dependency_points_to_install_lock(module, missing):
    # A fresh child prevents existing sys.modules from masking an absent optional dependency.
    code = """import importlib.abc,sys
sys.path.insert(0, sys.argv[1])
class Missing(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == sys.argv[3]:
            raise ModuleNotFoundError('controlled missing dependency', name=fullname)
sys.meta_path.insert(0, Missing())
try:
    module = __import__(sys.argv[2])
    if sys.argv[2] == "v2_embeddings":
        module.BGEProvider(cache_dir="unused-local-cache")
except ModuleNotFoundError as exc:
    assert 'requirements-v2-similarity.lock' in str(exc), str(exc)
else:
    raise AssertionError('optional dependency was not exercised')
"""
    result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", code, str(ROOT / "scripts"), module, missing], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
