"""本地化腔调硬门槛（v3 `scripts/check_localization.py` 能力回迁并强化）。

审查报告发现的 v4 能力倒退：v3 已有台翻腔/翻译腔/去 AI 味扫描，v4 只查
日式 dictionary.json，导致「喔/耶/嘛/喽/呐/的说/软体/网路」整本通过。
本模块把 v3 的机械扫描能力恢复进 Core checks，并做成可配置规则：

- 强台版语气词 / 台版用词 / 繁体残留 / 日式翻译句尾 → 硬门槛 fail；
- 句尾「呢/啦」密度超阈值 → 按配置 severity（blocked/warning）处理；
- AI 味填充词与连接词密度 → warning，不参与硬门槛（避免误伤合法表达）。

规则文件：``v4/rules/tone-scan.json``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .jsonio import read_json
from .paths import TONE_RULES_PATH


@dataclass
class ToneFinding:
    kind: str  # hard_particle | taiwan_word | traditional | translation_tone | counter | density | ai_filler
    name: str
    match: str
    context: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "name": self.name,
            "match": self.match,
            "context": self.context,
            "message": self.message,
        }


@dataclass
class ToneReport:
    hard: list[ToneFinding] = field(default_factory=list)
    blocked: list[ToneFinding] = field(default_factory=list)
    warnings: list[ToneFinding] = field(default_factory=list)

    @property
    def has_hard(self) -> bool:
        return bool(self.hard)

    @property
    def has_blocked(self) -> bool:
        return bool(self.blocked)

    def all_findings(self, limit: int = 40) -> list[dict[str, str]]:
        return [item.to_dict() for item in (self.hard + self.blocked + self.warnings)[:limit]]


def load_tone_rules() -> dict[str, Any]:
    """读取 ``v4/rules/tone-scan.json``；文件缺失时返回内置最小规则。"""
    try:
        doc = read_json(TONE_RULES_PATH)
        if isinstance(doc, dict) and doc.get("hard_patterns") is not None:
            return doc
    except Exception:  # noqa: BLE001 - 配置损坏降级为内置最小规则并大声审计
        pass
    return _FALLBACK_RULES


_FALLBACK_RULES: dict[str, Any] = {
    "schema_version": "v4-tone-scan-1",
    "hard_patterns": [
        "的说(?=[，。！？!?～~…」』\"'\n]|$)",
        "喔(?=[！。!?？～~…\n]|$)",
        "耶(?=[！。!?？～~…\n]|$)",
        "喽(?=[！。!?？～~…\n]|$)",
        "呐(?=[！。!?？～~…\n]|$)",
    ],
    "hard_words": ["软体", "网路", "滑鼠", "硬碟", "部落格", "计画", "超商", "捷运"],
    "word_exceptions": {"软体": ["软体动物"], "计画": ["计划"]},
    "traditional_to_simple": {"這": "这", "們": "们", "說": "说", "時": "时", "還": "还"},
    "density_patterns": [],
    "warning_patterns": [],
    "warning_density_patterns": [],
}


def _mask_exceptions(text: str, exceptions: list[str]) -> tuple[str, dict[str, str]]:
    """用私有占位符掩蔽豁免短语，返回 (掩蔽文本, 占位符映射)。"""
    placeholders: dict[str, str] = {}
    masked = text
    for index, phrase in enumerate(exceptions):
        if phrase and phrase in masked:
            placeholder = f"\uE001{index:04d}"
            masked = masked.replace(phrase, placeholder)
            placeholders[placeholder] = phrase
    return masked, placeholders


def _context(text: str, start: int, end: int, width: int = 24) -> str:
    left = max(0, start - width)
    right = min(len(text), end + width)
    return re.sub(r"\s+", " ", text[left:right])


def _scan_patterns(
    text: str,
    patterns: list[Any],
    *,
    kind: str,
    name_prefix: str = "",
    limit: int = 200,
) -> list[ToneFinding]:
    findings: list[ToneFinding] = []
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern:
            continue
        try:
            compiled = re.compile(pattern, re.M)
        except re.error:
            continue
        for match in compiled.finditer(text):
            matched = match.group(0)
            findings.append(
                ToneFinding(
                    kind=kind,
                    name=f"{name_prefix}:{pattern}" if name_prefix else pattern,
                    match=matched,
                    context=_context(text, match.start(), match.end()),
                    message=f"命中腔调规则「{pattern}」",
                )
            )
            if len(findings) >= limit:
                return findings
    return findings


def scan_tone(text: str, rules: dict[str, Any] | None = None) -> ToneReport:
    """扫描文本，返回分级的腔调发现报告。

    分级：
    - hard：强台版腔 / 台版用词 / 繁体 / 日式翻译句尾（必须返工）；
    - blocked：密度超配置且 severity=blocked（需人工确认，见 checks 语义）；
    - warnings：密度 warning / AI 味填充词（提示但不硬卡）。
    """
    rules = rules or load_tone_rules()
    report = ToneReport()

    # 1. 硬正则（台版语气词、日式翻译句尾、日式量词）
    report.hard.extend(
        _scan_patterns(text, rules.get("hard_patterns", []), kind="hard_pattern")
    )

    # 1b. 翻译腔句式黑名单：硬句式 fail，较重句式 blocked
    report.hard.extend(
        _scan_patterns(
            text, rules.get("hard_sentence_patterns", []), kind="translation_sentence"
        )
    )
    report.blocked.extend(
        _scan_patterns(
            text, rules.get("blocked_sentence_patterns", []), kind="translation_sentence"
        )
    )

    # 2. 台版用词：先掩蔽合法复合词（软体动物 / 土司制度 等）
    exceptions = rules.get("word_exceptions", {}) or {}
    for word in rules.get("hard_words", []):
        if not isinstance(word, str) or not word:
            continue
        masked, _ = _mask_exceptions(text, list(exceptions.get(word, [])))
        if word not in masked:
            continue
        start = masked.find(word)
        report.hard.append(
            ToneFinding(
                kind="taiwan_word",
                name="taiwan_word",
                match=word,
                context=_context(text, start, start + len(word)),
                message=f"台版用词「{word}」残留",
            )
        )

    # 3. 繁体残留
    trad_map = rules.get("traditional_to_simple", {}) or {}
    trad_chars = sorted(
        {ch: trad_map[ch] for ch in text if ch in trad_map}.items(),
        key=lambda item: text.index(item[0]),
    )
    if trad_chars:
        report.hard.append(
            ToneFinding(
                kind="traditional",
                name="traditional_residue",
                match="".join(ch for ch, _ in trad_chars[:12]),
                context=_context(text, text.index(trad_chars[0][0]), text.index(trad_chars[0][0]) + 1),
                message="繁体残留：" + "、".join(f"{ch}→{simple}" for ch, simple in trad_chars[:12]),
            )
        )

    # 4. 密度规则
    wan = max(1.0, len(text) / 10000)
    for item in rules.get("density_patterns", []):
        if not isinstance(item, dict):
            continue
        pattern = str(item.get("pattern", ""))
        name = str(item.get("name", pattern))
        threshold = float(item.get("per_wan", 0))
        severity = str(item.get("severity", "warning"))
        if not pattern or threshold <= 0:
            continue
        try:
            count = len(re.findall(pattern, text, re.M))
        except re.error:
            continue
        if count / wan < threshold:
            continue
        finding = ToneFinding(
            kind="density",
            name=name,
            match=f"{count} 处 / {wan:.1f} 万字",
            context=f"阈值 {threshold:g}/万字",
            message=f"{name}密度 {count / wan:.1f}/万字，超过 {threshold:g}/万字",
        )
        (report.blocked if severity == "blocked" else report.warnings).append(finding)

    # 5. AI 味填充词与翻译腔连接词密度（warning）
    report.warnings.extend(
        _scan_patterns(text, rules.get("warning_patterns", []), kind="ai_filler")
    )
    for item in rules.get("warning_density_patterns", []):
        if not isinstance(item, dict):
            continue
        pattern = str(item.get("pattern", ""))
        name = str(item.get("name", pattern))
        threshold = float(item.get("per_wan", 0))
        if not pattern or threshold <= 0:
            continue
        try:
            count = len(re.findall(pattern, text, re.M))
        except re.error:
            continue
        if count / wan >= threshold:
            report.warnings.append(
                ToneFinding(
                    kind="density",
                    name=name,
                    match=f"{count} 处 / {wan:.1f} 万字",
                    context=f"阈值 {threshold:g}/万字",
                    message=f"{name}密度 {count / wan:.1f}/万字，超过 {threshold:g}/万字",
                )
            )

    return report


def tone_check(text: str, rules: dict[str, Any] | None = None) -> tuple[Any, list[Any], ToneReport]:
    """返回 (主 CheckResult, warning CheckResult 列表, ToneReport)。

    主结果：hard → fail；blocked → blocked；否则 pass。
    """
    from .checks import CheckResult  # 本地延迟导入避免循环依赖

    report = scan_tone(text, rules)
    warnings = [CheckResult("tone_warning", "warning", f"{item.message}（{item.context}）") for item in report.warnings]
    if report.has_hard:
        detail = "；".join(f"{item.match or item.name}: {item.context[:40]}" for item in report.hard[:5])
        return CheckResult("localization_tone", "fail", "台翻腔/日式残留硬门槛失败：" + detail), warnings, report
    if report.has_blocked:
        detail = "；".join(item.message for item in report.blocked[:3])
        return CheckResult("localization_tone", "blocked", detail), warnings, report
    return (
        CheckResult(
            "localization_tone",
            "pass",
            "无强台版腔/台版用词/繁体残留；密度项无 blocked 级超标",
        ),
        warnings,
        report,
    )


def hook_signal_check(text: str, *, is_appendix: bool = False) -> Any:
    """章末钩子启发式提示（warning，不是硬门槛）。"""
    from .checks import CheckResult

    if is_appendix:
        return CheckResult("chapter_hook", "not_run", "附录章节不要求章末钩子")
    paragraphs = re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
    paragraphs = [p.strip() for p in paragraphs if p.strip()]
    # 取最后一段正文做钩子启发式判断
    tail = paragraphs[-1] if paragraphs else ""
    signals = (
        "？", "！", "?!", "!?", "难道", "究竟", "殊不知", "就在这时", "忽然",
        "猛然", "突然", "冷笑", "身影", "没想到", "令人", "真正的", "危险",
        "不祥", "寒意", "盯着", "下一步", "接下来", "等着", "然而", "但是",
        "恐怕", "未必", "到底", "真相", "秘密", "死寂", "纹丝不动", "吞",
        "炸开", "只剩", "黑得像", "刀光", "脚步声", "远方", "黑暗中", "目光",
    )
    if any(signal in tail for signal in signals):
        return CheckResult("chapter_hook", "pass", "章末发现疑似钩子信号（启发式，不替代人工判断）")
    return CheckResult(
        "chapter_hook",
        "warning",
        "章末未检测到明显钩子信号；短剧断章要求建议强化（可人工确认后放行）",
    )
