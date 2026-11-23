# -*- coding: utf-8 -*-
"""报错必须可行动（工单 T10 项1：依赖缺失给可行动错误）。

现场：`renderplan` 的分档语义是对的（NOT_RUN/PARTIAL 不算绿、占位桩严禁签发），
但收件人看到「剪映导出未验证（缺 jianying-editor 运行环境）」或
「RENDER_ENDPOINT 未配置（BLOCKED-CRED）」时，没有任何一句说下一步该怎么办——
装什么、配哪条、还是改走人工路径。红线是"依赖缺失要给可行动错误"。

判据不检查措辞，只要求每条缺件报错同时点名：①缺的是什么 ②可执行的下一步。
"""
import re
from pathlib import Path

import pytest

import renderplan.adapters as ad
import renderplan.stages as st

ACTION = re.compile(r"(请安装|需安装|安装 |pip install|export [A-Z_]+=|请设置|设置 [A-Z_]+=|"
                   r"改用|改走|请改用|重跑|再跑|参见|见 `|手工导入|人工导入|填 RENDER)")


def _assemble_note(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "_jianying_module", lambda: None)
    plan = {"source": "s", "style": "写实",
            "shots": [{"shot_id": "SH_1", "duration": 3.0, "dialogue": "台词"}]}
    res = st.run_assemble_stage(plan, tmp_path, ledger_path=None, run_id="t1")
    return str(res.get("note") or ""), str(res.get("status") or "")


def test_missing_jianying_env_names_a_next_step(monkeypatch, tmp_path):
    note, status = _assemble_note(monkeypatch, tmp_path)
    assert "jianying" in note.lower() or "剪映" in note, f"没点名缺什么：{note}"
    assert status in {"NOT_RUN", "PARTIAL"}, f"缺环境却签了绿：{status}"
    assert ACTION.search(note), f"缺可行动的下一步（装什么/改哪条/走哪条人工路径）：{note}"


def test_missing_render_endpoint_names_variable_and_fix(monkeypatch):
    monkeypatch.delenv("RENDER_ENDPOINT", raising=False)
    monkeypatch.delenv("RENDER_KEY", raising=False)
    with pytest.raises(RuntimeError) as got:
        ad._guard_credentials()
    msg = str(got.value)
    assert "RENDER_ENDPOINT" in msg, f"没点名缺哪个配置：{msg}"
    assert ACTION.search(msg), f"缺可行动的下一步：{msg}"


def test_placeholder_render_key_names_the_variable_and_fix(monkeypatch):
    monkeypatch.setenv("RENDER_ENDPOINT", "http://127.0.0.1:9/api")
    monkeypatch.setenv("RENDER_KEY", "REPLACE")
    with pytest.raises(RuntimeError) as got:
        ad._guard_credentials()
    msg = str(got.value)
    assert "RENDER_KEY" in msg, f"没点名哪条凭据是占位（只说『凭据』等于没说）：{msg}"
    assert ACTION.search(msg), f"缺可行动的下一步：{msg}"
