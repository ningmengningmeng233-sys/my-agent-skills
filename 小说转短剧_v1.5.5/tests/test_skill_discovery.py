# -*- coding: utf-8 -*-
"""SKILL.md frontmatter description 的发现性（SDO）微测（writing-skills 第二轮）。

判据来源：superpowers:writing-skills · Skill Discovery Optimization——
  a) 以触发条件开头（『当用户…』式），不得第一人称、不得抽象口号；
  b) 长度 ≤ 500 字符（按 Unicode 字符计，中文一字一符）；
  c) 「…」/『…』引号内具体触发词/症状 ≥ 5 个（用户原话与错误消息）；
  d) 不复述工作流：不得出现「流程为/步骤为/依次/三轨/一条指令/跑全链/跑通全链/
     第一步」这类把多阶段串成执行指令的句式（SDO 明文：description 只写「何时用」，
     写了流程会让代理照着描述干活而不读正文）。命令名（/dramacraft、
     dramacraft-pipeline）是合法触发词，不受本条限制。
失败消息一律打印实测值。本仓与「成片合成」存在「短剧/跑产线/最后一英里」触发
碰撞，消歧口径：本仓交付物止于剧本文本与质量门，见 test_e。
"""
import re
from pathlib import Path

SKILL_MD = Path(__file__).resolve().parents[1] / "SKILL.md"

BAN_PHRASES = ("流程为", "步骤为", "依次", "三轨", "一条指令", "跑全链", "跑通全链", "第一步")
STAGE_WORDS = ("先", "再", "然后", "接着", "最后", "首先", "继而", "逐步")


def _desc() -> str:
    text = SKILL_MD.read_text(encoding="utf-8")
    m = re.match(r"\A---\r?\n(.*?)\r?\n---", text, re.S)
    assert m, "SKILL.md 缺少 YAML frontmatter"
    fm = m.group(1)
    dm = re.search(r"^description:[ \t]*(.*)$", fm, re.M)
    assert dm, "frontmatter 缺少 description 字段"
    inline = dm.group(1).strip()
    if inline.startswith(">"):  # folded block：收集缩进行
        lines = []
        for ln in fm[dm.end():].split("\n"):
            if ln.strip() and not ln.startswith((" ", "\t")):
                break
            if ln.strip():
                lines.append(ln.strip())
        return " ".join(lines)
    return inline.strip('"').strip("'")


def test_a_trigger_first_not_first_person():
    d = _desc()
    assert re.match(r"^当用户", d.strip()), (
        f"a) description 必须以触发句『当用户…』开头，实测开头：{d[:40]!r}"
    )
    bad = re.findall(r"我可以|我会|我将|本人|帮你", d)
    assert not bad, f"a) description 含第一人称/服务口号，实测命中：{bad}"


def test_b_length_within_500():
    d = _desc()
    assert len(d) <= 500, f"b) description 实测 {len(d)} 字符，超过 500 上限"


def test_c_at_least_five_quoted_triggers():
    d = _desc()
    quoted = re.findall(r"[「『][^」』]{1,30}[」』]", d)
    assert len(quoted) >= 5, (
        f"c) 引号内具体触发词不足：实测 {len(quoted)} 个（要求 ≥5），实测列表：{quoted}"
    )


def test_d_no_workflow_restate():
    d = _desc()
    hit = [p for p in BAN_PHRASES if p in d]
    sentences = [s for s in d.split("。") if s.strip()]
    flow = [s for s in sentences if any(w in s for w in STAGE_WORDS)]
    assert not hit and len(flow) <= 1, (
        f"d) description 复述工作流：禁词命中={hit}；含阶段次序词的句子 {len(flow)} 个（要求 ≤1）：{flow}"
    )


def test_e_exclusion_routes_sibling_renderplan():
    """与「成片合成」碰撞消歧：排除句必须用可观察谓词指向真实技能名。"""
    d = _desc()
    m = re.search(r"不适用：.*", d, re.S)
    assert m, f"e) description 缺少『不适用：…』排除句，实测：{d!r}"
    tail = m.group(0)
    assert "成片合成" in tail, (
        f"e) 排除句未指向真实兄弟技能『成片合成』（分镜之后的渲染归它），实测排除句：{tail!r}"
    )
