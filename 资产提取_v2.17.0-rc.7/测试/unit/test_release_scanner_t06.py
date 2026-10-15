"""Production scanner must see separators without blocking short fixtures."""
import pytest
from scripts import build_release


def test_hyphenated_key_is_detected(tmp_path):
    path = tmp_path / "config.py"
    path.write_text("API_KEY = " + repr("sk-live-" + "a1" * 16), encoding="utf-8")
    with pytest.raises(SystemExit):
        build_release._sensitive_scan([path], tmp_path)


def test_short_synthetic_fixture_is_not_blocked(tmp_path):
    path = tmp_path / "config.py"
    path.write_text("KEY = " + repr("sk-" + "abc" * 3), encoding="utf-8")
    build_release._sensitive_scan([path], tmp_path)
