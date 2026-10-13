"""SKILL.md frontmatter description 的发现性（SDO）微测。

判据与 B站转录/知识库转录/柠檬思考 等仓的 `test_skill_discovery*` 同一口径，来自
writing-skills 的 Skill Discovery Optimization：
  a) 以触发条件开头（「当…」式），非第一人称、非服务口号；
  b) 长度 ≤ 500 字符（按 Unicode 字符计）；
  c) ≥5 个具体触发项（「…」/『…』引号项 + 「、」分隔的 2–15 字短项）；
  d) 不复述工作流：无「流程为/步骤为/依次执行」，且不得用句号把多阶段动词串成执行指令
     （含阶段词的句子 ≥2 个即判为流程摘要句串）；
  e) 负面清单必须**指明对端能力**（不能只写「交给对应技能」这类空路由）。
     本仓与兄弟仓口径的唯一差别：`test_skill_context_governance` 有一条更早的常驻隔离门
     （主入口出现外部技能标记 `seedance`/`director-styles`/`cinema-director` 等即判红），
     因此本仓的排除句按**能力域**路由而不是按外部技能 slug 路由——两条门都要满足，
     谁也不许为了让另一条变绿而被放宽。
失败消息一律打印实测值。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "SKILL.md"

STAGE_WORDS = ("先", "再", "然后", "接着", "最后", "首先", "继而", "逐步")
BAN_PHRASES = ("流程为", "步骤为", "依次执行")
VAGUE_ROUTES = ("交给对应技能", "其他技能处理", "对应技能处理")
# 排除句必须点到的能力域（本技能把非资产类视觉/视频需求让给谁）。
CAPABILITY_WORDS = ("生图", "广告", "视频", "分镜")


def _desc() -> str:
    text = SKILL.read_text(encoding="utf-8")
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
    assert re.match(r"^当", d.strip()), f"a) description 未以触发句『当…』开头，实测开头：{d[:40]!r}"
    bad = re.findall(r"我可以|我会|我将|帮你", d)
    assert not bad, f"a) description 含第一人称/服务口号，实测命中：{bad}"


def test_b_length_within_500():
    d = _desc()
    assert len(d) <= 500, f"b) description 实测 {len(d)} 字符，超过 500 上限"


def test_c_at_least_five_concrete_triggers():
    d = _desc()
    quoted = re.findall(r"[「『][^」』]{1,20}[」』]", d)
    segs = [s.strip() for s in re.split(r"、", d) if 2 <= len(s.strip()) <= 15]
    total = len(quoted) + len(segs)
    assert total >= 5, (
        f"c) 具体触发项不足：实测 {total}（引号项 {len(quoted)}={quoted}，、分隔短项 {len(segs)}={segs}）"
    )


def test_d_no_workflow_summary():
    d = _desc()
    hit = [p for p in BAN_PHRASES if p in d]
    assert not hit, f"d) description 出现流程摘要词，实测命中：{hit}"
    sentences = [s for s in re.split(r"。", d) if s.strip()]
    flow_sents = [s for s in sentences if any(w in s for w in STAGE_WORDS)]
    assert len(flow_sents) <= 1, (
        f"d) 句号串联阶段动词成执行指令：{len(flow_sents)} 个句子含阶段词：{flow_sents}"
    )


def test_e_negative_list_routes_to_concrete_capability():
    d = _desc()
    m = re.search(r"不要仅因[^。]*。|不适用：[^。]*", d)
    assert m, f"e) description 缺少负面清单/排除句，实测：{d!r}"
    tail = m.group(0)
    vague = [v for v in VAGUE_ROUTES if v in tail]
    assert not vague, f"e) 排除句仍是空路由（未指明对端能力），实测命中：{vague}｜整句：{tail!r}"
    hit = [w for w in CAPABILITY_WORDS if w in tail]
    assert len(hit) >= 2, (
        f"e) 排除句应点名 ≥2 个让出的能力域（候选 {CAPABILITY_WORDS}），实测 {hit}｜整句：{tail!r}"
    )
