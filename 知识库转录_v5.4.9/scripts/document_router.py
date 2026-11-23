# -*- coding: utf-8 -*-
"""资料类型建议路由：小说 / 知识资料 / 混合待确认。

这是建议器，不会静默替用户改写资料类型；低置信度结果必须人工确认。
"""
from __future__ import annotations

import re


def classify_text(text: str, user_type: str | None = None) -> dict:
    if user_type in {"novel", "knowledge", "mixed"}:
        return {"suggested_type": user_type, "confidence": 1.0, "signals": ["用户明确指定"], "requires_confirmation": False}
    sample = text or ""
    novel = 0
    knowledge = 0
    signals = []
    dialogue = len(re.findall(r"[“\"].{1,80}[”\"]", sample))
    definitions = len(re.findall(r"(?:是指|定义为|意味着|通常指|包括以下)", sample))
    numbers = len(re.findall(r"\d+(?:\.\d+)?\s*%|\d+\s*(?:个|项|条|次|年|月)", sample))
    chapters = len(re.findall(r"(?:第[一二三四五六七八九十百千万0-9]+章|Chapter\s*\d+)", sample, re.I))
    if dialogue >= 3:
        novel += 3
        signals.append("对话密度高")
    if re.search(r"(?:他|她|少年|少女|说道|心想|抬头|转身)", sample):
        novel += 2
        signals.append("人物动作/心理描写")
    if definitions >= 2:
        knowledge += 3
        signals.append("定义句密度高")
    if numbers >= 2:
        knowledge += 3
        signals.append("数据或指标较多")
    if re.search(r"(?:步骤|方法|注意事项|最佳实践|结论|参考文献)", sample):
        knowledge += 2
        signals.append("方法论结构明显")
    if chapters:
        novel += 1
        knowledge += 1
    total = novel + knowledge
    if total == 0 or abs(novel - knowledge) <= 1:
        suggested, confidence = "mixed", 0.5
    elif novel > knowledge:
        suggested, confidence = "novel", novel / total
    else:
        suggested, confidence = "knowledge", knowledge / total
    return {
        "suggested_type": suggested,
        "confidence": round(min(confidence, 1.0), 3),
        "signals": list(dict.fromkeys(signals))[:6],
        "requires_confirmation": confidence < 0.85,
    }


def main() -> int:
    import argparse
    import json
    parser = argparse.ArgumentParser(description="建议判断资料类型，不替用户静默路由")
    parser.add_argument("input", help="UTF-8 文本文件")
    parser.add_argument("--type", choices=["novel", "knowledge", "mixed"])
    args = parser.parse_args()
    with open(args.input, encoding="utf-8-sig", errors="replace") as f:
        print(json.dumps(classify_text(f.read(), args.type), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
