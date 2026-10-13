# -*- coding: utf-8 -*-
"""F-10：发布档位与裁剪清单判定。

红线：不许「文件不存在就放过」。缺件只有两种可接受情形之一才算过——
要么它本来就不在发布面上（命中清单声明的裁剪模式，且证据索引里存着它的 sha256），
要么它压根是 undeclared，必须报错。声明要随包的文件被删掉，永远报错。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from release_surface import ReleaseSurface  # noqa: E402

LOG = "evidence/R1/logs/static-test_demo.py.log"
INDEX = "evidence/R1/evidence-index.json"
KEPT = "scripts/audit_cli.py"


@pytest.fixture
def package(tmp_path):
    root = tmp_path / "pkg"
    (root / "scripts").mkdir(parents=True)
    (root / "evidence" / "R1" / "logs").mkdir(parents=True)
    (root / KEPT).write_text("# kept runtime file\n", encoding="utf-8")
    (root / LOG).write_text("gate run log line\n", encoding="utf-8")
    (root / INDEX).write_text(json.dumps({"files": [{"path": LOG, "sha256": "a" * 64}]}),
                              encoding="utf-8")
    manifest = {"release_tier": "local-stable", "withheld_patterns": [".log"],
                "closure_report": "evidence/R1",
                "files": {KEPT: {"sha256": "b" * 64}, INDEX: {"sha256": "c" * 64},
                          LOG: {"sha256": "d" * 64}}}
    (root / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_declared_artifact_deleted_still_errors(package):
    # 清单声明该日志随包，但磁盘上被删：即使它长得像被裁剪文件，也必须报错。
    surface = ReleaseSurface(package)
    assert (package / LOG).is_file()
    (package / LOG).unlink()
    ok, why = surface.check(LOG)
    assert not ok and "不见了" in why, why


def test_withheld_log_is_accepted_only_when_index_covers_it(package):
    surface = ReleaseSurface(package)
    manifest = json.loads((package / "release-manifest.json").read_text(encoding="utf-8"))
    del manifest["files"][LOG]            # 这一档不随包分发日志
    (package / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / LOG).unlink()              # 磁盘上确实没有
    surface = ReleaseSurface(package)
    ok, why = surface.check(LOG)
    assert ok and "封存" in why, why
    # 索引不再覆盖它 → 立刻回到不可接受
    (package / INDEX).write_text(json.dumps({"files": []}), encoding="utf-8")
    assert not ReleaseSurface(package).check(LOG)[0]


def test_undeclared_missing_path_never_gets_a_free_pass(package):
    surface = ReleaseSurface(package)
    ok, why = surface.check("evidence/R1/logs/other.LOG")
    assert not ok and "既未声明随包" in why, why
    ok2, _ = surface.check(KEPT)
    assert ok2, "在册且在场就该通过"


def test_pattern_list_comes_from_the_manifest_not_from_a_hardcoded_guess(package):
    manifest = json.loads((package / "release-manifest.json").read_text(encoding="utf-8"))
    manifest["withheld_patterns"] = []
    (package / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    surface = ReleaseSurface(package)
    del surface
    (package / LOG).unlink()
    fresh = ReleaseSurface(package)
    manifest2 = json.loads((package / "release-manifest.json").read_text(encoding="utf-8"))
    manifest2["files"][LOG] = {"sha256": "d" * 64}
    (package / "release-manifest.json").write_text(json.dumps(manifest2), encoding="utf-8")
    assert not ReleaseSurface(package).check(LOG)[0], "取消裁剪声明后不得再放过"


def _fake_report():
    from closure_gate_contract import GATE_ORDER
    return {"run_id": "R1", "commit": "h" * 40, "evidence_index_sha256": "a" * 64,
            "_closure_report_sha256": "b" * 64,
            "gates": {name: {"status": "NOT_RUN" if "E2E" in name and name.startswith(
                ("REAL", "MODEL", "VIDEO")) else "PASS"} for name in GATE_ORDER}}


def test_builder_contract_declares_tier_and_withheld_patterns():
    """构建器必须把档位与裁剪模式写进清单；随包清单由验收脚本再核一次。"""
    import build_release as br

    doc = br.build_manifest(version="9.9.9", head="h" * 40, branch="b", timestamp="T",
                           report=_fake_report(), batch_run_id="B",
                           files_map={"SKILL.md": {"sha256": "c" * 64, "size_bytes": 1}},
                           reasons={}, extra={})
    assert doc["release_tier"] == "local-stable"
    assert doc["withheld_patterns"] == [".log"], doc["withheld_patterns"]
    assert "封存" in doc["withheld_note"]
