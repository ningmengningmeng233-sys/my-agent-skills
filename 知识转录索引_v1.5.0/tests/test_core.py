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
