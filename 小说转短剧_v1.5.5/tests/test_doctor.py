import sys
import subprocess
from unittest.mock import patch
import pytest
from dramacraft.doctor import (
    check_python_version,
    check_node_available,
    check_validator_script,
    check_llm_nodes,
    run_doctor
)


def test_check_python_version():
    ok, msg = check_python_version()
    assert ok is True
    assert "Python" in msg
    assert f"{sys.version_info.major}.{sys.version_info.minor}" in msg


def test_check_node_available():
    with patch("shutil.which", return_value=None):
        ok, msg = check_node_available()
        assert ok is False
        assert "未检测到 Node.js" in msg

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "v20.11.0\n"
        with patch("shutil.which", return_value="/usr/bin/node"):
            ok, msg = check_node_available()
            assert ok is True
            assert "v20.11.0" in msg


def test_check_validator_script():
    ok, msg = check_validator_script()
    assert ok is True
    assert "validate.mjs" in msg


def test_run_doctor_all_steps():
    # 模拟环境健全
    with patch("dramacraft.doctor.check_node_available", return_value=(True, "Node.js v20.11.0 正常")):
        passed, report = run_doctor(probe_api=False)
        assert "[1/4] Python 运行环境" in report
        assert "[2/4] Node.js 运行时" in report
        assert "[3/4] 剧本质量门脚本" in report
        assert "[4/4] LLM 模型节点池" in report


def test_run_batch_blocks_when_no_node(monkeypatch):
    import run_batch
    monkeypatch.setattr("run_batch.check_node_available", lambda: (False, "未安装 Node.js"))
    monkeypatch.setattr(sys, "argv", ["run_batch.py", "--outline", "fixtures/sample_outline.json", "--target-seconds", "90"])

    with pytest.raises(SystemExit) as exc_info:
        run_batch.main()
    assert exc_info.value.code == 2
