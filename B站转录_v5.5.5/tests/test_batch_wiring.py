"""批量控制面的接线测试：僵尸回收必须作用在旧状态上，且不重跑已完成项。

这些是回归测试——最初的实现把 recover_stale 调用在「刚新建的状态」上，
而新批次里不可能有 running 条目，于是回收功能永远无事可做。
"""

from __future__ import annotations

import unittest

from scripts import batch_state, batch_transcribe as batch


class StateReuseTests(unittest.TestCase):
    def test_state_covers_pending_when_all_keys_present(self):
        import tempfile
        from pathlib import Path

        path = Path(tempfile.mkdtemp(prefix="reuse_")) / "batch_state.json"
        state = batch_state.BatchState.create(
            path=path, batch_id="B-1",
            items=[{"key": "BV1", "bvid": "BV1", "page": 1},
                   {"key": "BV2", "bvid": "BV2", "page": 1}])
        pending = [{"bvid": "BV1", "page": 1}]
        self.assertTrue(batch._state_covers_pending(state, pending))

    def test_state_does_not_cover_a_new_key(self):
        import tempfile
        from pathlib import Path

        path = Path(tempfile.mkdtemp(prefix="reuse_")) / "batch_state.json"
        state = batch_state.BatchState.create(
            path=path, batch_id="B-1",
            items=[{"key": "BV1", "bvid": "BV1", "page": 1}])
        self.assertFalse(batch._state_covers_pending(
            state, [{"bvid": "BV9", "page": 1}]))

    def test_multi_page_keys_use_the_same_convention(self):
        """状态里的 key 必须与 processed_key 一致，否则复用判定永远为假。"""
        import tempfile
        from pathlib import Path

        path = Path(tempfile.mkdtemp(prefix="reuse_")) / "batch_state.json"
        key = batch.processed_key("BV1", 3)
        state = batch_state.BatchState.create(
            path=path, batch_id="B-1",
            items=[{"key": key, "bvid": "BV1", "page": 3}])
        self.assertTrue(batch._state_covers_pending(
            state, [{"bvid": "BV1", "page": 3}]))
        self.assertEqual(key, "BV1_P3")


class RequeueFailedTests(unittest.TestCase):
    """复用旧状态时，上次失败的条目必须仍会在本次重试。

    最初的实现漏了这一步：`failed` 不可领取，状态复用又让它停在 failed 上，
    于是一次失败的任务在后续每次批量里都被静默跳过——而改造前是会重试的。
    """

    def setUp(self):
        import tempfile
        from pathlib import Path

        self.path = Path(tempfile.mkdtemp(prefix="requeue_")) / "batch_state.json"

    def make(self):
        return batch_state.BatchState.create(
            path=self.path, batch_id="B-1",
            items=[{"key": "BV1", "bvid": "BV1", "page": 1},
                   {"key": "BV2", "bvid": "BV2", "page": 1}])

    def test_previously_failed_item_becomes_claimable(self):
        state = self.make()
        state.mark("BV1", batch_state.FAILED, failure_code="transcribe_failed")
        pending = [{"bvid": "BV1", "page": 1}]
        count = batch._requeue_previously_failed(state, pending)
        self.assertEqual(count, 1)
        self.assertEqual(state.get("BV1")["status"], batch_state.RETRY_WAIT)
        self.assertIsNotNone(state.next_pending())

    def test_done_items_are_not_requeued(self):
        state = self.make()
        state.mark("BV1", batch_state.DONE)
        batch._requeue_previously_failed(state, [{"bvid": "BV1", "page": 1}])
        self.assertEqual(state.get("BV1")["status"], batch_state.DONE)

    def test_failed_items_not_in_pending_are_left_alone(self):
        """已被台账排除的旧失败条目不该被复活。"""
        state = self.make()
        state.mark("BV2", batch_state.FAILED, failure_code="transcribe_failed")
        batch._requeue_previously_failed(state, [{"bvid": "BV1", "page": 1}])
        self.assertEqual(state.get("BV2")["status"], batch_state.FAILED)

    def test_auth_required_items_are_untouched(self):
        """认证暂停的条目要等 resume-batch，不能被常规重跑顺手复活。"""
        state = self.make()
        state.claim("BV1")
        state.pause_for_auth("BV1")
        batch._requeue_previously_failed(state, [{"bvid": "BV1", "page": 1}])
        self.assertEqual(state.get("BV1")["status"], batch_state.AUTH_REQUIRED)


class StaleRecoveryOrderingTests(unittest.TestCase):
    def test_recovery_on_loaded_state_finds_interrupted_items(self):
        """模拟上次运行被杀：旧状态里有 running 条目，本次启动应能回收。"""
        import tempfile
        from pathlib import Path

        path = Path(tempfile.mkdtemp(prefix="recover_")) / "batch_state.json"
        state = batch_state.BatchState.create(
            path=path, batch_id="B-old",
            items=[{"key": "BV1", "bvid": "BV1", "page": 1}])
        state.claim("BV1")
        state.set_heartbeat("BV1", "2020-01-01T00:00:00+00:00")

        reloaded = batch_state.BatchState.load(path)
        recovered = reloaded.recover_stale(seconds=batch.STALE_HEARTBEAT_SECONDS)
        self.assertEqual(recovered, 1)
        self.assertEqual(reloaded.get("BV1")["status"], batch_state.RETRY_WAIT)

    def test_stale_threshold_exceeds_the_subprocess_timeout(self):
        """阈值必须大于单条任务的 subprocess 上限，否则会误杀正在跑的任务。"""
        self.assertGreater(batch.STALE_HEARTBEAT_SECONDS, 7200)


if __name__ == "__main__":
    unittest.main()
