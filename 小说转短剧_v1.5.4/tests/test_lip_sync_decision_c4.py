# -*- coding: utf-8 -*-
"""C4 —— 口型/唇形同步决策必须显式归属、机器可见。

0.2d 遗留决策：novel-storyboard 曾两次写「口型/唇形同步暂不管——那是生成管线的事」，
但当时没有生成管线技能，无人认领。现在 `成片合成` 将拥有 lip-sync 阶段，
接缝必须写进 SKILL.md 决策正文与 pipeline-contract 声明块，供 contract_check.py 对账。
"""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills_runtime" / "novel-storyboard" / "SKILL.md"

# 与 schemas/contract_check.py 保持同一解析口径
BLOCK_RE = re.compile(
    r"<!--\s*pipeline-contract:begin\s*(.*?)\s*pipeline-contract:end\s*-->", re.S
)


def _skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_lip_sync_no_longer_deferred():
    """SKILL.md 不得再出现「暂不管」的弃责措辞。"""
    text = _skill_text()
    assert "暂不管" not in text, "口型同步决策仍是弃责措辞，未显式归属"


def test_lip_sync_decision_owned_in_prose():
    """决策正文要点：归属 `成片合成`、本仓默认 off、可见影响与降级判定条件。"""
    text = _skill_text()
    assert "成片合成" in text
    assert "lip-sync" in text or "唇形同步" in text or "口型" in text
    # 降级判定条件：有对白的近景镜头若不开口型同步 → 画外音/讲述 + 字幕
    assert "画外音" in text and "字幕" in text


def test_contract_block_parses_with_lip_sync_keys():
    """pipeline-contract 声明块必须是合法 YAML 且带上新接缝键。"""
    text = _skill_text()
    blocks = [m.group(1) for m in BLOCK_RE.finditer(text)]
    assert blocks, "novel-storyboard SKILL.md 缺少 pipeline-contract 声明块"
    parsed = [yaml.safe_load(b) for b in blocks]
    parsed = [p for p in parsed if isinstance(p, dict)]
    seam = [p for p in parsed if "lip_sync_owner" in p and "lip_sync_default" in p]
    assert seam, "声明块未声明 lip_sync_owner / lip_sync_default 接缝"
    block = seam[0]
    assert block["lip_sync_owner"] == "成片合成"
    assert block["lip_sync_default"] == "off"


def test_contract_block_keeps_identity_keys():
    """新增接缝不得挤掉声明块原有的身份/契约键（checker 依赖它们）。"""
    text = _skill_text()
    blocks = [yaml.safe_load(m.group(1)) for m in BLOCK_RE.finditer(text)]
    blocks = [b for b in blocks if isinstance(b, dict)]
    seam = [b for b in blocks if "lip_sync_owner" in b][0]
    for key in ("id", "namespace", "role", "schema", "version", "skill_version"):
        assert key in seam, f"声明块丢失既有键 {key}"
