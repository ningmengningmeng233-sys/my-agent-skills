# -*- coding: utf-8 -*-
"""UMH M2 回归：主密钥原子创建（UMH-03）/ 队列原子持久化与显错（UMH-04）/ 悬空 scripts（UMH-01）。

受控 RED：以下断言在修复前必须失败，修复后通过。全 mock/stub，不碰真实外部资源。

跑法（vendor/mdcg 目录下）：
  python -X utf8 -m md_cg.test_umh_m2_atomic
"""
from __future__ import annotations

import io
import json
import os
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path
import tempfile

from . import coldverify, crypto
from . import fsutil


def _scrub_env(test):
    """清掉环境变量 KEK（否则文件逻辑被短路，测不到）。"""
    old = os.environ.pop(crypto.MASTER_ENV, None)

    def _restore():
        if old is not None:
            os.environ[crypto.MASTER_ENV] = old

    test.addCleanup(_restore)


class MasterKeyAtomicTest(unittest.TestCase):
    def test_concurrent_first_creation_never_overwrites(self):
        """并发首启：所有调用者必须拿到同一把密钥，文件不被后写者覆盖。"""
        _scrub_env(self)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "master.key")
            n = 16
            barrier = threading.Barrier(n)
            results, errors = [], []

            def _worker():
                try:
                    barrier.wait(timeout=10)
                    results.append(crypto.load_master_key(master_file=path))
                except Exception as e:  # noqa: BLE001
                    errors.append(e)

            threads = [threading.Thread(target=_worker) for _ in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            self.assertEqual(errors, [])
            self.assertEqual(len(results), n)
            first = results[0]
            self.assertEqual(len(first), crypto.KEY_LEN)
            for key in results[1:]:
                self.assertEqual(key, first, "并发首启必须读回同一把主密钥，覆盖即历史不可解")
            with open(path, "r", encoding="utf-8") as f:
                on_disk = crypto._b64d(f.read().strip())
            self.assertEqual(on_disk, first)

    def test_existing_key_is_read_back_not_regenerated(self):
        _scrub_env(self)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "master.key")
            first = crypto.load_master_key(master_file=path)
            second = crypto.load_master_key(master_file=path)
            self.assertEqual(first, second)


class ColdVerifyPersistTest(unittest.TestCase):
    def test_persist_routes_through_atomic_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            q = coldverify.ColdVerifyQueue(root=tmp)
            calls = []
            real = fsutil.atomic_write

            def _spy(path, data, *a, **k):
                calls.append(path)
                return real(path, data, *a, **k)

            fsutil.atomic_write = _spy
            try:
                q.enqueue("n1", action="reverify")
            finally:
                fsutil.atomic_write = real
            self.assertEqual(calls, [os.path.join(tmp, coldverify.QUEUE_FILE)])

    def test_persist_failure_is_logged_and_raised_memory_intact(self):
        """落盘失败：记日志 + 向上抛，内存队列不受影响（严禁 except: pass）。"""
        with tempfile.TemporaryDirectory() as tmp:
            q = coldverify.ColdVerifyQueue(root=tmp)
            real = fsutil.atomic_write

            def _boom(*a, **k):
                raise OSError("disk full（合成故障）")

            fsutil.atomic_write = _boom
            try:
                buf = io.StringIO()
                with redirect_stderr(buf):
                    with self.assertRaises(OSError):
                        q.enqueue("n1", action="reverify")
            finally:
                fsutil.atomic_write = real
            self.assertIn("持久化失败", buf.getvalue())
            st = q.status()
            self.assertEqual(st["queue_size"], 1, "落盘失败不得丢内存队列")
            self.assertEqual(st["stats"]["enqueued"], 1)


class PackageScriptsTest(unittest.TestCase):
    def test_no_dangling_script_targets(self):
        pkg = Path(__file__).resolve().parents[3]
        data = json.loads((pkg / "package.json").read_text(encoding="utf-8"))
        dangling = []
        for name, cmd in (data.get("scripts") or {}).items():
            for token in str(cmd).split():
                if token.endswith((".mjs", ".js", ".json")) or token == "tsconfig.json":
                    if not (pkg / token).exists():
                        dangling.append("%s: %s" % (name, token))
        self.assertEqual(dangling, [])


if __name__ == "__main__":
    unittest.main()
