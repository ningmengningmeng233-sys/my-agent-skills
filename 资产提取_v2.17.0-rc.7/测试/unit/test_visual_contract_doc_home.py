# -*- coding: utf-8 -*-
"""F-21：T07 机械搬移时漏带的契约文档段必须住在新家，且逐字不许改。

现场：`visual_schema.py` 原第 1-12 行的权威口径（TOML 唯一机器事实源、路径相对资产库、
写入确定且原子、中文直写 UTF-8、枚举取英文稳定值）没跟着搬走，读代码的人少了一段口径。
行为没变（往返与确定性测试把住），但「搬移=逐字搬运」这句话要有尺子撑着，不能靠印象。
"""
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "engine"

TYPES_LINES = [
    "- TOML 是视觉任务的**唯一机器事实源**；Markdown 阅读稿由 TOML 渲染（禁止双份手工维护）。",
    "- 任务 ID 形如 `<资产ID>-<任务种类>-vNN`；所有路径保存为**相对资产库路径**。",
    "内部枚举取英文稳定值（机器事实源），用户输出经 ``user_status_label`` 取中文（规格 8）。",
]
IO_LINES = [
    "- 写入确定性（同一对象两次写出字节一致）且原子（临时文件 + os.replace）。",
    r"- 中文直接写 UTF-8，绝不转义为 ``\\uXXXX``；Windows 反斜杠输入归一化为 ``/``。",
]


def _head(path):
    return path.read_text(encoding="utf-8")


def test_contract_lines_live_in_visual_types_verbatim():
    text = _head(SCRIPTS / "visual_types.py")
    missing = [ln for ln in TYPES_LINES if ln not in text]
    assert not missing, f"契约段未逐字归位：{missing}"


def test_write_discipline_lines_live_in_visual_io_verbatim():
    text = _head(SCRIPTS / "visual_io.py")
    missing = [ln for ln in IO_LINES if ln not in text]
    assert not missing, f"写盘纪律段未逐字归位：{missing}"


def test_facade_still_reexports_both_modules():
    """归位是文档层改动，门面前向兼容不许因此断掉。"""
    import sys
    sys.path.insert(0, str(SCRIPTS.parent))
    from engine import visual_schema as vs
    for name in ("VisualTask", "VisualPlan", "write_visual_plan",
                 "load_visual_plan", "render_visual_plan", "normalize_rel_path"):
        assert hasattr(vs, name), f"门面不再导出 {name}"
