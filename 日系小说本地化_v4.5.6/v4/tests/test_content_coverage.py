# -*- coding: utf-8 -*-
"""Task 9.9.2/9.9.3：逐句内容覆盖清单与判定（离线纯函数部分）。

背景（真实批次实测）：译者会在改写阶段漏掉源句——最典型是段末景物/收束句。
ch-0006 的 `地面には、黒い染みだけが残った` 连续 8 个版本全部漏译，事件门正确拦截；
而当时把它降级为 warning 的放宽（已回退）让 5 个内容缺失的章节被误批准。

本模块按工单 §9.9.2 建立覆盖清单：**每个源句都是必需项**（含未进事件图的普通句），
低置信句只是「人工复核优先级高」，绝不因此免除；缺失/部分/不确定一律阻断。
"""
from __future__ import annotations

from v4.core.content_coverage import CoverageFinding, aggregate_coverage, build_coverage_requirements, split_source_sentences

# ch-0006 真实源稿（title 为第 1 段；末段即被漏译 8 个版本的句子）
CH0006_SOURCE = """第6章 魔物の夜

魔物は、夜の九時に現れた。

まず、犬のような鳴き声が聞こえた。次に、生臭い風が吹いた。

森田が剣を抜き、藤原武が側面に回り込む。

凛は呪文を唱え、蒼は矢を放った。

どん、という音とともに、魔物の体が崩れていく。

「今だ！」と森田が叫んだ。

地面には、黒い染みだけが残った。
"""


def _requirements():
    return build_coverage_requirements(
        chapter_id="ch-0006", source_text=CH0006_SOURCE, events_graph=None, input_language="ja"
    )


def _findings(requirements, status: str) -> list[CoverageFinding]:
    return [
        CoverageFinding(requirement_id=r.id, status=status, evidence="测试构造")
        for r in requirements
    ]


def test_every_source_sentence_becomes_mandatory_requirement() -> None:
    """每个源句都必须有覆盖项，且默认必需（含未进事件图的普通句）。"""
    reqs = _requirements()
    assert reqs, "未生成任何覆盖项"
    assert all(r.mandatory for r in reqs), "存在非必需项——普通句也不能漏检"
    ids = [r.id for r in reqs]
    assert len(ids) == len(set(ids)), f"覆盖项 ID 重复：{ids}"


def test_last_scenery_sentence_gets_its_own_requirement() -> None:
    """ch-0006 末句「地面には、黒い染みだけが残った」必须独立成项（工单 §9.9.2）。"""
    reqs = _requirements()
    last = [r for r in reqs if "染み" in r.source_text]
    assert len(last) == 1, f"末句未独立成项：{[(r.id, r.source_text) for r in reqs[-3:]]}"
    assert last[0].id == "s-0008-001", f"覆盖项 ID 应为 s-0008-001，实际 {last[0].id}"
    assert last[0].mandatory is True


def test_multi_sentence_paragraph_is_split_per_sentence() -> None:
    """一个段落含多句时必须逐句切分（不能整段只算一项）。"""
    text = "まず、犬のような鳴き声が聞こえた。次に、生臭い風が吹いた。"
    sentences = split_source_sentences(text)
    assert len(sentences) == 2, f"切分错误：{sentences}"
    assert sentences[0].endswith("。") and "鳴き声" in sentences[0]
    assert "生臭い風" in sentences[1]


def test_low_confidence_linked_sentence_is_mandatory_but_high_priority() -> None:
    """低置信事件关联句：人工复核优先级 high，但仍是必需项（不得免除）。"""
    graph = {
        "events": [
            {
                "event_id": "e-0008",
                "ordinal": 8,
                "participants": ["地面"],
                "action": "地面には、黒い染みだけが残った",
                "result": "无明确结果",
                "world_function": "无",
                "confidence": 0.3,
            }
        ]
    }
    reqs = build_coverage_requirements(
        chapter_id="ch-0006", source_text=CH0006_SOURCE, events_graph=graph, input_language="ja"
    )
    target = next(r for r in reqs if "染み" in r.source_text)
    assert target.manual_review_priority == "high", "低置信关联句应标记高优先级复核"
    assert target.mandatory is True, "高优先级不等于可免除"
    assert "e-0008" in target.linked_event_ids, "未关联到事件"


def test_absent_or_partial_blocks_delivery() -> None:
    """absent / partial 一律 fail（工单 §9.9.3 判定表）。"""
    reqs = _requirements()
    absent = _findings(reqs, "absent")
    result, blocking = aggregate_coverage(reqs, absent)
    assert result == "fail" and blocking, "absent 未阻断"
    for index, status in enumerate(("partial",)):
        mixed = _findings(reqs, "present")
        mixed[index] = CoverageFinding(requirement_id=mixed[index].requirement_id, status=status)
        result2, blocking2 = aggregate_coverage(reqs, mixed)
        assert result2 == "fail" and blocking2, f"{status} 未阻断"


def test_uncertain_is_blocked_not_passed() -> None:
    """uncertain 必须 blocked，绝不默认放行。"""
    reqs = _requirements()
    findings = _findings(reqs, "present")
    findings[3] = CoverageFinding(requirement_id=findings[3].requirement_id, status="uncertain")
    result, blocking = aggregate_coverage(reqs, findings)
    assert result == "blocked" and blocking, "uncertain 被放行"


def test_all_present_passes() -> None:
    """全部 present 才通过。"""
    reqs = _requirements()
    result, blocking = aggregate_coverage(reqs, _findings(reqs, "present"))
    assert result == "pass" and blocking == []


def test_missing_finding_is_treated_as_uncertain() -> None:
    """缺项（未判定的必需句）按 uncertain 处理——漏检不得静默通过。"""
    reqs = _requirements()
    findings = _findings(reqs, "present")[:-1]  # 少判一项
    result, blocking = aggregate_coverage(reqs, findings)
    assert result == "blocked", f"缺项被放行，结果={result}"
    assert any(item["requirement_id"] == reqs[-1].id for item in blocking)


def test_exempted_requires_matching_approved_decision_id() -> None:
    """exempted 仅在有精确指向该 requirement 的已批准决策时通过。"""
    reqs = list(_requirements())
    target_id = reqs[-1].id
    findings = _findings(reqs, "present")
    findings[-1] = CoverageFinding(requirement_id=target_id, status="exempted", evidence="人工豁免")

    # 无决策 → 必须失败
    result, blocking = aggregate_coverage(reqs, findings, approved_exemption_ids=set())
    assert result == "fail", "缺决策 ID 的 exempted 竟被放行"
    assert blocking

    # 决策 ID 指向别的 requirement → 仍失败
    result2, _ = aggregate_coverage(reqs, findings, approved_exemption_ids={"s-0001-001"})
    assert result2 == "fail", "决策未精确指向该覆盖项却放行"

    # 决策精确指向 → 通过
    result3, blocking3 = aggregate_coverage(reqs, findings, approved_exemption_ids={target_id})
    assert result3 == "pass" and blocking3 == [], f"合法豁免被拒：{blocking3}"


def test_ch0006_omitted_last_sentence_is_caught() -> None:
    """固化结构：末句「黒い染み」漏译必须被覆盖门捕获（工单 §五.12）。"""
    reqs = _requirements()
    findings = _findings(reqs, "present")
    missing = next(r for r in reqs if "染み" in r.source_text)
    findings = [f for f in findings if f.requirement_id != missing.id]
    findings.append(
        CoverageFinding(requirement_id=missing.id, status="absent", evidence="译文无对应语义")
    )
    result, blocking = aggregate_coverage(reqs, findings)
    assert result == "fail", "末句漏译未被捕获"
    assert missing.source_text.strip().startswith("地面には")
    assert any(item["requirement_id"] == missing.id for item in blocking)
