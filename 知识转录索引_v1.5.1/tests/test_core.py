"""核心索引纯函数回归测试。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import common
from 检索 import cluster, keyword_hits
from 切条目 import extract_json
from 校对 import fix_terms


def test_artifact_key_separates_same_stem() -> None:
    assert common.artifact_key("a/foo.txt", "a" * 64) != common.artifact_key("b/foo.txt", "b" * 64)


def test_fix_terms_uses_word_boundaries() -> None:
    assert fix_terms("Agency Client", [("Agen", "Agent"), ("Client", "Qwen-Image")]) == "Agency Qwen-Image"


def test_fix_terms_keeps_chinese_whitelist() -> None:
    assert fix_terms("洛拉", [("洛拉", "LoRA")]) == "LoRA"


def test_extract_json_accepts_trailing_comma() -> None:
    assert extract_json('{"items": [], "term_fixes": [],}') == {"items": [], "term_fixes": []}


def test_keyword_hits_ignores_short_noise() -> None:
    entry = {"name": "怎么安装", "summary": "如何使用", "excerpt": "ComfyUI 安装流程"}
    assert keyword_hits("怎么", entry) == 0


def test_cluster_groups_by_tag() -> None:
    result = cluster([{"tag": "训练", "score": 0.8}, {"tag": "训练", "score": 0.7}])
    assert len(result) == 1
    assert result[0]["tag"] == "训练"
    assert len(result[0]["items"]) == 2


def test_unconfigured_src_dir_fails_closed_on_io() -> None:
    sentinel = common._UnconfiguredPath("未配置 SRC_DIR")
    import pytest
    with pytest.raises(RuntimeError, match="未配置 SRC_DIR"):
        _ = (sentinel / "索引库").exists()


def test_redirect_src_scan_sources_and_skips(tmp_path: Path) -> None:
    old_src = common.SRC_DIR
    try:
        common.redirect_src(tmp_path)
        (tmp_path / "视频A_作者甲_20260101_BV1xx411c7mD.txt").write_text("正文内容一", encoding="utf-8")
        (tmp_path / "普通笔记无BV号.txt").write_text("普通文本", encoding="utf-8")
        (tmp_path / "BV_TEST_残留.txt").write_text("测试残留", encoding="utf-8")

        scans = common.scan_sources()
        skips = common.scan_skips()
        assert len(scans) == 1
        assert scans[0]["bv"] == "BV1xx411c7mD"
        assert len(skips) == 1
        assert skips[0]["rel"] == "普通笔记无BV号.txt"

        manifest = {"files": {}}
        common.assign_artifact_keys(scans, manifest)
        assert scans[0]["artifact_key"].startswith("视频A_作者甲_20260101_BV1xx411c7mD--")

        common.ensure_dirs()
        assert common.IDX_DIR.is_dir() and common.PROOF_DIR.is_dir() and common.VEC_DIR.is_dir()
    finally:
        common.SRC_DIR = old_src
        common.IDX_DIR = old_src / "索引库"
        common.PROOF_DIR = old_src / "校对版"
        common.VEC_DIR = old_src / "向量库"
        common.MANIFEST = common.IDX_DIR / "manifest.json"
        common.VEC_META = common.VEC_DIR / "metadata.json"
        common.STATE_DIR = old_src / "索引配置"


def test_bv_from_name_extracts_valid_bvid() -> None:
    assert common.bv_from_name("教程_作者_20260501_BV1ab411c7mD.txt") == "BV1ab411c7mD"
    assert common.bv_from_name("无BV号普通文档.txt") is None
