# -*- coding: utf-8 -*-
"""规则分叉探测器（优化项：把"整晚反复踩的坑"变成机器可查）。

## 为什么需要它

2026-09-20/21 这一轮 8 个修复里有 5 个是**同一类病**：同一件事有多份口径。

- 段级提取模板教 `[状态:受损]`，消费侧却要求写母资产名；
- 校验器查「画风锁定段-镜头」，生成侧压根不比对；
- 校验器查描述文档的单行顿号，生成侧只查必填标记；
- 生成侧写的跳过清单是短横条目，覆盖率门只认表格行；
- 引用检查只看 Markdown 别名列，索引里的结构化别名视而不见。

每一条的现场代价都是"白跑一整趟全链（20–40 分钟真端点）才在 verify 被拦下"。
本模块把这类分叉**变成一条会红的测试**：校验器新增/改名任何一条标签，而生成侧没跟上，
这里的断言就失败，并直接告诉你缺的是哪几条。

## 口径

- **校验器标签集**：从 `check_schedule.BASE_CHECKS` / `build_style_lock_checks` 与
  `check_ai_flavor` 的探针**推导**（不手抄），避免探测器自己又变成第三份口径。
- **生成侧标签集**：`prompt_rules._COMPLIANCE_MARKERS` / `style_lock_layer_checks` /
  `dunhao_violations` / `abstract_word_hits`。
- 允许的差异必须**登记在 KNOWN_GAP 里并写明理由**；未登记即为分叉（测试失败）。
"""
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

from engine import prompt_rules  # noqa: E402
from check_schedule import BASE_CHECKS, build_style_lock_checks, check_ai_flavor  # noqa: E402

TYPES = ("人物", "场景", "道具")

# 校验器在做、生成侧**有意不做**的项：每条都要写清为什么可以只在校验器上。
# 判据：生成侧不做会不会让用户白跑一整趟？不会（因为生成侧已能拦住绝大多数，
# 剩下的属于"文件级/跨界"判据，且 verify 报文里能一眼看到）→ 可登记；否则必须接进生成侧。
KNOWN_GAP: dict[str, str] = {
    "纯中文": "文件级判据：色值 #RRGGBB 与【】机读元数据行要豁免，而豁免口径在合并文档的解析层；生成侧逐节看不到节头元数据，硬抄一份豁免表反而会造出第三份口径",
    "AI味措辞": "起手式/翻案腔属行文风格，改一版措辞的成本高于收益；verify 报文能一眼定位到句子",
    "过度省略号": "同上（风格类）",
    "量化虚词堆砌": "同上（风格类）",
    "转折堆砌": "同上（风格类）",
    "视图定性段": "由代码按风格包逐字注入（build_view_block），模型照抄即得；缺它时 verify 报文点名到段，且风格包内容不属生成侧可判断范围",
    "负面段含画风负面": "负面段由代码并集组装后整段注入，生成侧照抄即得；改由生成侧判断等于重算一遍组装块",
}

# 探针：每个 AI 味分支各给一例，用来**推导**校验器会产出的标签名（避免手抄标签清单）。
AI_FLAVOR_PROBES = (
    "说白了，这个角色很强。",                                  # 起手式/翻案腔
    "绝美的宫殿在夜色中震撼登场。",                            # 抽象空洞词
    "、".join(f"要素{i}" for i in range(12)),                   # 顿号罗列过密
    "他……回头看……庭院已空。庭院已空无人声。",                 # 过度省略号
    "他微微侧头。微微皱眉。微微抬手。",
    "然而灯灭了。但是月升了。不过云散了。可风还在。",
)

# 生成侧已知的 AI 味标签前缀（与 prompt_rules 的 FLAVOR_HIT_PREFIX 同义；此处只做前缀剥离）
_HIT_PREFIX = "命中·"


def _strip_kind(label: str) -> str:
    """把带取值的标签收敛成"种类"，便于与生成侧比对（如 抽象空洞词·极致 → 抽象空洞词）。"""
    return label.split("·", 1)[0]


def checker_labels(type_key: str, card: dict) -> set[str]:
    """校验器对一个提示词节可能产出的全部标签（按种类归并）。"""
    labels = {_strip_kind(item[0]) for item in BASE_CHECKS[type_key]}
    labels |= {_strip_kind(c[0]) for c in build_style_lock_checks(card, type_key, "古风")}
    for probe in AI_FLAVOR_PROBES:
        labels |= {_strip_kind(l[len(_HIT_PREFIX):] if l.startswith(_HIT_PREFIX) else l)
                   for l in check_ai_flavor(probe)}
    labels.add("纯中文")
    return labels


def generator_labels(type_key: str, card: dict) -> set[str]:
    """生成侧自检对一个提示词节实际会产出的标签（按种类归并）。

    含三层：硬判据（`_COMPLIANCE_MARKERS`）、软判据（`extra_token_missing` 直接源自校验器常量）、
    以及锁定分层/用词/顿号。
    """
    labels: set[str] = set()
    for item in prompt_rules._COMPLIANCE_MARKERS[type_key]:
        labels.add(_strip_kind(item[0]))
    # 软判据：把校验器常量里那些"非硬判据"的标签也算作生成侧会报的（extra_token_missing 的实现）
    hard = {item[0] for item in prompt_rules._COMPLIANCE_MARKERS[type_key]}
    for source in (BASE_CHECKS[type_key],):
        for item in source:
            if item[0] not in hard:
                labels.add(_strip_kind(item[0]))
    labels |= {_strip_kind(c[0]) for c in prompt_rules.style_lock_layer_checks(card, type_key)}
    labels.update({"顿号超限", "抽象空洞词"})
    return labels


# 同一体规则在两边的**不同叫法**：规则同源、只是标签文案不同（不视为分叉，但要显式登记）。
LABEL_ALIASES = {
    "顿号罗列过密": "顿号超限",
    "顿号超限": "顿号罗列过密",
}


CARD = {"id": "A2", "name": "仙侠奇幻风", "pack": "", "layers": [
    "1. 建模层：次世代高精度建模", "2. 渲染层：物理渲染材质", "4. 镜头：大气构图与留白"]}


class TestRuleDesyncDetector(unittest.TestCase):
    """校验器新增一条规则、生成侧没跟上 → 这里必须红，并在报文里点名。"""

    def test_no_unregistered_gap_between_checker_and_generator(self):
        for t in TYPES:
            with self.subTest(type=t):
                gap = {_strip_kind(x) for x in checker_labels(t, CARD)} - \
                      {_strip_kind(x) for x in generator_labels(t, CARD)}
                # 同源异名的规则按别名折掉（如 顿号罗列过密 ↔ 顿号超限）
                gap = {g for g in gap if LABEL_ALIASES.get(g) not in generator_labels(t, CARD)}
                unregistered = sorted(gap - set(KNOWN_GAP))
                self.assertEqual(
                    unregistered, [],
                    f"{t}：校验器有、生成侧没有、且未登记的规则 {unregistered}。"
                    "处理方式二选一：① 接进生成侧自检（首选）；"
                    "② 登记进 KNOWN_GAP 并写明「为什么可以只在校验器上」。",
                )

    def test_registered_gaps_are_still_real(self):
        """已登记的差异不得变成死条目：校验器不再有这条规则时，登记项要清理。"""
        for t in TYPES:
            all_checker = set()
            for x in TYPES:
                all_checker |= checker_labels(x, CARD)
            for gap_label in list(KNOWN_GAP):
                if gap_label in all_checker:
                    continue
                self.assertTrue(
                    gap_label not in set(generator_labels(t, CARD)),
                    f"KNOWN_GAP 里的『{gap_label}』两边都没有了，请从登记表移除",
                )

    def test_entered_rules_are_actually_shared(self):
        """已接进生成侧的几条必须是**同一份实现**（防"接了个复制品"）。"""
        from check_schedule import ABSTRACT_WORDS
        self.assertIs(ABSTRACT_WORDS, prompt_rules.ABSTRACT_WORDS)
        via_checker = {c for c in build_style_lock_checks(CARD, "人物", "古风") if isinstance(c[1], tuple)}
        for item in prompt_rules.style_lock_layer_checks(CARD, "人物"):
            self.assertIn(item, via_checker, "锁定分层判据必须是同一份实现")


if __name__ == "__main__":
    unittest.main()
