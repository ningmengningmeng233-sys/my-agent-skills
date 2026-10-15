"""E 级环境豁免的机器契约：豁免必须点名登记能力，且 SKIP 永远不算通过。

回归背景（S2 疑点 3）：环境相关断言原先各测试文件自带 `shutil.which` +
`find_spec` 探针，写错探测条件就等于永久绿；能力名拼错时 `skipif` 会静默
放过整条用例。本文件把两条纪律钉死：
1. 未登记能力名直接抛错——假豁免不可构造；
2. 缺能力时用例只进 skipped，永不进 passed——SKIP ≠ 绿。
"""

import unittest
from unittest import mock

from tests import conftest
from tests.conftest import capability_ready, requires_env


class CapabilityRegistryTests(unittest.TestCase):

    def test_unregistered_capability_raises_instead_of_silently_skipping(self):
        with self.assertRaises(KeyError) as ctx:
            capability_ready("网络很稳")
        self.assertIn("未登记的能力", str(ctx.exception))

    def test_error_message_lists_the_registrations_you_can_use(self):
        with self.assertRaises(KeyError) as ctx:
            capability_ready("gpu")
        for name in conftest._CAPABILITIES:
            self.assertIn(name, str(ctx.exception))

    def test_registry_names_are_stable_so_exemptions_cannot_drift(self):
        self.assertEqual({"ocr-engine", "installed-doctor"},
                         set(conftest._CAPABILITIES))

    def test_a_broken_probe_counts_as_not_ready_not_as_error(self):
        with mock.patch.dict(conftest._CAPABILITIES,
                             {"ocr-engine": lambda: 1 / 0}):
            self.assertFalse(capability_ready("ocr-engine"))


class RequiresEnvTests(unittest.TestCase):

    def test_skip_reason_carries_the_env_tier_marker(self):
        with mock.patch.dict(conftest._CAPABILITIES, {"gpu": lambda: False}):
            decorated = requires_env("gpu", "缺 CUDA，端到端跑不了")

            def case():
                raise AssertionError("缺能力时不该被执行")

        skipped = decorated(case)
        self.assertTrue(getattr(skipped, "__unittest_skip__", False))
        self.assertEqual("SKIP(E): 缺 CUDA，端到端跑不了",
                         skipped.__unittest_skip_why__)

    def test_ready_capability_leaves_the_case_untouched(self):
        with mock.patch.dict(conftest._CAPABILITIES, {"gpu": lambda: True}):
            decorated = requires_env("gpu", "缺 CUDA")

            def case():
                pass

            self.assertIs(case, decorated(case))

    def test_a_skipped_case_adds_nothing_to_the_pass_count(self):
        with mock.patch.dict(conftest._CAPABILITIES, {"gpu": lambda: False}):

            class Case(unittest.TestCase):
                @requires_env("gpu", "缺 CUDA")
                def test_green_on_gpu(self):
                    pass

            result = unittest.TestResult()
            unittest.TestLoader().loadTestsFromTestCase(Case).run(result)

        passed = (result.testsRun - len(result.skipped)
                  - len(result.failures) - len(result.errors))
        self.assertTrue(result.wasSuccessful())
        # 柠檬纪律：wasSuccessful 与 testsRun 都会把豁免算成"跑过了"，
        # 唯一诚实的计数是真跑真过的条数——它必须是 0，且豁免留在 skipped 里显形。
        self.assertEqual(0, passed)
        self.assertEqual(1, len(result.skipped))
        self.assertIn("SKIP(E): 缺 CUDA", result.skipped[0][1])


if __name__ == "__main__":
    unittest.main()
