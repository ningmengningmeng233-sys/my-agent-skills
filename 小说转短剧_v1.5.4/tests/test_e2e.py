import json
import os
import subprocess
from conftest import run_py
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_e2e_cli_validate_only():
    script_path = os.path.join("fixtures", "sample_script.json")
    outline_path = os.path.join("fixtures", "sample_outline.json")
    cmd = [
        "run_batch.py",
        "--validate-only", script_path,
        "--outline", outline_path,
        "--art", os.path.join("fixtures", "sample_art.json")
    ]
    res = run_py(cmd, cwd=ROOT)
    assert res.returncode == 0
    assert "全部 12 道质量门 100% 验收通过" in res.stdout
    assert "本门跳过" not in res.stdout  # 门10 实扫
    # 时长对照裸露：通过时也要能肉眼看到逐集 估算vs目标
    assert "估" in res.stdout and "目标" in res.stdout and "第1集" in res.stdout


def test_e2e_cli_assemble_and_export_all(tmp_path):
    # 自播种：从满分样例剧本拆出集缓存，不依赖环境里任何历史残留
    workdir = tmp_path / "workspace"
    episodes_dir = workdir / "state" / "episodes"
    episodes_dir.mkdir(parents=True)
    sample = json.load(open(r"fixtures/sample_script.json", encoding="utf-8"))
    outline_data = json.load(open(r"fixtures/sample_outline.json", encoding="utf-8"))
    from dramacraft.outline_lint import compute_outline_hash
    h = compute_outline_hash(outline_data)
    for ep in sample["episodes"]:
        ep_c = dict(ep)
        ep_c["_meta"] = {"engineVersion": "1.2.0", "schemaVersion": 2, "outlineHash": h}
        with open(episodes_dir / f"ep{ep['ep']:03d}.json", "w", encoding="utf-8") as fp:
            json.dump(ep_c, fp, ensure_ascii=False)

    cmd = [
        "run_batch.py",
        "--outline", os.path.join("fixtures", "sample_outline.json"),
        "--target-seconds", "90",
        "--art", os.path.join("fixtures", "sample_art.json"),
        "--workdir", str(workdir),
        "--assemble-only",
        "--formats", "markdown", "csv", "json", "html"
    ]
    res = run_py(cmd, cwd=ROOT)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "全流程批量跑批成功" in res.stdout

    output_dir = workdir / "output"
    assert os.path.exists(str(output_dir / "script.md"))
    assert os.path.exists(str(output_dir / "script_breakdown.csv"))
    assert os.path.exists(str(output_dir / "script.json"))
    assert os.path.exists(str(output_dir / "script-report.html"))

    # 验证导出的 script.json 剥除了 _meta，不污染下游分镜/生图
    out_json = json.load(open(str(output_dir / "script.json"), encoding="utf-8"))
    for ep in out_json["episodes"]:
        assert "_meta" not in ep, "交付物的 script.json 绝不能携带内部 _meta 键污染下游"

    # 交付给人看的导演台本必须用人名，不能漏出角色编号
    md_text = open(str(output_dir / "script.md"), encoding="utf-8").read()
    assert "林晨（" in md_text and "C01（" not in md_text
