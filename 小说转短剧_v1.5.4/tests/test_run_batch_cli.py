import json
import os
from conftest import run_py, run_py_narrow_console

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(extra_args):
    return run_py([os.path.join(ROOT, "run_batch.py")] + extra_args, cwd=ROOT)


def test_cli_refuses_missing_outline():
    # 业务参数硬闸：缺大纲必须拒跑，而不是静默用样例大纲开工
    res = _run(["--assemble-only", "--target-seconds", "90", "--workdir", "x"])
    assert res.returncode != 0
    assert "--outline" in (res.stdout + res.stderr)


def test_cli_refuses_missing_target_seconds():
    res = _run(["--assemble-only", "--outline", os.path.join("fixtures", "sample_outline.json"), "--workdir", "x"])
    assert res.returncode != 0
    assert "--target-seconds" in (res.stdout + res.stderr)


def test_cli_refuses_dirty_outline(tmp_path):
    # 大纲预审门装在跑批入口：根不合格，全盘拒跑
    outline = json.load(open(os.path.join(ROOT, "fixtures", "sample_outline.json"), encoding="utf-8"))
    outline["characters"][0]["desire"] = ""
    p = tmp_path / "dirty_outline.json"
    p.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    res = _run([
        "--assemble-only", "--target-seconds", "90",
        "--outline", str(p), "--workdir", str(tmp_path / "ws")
    ])
    assert res.returncode == 2
    assert "大纲预审门" in (res.stdout + res.stderr) and "[O2]" in (res.stdout + res.stderr)


def test_cli_refuses_non_uniform_target_seconds(tmp_path):
    # 反塞防御：逐集乱填 targetSeconds（凑时长门的指纹）直接拒跑
    outline = json.load(open(os.path.join(ROOT, "fixtures", "sample_outline.json"), encoding="utf-8"))
    outline["episodes"][1]["targetSeconds"] = 45
    outline["episodes"][2]["targetSeconds"] = 33
    bad_outline = tmp_path / "tampered_outline.json"
    bad_outline.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")

    res = _run([
        "--assemble-only", "--target-seconds", "90",
        "--outline", str(bad_outline), "--workdir", str(tmp_path / "ws")
    ])
    assert res.returncode == 2
    out = res.stdout + res.stderr
    assert "反塞" in out or "不一致" in out
    assert "2" in out and "3" in out  # 点名违规集数


# ---------------------------------------------------------------- _meta 打章（真跑 2026-09-23 抓出的阻断）

def _sample_workspace(tmp_path, with_meta: bool):
    from dramacraft.outline_lint import compute_outline_hash
    outline = json.load(open(os.path.join(ROOT, "fixtures", "sample_outline.json"), encoding="utf-8"))
    ws = tmp_path / "ws"
    ep_dir = ws / "state" / "episodes"
    ep_dir.mkdir(parents=True, exist_ok=True)
    sample = json.load(open(os.path.join(ROOT, "fixtures", "sample_script.json"), encoding="utf-8"))
    eps = [e for e in sample["episodes"] if e.get("ep") in (1, 2, 3)]
    for ep in eps:
        payload = json.loads(json.dumps(ep))          # 照工作流派工人的写法：正文写全、_meta 由引擎补
        if with_meta:
            payload["_meta"] = {"engineVersion": "1.2.1", "schemaVersion": 2,
                                "outlineHash": compute_outline_hash(outline),
                                "gatesPassedAt": "2026-09-23T00:00:00+08:00"}
        (ep_dir / f"ep{payload['ep']:03d}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return ws, ep_dir, eps


def test_stamp_only_closes_the_assembler_cache_gate(tmp_path):
    """真跑 2026-09-23 实测的阻断：12 门全过的单集件缺 _meta，--assemble-only 直接 exit 2；
    补章后装配通过——工作流模式的「工人写件 + 引擎补章」契约就此闭合。"""
    from dramacraft.outline_lint import compute_outline_hash
    outline_path = os.path.join(ROOT, "fixtures", "sample_outline.json")
    ws, ep_dir, eps = _sample_workspace(tmp_path, with_meta=False)
    base = ["--outline", outline_path, "--target-seconds", "90", "--workdir", str(ws), "--assemble-only"]

    res = _run(base)
    assert res.returncode == 2, (res.returncode, res.stdout[-400:])
    assert "缺少 _meta 元数据" in (res.stdout + res.stderr)          # 红灯：装配器把好件当旧缓存拒收

    for ep in eps:
        cp = _run(["--stamp-only", str(ep_dir / f"ep{ep['ep']:03d}.json"), "--outline", outline_path])
        assert cp.returncode == 0, cp.stdout + cp.stderr
    stamped = json.loads((ep_dir / "ep001.json").read_text(encoding="utf-8"))
    assert stamped["_meta"]["schemaVersion"] >= 2
    assert stamped["_meta"]["outlineHash"] == compute_outline_hash(
        json.load(open(outline_path, encoding="utf-8")))

    res2 = _run(base)
    assert "缺少 _meta 元数据" not in (res2.stdout + res2.stderr)
    assert res2.returncode == 0, (res2.returncode, res2.stdout[-600:])


def test_check_ep_flags_contract_shape_violations(tmp_path):
    """契约形状闸（2026-09-23 T6 真跑抓出）：写手件缺 `location`/`type` 时 12 门照样放行
    （validate.mjs 按 `line` 是否字符串认对白），可下游导演规划按契约读对白与地点锚点，
    于是两相位之后才炸（实测 plan_lint 22 条硬伤，全是「台词在原剧本里找不到」）。
    形状闸挂在引擎侧：写手过门之前、入库补章之前都要拦。"""
    ws, ep_dir, eps = _sample_workspace(tmp_path, with_meta=False)
    good = _run(["--check-ep", str(ep_dir / "ep001.json"), "--json", str(tmp_path / "good.json")])
    assert good.returncode == 0, good.stdout + good.stderr

    bad = json.loads((ep_dir / "ep001.json").read_text(encoding="utf-8"))
    for sc in bad["scenes"]:
        sc.pop("location", None)
        for b in sc["flow"]:
            b.pop("type", None)
    p = tmp_path / "bad_ep.json"
    p.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    r = _run(["--check-ep", str(p), "--json", str(tmp_path / "bad.json")])
    assert r.returncode == 1
    out = r.stdout + r.stderr
    assert "location" in out and "type" in out
    rep = json.loads((tmp_path / "bad.json").read_text(encoding="utf-8"))
    assert rep["ok"] is False and len(rep["hard"]) >= 2


def test_check_ep_merges_findings_into_gate_report(tmp_path):
    """形状硬伤要能并进 12 门报告：熔断器读的是那一份，工作流就按这条接。"""
    ws, ep_dir, eps = _sample_workspace(tmp_path, with_meta=False)
    bad = json.loads((ep_dir / "ep002.json").read_text(encoding="utf-8"))
    for sc in bad["scenes"]:
        sc.pop("location", None)
    p = tmp_path / "bad_ep.json"
    p.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    report = tmp_path / "val.json"
    report.write_text(json.dumps({"gates": [], "hard": ["原有硬伤"], "warn": []}, ensure_ascii=False),
                      encoding="utf-8")
    r = _run(["--check-ep", str(p), "--merge-into", str(report)])
    assert r.returncode == 1
    doc = json.loads(report.read_text(encoding="utf-8"))
    assert doc["hard"][0] == "原有硬伤" and any("location" in h for h in doc["hard"])


def test_stamp_only_refuses_episode_that_breaks_the_contract(tmp_path):
    """不合契约的件不许补章入库——补章就是发通行证。"""
    ws, ep_dir, eps = _sample_workspace(tmp_path, with_meta=False)
    p = ep_dir / "ep003.json"
    bad = json.loads(p.read_text(encoding="utf-8"))
    for sc in bad["scenes"]:
        sc.pop("location", None)
    p.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    r = _run(["--stamp-only", str(p), "--outline", os.path.join(ROOT, "fixtures", "sample_outline.json")])
    assert r.returncode == 1, r.stdout + r.stderr
    assert "location" in (r.stdout + r.stderr)
    assert "_meta" not in json.loads(p.read_text(encoding="utf-8")), "不合契约的件不该拿到 _meta 通行证"


def test_stamp_only_records_external_writer_in_ledger(tmp_path):
    """工作流写手写在外面的件：补章时把「外部写入」记进本轮台账，
    生产记录的来源栏才有据可依（不许拿「文件在库里」猜执笔模型）。"""
    ws, ep_dir, eps = _sample_workspace(tmp_path, with_meta=False)
    outline_path = os.path.join(ROOT, "fixtures", "sample_outline.json")

    cp = _run(["--stamp-only", str(ep_dir / "ep001.json"), "--outline", outline_path,
               "--stamped-by", "工作流写手"])
    assert cp.returncode == 0, cp.stdout + cp.stderr
    events = [json.loads(ln) for ln in
              (ws / "state" / "ledger.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
    imported = [e for e in events if e.get("event") == "episode_imported"]
    assert len(imported) == 1 and imported[0]["ep"] == 1 and imported[0]["by"] == "工作流写手"

    # 不在标准工作区布局里的件（人工随手补章）：不写账，也不许凭空造出工作区目录
    stray = tmp_path / "stray.json"
    stray.write_text(json.dumps({"ep": 1, "scenes": [{"sceneId": "S01", "location": "屋里·夜",
                                                     "characters": ["C01"],
                                                     "flow": [{"type": "action", "action": "叶青把信压在桌上。"}]}]},
                                ensure_ascii=False), encoding="utf-8")
    before = sorted(os.listdir(tmp_path))
    cp2 = _run(["--stamp-only", str(stray), "--outline", outline_path])
    assert cp2.returncode == 0, cp2.stdout + cp2.stderr
    assert sorted(os.listdir(tmp_path)) == before


def test_stamp_only_usage_gate(tmp_path):
    ep = tmp_path / "ep001.json"
    ep.write_text('{"ep": 1}', encoding="utf-8")
    assert _run(["--stamp-only", str(ep)]).returncode == 1                      # 缺 --outline
    assert _run(["--stamp-only", str(tmp_path / "nope.json"),
                 "--outline", os.path.join(ROOT, "fixtures", "sample_outline.json")]).returncode == 1


# ---------------------------------------------------------------- 窄编码终端（票 7）

def _plain_console_env():
    """普通 Windows 终端：ANSI 代码页（cp936），且不预设 PYTHONUTF8。"""
    env = dict(os.environ, PYTHONIOENCODING="cp936")
    env.pop("PYTHONUTF8", None)      # 不摘掉它，这个测试在 UTF-8 机器上会假绿
    return env


def test_doctor_cp936_output_does_not_crash(tmp_path):
    cp = run_py_narrow_console(["run_batch.py", "--doctor"], cwd=ROOT,
                               env=_plain_console_env())
    assert b"UnicodeEncodeError" not in cp.stderr, cp.stderr.decode("cp936", "replace")[-800:]
    assert cp.returncode in (0, 1)   # 由 doctor 健康状态决定，不允许编码异常


def test_doctor_cp936_escapes_unencodable_chars_instead_of_dropping_them():
    """窄编码终端里 emoji 用可读转义显示：诊断文字不许被吞掉，退出码也不许变。"""
    cp = run_py_narrow_console(["run_batch.py", "--doctor"], cwd=ROOT,
                               env=_plain_console_env())
    text = cp.stdout.decode("cp936", "replace")
    assert "DramaCraft" in text and len(text) > 200          # 报告本体还在
    assert "\\U0001fa7a" in text or "🩺" in cp.stdout.decode("utf-8", "replace")


def test_engine_cli_check_marks_survive_narrow_console(tmp_path):
    """所有引擎 CLI 都经 dramacraft 包进来，✓/✗ 这类记号在 gbk 终端同样不许把 CLI 打崩。"""
    from dramacraft.meeting_pack import append_concerns
    work = tmp_path / "ws"
    append_concerns(work, [{"id": "RC1", "t": "开头太慢"}], kind="reader")
    cp = run_py_narrow_console(["-m", "dramacraft.meeting_pack", "check-closure",
                                "--workdir", str(work), "--json", str(tmp_path / "c.json")],
                               cwd=ROOT, env=_plain_console_env())
    assert cp.returncode == 4, cp.stderr.decode("cp936", "replace")   # 未闭环就是 4，不是崩溃
    assert b"UnicodeEncodeError" not in cp.stderr
    assert "RC1" in cp.stdout.decode("cp936", "replace")
