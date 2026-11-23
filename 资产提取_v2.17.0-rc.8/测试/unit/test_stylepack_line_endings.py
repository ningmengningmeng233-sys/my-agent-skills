# -*- coding: utf-8 -*-
"""风格包行尾与哈希回归（工单 Z07）。

背景：分享构建把风格包里 26 个文本按 CRLF 落盘，而随包 `manifest.toml` 钉的是权威 LF 字节，
于是 `stylepack_verify` 读出 26 条「内容漂移」，视觉导出被 STYLEPACK_UNAVAILABLE 拦下。
本测试钉两条不变量，防止发布工具再造成同类漂移：
①风格包内**每个在册文本**都不得含 CR（LF-only）；
②在盘文件的 SHA-256 必须精确等于清单声明值。
只测包内已随附的文件；按策略排除的参考图/来源图不在本测试射程。
"""
from __future__ import annotations

import hashlib
import pathlib
import sys

PACK_DIR = pathlib.PurePath("references") / "styles" / "仙侠国风电影CG" / "1.0.0"
ROOT = pathlib.Path(__file__).resolve().parents[2]
CONTENT_ROOT = ROOT / PACK_DIR / "private"
sys.path.insert(0, str(ROOT / "scripts"))


def declared_files() -> list[dict]:
    from engine.common import toml_module  # 3.11 用 tomllib、3.10 退到 tomli，仓内唯一口径
    with (ROOT / PACK_DIR / "manifest.toml").open("rb") as fh:
        man = toml_module().load(fh)
    return list(man["files"])


TEXT_SUFFIXES = (".md", ".txt", ".json", ".toml", ".py")


def present_text_files() -> list[tuple[dict, pathlib.Path]]:
    """只射程内的**文本**件：PNG 等二进制天然含 0x0D，按 CR 判会自己造假红（实测踩过）。"""
    out = []
    for f in declared_files():
        p = CONTENT_ROOT / f["path"]
        if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES:
            out.append((f, p))
    return out


def test_pack_is_present():
    """清单与在盘件都要找得到——路径假设变了就先炸这里，别让它静默空跑。"""
    assert (ROOT / PACK_DIR / "manifest.toml").is_file()
    assert len(present_text_files()) > 0, "风格包文本一个都没在盘，测试没有射程"


def test_no_carriage_returns_in_pack_texts():
    bad = [(f["path"], p.read_bytes().count(b"\r"))
           for f, p in present_text_files() if b"\r" in p.read_bytes()]
    assert not bad, f"风格包文本含 CR（应为 LF-only）：{bad[:6]}"


def test_pack_text_hashes_equal_manifest_declarations():
    bad = [(f["path"], f["sha256"][:12], hashlib.sha256(p.read_bytes()).hexdigest()[:12])
           for f, p in present_text_files()
           if hashlib.sha256(p.read_bytes()).hexdigest() != f["sha256"]]
    assert not bad, f"风格包文本与清单声明不符：{bad[:6]}"


def test_stylepack_verify_reports_no_issue_for_present_texts():
    """产品自己的尺也要说一致——不另抄一份判据。"""
    import sys
    sys.path.insert(0, str(ROOT / "scripts" / "engine"))
    import stylepack_verify as S
    issues = S.verify_pack(CONTENT_ROOT)
    assert not issues, f"stylepack_verify 仍报不一致：{issues[:4]}"
