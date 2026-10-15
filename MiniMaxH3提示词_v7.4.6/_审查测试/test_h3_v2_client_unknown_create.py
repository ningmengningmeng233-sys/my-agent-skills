"""TICKET-22 MM-04：create 成功未知不得盲目重发（防重复计费）。

create 异步任务遇传输超时（status == -1，含 RequestTimeout / Timeout）或
网关 502/503/504 时，服务端是否建单未知：必须返回 UNKNOWN_CREATE 并提示
经状态查询核实，禁止上层当成确定业务拒绝而盲目重发。

确定性状态（CREATED / AUTH_401 / QUOTA_402 / INVALID_422 / RATE_429 等）
保持不变。

运行：
    python "MiniMaxH3提示词_v7.4.5/_审查测试/test_h3_v2_client_unknown_create.py" -v
    python -m pytest MiniMaxH3提示词_v7.4.5/_审查测试/test_h3_v2_client_unknown_create.py -q
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import h3_v2_client  # noqa: E402

_BODY = {"model": "MiniMax-H3", "content": [{"type": "text", "text": "t"}]}


def _create_with(status, payload):
    with mock.patch.object(h3_v2_client, "_http_json",
                           return_value=(status, payload)):
        return h3_v2_client.create_task("https://api.minimax.io", "KEY",
                                        dict(_BODY), dry_run=False)


class UnknownCreateTest(unittest.TestCase):
    def test_timeout_is_unknown_create(self):
        res = _create_with(-1, {"transport_error": "RequestTimeout: timed out"})
        self.assertEqual(res["state"], "UNKNOWN_CREATE")
        self.assertIn("query", res.get("note", "").lower())

    def test_gateway_502_503_504_are_unknown_create(self):
        for status in (502, 503, 504):
            with self.subTest(status=status):
                res = _create_with(status, {"base_resp": {"status_code": status}})
                self.assertEqual(res["state"], "UNKNOWN_CREATE")
                self.assertIn("query", res.get("note", "").lower())

    def test_deterministic_states_unchanged(self):
        cases = [
            ((401, {}), "AUTH_401"),
            ((402, {}), "QUOTA_402"),
            ((422, {}), "INVALID_422"),
            ((429, {}), "RATE_429"),
        ]
        for (status, payload), expected in cases:
            with self.subTest(status=status):
                res = _create_with(status, payload)
                self.assertEqual(res["state"], expected)

    def test_created_unchanged(self):
        res = _create_with(200, {"task_id": "task-123"})
        self.assertEqual(res["state"], "CREATED")
        self.assertEqual(res["task_id"], "task-123")


if __name__ == "__main__":
    unittest.main()
