"""风格软评估（只给指标，不冒充读者结论）。

按 spec §10.2，本模块提供可审计的启发式指标，不参与 Core 硬门槛。
所有证据都必须带数字，不得把启发式结果写成“好/坏”的确定判断。
"""

from __future__ import annotations

import re
from statistics import mean, median, pstdev
from typing import Any

_SENTENCE_END = re.compile(r"[。！？!?…]")
_AI_PHRASES = (
    "首先", "其次", "最后", "总而言之", "综上所述", "值得注意的是", "不难发现",
    "总的来说", "与此同时", "换句话说", "由此可见", "众所周知", "总的来说",
    "仿佛", "不禁", "缓缓", "淡淡", "微微",
)
_PAYOFF_WORDS = (
    "打脸", "震惊", "碾压", "突破", "反转", "收获", "升级", "装逼",
    "强者", "震撼", "不敢相信", "目瞪口呆", "扬眉吐气",
)
_EMOTION_WORDS = ("怒", "惊", "喜", "悲", "恨", "怕", "羞", "激动", "颤抖", "泪", "笑", "哭")
_DIALOGUE_VERBS = ("说道", "说", "道", "问", "答", "笑道", "低声道", "喊道", "问道", "回答")


def _count_words(text: str, words: tuple[str, ...]) -> int:
    return sum(text.count(word) for word in words)


def _dialogue_chars(text: str) -> int:
    return sum(
        1
        for char in text
        if char in "“”「」『』\"'"
    )


def evaluate(source_text: str, output_text: str) -> list[dict[str, Any]]:
    """返回 checks.soft_evaluations 契约数组。"""
    text = output_text or source_text
    sentences = [s.strip() for s in _SENTENCE_END.split(text) if s.strip()]
    if not sentences:
        sentences = [text.strip()] if text.strip() else ["（空）"]
    lengths = [len(s) for s in sentences]
    paragraphs = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    para_lengths = [len(p) for p in paragraphs] or [0]
    chars = max(len(text), 1)

    dialogue_ratio = _dialogue_chars(text) / max(len(text), 1)
    payoff_density = _count_words(text, _PAYOFF_WORDS) * 1000 / chars
    ai_phrase_count = _count_words(text, _AI_PHRASES)
    ai_density = ai_phrase_count * 1000 / chars
    emotion_density = _count_words(text, _EMOTION_WORDS) * 1000 / chars
    working = text
    verb_counts: dict[str, int] = {}
    for verb in sorted(_DIALOGUE_VERBS, key=len, reverse=True):
        verb_counts[verb] = working.count(verb)
        working = working.replace(verb, "")
    attribution_total = max(sum(verb_counts.values()), 1)
    say_ratio = verb_counts.get("说道", 0) / attribution_total

    length_variance = pstdev(lengths) if len(lengths) > 1 else 0.0
    uniform_flag = length_variance < 8 and len(sentences) >= 8

    return [
        {
            "dimension": "web_novel_voice",
            "result": "indicator",
            "evidence": (
                f"段落数={len(paragraphs)}，中位段长={median(para_lengths):.1f}，"
                f"平均句长={mean(lengths):.1f}±{length_variance:.1f}"
            ),
        },
        {
            "dimension": "payoff_quality",
            "result": "indicator",
            "evidence": f"爽点词密度={payoff_density:.2f} 次/千字（仅词表计数，不代表读者爽感）",
        },
        {
            "dimension": "ai_taste",
            "result": "warning" if (ai_density >= 1.5 or uniform_flag) else "indicator",
            "evidence": (
                f"AI 常用表达={ai_phrase_count} 次（{ai_density:.2f}/千字），"
                f"句长方差={length_variance:.1f}，{'句式较均匀' if uniform_flag else '句式有变化'}"
            ),
        },
        {
            "dimension": "emotion",
            "result": "indicator",
            "evidence": f"情绪词密度={emotion_density:.2f} 次/千字，对话标点占比={dialogue_ratio:.1%}",
        },
        {
            "dimension": "narrator_voice",
            "result": "warning" if say_ratio >= 0.7 else "indicator",
            "evidence": (
                f"对白引导词分布={verb_counts}；"
                f"「说道」占比={say_ratio:.0%}（>70% 提示声线单调）"
            ),
        },
        {
            "dimension": "reader_perception",
            "result": "heuristic_only",
            "evidence": "以上均为词表/句长启发式指标，不构成读者观感结论；最终观感需人工试读或盲审。",
        },
    ]
