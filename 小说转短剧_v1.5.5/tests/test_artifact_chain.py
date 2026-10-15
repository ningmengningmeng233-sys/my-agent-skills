# -*- coding: utf-8 -*-
"""T4 改稿下游失效链（工单 T4；方案 §6 编排纪律 + Review Focus #1）

链条：剧本 → 规划 → 镜头组 → 逐集监督。上游任一件内容变了，下游整链失效、必须重跑。
判据是**内容指纹**不是时间戳：每级落盘时把「当时上游各件指纹」写进自己的 `_meta.upstreamHashes`。

同时覆盖工作流越权硬闸用的 snapshot/verify（对任意文件列表算/比 sha256）。
"""
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plan_fixtures as fx  # noqa: E402
import shot_fixtures as sfx  # noqa: E402

from dramacraft.artifact_chain import (  # noqa: E402
    STAGES,
    artifact_path,
    chain_stale,
    emit_chain,
    stale_stages,
)
from conftest import run_py

EP = 3


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _make_chain(work: Path):
    """造一条新鲜链：剧本 → 规划 → 镜头组 → 监督，逐级盖章。返回**盖章后**的件。"""
    ep = fx.get_ep(fx.sample_script(), 1)
    ep = dict(ep, ep=EP)
    plan = fx.make_valid_plan(ep, ep_num=EP)
    shots = sfx.make_valid_shots(ep, plan, 90, ep_num=EP)
    supervision = {"ep": EP, "schemaVersion": 1, "findings": [], "verdict": "pass"}
    _write_json(artifact_path(work, EP, "script"), ep)
    _write_json(artifact_path(work, EP, "plan"), plan)
    emit_chain(work, EP, "plan")
    _write_json(artifact_path(work, EP, "shots"), shots)
    emit_chain(work, EP, "shots")
    _write_json(artifact_path(work, EP, "supervision"), supervision)
    emit_chain(work, EP, "supervision")
    return (_read(work, "script"), _read(work, "plan"), _read(work, "shots"))


def _read(work: Path, stage: str):
    return json.loads(artifact_path(work, EP, stage).read_text("utf-8"))


def _rewrite_script(work: Path, new_line="改了一句台词，剧本被重做了。"):
    p = artifact_path(work, EP, "script")
    doc = json.loads(p.read_text("utf-8"))
    for scene in doc["scenes"]:
        for beat in scene.get("flow") or []:
            if beat.get("type") == "dialogue":
                beat["line"] = new_line
                _write_json(p, doc)
                return
    raise AssertionError("夹具里应当有台词")


def _run_cli(*args):
    return run_py(["-m", "dramacraft.artifact_chain", *map(str, args)], cwd=ROOT)


# ---------------------------------------------------------------- 链条新鲜/失效

def test_fresh_chain_reports_all_fresh(tmp_path):
    work = tmp_path / "ws"
    _make_chain(work)
    assert [chain_stale(work, EP, s) for s in STAGES] == [False, False, False]
    assert stale_stages(work, EP) == []


def test_rewritten_script_invalidates_downstream(tmp_path):
    """Review Focus #1 主钉：上游剧本被重做/手改 → 下游整链失效，绝不复用。"""
    work = tmp_path / "ws"
    _make_chain(work)
    _rewrite_script(work)
    assert chain_stale(work, EP, "plan") and chain_stale(work, EP, "shots") \
        and chain_stale(work, EP, "supervision")
    out = tmp_path / "check.json"
    cp = _run_cli("check", "--workdir", work, "--ep", EP, "--json", out)
    assert cp.returncode == 3
    payload = json.loads(out.read_text("utf-8"))
    assert [x["stage"] for x in payload["stale"]] == ["plan", "shots", "supervision"]


def test_hand_tampered_plan_only_kills_shots_and_supervision(tmp_path):
    work = tmp_path / "ws"
    _, plan, _ = _make_chain(work)
    plan["scenes"][0]["notes"] = "手工改过的导演备注"
    _write_json(artifact_path(work, EP, "plan"), plan)
    assert (chain_stale(work, EP, "plan"), chain_stale(work, EP, "shots"),
            chain_stale(work, EP, "supervision")) == (False, True, True)


def test_stamping_meta_does_not_change_own_content_hash(tmp_path):
    """盖章写进 _meta，不得把本件自己的内容指纹改掉——否则链会自激。"""
    work = tmp_path / "ws"
    _make_chain(work)
    # 刚造的链必须整条新鲜：监督件记录的镜头件指纹，是「盖过章的镜头件」算出来的
    # ⇒ 若盖章会改指纹，这条就会红（这就是不变量本身）
    assert chain_stale(work, EP, "supervision") is False
    # 真改了内容才失效
    shots = _read(work, "shots")
    shots["groups"][0]["content"] = "改了一句话，镜头组被动过"
    _write_json(artifact_path(work, EP, "shots"), shots)
    assert chain_stale(work, EP, "supervision") is True
    emit_chain(work, EP, "supervision")            # 重新盖章后恢复新鲜
    assert chain_stale(work, EP, "supervision") is False


def test_absent_stage_is_not_stale(tmp_path):
    """库里的件根本不存在 ≠ 失效（有没有归盘库管，失不失效归本模块管）。"""
    work = tmp_path / "ws"
    _write_json(artifact_path(work, EP, "script"), fx.get_ep(fx.sample_script(), 1))
    assert chain_stale(work, EP, "plan") is False
    out = tmp_path / "check.json"
    cp = _run_cli("check", "--workdir", work, "--ep", EP, "--json", out)
    assert cp.returncode == 0
    payload = json.loads(out.read_text("utf-8"))
    assert payload["stale"] == [] and payload["absent"] == ["plan", "shots", "supervision"]


def test_missing_upstream_file_invalidates_downstream(tmp_path):
    work = tmp_path / "ws"
    _make_chain(work)
    artifact_path(work, EP, "plan").unlink()      # 上游件被删
    assert chain_stale(work, EP, "shots") is True


def test_emit_chain_requires_stage_file(tmp_path):
    work = tmp_path / "ws"
    _write_json(artifact_path(work, EP, "script"), fx.get_ep(fx.sample_script(), 1))
    from dramacraft.artifact_chain import ChainError
    with pytest.raises(ChainError):
        emit_chain(work, EP, "plan")               # 本阶段件还没落盘就盖章 = 用法错


def test_emit_chain_records_upstream_hashes(tmp_path):
    work = tmp_path / "ws"
    _make_chain(work)
    meta = json.loads(artifact_path(work, EP, "plan").read_text("utf-8"))["_meta"]
    assert set(meta["upstreamHashes"]) == {"script"}
    meta2 = json.loads(artifact_path(work, EP, "supervision").read_text("utf-8"))["_meta"]
    assert set(meta2["upstreamHashes"]) == {"script", "plan", "shots"}


# ---------------------------------------------------------------- 监督双件（票 1）

def test_supervision_report_and_chain_are_two_files(tmp_path):
    from dramacraft.artifact_chain import write_supervision_record
    _make_chain(tmp_path)
    artifact_path(tmp_path, EP, "supervision").unlink()
    md = artifact_path(tmp_path, EP, "supervision").with_suffix(".md")
    md.write_text("# 监督报告\n\n总评：放行", encoding="utf-8")
    record = write_supervision_record(tmp_path, EP, True, [])
    assert record["verdict"] == "pass"
    assert md.exists()
    assert artifact_path(tmp_path, EP, "supervision").exists()
    assert not chain_stale(tmp_path, EP, "supervision")


def test_supervision_record_needs_the_human_readable_report(tmp_path):
    """监督员没把报告写下来 = 没审过：只写机读件放行是「空壳通过」。"""
    from dramacraft.artifact_chain import ChainError, write_supervision_record
    work = tmp_path / "ws"
    _make_chain(work)
    artifact_path(work, EP, "supervision").unlink()
    with pytest.raises(ChainError) as err:
        write_supervision_record(work, EP, True, [])
    assert "监督报告" in str(err.value)
    assert not artifact_path(work, EP, "supervision").exists()   # 不许落下空壳机读件


def test_supervision_record_rejects_blank_report(tmp_path):
    from dramacraft.artifact_chain import ChainError, write_supervision_record
    work = tmp_path / "ws"
    _make_chain(work)
    artifact_path(work, EP, "supervision").unlink()
    artifact_path(work, EP, "supervision").with_suffix(".md").write_text("   \n\n", encoding="utf-8")
    with pytest.raises(ChainError) as err:
        write_supervision_record(work, EP, True, [])
    assert "空文件" in str(err.value)


def test_supervision_record_needs_upstream(tmp_path):
    """上游缺件时链条本就该断：不许盖出一个「新鲜」的监督件。"""
    from dramacraft.artifact_chain import ChainError, write_supervision_record
    work = tmp_path / "ws"
    _make_chain(work)
    artifact_path(work, EP, "supervision").with_suffix(".md").write_text("# 监督报告\n\n总评：放行", encoding="utf-8")
    artifact_path(work, EP, "plan").unlink()
    with pytest.raises(ChainError):
        write_supervision_record(work, EP, True, [])


def test_supervision_fail_record_replaces_stale_pass(tmp_path):
    """返修轮次最怕旧件冒充：本轮没放行就得把上一轮的 pass 件覆盖掉。"""
    from dramacraft.artifact_chain import write_supervision_record
    work = tmp_path / "ws"
    _make_chain(work)
    artifact_path(work, EP, "supervision").with_suffix(".md").write_text("# 监督报告\n\n总评：放行", encoding="utf-8")
    assert write_supervision_record(work, EP, True, [])["verdict"] == "pass"
    record = write_supervision_record(work, EP, False, ["R2 主角动机塌了", "R4 时长超标"])
    assert record["verdict"] == "fail" and len(record["findings"]) == 2
    on_disk = json.loads(artifact_path(work, EP, "supervision").read_text("utf-8"))
    assert on_disk["verdict"] == "fail"                      # 旧 pass 不能留在盘上
    assert not (on_disk.get("_meta") or {}).get("upstreamHashes")   # 未放行的件不盖链章


def test_cli_supervise_writes_both_files_and_chains(tmp_path):
    work = tmp_path / "ws"
    _make_chain(work)
    artifact_path(work, EP, "supervision").unlink()
    artifact_path(work, EP, "supervision").with_suffix(".md").write_text("# 监督报告\n\n总评：放行", encoding="utf-8")
    out = tmp_path / "sup.json"
    cp = _run_cli("supervise", "--workdir", work, "--ep", EP, "--verdict", "pass",
                  "--finding", "无", "--json", out)
    assert cp.returncode == 0, cp.stderr
    assert json.loads(out.read_text("utf-8"))["verdict"] == "pass"
    assert not chain_stale(work, EP, "supervision")
    cp2 = _run_cli("supervise", "--workdir", work, "--ep", EP, "--verdict", "ok", "--json", out)
    assert cp2.returncode == 1                              # 裁决只有 pass/fail 两档


# ---------------------------------------------------------------- 监督放行两条路径（票 2 的确定性内核）

def _supervise(work: Path, verdict: str, findings=()):
    args = ["supervise", "--workdir", work, "--ep", EP, "--verdict", verdict]
    for item in findings:
        args += ["--finding", item]
    args += ["--json", str(work / "state" / "reports" / f"sup-{verdict}.json")]
    return _run_cli(*args)


def test_supervision_first_round_release_lands_both_files(tmp_path):
    """首轮放行：人读报告 + 机读件 + 链章三样齐，工作流才认放行。"""
    work = tmp_path / "ws"
    _make_chain(work)
    artifact_path(work, EP, "supervision").unlink()
    artifact_path(work, EP, "supervision").with_suffix(".md").write_text("# 监督报告\n\n总评：放行", encoding="utf-8")
    cp = _supervise(work, "pass")
    assert cp.returncode == 0, cp.stderr
    rec = json.loads(artifact_path(work, EP, "supervision").read_text("utf-8"))
    assert rec["verdict"] == "pass" and set(rec["_meta"]["upstreamHashes"]) == {"script", "plan", "shots"}
    assert chain_stale(work, EP, "supervision") is False


def test_supervision_rework_then_release_restamps_against_new_shots(tmp_path):
    """首轮拦下→返修→放行：未放行的记录不盖链章，返修后重新盖章要对上**新的**镜头件。"""
    work = tmp_path / "ws"
    _make_chain(work)
    artifact_path(work, EP, "supervision").with_suffix(".md").write_text("# 监督报告\n\n总评：拦下", encoding="utf-8")
    assert _supervise(work, "fail", ["R2 主角动机塌了"]).returncode == 0
    rec = json.loads(artifact_path(work, EP, "supervision").read_text("utf-8"))
    assert rec["verdict"] == "fail" and not (rec.get("_meta") or {}).get("upstreamHashes")

    shots = _read(work, "shots")
    shots["groups"][0]["content"] = "返修工按监督意见改了这一组"
    _write_json(artifact_path(work, EP, "shots"), shots)
    artifact_path(work, EP, "supervision").with_suffix(".md").write_text("# 监督报告\n\n总评：复核通过，放行", encoding="utf-8")
    assert _supervise(work, "pass").returncode == 0
    assert chain_stale(work, EP, "supervision") is False
    # 放行件记录的镜头件指纹，必须是返修后那一版
    from dramacraft.artifact_chain import file_content_hash
    recorded = json.loads(artifact_path(work, EP, "supervision").read_text("utf-8"))["_meta"]["upstreamHashes"]
    assert recorded["shots"] == file_content_hash(artifact_path(work, EP, "shots"))


def test_supervision_claiming_pass_without_report_is_refused(tmp_path):
    """监督员回话说放行、报告却没落盘：机器拒收（工作流据此判本轮未放行）。"""
    work = tmp_path / "ws"
    _make_chain(work)
    artifact_path(work, EP, "supervision").unlink()
    cp = _supervise(work, "pass")
    assert cp.returncode == 1 and "监督报告" in cp.stderr
    assert not artifact_path(work, EP, "supervision").exists()


# ---------------------------------------------------------------- CLI 输入纪律

def test_cli_check_missing_json_path_is_usage_error(tmp_path):
    cp = _run_cli("check", "--workdir", tmp_path, "--ep", EP)
    assert cp.returncode == 1


def test_cli_check_missing_workdir_input_is_not_reported(tmp_path):
    out = tmp_path / "check.json"
    cp = _run_cli("check", "--workdir", tmp_path / "nope", "--ep", "0", "--json", out)
    assert cp.returncode == 1 and not out.exists()


# ---------------------------------------------------------------- snapshot / verify（越权硬闸）

def test_snapshot_verify_roundtrip(tmp_path):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text('{"x": 1}', encoding="utf-8")
    b.write_text('{"y": 2}', encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    cp = _run_cli("snapshot", "--json", manifest, a, b)
    assert cp.returncode == 0 and json.loads(manifest.read_text("utf-8"))["files"][str(a)]
    cp2 = _run_cli("verify", "--manifest", manifest, "--json", tmp_path / "v.json")
    assert cp2.returncode == 0

    a.write_text('{"x": 999}', encoding="utf-8")
    cp3 = _run_cli("verify", "--manifest", manifest, "--json", tmp_path / "v.json")
    assert cp3.returncode == 5
    payload = json.loads((tmp_path / "v.json").read_text("utf-8"))
    assert [x["path"] for x in payload["changed"]] == [str(a)]


def test_snapshot_missing_file_is_usage_error(tmp_path):
    manifest = tmp_path / "manifest.json"
    cp = _run_cli("snapshot", "--json", manifest, tmp_path / "nope.json")
    assert cp.returncode == 1 and not manifest.exists()


def test_verify_reports_removed_file(tmp_path):
    a = tmp_path / "a.json"
    a.write_text("{}", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    assert _run_cli("snapshot", "--json", manifest, a).returncode == 0
    a.unlink()
    out = tmp_path / "v.json"
    cp = _run_cli("verify", "--manifest", manifest, "--json", out)
    assert cp.returncode == 5
    assert [x["path"] for x in json.loads(out.read_text("utf-8"))["removed"]] == [str(a)]


def test_verify_rejects_manifest_with_duplicate_entries(tmp_path):
    """清单里同一路径写两次（哪怕摘要互相矛盾）必须拒收，不能「后写覆盖前写」后报全过。

    现场：`json.loads` 对重复键静默取最后一个，复核端只看得到一份摘要。
    一份被手工编辑/拼接过的清单只要把正确摘要放在后面，就能把篡改伪装成 matched。
    """
    a = tmp_path / "a.json"
    a.write_text('{"x": 1}', encoding="utf-8")
    import hashlib
    good = hashlib.sha256(a.read_bytes()).hexdigest()
    manifest = tmp_path / "manifest.json"
    key = str(a.resolve()).replace("\\", "/")
    manifest.write_text(
        '{"schemaVersion": 1, "files": {"%s": "%s", "%s": "%s"}}' % (key, "0" * 64, key, good),
        encoding="utf-8")
    out = tmp_path / "v.json"
    cp = _run_cli("verify", "--manifest", manifest, "--json", out)
    assert cp.returncode != 0, (cp.returncode, cp.stdout, cp.stderr)
    text = cp.stdout + cp.stderr
    assert "重复" in text, text[:400]


def test_verify_rejects_even_identical_duplicate_entries(tmp_path):
    """两条一模一样的重复项也拒收：静默折叠过的清单已经不是生成器写出的那份，出处不可信。"""
    a = tmp_path / "a.json"
    a.write_text('{"x": 1}', encoding="utf-8")
    import hashlib
    good = hashlib.sha256(a.read_bytes()).hexdigest()
    manifest = tmp_path / "manifest.json"
    key = str(a.resolve()).replace("\\", "/")
    manifest.write_text(
        '{"schemaVersion": 1, "files": {"%s": "%s", "%s": "%s"}}' % (key, good, key, good),
        encoding="utf-8")
    cp = _run_cli("verify", "--manifest", manifest, "--json", tmp_path / "v.json")
    assert cp.returncode != 0
    assert "重复" in (cp.stdout + cp.stderr)


# ---------------------------------------------------------------- run_batch 盘库接入

def test_run_batch_reports_stale_downstream_instead_of_redoing_script(tmp_path):
    """改稿后：剧本本身视为当前真值（不重做、不烧额度），只把「下游待重跑」报出来。"""
    from dramacraft.outline_lint import compute_outline_hash

    outline_path = os.path.join(ROOT, "fixtures", "sample_outline.json")
    outline = json.loads(open(outline_path, encoding="utf-8").read())
    outline_hash = compute_outline_hash(outline)
    ws = tmp_path / "ws"
    ep_dir = ws / "state" / "episodes"
    ep_dir.mkdir(parents=True, exist_ok=True)
    sample = json.loads(open(os.path.join(ROOT, "fixtures", "sample_script.json"), encoding="utf-8").read())
    for ep_n in (1, 2, 3):
        payload = json.loads(json.dumps(next(e for e in sample["episodes"] if e["ep"] == ep_n)))
        payload["_meta"] = {"engineVersion": "1.2.1", "schemaVersion": 2,
                            "outlineHash": outline_hash, "gatesPassedAt": "2026-09-22T00:00:00+08:00"}
        _write_json(ep_dir / f"ep{ep_n:03d}.json", payload)

    # 给第 1 集造一条「旧的」下游链：规划件盖的是旧剧本的指纹
    _write_json(artifact_path(ws, 1, "plan"), {"ep": 1, "schemaVersion": 1, "stats": {}, "scenes": []})
    emit_chain(ws, 1, "plan")
    ep1 = json.loads((ep_dir / "ep001.json").read_text("utf-8"))
    ep1["scenes"][0]["flow"][0]["action"] = "手改了一句动作，剧本变过。"
    _write_json(ep_dir / "ep001.json", ep1)

    res = run_py([os.path.join(ROOT, "run_batch.py"),
                  "--outline", outline_path, "--target-seconds", "90",
                  "--workdir", str(ws), "--assemble-only"], cwd=ROOT)
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    assert "后续环节待重做" in out and "第 1 集（导演规划）" in out, out
    assert "旧的不会被自动沿用" in out, out
    # 剧本件没被重做（内容就是手改后的那一版）
    assert "手改了一句动作" in (ep_dir / "ep001.json").read_text("utf-8")
