"""batch_state 的行为测试：批次状态、认证暂停恢复、僵尸任务回收与容错。

状态文件是批量的「控制面」——它决定 Cookie 失效时是继续空转还是停下、
进程被杀后能否续跑。这些测试锁定的是状态迁移规则。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import batch_state


class BatchStateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bstate_"))
        self.path = self.tmp / "batch_state.json"

    def make(self, keys=("BV1", "BV2", "BV3")):
        return batch_state.BatchState.create(
            path=self.path, batch_id="B-test",
            items=[{"key": k, "bvid": k, "page": 1} for k in keys])


class CreateTests(BatchStateCase):
    def test_create_initialises_every_item_pending(self):
        state = self.make()
        self.assertEqual(state.status, "running")
        self.assertEqual([i["status"] for i in state.items],
                         ["pending", "pending", "pending"])

    def test_create_is_persisted_to_disk(self):
        self.make()
        self.assertTrue(self.path.is_file())
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["batch_id"], "B-test")

    def test_reload_round_trips(self):
        self.make()
        again = batch_state.BatchState.load(self.path)
        self.assertEqual(len(again.items), 3)
        self.assertEqual(again.status, "running")


class ClaimAndCompleteTests(BatchStateCase):
    def test_claim_marks_running_and_stamps_heartbeat(self):
        state = self.make()
        state.claim("BV1")
        item = state.get("BV1")
        self.assertEqual(item["status"], "running")
        self.assertTrue(item["heartbeat_at"])

    def test_done_marks_completed(self):
        state = self.make()
        state.claim("BV1")
        state.mark("BV1", "done")
        self.assertEqual(state.get("BV1")["status"], "done")

    def test_next_pending_skips_finished_items(self):
        state = self.make()
        state.mark("BV1", "done")
        state.mark("BV2", "failed")
        self.assertEqual(state.next_pending()["key"], "BV3")

    def test_next_pending_returns_none_when_all_settled(self):
        state = self.make(keys=("BV1",))
        state.mark("BV1", "done")
        self.assertIsNone(state.next_pending())

    def test_retry_wait_items_are_claimable(self):
        """待重试的条目必须能再次被领取，否则重试形同虚设。"""
        state = self.make(keys=("BV1",))
        state.mark("BV1", "retry_wait")
        self.assertEqual(state.next_pending()["key"], "BV1")


class AuthPauseTests(BatchStateCase):
    def test_pause_for_auth_stops_the_whole_batch(self):
        state = self.make()
        state.claim("BV1")
        state.pause_for_auth("BV1")
        self.assertEqual(state.status, "paused_auth")
        self.assertEqual(state.get("BV1")["status"], "auth_required")

    def test_paused_batch_claims_nothing_further(self):
        """认证失效后不能继续领取——继续跑只会触发更多风控。"""
        state = self.make()
        state.claim("BV1")
        state.pause_for_auth("BV1")
        self.assertIsNone(state.next_pending())

    def test_resume_moves_auth_items_back_to_retry(self):
        state = self.make()
        state.claim("BV1")
        state.pause_for_auth("BV1")
        state.resume()
        self.assertEqual(state.status, "running")
        self.assertEqual(state.get("BV1")["status"], "retry_wait")

    def test_resume_does_not_rerun_completed_items(self):
        state = self.make()
        state.mark("BV1", "done")
        state.claim("BV2")
        state.pause_for_auth("BV2")
        state.resume()
        self.assertEqual(state.get("BV1")["status"], "done")
        self.assertEqual(state.next_pending()["key"], "BV2")

    def test_resume_is_rejected_when_not_paused(self):
        state = self.make()
        with self.assertRaises(ValueError):
            state.resume()


class StaleRecoveryTests(BatchStateCase):
    def test_stale_running_item_is_requeued(self):
        """进程被杀后留下的 in-flight 条目必须能回收，否则整批卡住。"""
        state = self.make()
        state.claim("BV1")
        state.set_heartbeat("BV1", "2020-01-01T00:00:00+00:00")
        recovered = state.recover_stale(seconds=60)
        self.assertEqual(recovered, 1)
        self.assertEqual(state.get("BV1")["status"], "retry_wait")

    def test_fresh_item_is_not_recovered(self):
        state = self.make()
        state.claim("BV1")
        self.assertEqual(state.recover_stale(seconds=3600), 0)
        self.assertEqual(state.get("BV1")["status"], "running")

    def test_done_items_are_never_recovered(self):
        state = self.make()
        state.mark("BV1", "done")
        state.set_heartbeat("BV1", "2020-01-01T00:00:00+00:00")
        self.assertEqual(state.recover_stale(seconds=60), 0)
        self.assertEqual(state.get("BV1")["status"], "done")


class FaultToleranceTests(BatchStateCase):
    def test_corrupt_file_is_quarantined_not_raised(self):
        """状态文件损坏不该让整批死掉——重命名为 .corrupt 后按新批次继续。"""
        self.path.write_text("{ 这不是 JSON", encoding="utf-8")
        state = batch_state.BatchState.load(self.path)
        self.assertIsNone(state)
        self.assertTrue(self.path.with_suffix(".json.corrupt").is_file())

    def test_missing_file_loads_as_none(self):
        self.assertIsNone(batch_state.BatchState.load(self.tmp / "nope.json"))

    def test_summary_counts_by_status(self):
        state = self.make()
        state.mark("BV1", "done")
        state.claim("BV2")
        state.pause_for_auth("BV2")
        summary = state.summary()
        self.assertEqual(summary["done"], 1)
        self.assertEqual(summary["auth_required"], 1)
        self.assertEqual(summary["pending"], 1)


if __name__ == "__main__":
    unittest.main()
