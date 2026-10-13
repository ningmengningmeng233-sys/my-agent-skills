"""TICKET-23 MM-05：路由回退能力矩阵与正式配置单一真源对齐。

外部能力配置读取失败时，router.load_model_capabilities() 回退到硬编码
矩阵。该回退矩阵曾把 H3-Max 的 ref2va 标记为支持，而正式
config/model-capabilities.json 中该组合为 REQUIRES_CONFIRMATION（公开 V2
未列出 H3-Max Ref2VA）：回退路径误放行会导致降级模式下放行本应拦截的
组合。本测试钉死：降级模式下 H3-Max + ref2va 必须被拦截（或要求确认，
即 validate 返回 False）。

运行：
    python "MiniMaxH3提示词_v7.4.5/_审查测试/test_router_fallback_capability.py" -v
    python -m pytest MiniMaxH3提示词_v7.4.5/_审查测试/test_router_fallback_capability.py -q
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import router  # noqa: E402


def _fallback_matrix():
    """模拟外部能力配置读取失败，取硬编码回退矩阵。"""
    with mock.patch("os.path.exists", return_value=False):
        return router.load_model_capabilities()


class RouterFallbackCapabilityTest(unittest.TestCase):
    def test_h3max_ref2va_blocked_in_fallback(self):
        fallback = _fallback_matrix()
        with mock.patch.object(router, "MODEL_CAPABILITIES", fallback):
            ok, err, _sugg = router.validate_model_mode_combination(
                "H3-Max", "ref2va")
        self.assertFalse(ok, "降级模式下 H3-Max + ref2va 必须被拦截或要求确认")
        self.assertIsNotNone(err)

    def test_fallback_still_allows_supported_combos(self):
        # 最小修正范围：仅收紧 H3-Max + ref2va，不误伤已确认组合。
        fallback = _fallback_matrix()
        with mock.patch.object(router, "MODEL_CAPABILITIES", fallback):
            for model, mode in (("H3", "ref2va"), ("H3-Max", "t2va"),
                                ("H3-Max", "fl2va")):
                with self.subTest(model=model, mode=mode):
                    ok, _err, _sugg = router.validate_model_mode_combination(
                        model, mode)
                    self.assertTrue(ok, f"{model} + {mode} 应继续放行")


if __name__ == "__main__":
    unittest.main()
