# -*- coding: utf-8 -*-
"""T5（v1.4.0 创作纵深）：humanizer 24 类全量包。

血统（附录 A）：24 类模式定义、before/after 范例、Changes made 格式 = 照搬
`work研究/content-creator/skills/humanizer/SKILL.md` 全文；机读禁词 = 照搬 fbs
`s2-quality-machine-lexicon.json` + `s5-buzzword-lexicon.json` 中文化映射。
三分法（Spec §九附注1）：regex→双词表、style→写作纪律、markdown 排版类 14/15/16→导出器自检清单。
"""
import io
import json
import os
import re

from dramacraft.self_healing import (
    build_healing_prompt,
    render_ai_tone_examples,
    DEFAULT_SYSTEM_PROMPT,
    run_quality_gate,
)
from dramacraft.ingestor import _read_file_safe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATCH_DIR = os.path.join(ROOT, "skills_runtime", "dramacraft_patch")

# 24 类清单（硬编码比对：少一类即红）
CLASS_NAMES = {
    "1": "虚假意义拔高", "2": "虚假知名度与媒体背书", "3": "假深度分析（分词悬挂）",
    "4": "广告推销腔", "5": "模糊来源与推脱词", "6": "提纲式挑战与展望",
    "7": "泛用 AI 高频词", "8": "回避系动词（堪称/不失为）", "9": "否定式对仗",
    "10": "三段式排比", "11": "同义轮换（花式重复）", "12": "虚假范围",
    "13": "破折号滥用", "14": "粗体滥用", "15": "行内标题竖排清单",
    "16": "标题首字母大写", "17": "表情符号", "18": "引号全半角混用",
    "19": "协作沟通残留", "20": "知识截止免责声明", "21": "谄媚腔",
    "22": "填充短语", "23": "过度模糊限定", "24": "空洞积极结尾",
}
STYLE_KINDS = {"10": "style", "11": "style", "14": "export", "15": "export", "16": "export"}


def _load(name):
    with io.open(os.path.join(PATCH_DIR, name), encoding="utf-8") as fp:
        return json.load(fp)


# ---------- ① 24 类全量在册 ----------

def test_examples_pack_registers_all_24_classes():
    pack = _load("ai_tone_examples.json")
    ids = [k for k in pack if not k.startswith("_")]
    assert sorted(ids, key=int) == sorted(CLASS_NAMES, key=int), "24 类必须全量在册，一类不落"
    for cid, name in CLASS_NAMES.items():
        item = pack[cid]
        assert item["name"] == name
        assert item["kind"] in ("regex", "style", "export")
        assert str(item["before"]).strip() and str(item["after"]).strip()
        assert str(item["why"]).strip(), f"第 {cid} 类缺 why（Changes made 口径）"
    kinds = {cid: pack[cid]["kind"] for cid in CLASS_NAMES}
    assert kinds == {cid: STYLE_KINDS.get(cid, "regex") for cid in CLASS_NAMES}


def test_regex_classes_are_wired_into_lexicons():
    """kind=regex 的类必须真在双词表里有落点（否则只是一纸清单）。"""
    pack = _load("ai_tone_examples.json")
    pattern_names = {p["name"] for p in _load("ai_tone_patterns.json")}
    banned = set(_load("banned_words.json"))
    for cid, item in pack.items():
        if cid.startswith("_") or item["kind"] != "regex":
            continue
        wired = any(n in pattern_names for n in item.get("patterns", [])) or \
            any(w in banned for w in item.get("words", []))
        assert wired, f"第 {cid} 类（{item['name']}）regex 类未接进双词表"


def test_word_lexicon_absorbed_fbs_buzzwords():
    banned = _load("banned_words.json")
    assert len(banned) > 40, "动作行词表须吸收 humanizer 词根与 fbs 两份禁词表条目"
    assert len(banned) == len(set(banned)), "词表不得有重复条目"


def test_pattern_lexicon_is_valid_regex_and_grew():
    pats = _load("ai_tone_patterns.json")
    assert len(pats) >= 20, "台词 AI 腔正则表须含 humanizer regex 类"
    for p in pats:
        assert p["name"].strip() and p["pattern"].strip()
        re.compile(p["pattern"]), f"非法正则：{p['name']}"   # Python 侧可编译
        re.compile(p["pattern"]).search("")                  # 空串不炸


# ---------- ② 新条目真能命中 + 进 hits ----------

def test_new_pattern_hits_ai_style_line():
    from dramacraft.self_healing import run_quality_gate as gate
    script = {
        "source": "AI 腔样例",
        "episodes": [{
            "ep": 1, "targetSeconds": 90, "hook": "h", "hookBeat": [1, 1], "cliff": "c",
            "beatsClaimed": ["b"],
            "scenes": [{"sceneId": "S01", "characters": ["C01"], "flow": [
                {"type": "dialogue", "speaker": "C01",
                 "line": "这不只是一次合作，这是一个里程碑，标志着我们迈入新时代。"},
            ]}],
        }],
    }
    hits = []
    passed, _report, failed = gate(script, hits_collector=hits)
    assert passed is False
    assert hits, "新正则条目必须能把命中记录进 hits 数组"
    assert any(h.get("gate") == "ai-tone-removal" for h in hits)


# ---------- ③ fixtures 12/12 零误伤 ----------

def test_no_new_term_matches_fixture_lines_or_actions():
    """直扫样例剧本的每一句台词与动作行：新词表零命中（零误伤死条款）。"""
    sample = json.loads(_read_file_safe(os.path.join(ROOT, "fixtures", "sample_script.json")))
    patterns = [(p["name"], re.compile(p["pattern"])) for p in _load("ai_tone_patterns.json")]
    banned = _load("banned_words.json")
    offenders = []
    for ep in sample["episodes"]:
        for sc in ep.get("scenes", []):
            for beat in sc.get("flow", []):
                line = beat.get("line")
                if isinstance(line, str):
                    for name, rx in patterns:
                        if rx.search(line):
                            offenders.append((ep["ep"], name, line[:30]))
                act = beat.get("action")
                if isinstance(act, str):
                    for w in banned:
                        if w in act:
                            offenders.append((ep["ep"], w, act[:30]))
    assert offenders == [], f"样例剧本被新词表误伤：{offenders}"


def test_fixture_script_still_passes_all_gates():
    script = json.loads(_read_file_safe(os.path.join(ROOT, "fixtures", "sample_script.json")))
    outline = json.loads(_read_file_safe(os.path.join(ROOT, "fixtures", "sample_outline.json")))
    passed, report, failed = run_quality_gate(script, outline_data=outline)
    assert passed is True, report
    assert failed == []


# 字面义反例语料：这些是**完全正常**的中文台词与动作行，新词表一条都不许命中。
# G11/G12 是否决门（回炉 3 轮改不掉字面义 → 该集必然生成失败），故这是零误伤死条款的主防线。
_LITERAL_ACTIONS = [
    "林晨把两块木板对齐了桌沿。",
    "他一拳打通了那堵墙。",
    "泥沙慢慢沉淀到缸底。",
    "石狮子底座上落满了灰。",
    "两拨人马协同冲进了院子。",
    "两根铁链联动着把闸门拽了起来。",
    "他抓紧栏杆的抓手，翻了上去。",
    "他从侧面卡位，把对手挤出了边线。",
    "钢索绷紧，势能全压在那根横梁上。",
    "他一记组合拳砸在沙袋上。",
    "他见证了那场大火烧了整整一夜。",
    "老石匠为祠堂奠定了第一块基石。",
    "他问了一个好问题，屋里安静下来。",
    "他问起当年的原因，老人却不肯说。",
]

_LITERAL_LINES = [
    "大哥、二哥、三弟都来了。",
    "他说「我不去」，然后就走了。",
    "你把门关上。",
    "这主意行。",
    "坐下说。",
    "钱我不要，人我带走。",
]


def test_word_lexicon_has_no_literal_meaning_collisions():
    banned = _load("banned_words.json")
    bad = [(w, s) for s in _LITERAL_ACTIONS for w in banned if w in s]
    assert bad == [], f"动作行词表误伤正常中文（字面义）：{bad}"


def test_pattern_lexicon_has_no_literal_meaning_collisions():
    patterns = [(p["name"], re.compile(p["pattern"])) for p in _load("ai_tone_patterns.json")]
    bad = [(n, s) for s in _LITERAL_LINES for n, rx in patterns if rx.search(s)]
    assert bad == [], f"台词正则误伤正常中文：{bad}"


def test_literal_corpus_passes_both_hard_gates():
    """把反例语料当真实剧本喂进 12 门：G11/G12 必须全绿（误伤即集数生成永久失败）。"""
    ep = {
        "ep": 1, "targetSeconds": 90, "hook": "h", "hookBeat": [1, 1], "cliff": "c",
        "beatsClaimed": ["b"],
        "scenes": [{"sceneId": "S01", "characters": ["C01"], "flow": (
            [{"type": "action", "action": s} for s in _LITERAL_ACTIONS]
            + [{"type": "dialogue", "speaker": "C01", "line": s} for s in _LITERAL_LINES]
        )}],
    }
    _passed, report, failed = run_quality_gate({"source": "反例", "episodes": [ep]})
    hits = [f for f in failed if "文学病" in f or "辩论腔" in f or "翻案腔" in f
            or "AI" in f or "禁词" in f or "腔" in f]
    assert hits == [], f"反例语料被硬门误伤：{hits}\n{report}"


# ---------- ④ 回炉指令携带 before→after ----------

def test_render_examples_from_hits_carries_before_after():
    hits = [{"gate": "ai-tone-removal", "pattern": "AI腔：虚假意义拔高"}]
    block = render_ai_tone_examples(hits)
    assert "改写示范" in block
    assert "改前" in block and "改后" in block


def test_render_examples_returns_empty_without_hits():
    assert render_ai_tone_examples([]) == ""
    assert render_ai_tone_examples(None) == ""


def test_healing_prompt_can_carry_example_block():
    prompt = build_healing_prompt({"ep": 1}, ["[违规] x"],
                                  examples_block="【AI 味改写示范】\n改前：A\n改后：B")
    assert "改写示范" in prompt and "改后：B" in prompt


def test_render_examples_missing_file_is_silent(monkeypatch):
    import dramacraft.self_healing as sh
    monkeypatch.setattr(sh, "AI_TONE_EXAMPLES_PATH", os.path.join(str(ROOT), "不存在.json"))
    assert sh.render_ai_tone_examples([{"gate": "ai-tone-removal",
                                        "pattern": "AI腔：虚假意义拔高"}]) == ""


# ---------- ⑤ 正向写作纪律入初稿 system prompt ----------

def test_default_system_prompt_carries_soul_discipline():
    assert "写出人味" in DEFAULT_SYSTEM_PROMPT
    for kw in ("句长", "犹疑", "半句"):
        assert kw in DEFAULT_SYSTEM_PROMPT, f"正向纪律缺要点：{kw}"


# ---------- ⑥ 导出器排版自检清单（三类，不设闸） ----------

def test_exporter_style_checklist_covers_three_classes():
    from dramacraft.exporter import EXPORT_STYLE_CHECKLIST
    assert "粗体" in EXPORT_STYLE_CHECKLIST
    assert "行内标题" in EXPORT_STYLE_CHECKLIST
    assert "标题" in EXPORT_STYLE_CHECKLIST
