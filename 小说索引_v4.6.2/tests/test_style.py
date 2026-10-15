import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS = os.path.join(ROOT, "scripts")


@pytest.fixture
def style_env(tmp_lib, sample_segment, sample_index):
    sys.path.insert(0, SCRIPTS)
    from index_db import IndexDB

    with IndexDB(tmp_lib) as db:
        db.mark_done(sample_segment, sample_index, prompt_tokens=10, completion_tokens=20)
    return {"novel": "test", "author": "author", "lib": tmp_lib}


def test_lightweight_style_card_created_outside_lib(style_env):
    sys.path.insert(0, SCRIPTS)
    import style_distillation

    result = style_distillation.run_style_distillation(
        style_env["lib"],
        style_env["novel"],
        style_env["author"],
    )
    assert result.get("mode") == "lightweight"
    assert result.get("error") is None
    path = Path(result["path"])
    assert path.name == "style-card.md"
    assert "风格卡" in path.parts
    assert path.parent.name == "test-author"
    text = path.read_text(encoding="utf-8")
    assert "轻量风格卡" in text
    assert "叙事模型" in text
    assert "诚实边界" in text


def test_external_python_skill_invoked(style_env, tmp_path):
    skill = tmp_path / "fake_skill.py"
    marker = tmp_path / "ran.flag"
    skill.write_text(
        "import argparse\n"
        "import sys\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--lib')\n"
        "parser.add_argument('--novel')\n"
        "parser.add_argument('--author')\n"
        "args = parser.parse_args()\n"
        f"open(r'{marker}', 'w').write(f'{{args.novel}}-{{args.author}}')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )

    sys.path.insert(0, SCRIPTS)
    import style_distillation

    result = style_distillation.run_style_distillation(
        style_env["lib"],
        style_env["novel"],
        style_env["author"],
        style_skill=str(skill),
    )
    assert result.get("mode") == "external"
    assert result.get("returncode") == 0
    assert marker.read_text(encoding="utf-8") == "test-author"


@pytest.mark.skipif(os.name == "nt", reason="shlex command strings are posix-oriented")
def test_external_command_skill_invoked(style_env, tmp_path):
    marker = tmp_path / "cmd.flag"
    cmd = f"{sys.executable} -c \"open(r'{marker}', 'w').write('ok')\""

    sys.path.insert(0, SCRIPTS)
    import style_distillation

    result = style_distillation.run_style_distillation(
        style_env["lib"],
        style_env["novel"],
        style_env["author"],
        style_skill=cmd,
    )
    assert result.get("mode") == "external"
    assert result.get("returncode") == 0
    assert marker.read_text(encoding="utf-8") == "ok"


def test_external_skill_failure_reported(style_env, tmp_path):
    skill = tmp_path / "fail_skill.py"
    skill.write_text("import sys\nsys.stderr.write('boom')\nsys.exit(1)\n", encoding="utf-8")

    sys.path.insert(0, SCRIPTS)
    import style_distillation

    result = style_distillation.run_style_distillation(
        style_env["lib"],
        style_env["novel"],
        style_env["author"],
        style_skill=str(skill),
    )
    assert result.get("mode") == "external"
    assert result.get("error") is not None
    assert result.get("returncode") == 1


def test_orchestrate_style_command_runs(style_env):
    ORCHESTRATE = os.path.join(ROOT, "scripts", "orchestrate.py")
    cmd = [
        sys.executable,
        ORCHESTRATE,
        "--lib",
        style_env["lib"],
        "style",
        "--novel",
        style_env["novel"],
        "--author",
        style_env["author"],
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=False)
    assert result.returncode == 0, result.stderr
    card = Path(style_env["lib"]).parent / "风格卡" / "test-author" / "style-card.md"
    assert card.exists()
