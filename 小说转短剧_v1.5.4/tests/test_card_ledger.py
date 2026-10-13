# -*- coding: utf-8 -*-
import json
import os
import pytest

from dramacraft.card_ledger import CardLedger, VALID_CARD_TYPES, VALID_CARD_STATUSES


def test_card_ledger_basic_add_and_query(tmp_path):
    ledger_file = tmp_path / "inbox" / "cards.jsonl"
    ledger = CardLedger(str(ledger_file))

    cid1 = ledger.add_card("陆凡是一个隐退神医，平时当外卖员", card_type="character", tags=["主角", "神医"])
    cid2 = ledger.add_card("豪门寿宴上，富二代医生嘲弄外卖员", card_type="idea", tags=["冲突", "打脸"])
    cid3 = ledger.add_card("阎王要你三更死，我留你到五更天！", card_type="line", tags=["金句"])

    assert cid1 == "card_001"
    assert cid2 == "card_002"
    assert cid3 == "card_003"

    fresh_all = ledger.get_fresh_cards()
    assert len(fresh_all) == 3

    fresh_characters = ledger.get_fresh_cards(types=["character"])
    assert len(fresh_characters) == 1
    assert fresh_characters[0]["id"] == "card_001"


def test_card_ledger_status_transitions(tmp_path):
    ledger_file = tmp_path / "cards.jsonl"
    ledger = CardLedger(str(ledger_file))

    cid = ledger.add_card("主角身怀绝技", card_type="idea")
    card = ledger.get_card(cid)
    assert card["status"] == "fresh"

    # 绑定到第 1 集
    ledger.bind_to_ep([cid], ep=1)
    card = ledger.get_card(cid)
    assert card["status"] == "in_use"
    assert card["used_by_eps"] == [1]

    # 消耗卡片
    ledger.mark_spent([cid])
    card = ledger.get_card(cid)
    assert card["status"] == "spent"

    # 再次查询 fresh 卡片，该卡已被排除
    assert len(ledger.get_fresh_cards()) == 0


def test_card_ledger_import_text_auto_chunking(tmp_path):
    ledger_file = tmp_path / "cards.jsonl"
    ledger = CardLedger(str(ledger_file))

    raw_text = """
    主角陆凡：隐世神医，表面是个懦弱的外卖员。
    
    “你不过是个送外卖的，也配踏进顾家大门？”
    
    外卖员在寿宴上用银针救醒了首富顾老爷子，反派当场撕毁假药方。
    """
    card_ids = ledger.import_text(raw_text)
    assert len(card_ids) >= 3

    cards = [ledger.get_card(cid) for cid in card_ids]
    types = [c["type"] for c in cards]
    # 引号台词应被识别为 line
    assert "line" in types
    # 人物描述应被识别为 character 或 idea
    assert any(c["type"] in ("character", "idea") for c in cards)
