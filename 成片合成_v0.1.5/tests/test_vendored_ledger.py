# -*- coding: utf-8 -*-
"""包内共享内核副本的完整性：解析必须落在包内，摘要必须对得上登记。

现场（2026-10-02，cand1 解压验收）：`tests/test_ledger.py` 在收件机上报
「缺统一台账内核: …\\schemas\\ledger.py」——它只往工作区同级 `schemas/` 找，
那是开发柜特权。修法是把 C8 内核 vendor 进包并改解析顺序，本文件钉住修好的两件事：
① 解析结果必须在**包内**（不依赖柜根）；② 包内副本与 `VENDORED.md` 登记的 sha256 一致。
"""
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor" / "schemas"


def _pins():
    text = (VENDOR / "VENDORED.md").read_text(encoding="utf-8", errors="replace")
    return dict(re.findall(r"副本 sha256（([^）]+)） \| ([0-9a-f]{64})", text))


def test_kernel_resolution_stays_inside_the_package():
    """未设 env 时，内核必须解析到包内副本；解析到柜根等于收件人必缺件。"""
    import os
    os.environ.pop("UNIFIED_LEDGER_PY", None)
    from renderplan.plan import UNIFIED_LEDGER_PY as kernel
    assert VENDOR.is_dir(), "缺包内 vendor/schemas/：C8 内核没随包"
    try:
        inside = kernel.resolve().relative_to(ROOT.resolve())
    except ValueError:
        raise AssertionError(f"内核解析到包外：{kernel}（收件机没有柜根就会缺件）")
    assert inside.as_posix().startswith("vendor/"), f"解析结果不在包内：{inside}"


def test_vendored_files_match_registered_digests():
    pins = _pins()
    assert pins, "VENDORED.md 里没有可解析的 sha256 登记行"
    bad = []
    for name, want in pins.items():
        f = VENDOR / name
        if not f.is_file():
            bad.append((name, "MISSING"))
            continue
        got = hashlib.sha256(f.read_bytes()).hexdigest()
        if got != want:
            bad.append((name, f"{got[:12]}… != {want[:12]}…"))
    assert not bad, f"包内副本与登记不符（改上游须同步重算）：{bad}"


def test_vendored_kernel_actually_validates_records():
    """不只是文件在位：内核必须真能加载并给出 validate()。"""
    import importlib.util
    kernel = VENDOR / "ledger.py"
    spec = importlib.util.spec_from_file_location("_vendored_run_ledger", kernel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert callable(getattr(mod, "validate", None)), "包内内核没有 validate()"
