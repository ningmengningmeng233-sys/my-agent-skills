"""Task 2 guard：时长政策唯一事实源 config/duration-policy.json 的加载与 Fail-Closed 校验。

覆盖五层：
1. 契约冻结值（真实配置，且配置文件逐字节等于工单冻结 JSON）；
2. 文件层 fail-closed：缺失 / JSON 损坏 / 顶层非对象 / 非法 UTF-8 / 空文件 /
   重复键 / 超深嵌套；
3. 字段层 fail-closed：整键缺失、类型、键集合、数值越界、statuses 白名单、
   schema_version；
4. 反硬编码：临时目录里写入数值与真实配置不同的合法配置，返回值必须原样来自文件，
   任何写在 Python 里的默认值都会让这些用例失败；
5. 断言一律用 `self.assertEqual` 等不可被 `python -O` 剥离的形式。
"""
from __future__ import annotations

import copy
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from versioning import (  # noqa: E402
    VersionValidationError,
    get_version,
    load_duration_policy,
    load_version_info,
)

REAL_POLICY_PATH = SKILL_ROOT / "config" / "duration-policy.json"

# 工单 Step 3 冻结的配置文件，逐字节钉住（缩进、键序、末尾换行都算契约）。
FROZEN_POLICY_BYTES = (
    "{\n"
    '  "schema_version": 1,\n'
    '  "clip_seconds": {"min": 4, "max": 10},\n'
    '  "speech_chars_per_second": {"min": 3, "max": 4},\n'
    '  "overage_review_ratio": 0.2,\n'
    '  "padding_policy": {"mode": "selective", "min_seconds": 0.3, "max_seconds": 0.5},\n'
    '  "statuses": ["PASS_EXACT", "PASS_WITH_TRIM", "REVIEW_REQUIRED", "FAIL"]\n'
    "}\n"
).encode("utf-8")

# 合法，但数值与真实配置完全不同的样本：任何硬编码在 Python 侧的默认值都会被它戳穿。
ALTERNATE_VALID_POLICY = {
    "schema_version": 1,
    "clip_seconds": {"min": 5, "max": 8},
    "speech_chars_per_second": {"min": 2, "max": 3.5},
    "overage_review_ratio": 0.35,
    "padding_policy": {"mode": "selective", "min_seconds": 0.1, "max_seconds": 0.9},
    "statuses": ["PASS_EXACT", "PASS_WITH_TRIM", "REVIEW_REQUIRED", "FAIL"],
}


def valid_policy() -> dict:
    return copy.deepcopy(ALTERNATE_VALID_POLICY)


class PolicyFixtureMixin:
    """临时 repo_root 夹具：只写 config/duration-policy.json，不依赖真实仓库内容。"""

    def setUp(self):
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.repo_root = Path(self._tmpdir.name)
        self.config_dir = self.repo_root / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.policy_path = self.config_dir / "duration-policy.json"

    def write_policy(self, policy) -> Path:
        self.policy_path.write_text(
            json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return self.policy_path

    def write_raw(self, raw) -> Path:
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        self.policy_path.write_bytes(raw)
        return self.policy_path

    def loaded_policy(self) -> dict:
        return load_duration_policy(repo_root=str(self.repo_root))

    def assert_rejected(self, policy) -> str:
        """写入配置并断言加载失败，返回错误信息。"""
        self.write_policy(policy)
        with self.assertRaises(VersionValidationError) as ctx:
            self.loaded_policy()
        message = str(ctx.exception)
        self.assertTrue(message.strip(), "拒绝时错误信息不能为空")
        return message


class DurationPolicyContractTest(unittest.TestCase):
    """工单冻结契约：真实配置必须给出这些数值。

    断言一律用 `self.assertEqual` 而不是裸 `assert`：裸断言在 `python -O` 下会被
    整体剥离，那样配置与冻结契约矛盾时套件仍然报 OK —— 契约测试必须不可优化。
    """

    def test_duration_policy_contract(self):
        policy = load_duration_policy()
        self.assertEqual(policy["clip_seconds"], {"min": 4, "max": 10})
        self.assertEqual(policy["speech_chars_per_second"], {"min": 3, "max": 4})
        self.assertEqual(policy["overage_review_ratio"], 0.20)
        self.assertEqual(policy["padding_policy"]["mode"], "selective")
        self.assertEqual(
            policy["statuses"],
            ["PASS_EXACT", "PASS_WITH_TRIM", "REVIEW_REQUIRED", "FAIL"],
        )


class DurationPolicyFileLevelTest(PolicyFixtureMixin, unittest.TestCase):
    """文件层 fail-closed。"""

    def test_missing_config_file_is_rejected(self):
        self.assertFalse(self.policy_path.exists())
        with self.assertRaises(VersionValidationError):
            self.loaded_policy()

    def test_corrupted_json_syntax_is_rejected(self):
        self.write_raw('{"schema_version": 1, "clip_seconds": {"min": 4, "max": 10}')
        with self.assertRaises(VersionValidationError):
            self.loaded_policy()

    def test_empty_file_is_rejected(self):
        self.write_raw("")
        with self.assertRaises(VersionValidationError):
            self.loaded_policy()

    def test_top_level_list_is_rejected(self):
        self.write_policy([])
        with self.assertRaises(VersionValidationError):
            self.loaded_policy()

    def test_top_level_scalar_is_rejected(self):
        for bad in ("duration-policy", 7, 1.5, True, None):
            with self.subTest(bad=bad):
                self.write_policy(bad)
                with self.assertRaises(VersionValidationError):
                    self.loaded_policy()

    def test_invalid_utf8_bytes_are_rejected_without_unicode_decode_error(self):
        self.write_raw(b"\xff\xfe\x00")
        try:
            self.loaded_policy()
        except VersionValidationError as exc:
            self.assertIn("UTF-8", str(exc))
        except UnicodeDecodeError as exc:  # pragma: no cover - 失败路径
            self.fail(f"load_duration_policy 泄漏了 UnicodeDecodeError: {exc}")
        else:  # pragma: no cover - 失败路径
            self.fail("非法 UTF-8 配置必须被拒绝")


class DurationPolicySchemaVersionTest(PolicyFixtureMixin, unittest.TestCase):
    def test_schema_version_missing_is_rejected(self):
        policy = valid_policy()
        del policy["schema_version"]
        self.assert_rejected(policy)

    def test_schema_version_string_is_rejected(self):
        policy = valid_policy()
        policy["schema_version"] = "1"
        self.assert_rejected(policy)

    def test_schema_version_bool_is_rejected(self):
        policy = valid_policy()
        policy["schema_version"] = True
        self.assert_rejected(policy)

    def test_schema_version_unsupported_is_rejected(self):
        policy = valid_policy()
        policy["schema_version"] = 2
        self.assert_rejected(policy)

    def test_schema_version_float_is_rejected(self):
        policy = valid_policy()
        policy["schema_version"] = 1.0
        self.assert_rejected(policy)


class DurationPolicyMissingKeyTest(PolicyFixtureMixin, unittest.TestCase):
    """整键缺失必须被拒绝（`schema_version` 的同类覆盖在 SchemaVersionTest）。

    这些用例也是"往缺字段注入 Python 默认值"这类回归的唯一杀手：把 `= None` 之类
    的坏值写进文件时，`setdefault(k, default)` 式的变异无感；只有把键整个删掉，
    默认值注入才会被暴露。
    """

    def test_clip_seconds_missing_key_is_rejected(self):
        policy = valid_policy()
        del policy["clip_seconds"]
        self.assert_rejected(policy)

    def test_speech_chars_per_second_missing_key_is_rejected(self):
        policy = valid_policy()
        del policy["speech_chars_per_second"]
        self.assert_rejected(policy)

    def test_overage_review_ratio_missing_key_is_rejected(self):
        policy = valid_policy()
        del policy["overage_review_ratio"]
        self.assert_rejected(policy)

    def test_padding_policy_missing_key_is_rejected(self):
        policy = valid_policy()
        del policy["padding_policy"]
        self.assert_rejected(policy)

    def test_statuses_missing_key_is_rejected(self):
        policy = valid_policy()
        del policy["statuses"]
        self.assert_rejected(policy)


class DurationPolicyDuplicateKeyTest(PolicyFixtureMixin, unittest.TestCase):
    """重复键必须报错，而不是静默地"后写的胜出"。"""

    def _raw_with_duplicate_ratio(self) -> str:
        return (
            "{\n"
            '  "schema_version": 1,\n'
            '  "clip_seconds": {"min": 5, "max": 8},\n'
            '  "speech_chars_per_second": {"min": 2, "max": 3.5},\n'
            '  "overage_review_ratio": 0.2,\n'
            '  "overage_review_ratio": 0.9,\n'
            '  "padding_policy": {"mode": "selective", "min_seconds": 0.1, "max_seconds": 0.9},\n'
            '  "statuses": ["PASS_EXACT", "PASS_WITH_TRIM", "REVIEW_REQUIRED", "FAIL"]\n'
            "}\n"
        )

    def _raw_with_duplicate_inner_min(self) -> str:
        return (
            "{\n"
            '  "schema_version": 1,\n'
            '  "clip_seconds": {"min": 5, "min": 6, "max": 8},\n'
            '  "speech_chars_per_second": {"min": 2, "max": 3.5},\n'
            '  "overage_review_ratio": 0.35,\n'
            '  "padding_policy": {"mode": "selective", "min_seconds": 0.1, "max_seconds": 0.9},\n'
            '  "statuses": ["PASS_EXACT", "PASS_WITH_TRIM", "REVIEW_REQUIRED", "FAIL"]\n'
            "}\n"
        )

    def test_duplicate_top_level_key_is_rejected(self):
        self.write_raw(self._raw_with_duplicate_ratio())
        with self.assertRaises(VersionValidationError) as ctx:
            self.loaded_policy()
        self.assertIn("overage_review_ratio", str(ctx.exception))

    def test_duplicate_inner_key_is_rejected(self):
        self.write_raw(self._raw_with_duplicate_inner_min())
        with self.assertRaises(VersionValidationError):
            self.loaded_policy()

    def test_nested_objects_with_distinct_keys_still_parse(self):
        self.write_policy(valid_policy())
        self.assertEqual(self.loaded_policy(), valid_policy())


class DurationPolicyDeepNestingTest(PolicyFixtureMixin, unittest.TestCase):
    """超深嵌套不得把解释器级 RecursionError 泄漏到调用方。"""

    def test_deeply_nested_json_is_rejected_without_recursion_error(self):
        self.write_raw('{"extra_deep": ' + "[" * 20_000 + "]" * 20_000 + "}")
        try:
            self.loaded_policy()
        except VersionValidationError as exc:
            self.assertIn("nest", str(exc))
        except RecursionError as exc:  # pragma: no cover - 失败路径
            self.fail(f"load_duration_policy 泄漏了 RecursionError: {exc}")
        else:  # pragma: no cover - 失败路径
            self.fail("超深嵌套的 JSON 必须被拒绝")


class DurationPolicyFieldTypeTest(PolicyFixtureMixin, unittest.TestCase):
    def test_clip_seconds_not_object_is_rejected(self):
        for bad in ([4, 10], "4-10", 4, None, True):
            with self.subTest(bad=bad):
                policy = valid_policy()
                policy["clip_seconds"] = bad
                self.assert_rejected(policy)

    def test_clip_seconds_missing_key_is_rejected(self):
        for missing in ("min", "max"):
            with self.subTest(missing=missing):
                policy = valid_policy()
                del policy["clip_seconds"][missing]
                self.assert_rejected(policy)

    def test_clip_seconds_extra_key_is_rejected(self):
        policy = valid_policy()
        policy["clip_seconds"]["step"] = 1
        self.assert_rejected(policy)

    def test_clip_seconds_max_wrong_type_is_rejected(self):
        for bad in ("10", None, True, [10]):
            with self.subTest(bad=bad):
                policy = valid_policy()
                policy["clip_seconds"]["max"] = bad
                self.assert_rejected(policy)

    def test_speech_chars_per_second_missing_is_rejected(self):
        policy = valid_policy()
        del policy["speech_chars_per_second"]
        self.assert_rejected(policy)

    def test_speech_chars_per_second_wrong_shape_is_rejected(self):
        for bad in ({"min": 3}, {"min": 3, "max": 4, "extra": 1}, [3, 4], "3-4", None):
            with self.subTest(bad=bad):
                policy = valid_policy()
                policy["speech_chars_per_second"] = bad
                self.assert_rejected(policy)

    def test_overage_review_ratio_wrong_type_is_rejected(self):
        for bad in ("0.35", None, True, {"value": 0.35}, [0.35]):
            with self.subTest(bad=bad):
                policy = valid_policy()
                policy["overage_review_ratio"] = bad
                self.assert_rejected(policy)

    def test_padding_policy_not_object_is_rejected(self):
        for bad in ("selective", 0.3, [0.3, 0.5], None, True):
            with self.subTest(bad=bad):
                policy = valid_policy()
                policy["padding_policy"] = bad
                self.assert_rejected(policy)

    def test_padding_policy_unknown_mode_is_rejected(self):
        for bad in ("always", "SELECTIVE", "", "  ", None, 3):
            with self.subTest(bad=bad):
                policy = valid_policy()
                policy["padding_policy"]["mode"] = bad
                self.assert_rejected(policy)

    def test_padding_policy_seconds_wrong_type_is_rejected(self):
        for key in ("min_seconds", "max_seconds"):
            for bad in ("0.3", None, True, [0.3]):
                with self.subTest(key=key, bad=bad):
                    policy = valid_policy()
                    policy["padding_policy"][key] = bad
                    self.assert_rejected(policy)

    def test_padding_policy_seconds_missing_is_rejected(self):
        for missing in ("min_seconds", "max_seconds"):
            with self.subTest(missing=missing):
                policy = valid_policy()
                del policy["padding_policy"][missing]
                self.assert_rejected(policy)

    def test_statuses_not_list_is_rejected(self):
        for bad in ("PASS_EXACT", {"PASS_EXACT": True}, None, 4):
            with self.subTest(bad=bad):
                policy = valid_policy()
                policy["statuses"] = bad
                self.assert_rejected(policy)

    def test_statuses_empty_list_is_rejected(self):
        policy = valid_policy()
        policy["statuses"] = []
        self.assert_rejected(policy)

    def test_statuses_empty_string_element_is_rejected(self):
        for bad in ("", "   "):
            with self.subTest(bad=repr(bad)):
                policy = valid_policy()
                policy["statuses"] = ["PASS_EXACT", "PASS_WITH_TRIM", "REVIEW_REQUIRED", "FAIL", bad]
                self.assert_rejected(policy)

    def test_statuses_non_string_element_is_rejected(self):
        for bad in (None, 1, ["FAIL"]):
            with self.subTest(bad=bad):
                policy = valid_policy()
                policy["statuses"] = ["PASS_EXACT", "PASS_WITH_TRIM", "REVIEW_REQUIRED", "FAIL", bad]
                self.assert_rejected(policy)

    def test_statuses_missing_required_status_is_rejected(self):
        for dropped in ("PASS_EXACT", "PASS_WITH_TRIM", "REVIEW_REQUIRED", "FAIL"):
            with self.subTest(dropped=dropped):
                policy = valid_policy()
                policy["statuses"] = [s for s in policy["statuses"] if s != dropped]
                self.assert_rejected(policy)

    def test_statuses_duplicate_is_rejected(self):
        policy = valid_policy()
        policy["statuses"] = ["PASS_EXACT", "PASS_EXACT", "PASS_WITH_TRIM", "REVIEW_REQUIRED", "FAIL"]
        self.assert_rejected(policy)


class DurationPolicyNumericBoundsTest(PolicyFixtureMixin, unittest.TestCase):
    def test_clip_seconds_min_zero_is_rejected(self):
        policy = valid_policy()
        policy["clip_seconds"]["min"] = 0
        self.assert_rejected(policy)

    def test_clip_seconds_negative_min_is_rejected(self):
        policy = valid_policy()
        policy["clip_seconds"]["min"] = -1
        self.assert_rejected(policy)

    def test_clip_seconds_max_less_than_min_is_rejected(self):
        policy = valid_policy()
        policy["clip_seconds"] = {"min": 9, "max": 8}
        self.assert_rejected(policy)

    def test_speech_chars_per_second_min_greater_than_max_is_rejected(self):
        policy = valid_policy()
        policy["speech_chars_per_second"] = {"min": 4, "max": 3}
        self.assert_rejected(policy)

    def test_overage_review_ratio_zero_is_rejected(self):
        policy = valid_policy()
        policy["overage_review_ratio"] = 0
        self.assert_rejected(policy)

    def test_overage_review_ratio_negative_is_rejected(self):
        policy = valid_policy()
        policy["overage_review_ratio"] = -0.2
        self.assert_rejected(policy)

    def test_overage_review_ratio_one_point_five_is_rejected(self):
        policy = valid_policy()
        policy["overage_review_ratio"] = 1.5
        self.assert_rejected(policy)

    def test_overage_review_ratio_one_is_rejected(self):
        policy = valid_policy()
        policy["overage_review_ratio"] = 1
        self.assert_rejected(policy)

    def test_padding_policy_negative_min_is_rejected(self):
        policy = valid_policy()
        policy["padding_policy"]["min_seconds"] = -0.1
        self.assert_rejected(policy)

    def test_padding_policy_min_greater_than_max_is_rejected(self):
        policy = valid_policy()
        policy["padding_policy"]["min_seconds"] = 0.9
        policy["padding_policy"]["max_seconds"] = 0.5
        self.assert_rejected(policy)


class DurationPolicyNonFiniteLiteralTest(PolicyFixtureMixin, unittest.TestCase):
    """json.loads 默认接受 NaN/Infinity 字面量，它们必须被拒绝且不得进入返回值。"""

    def _raw_policy(self, clip_max: str, ratio: str) -> str:
        return (
            "{\n"
            '  "schema_version": 1,\n'
            f'  "clip_seconds": {{"min": 5, "max": {clip_max}}},\n'
            '  "speech_chars_per_second": {"min": 2, "max": 3.5},\n'
            f'  "overage_review_ratio": {ratio},\n'
            '  "padding_policy": {"mode": "selective", "min_seconds": 0.1, "max_seconds": 0.9},\n'
            '  "statuses": ["PASS_EXACT", "PASS_WITH_TRIM", "REVIEW_REQUIRED", "FAIL"]\n'
            "}\n"
        )

    def test_nan_literal_is_rejected(self):
        self.write_raw(self._raw_policy("NaN", "0.35"))
        with self.assertRaises(VersionValidationError):
            self.loaded_policy()

    def test_infinity_literal_is_rejected(self):
        self.write_raw(self._raw_policy("Infinity", "0.35"))
        with self.assertRaises(VersionValidationError):
            self.loaded_policy()

    def test_negative_infinity_literal_is_rejected(self):
        self.write_raw(self._raw_policy("5", "-Infinity"))
        with self.assertRaises(VersionValidationError):
            self.loaded_policy()

    def test_nan_literal_in_padding_policy_is_rejected(self):
        self.write_raw(
            '{"schema_version": 1, "clip_seconds": {"min": 5, "max": 8}, '
            '"speech_chars_per_second": {"min": 2, "max": 3.5}, "overage_review_ratio": 0.35, '
            '"padding_policy": {"mode": "selective", "min_seconds": NaN, "max_seconds": 0.9}, '
            '"statuses": ["PASS_EXACT", "PASS_WITH_TRIM", "REVIEW_REQUIRED", "FAIL"]}'
        )
        with self.assertRaises(VersionValidationError):
            self.loaded_policy()

    def test_valid_policy_returns_only_finite_numbers(self):
        self.write_policy(valid_policy())
        policy = self.loaded_policy()
        numbers = [
            policy["clip_seconds"]["min"],
            policy["clip_seconds"]["max"],
            policy["speech_chars_per_second"]["min"],
            policy["speech_chars_per_second"]["max"],
            policy["overage_review_ratio"],
            policy["padding_policy"]["min_seconds"],
            policy["padding_policy"]["max_seconds"],
        ]
        for value in numbers:
            self.assertIsInstance(value, (int, float))
            self.assertNotIsInstance(value, bool)
            self.assertTrue(math.isfinite(value), f"返回值中出现非有限数: {value!r}")


class DurationPolicySourceOfTruthTest(PolicyFixtureMixin, unittest.TestCase):
    """反“复制默认值”：返回值必须原样来自文件。"""

    def test_temporary_config_values_are_returned_verbatim(self):
        policy = valid_policy()
        self.write_policy(policy)
        loaded = self.loaded_policy()
        self.assertEqual(loaded, policy)
        self.assertEqual(loaded["clip_seconds"], {"min": 5, "max": 8})
        self.assertEqual(loaded["speech_chars_per_second"], {"min": 2, "max": 3.5})
        self.assertEqual(loaded["overage_review_ratio"], 0.35)
        self.assertNotEqual(loaded["clip_seconds"], {"min": 4, "max": 10})
        self.assertNotEqual(loaded["overage_review_ratio"], 0.2)

    def test_values_follow_file_not_python_defaults(self):
        other = valid_policy()
        other["clip_seconds"] = {"min": 6, "max": 9}
        other["speech_chars_per_second"] = {"min": 1, "max": 2}
        other["overage_review_ratio"] = 0.75
        other["padding_policy"] = {
            "mode": "selective",
            "min_seconds": 0.0,
            "max_seconds": 2.0,
        }
        other["statuses"] = ["FAIL", "REVIEW_REQUIRED", "PASS_WITH_TRIM", "PASS_EXACT"]
        self.write_policy(other)
        loaded = self.loaded_policy()
        self.assertEqual(loaded, other)
        self.assertEqual(
            loaded["statuses"], ["FAIL", "REVIEW_REQUIRED", "PASS_WITH_TRIM", "PASS_EXACT"]
        )

    def test_default_call_matches_disk_file_without_implicit_merge(self):
        self.assertTrue(REAL_POLICY_PATH.is_file(), f"缺少真实配置: {REAL_POLICY_PATH}")
        on_disk = json.loads(REAL_POLICY_PATH.read_text(encoding="utf-8"))
        loaded = load_duration_policy()
        self.assertIsInstance(loaded, dict)
        self.assertEqual(loaded, on_disk)
        self.assertEqual(sorted(loaded.keys()), sorted(on_disk.keys()))

    def test_real_config_file_is_valid_utf8_json_with_trailing_newline(self):
        raw = REAL_POLICY_PATH.read_bytes()
        raw.decode("utf-8")
        self.assertTrue(raw.endswith(b"\n"), "真实配置应以换行结尾")
        json.loads(raw.decode("utf-8"))

    def test_real_config_file_matches_frozen_json_up_to_line_endings(self):
        raw = REAL_POLICY_PATH.read_bytes()
        # 本仓库 core.autocrlf=true，git checkout 会把文本文件还原成 CRLF（同目录其它
        # config 就是 CRLF），所以行尾先归一化再比对 —— 否则测试会随检出平台假红。
        # 缩进、键序、数值与末尾换行仍然属于冻结契约，任何一处漂移都会失败。
        normalized = raw.replace(b"\r\n", b"\n")
        self.assertEqual(
            normalized,
            FROZEN_POLICY_BYTES,
            "config/duration-policy.json 必须与工单冻结的 JSON 一致（行尾除外）",
        )

    def test_existing_version_info_api_is_untouched(self):
        info = load_version_info()
        self.assertEqual(info["version"], get_version())
        self.assertTrue(info["release_name"])


if __name__ == "__main__":
    unittest.main()
