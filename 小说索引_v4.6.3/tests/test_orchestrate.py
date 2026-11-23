import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ORCHESTRATE = os.path.join(ROOT, "scripts", "orchestrate.py")


def _run(*args: str) -> subprocess.CompletedProcess:
    cmd = [sys.executable, ORCHESTRATE, *args]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=False)


def test_orchestrate_init_creates_directories(tmp_path):
    lib = tmp_path / "lib"
    result = _run("--lib", str(lib), "init")
    assert result.returncode == 0, result.stderr
    for d in ("小说原文", "小说索引", "章节索引"):
        assert (lib / d).exists()


def test_orchestrate_status_empty_lib(tmp_path):
    lib = tmp_path / "lib"
    result = _run("--lib", str(lib), "status")
    assert result.returncode == 0, result.stdout
    assert "断点库尚未创建" in result.stdout


def test_orchestrate_status_shows_book(tmp_lib, sample_segment, sample_index):
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    from index_db import IndexDB

    with IndexDB(tmp_lib) as db:
        db.mark_done(sample_segment, sample_index, prompt_tokens=10, completion_tokens=20)

    result = _run("--lib", tmp_lib, "status")
    assert result.returncode == 0, result.stdout
    assert "test" in result.stdout
    assert "author" in result.stdout
    assert "10" in result.stdout


def test_orchestrate_semantic_build_delegates(tmp_path, monkeypatch):
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import orchestrate

    captured: list[list[str]] = []
    monkeypatch.setattr(orchestrate, "_run", lambda cmd: (captured.append(cmd), 0)[1])

    parser = orchestrate._build_parser()
    args = parser.parse_args(["--lib", str(tmp_path), "semantic", "build", "--force", "--model", "fake"])
    rc = orchestrate.cmd_semantic(args)

    assert rc == 0
    assert captured
    assert "semantic_index.py" in captured[0][1]
    assert "--build" in captured[0]
    assert "--force" in captured[0]
    assert "fake" in captured[0]


def test_orchestrate_semantic_search_delegates(tmp_path, monkeypatch):
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import orchestrate

    captured: list[list[str]] = []
    monkeypatch.setattr(orchestrate, "_run", lambda cmd: (captured.append(cmd), 0)[1])

    parser = orchestrate._build_parser()
    args = parser.parse_args(["--lib", str(tmp_path), "semantic", "search", "query", "--json"])
    rc = orchestrate.cmd_semantic(args)

    assert rc == 0
    assert captured
    assert "semantic_search.py" in captured[0][1]
    assert "query" in captured[0]
    assert "--json" in captured[0]
