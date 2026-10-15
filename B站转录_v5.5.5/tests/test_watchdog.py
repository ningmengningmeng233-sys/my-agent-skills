"""看门狗的判定边界：什么情况下该续跑，什么情况下绝不该动。

由来（2026-09-18）：电脑 14:37 意外断电，批次进程消失；机器 14:39 就起来了，
但没人拉起批次，白等 27 分钟。看门狗要解决的正是这个，而它的两种误判代价都不小：
该跑没跑＝继续白等；不该跑却跑了＝重复转写、白撞一轮风控。所以这里把边界钉死。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import watchdog_resume as watchdog

NOW = datetime(2026, 9, 18, 15, 0, tzinfo=timezone.utc)


def payload(*, status="running", items=None, paused_at=None):
    base = {
        "batch_id": "B-test",
        "status": status,
        "items": items if items is not None else [{"status": "pending"}],
    }
    if paused_at is not None:
        base["paused_at"] = paused_at
    return base


class DecideTests(unittest.TestCase):
    def test_live_process_always_skips(self):
        """批次正在跑就不能再拉一个——重复转写会同时吃显存并撞风控。"""
        action, reason = watchdog.decide(payload(), process_alive=True, now=NOW)
        self.assertEqual(action, "skip")
        self.assertIn("存活", reason)

    def test_no_state_file_skips(self):
        """没有状态文件说明还没有批次，看门狗不许自作主张开一批新的。"""
        action, reason = watchdog.decide(None, process_alive=False, now=NOW)
        self.assertEqual(action, "skip")
        self.assertIn("不自动开新批", reason)

    def test_completed_batch_skips(self):
        action, _ = watchdog.decide(
            payload(status="completed", items=[{"status": "done"}]), process_alive=False)
        self.assertEqual(action, "skip")

    def test_running_status_without_unfinished_items_skips(self):
        """状态写着 running 但条目都结算完了，等于跑完，不该再拉起来。"""
        action, reason = watchdog.decide(
            payload(items=[{"status": "done"}, {"status": "failed"}]), process_alive=False)
        self.assertEqual(action, "skip")
        self.assertIn("已完成", reason)

    def test_paused_auth_skips_until_human_logs_in(self):
        """认证暂停要等人登录，拉起多少次都没用，只会白扫一遍收藏夹。"""
        action, reason = watchdog.decide(payload(status="paused_auth"), process_alive=False)
        self.assertEqual(action, "skip")
        self.assertIn("登录", reason)

    def test_paused_risk_waits_for_cooldown(self):
        action, reason = watchdog.decide(
            payload(status="paused_risk", paused_at=(NOW - timedelta(minutes=5)).isoformat()),
            process_alive=False, now=NOW)
        self.assertEqual(action, "skip")
        self.assertIn("冷却", reason)

    def test_paused_risk_resumes_after_cooldown(self):
        action, reason = watchdog.decide(
            payload(status="paused_risk", paused_at=(NOW - timedelta(minutes=45)).isoformat()),
            process_alive=False, now=NOW)
        self.assertEqual(action, "resume")
        self.assertIn("冷却已过", reason)

    def test_paused_risk_without_timestamp_resumes(self):
        """时间戳缺失或损坏时按最坏情况处理成「很久以前」，否则批次会被永久搁置。"""
        action, _ = watchdog.decide(payload(status="paused_risk", paused_at="不是时间"),
                                    process_alive=False, now=NOW)
        self.assertEqual(action, "resume")

    def test_interrupted_batch_resumes_and_reports_counts(self):
        action, reason = watchdog.decide(
            payload(items=[{"status": "running"}, {"status": "retry_wait"},
                           {"status": "pending"}, {"status": "done"}]),
            process_alive=False, now=NOW)
        self.assertEqual(action, "resume")
        self.assertIn("running=1", reason)
        self.assertIn("pending=1", reason)
        self.assertIn("retry_wait=1", reason)


class UnfinishedCountsTests(unittest.TestCase):
    def test_settled_statuses_are_not_counted(self):
        counts = watchdog.unfinished_counts(
            {"items": [{"status": "done"}, {"status": "failed"}]})
        self.assertEqual(counts, {})

    def test_missing_items_key_is_tolerated(self):
        self.assertEqual(watchdog.unfinished_counts({}), {})


class ParseTimeTests(unittest.TestCase):
    def test_naive_timestamp_is_assumed_utc(self):
        parsed = watchdog.parse_time("2026-09-18T04:24:06.862510")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_garbage_returns_none(self):
        self.assertIsNone(watchdog.parse_time("今天"))
        self.assertIsNone(watchdog.parse_time(None))


class LockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="watchdog_"))
        self.lock = self.tmp / "watchdog.lock"

    def test_second_acquire_is_refused_while_held(self):
        """登录触发与定时触发可能同时到点，不锁就可能拉出两个批次。"""
        self.assertTrue(watchdog.acquire_lock(self.lock, now=NOW))
        self.assertFalse(watchdog.acquire_lock(self.lock, now=NOW))
        self.assertEqual(json.loads(self.lock.read_text(encoding="utf-8"))["pid"],
                         __import__("os").getpid())

    def test_release_allows_reacquire(self):
        self.assertTrue(watchdog.acquire_lock(self.lock, now=NOW))
        watchdog.release_lock(self.lock)
        self.assertTrue(watchdog.acquire_lock(self.lock, now=NOW + timedelta(seconds=1)))

    def test_stale_lock_is_stolen(self):
        """上次异常退出留下的锁不能把看门狗永久锁死。"""
        self.assertTrue(watchdog.acquire_lock(self.lock, now=NOW))
        later = NOW + timedelta(seconds=watchdog.LOCK_STALE_SECONDS + 1)
        self.assertTrue(watchdog.acquire_lock(self.lock, now=later))

    def test_release_is_silent_when_missing(self):
        watchdog.release_lock(self.tmp / "nope.lock")  # 不应抛异常


class EnvironmentTests(unittest.TestCase):
    def test_batch_processes_returns_pids(self):
        """存活探测不能抛异常——它跑在无人值守的计划任务里。"""
        pids = watchdog.batch_processes()
        self.assertIsInstance(pids, list)
        for pid in pids:
            self.assertIsInstance(pid, int)

    def test_python_bin_points_at_an_existing_interpreter(self):
        self.assertTrue(Path(watchdog.python_bin()).is_file())

    def test_missing_state_loads_as_none(self):
        self.assertIsNone(watchdog.load_payload(self.tmp_path_missing()))

    def test_corrupt_state_loads_as_none(self):
        broken = Path(tempfile.mkdtemp(prefix="watchdog_bad_")) / "batch_state.json"
        broken.write_text("{ 这不是 JSON", encoding="utf-8")
        self.assertIsNone(watchdog.load_payload(broken))

    def test_good_state_round_trips(self):
        good = Path(tempfile.mkdtemp(prefix="watchdog_ok_")) / "batch_state.json"
        good.write_text(json.dumps(payload()), encoding="utf-8")
        self.assertEqual(watchdog.load_payload(good)["batch_id"], "B-test")

    def tmp_path_missing(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="watchdog_none_")) / "batch_state.json"


class LaunchTests(unittest.TestCase):
    def test_launch_starts_a_detached_process_and_captures_output(self):
        """续跑路径必须真能把进程拉起来，且输出落到日志文件里。

        不能拿正在跑的批次去试这一路（会撞出重复转写），所以用替身脚本验机制：
        起进程、脱离、stdout 重定向、返回 PID。
        """
        tmp = Path(tempfile.mkdtemp(prefix="watchdog_launch_"))
        fake = tmp / "fake_batch.py"
        fake.write_text("print('FAKE-BATCH-STARTED')\n", encoding="utf-8")
        logs = tmp / "logs"
        original = watchdog.BATCH_SCRIPT
        watchdog.BATCH_SCRIPT = str(fake)
        try:
            pid, log_path = watchdog.launch_batch(sys.executable, logs_dir=logs)
        finally:
            watchdog.BATCH_SCRIPT = original

        self.assertGreater(pid, 0)
        self.assertEqual(log_path.parent, logs)
        for _ in range(60):
            if log_path.is_file() and "FAKE-BATCH-STARTED" in log_path.read_text(
                    encoding="utf-8", errors="replace"):
                break
            time.sleep(0.1)
        self.assertIn("FAKE-BATCH-STARTED",
                      log_path.read_text(encoding="utf-8", errors="replace"))

    def test_launch_log_name_follows_the_batch_run_convention(self):
        tmp = Path(tempfile.mkdtemp(prefix="watchdog_name_"))
        fake = tmp / "fake_batch.py"
        fake.write_text("pass\n", encoding="utf-8")
        original = watchdog.BATCH_SCRIPT
        watchdog.BATCH_SCRIPT = str(fake)
        try:
            _, log_path = watchdog.launch_batch(sys.executable, logs_dir=tmp / "logs")
        finally:
            watchdog.BATCH_SCRIPT = original
        self.assertTrue(log_path.name.startswith("batch_run_"))
        self.assertTrue(log_path.name.endswith(".log"))


class ReviveBudgetTests(unittest.TestCase):
    """续跑频率上限：崩溃循环必须停下来，不能一直空烧 B站 API。

    每次续跑都会让批次重扫一遍收藏夹。进程若是反复死（2026-09-18 一天内已见两回
    静默死亡），看门狗每轮都拉一次就成了「死→重扫→再死」的空转：既没进展，
    又在攒风控。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="watchdog_revive_"))
        self.log = self.tmp / "watchdog.log"

    def write_entry(self, minutes_ago: float, text: str = "已续跑：回收 1 个僵尸，新进程 PID 1"):
        when = datetime.now().astimezone() - timedelta(minutes=minutes_ago)
        with open(self.log, "a", encoding="utf-8") as sink:
            sink.write(f"[{when:%Y-%m-%d %H:%M:%S}] {text}\n")

    def test_counts_only_revives_inside_the_window(self):
        self.write_entry(5)
        self.write_entry(30)
        self.write_entry(120)  # 窗外
        self.assertEqual(watchdog.recent_revives(self.log, window_seconds=3600), 2)

    def test_non_revive_lines_are_ignored(self):
        self.write_entry(5, text="判定 skip：批次进程存活")
        self.assertEqual(watchdog.recent_revives(self.log), 0)

    def test_missing_log_counts_zero(self):
        self.assertEqual(watchdog.recent_revives(self.tmp / "nope.log"), 0)

    def test_malformed_lines_do_not_crash(self):
        self.log.write_text(
            "已续跑：没有时间戳\n[不是时间] 已续跑\n[2026-13-45 99:99:99] 已续跑\n",
            encoding="utf-8")
        self.assertEqual(watchdog.recent_revives(self.log), 0)

    def test_log_stamp_is_read_as_local_time(self):
        """日志写的是本地时间；当成 UTC 会偏一个时区，上限就永远不生效。"""
        now_local = datetime.now().astimezone().replace(microsecond=0)
        parsed = watchdog.parse_log_stamp(f"{now_local:%Y-%m-%d %H:%M:%S}")
        self.assertIsNotNone(parsed)
        self.assertLess(abs((parsed - now_local).total_seconds()), 1)

    def test_rate_limit_skips_resume(self):
        action, reason = watchdog.decide(payload(), process_alive=False,
                                         revives_recently=watchdog.MAX_REVIVES_PER_WINDOW,
                                         now=NOW)
        self.assertEqual(action, "skip")
        self.assertIn("崩溃循环", reason)

    def test_under_the_limit_still_resumes(self):
        action, _ = watchdog.decide(payload(), process_alive=False,
                                    revives_recently=watchdog.MAX_REVIVES_PER_WINDOW - 1,
                                    now=NOW)
        self.assertEqual(action, "resume")

    def test_budget_also_applies_to_risk_resume(self):
        action, reason = watchdog.decide(
            payload(status="paused_risk", paused_at=(NOW - timedelta(hours=2)).isoformat()),
            process_alive=False, revives_recently=watchdog.MAX_REVIVES_PER_WINDOW, now=NOW)
        self.assertEqual(action, "skip")
        self.assertIn("崩溃循环", reason)


class CrashTailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="watchdog_tail_"))

    def test_returns_the_newest_run_log_tail(self):
        old = self.tmp / "batch_run_20260918-100000.log"
        new = self.tmp / "batch_run_20260918-110000.log"
        old.write_text("旧的一次\n", encoding="utf-8")
        new.write_text("📌 [65/416] 开始\ndead line\n", encoding="utf-8")
        os.utime(old, (1, 1))
        os.utime(new, (2, 2))
        text = watchdog.crash_tail(self.tmp)
        self.assertIn("batch_run_20260918-110000.log", text)
        self.assertIn("dead line", text)

    def test_falls_back_when_no_logs(self):
        self.assertIn("没有可用", watchdog.crash_tail(self.tmp))


if __name__ == "__main__":
    unittest.main()
