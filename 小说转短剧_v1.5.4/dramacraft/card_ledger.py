# -*- coding: utf-8 -*-
"""素材卡池管理器（v1.2.5 灵感孵化·选题会地基）。

存储路径：<workdir>/inbox/cards.jsonl
每行一个合法 JSON 对象：{id, type, text, tags, status, used_by_eps}
- type 仅限 5 类：idea / character / world_rule / fact / line
- status 状态迁移：fresh -> in_use -> spent (或 shelved)
支持文本随笔自动打标入库、线程安全保护与单集卡片绑定溯源。
"""
import json
import os
import re
import threading
from typing import Any, Dict, List, Optional

VALID_CARD_TYPES = {"idea", "character", "world_rule", "fact", "line"}
VALID_CARD_STATUSES = {"fresh", "in_use", "spent", "shelved"}


class CardLedger:
    def __init__(self, path: str):
        self.path = os.path.abspath(str(path))
        self._lock = threading.Lock()
        self._ensure_file()

    def _ensure_file(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if not os.path.exists(self.path):
            with open(self.path, "w", encoding="utf-8"):
                pass

    def _read_all(self) -> List[Dict[str, Any]]:
        cards = []
        if not os.path.exists(self.path):
            return cards
        with open(self.path, "r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    cards.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return cards

    def _write_all(self, cards: List[Dict[str, Any]]) -> None:
        tmp_path = f"{self.path}.tmp.{os.getpid()}.{threading.get_ident()}"
        with open(tmp_path, "w", encoding="utf-8") as fp:
            for c in cards:
                fp.write(json.dumps(c, ensure_ascii=False) + "\n")
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_path, self.path)

    def add_card(self, text: str, card_type: str = "idea", tags: Optional[List[str]] = None) -> str:
        """单张卡片安全追加写入"""
        clean_text = str(text or "").strip()
        if not clean_text:
            raise ValueError("卡片内容不能为空")

        c_type = card_type.lower()
        if c_type not in VALID_CARD_TYPES:
            c_type = "idea"

        with self._lock:
            cards = self._read_all()
            new_idx = len(cards) + 1
            card_id = f"card_{new_idx:03d}"
            card_obj = {
                "id": card_id,
                "type": c_type,
                "text": clean_text,
                "tags": tags or [],
                "status": "fresh",
                "used_by_eps": []
            }
            cards.append(card_obj)
            self._write_all(cards)
            return card_id

    def get_card(self, card_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cards = self._read_all()
            for c in cards:
                if c.get("id") == card_id:
                    return dict(c)
        return None

    def get_fresh_cards(self, types: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """获取所有未被消耗 (status == fresh) 的卡片，可按类型过滤"""
        with self._lock:
            cards = self._read_all()
            filtered = [c for c in cards if c.get("status") == "fresh"]
            if types:
                allowed_types = set(types)
                filtered = [c for c in filtered if c.get("type") in allowed_types]
            return [dict(c) for c in filtered]

    def bind_to_ep(self, card_ids: List[str], ep: int) -> None:
        """将一组卡片绑定到指定集数，状态转为 in_use"""
        target_ids = set(card_ids)
        with self._lock:
            cards = self._read_all()
            changed = False
            for c in cards:
                if c.get("id") in target_ids:
                    used = c.setdefault("used_by_eps", [])
                    if ep not in used:
                        used.append(ep)
                    if c.get("status") == "fresh":
                        c["status"] = "in_use"
                    changed = True
            if changed:
                self._write_all(cards)

    def mark_spent(self, card_ids: List[str]) -> None:
        """标记卡片已被剧本生产消耗完成 (spent)"""
        target_ids = set(card_ids)
        with self._lock:
            cards = self._read_all()
            changed = False
            for c in cards:
                if c.get("id") in target_ids:
                    c["status"] = "spent"
                    changed = True
            if changed:
                self._write_all(cards)

    def import_text(self, text: str) -> List[str]:
        """将用户的随笔或大白话段落自动切分成结构化素材卡片"""
        paragraphs = [p.strip() for p in text.splitlines() if p.strip()]
        added_ids = []

        for p in paragraphs:
            # 规则1: 双引号或中文引号包裹 -> 金句台词 line
            if re.search(r"[“\"].*?[”\"]", p):
                cid = self.add_card(p, card_type="line", tags=["金句"])
            # 规则2: 包含角色、主角、反派、医生、神医、妻子等人物词 -> character
            elif any(k in p for k in ("主角", "反派", "配角", "人物", "性格", "外貌", "身份", "：", ":")):
                cid = self.add_card(p, card_type="character", tags=["人物设定"])
            # 规则3: 包含设定、世界、规则、限制 -> world_rule
            elif any(k in p for k in ("世界观", "规则", "设定", "限制", "背景")):
                cid = self.add_card(p, card_type="world_rule", tags=["世界规则"])
            else:
                # 默认当作点子情节
                cid = self.add_card(p, card_type="idea", tags=["情节灵感"])
            added_ids.append(cid)

        return added_ids
