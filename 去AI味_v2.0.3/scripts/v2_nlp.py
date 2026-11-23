#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2_nlp.py - 可选中文 NLP 通道的薄适配（默认 none，组件与许可分别披露）。

合同（v2.0.0 beta P14；执行补充 P14 段）：

  1. **默认后端 none**：本模块导入时不碰任何第三方组件；查能力用
     ``importlib.util.find_spec``，只查可得性，**不执行组件代码**，更不取权重。
     上游实测（2026-10-06）：HanLP 发行依赖里含 ``hanlp-downloader``，LTP README
     自己写「默认 huggingface 下载，可能需要代理」——隐式下载是这两个组件的默认
     行为，所以「不自动下载」只能由本层实现：本层永远不替用户挑权重标识，
     ``component`` 不给就拒，选择了也不在 ``analyze`` 之前实例化。
  2. **不做静默换后端**：显式选了 hanlp/ltp 而组件缺失或入口形状不符，抛
     :class:`OptionalBackendUnavailable` 并照实说明，**不返回 none 的结果冒充**。
  3. **许可与能力分两把尺**：``available`` 只说装没装、能不能跑；许可三层
     （组件代码 / 预训练权重 / 服务条款）各自一个布尔，具体条款文本只写在
     ``THIRD_PARTY_NOTICES.md``（本文件不抄一份可能过期的断言）。许可未声明或
     权重含非商业条款的组件，选择时必须显式 ``license_ack=True``。
  4. **不进判级面**：本模块不与 ``v2_report`` / ``v2_edit`` / ``v2_contracts``
     发生任何导入关系；NLP 结果只做句子/词级切分与披露，不构成「这是 AI 写的」
     的证据，也不产生改写授权（总工单全局约束 8：相似度、词汇或频率不单独授权修改）。
  5. 真实组件推理在本机为 **NOT_RUN**：测试用假后端证明适配形状，桩跑绿不等于
     实际 NLP 推理通过（``real_component_executed`` 恒为 False，除非调用方真的
     装了组件并显式选它）。
"""
from __future__ import annotations

import importlib
import importlib.util
from typing import Callable

BACKENDS: tuple[str, ...] = ("none", "hanlp", "ltp")
DEFAULT_BACKEND = "none"
ANALYZE_SCHEMA = 1
#: 覆盖：none 只做句子级切分，词/词性一律如实报「没做」。
_COVERAGE_NONE = {"sentences": True, "tokens": False, "pos": False}
_COVERAGE_FULL = {"sentences": True, "tokens": True, "pos": True}
_SENTENCE_ENDERS = "。！？…"

# 上游入口形状取自各自 README 的最小用法（2026-10-06 逐字核对，见 THIRD_PARTY_NOTICES.md）：
#   import hanlp; hanlp.load(<权重标识>) -> 可调 pipeline([句子])
#   from ltp import LTP; LTP(<模型标识>).pipeline([句子], tasks=[...])
OPTIONAL_COMPONENTS: dict[str, dict] = {
    "hanlp": {
        "module": "hanlp", "factory": "load", "instantiates": "function",
        "result_keys": ("tok", "pos"),
        "component_license_declared": True,
        "model_license_commercial_ok": False,
        "service_license_declared": False,
        "upstream": "https://github.com/hankcs/HanLP",
        "notice_anchor": "可选中文 NLP 通道（未捆绑）",
    },
    "ltp": {
        "module": "ltp", "factory": "LTP", "instantiates": "class",
        "result_keys": ("cws", "pos"),
        "component_license_declared": False,
        "model_license_commercial_ok": False,
        "service_license_declared": False,
        "upstream": "https://github.com/HIT-SCIR/ltp",
        "notice_anchor": "可选中文 NLP 通道（未捆绑）",
    },
}


class OptionalBackendUnavailable(RuntimeError):
    """显式选的组件不可用/形状不符/许可未确认：披露并暂停，不静默换后端。"""

    def __init__(self, backend: str, reason: str, *, fallback: str | None = None):
        super().__init__(f"可选 NLP 组件 {backend} 不可用：{reason}"
                         "（不静默改用 none，也不冒充组件运行）")
        self.backend = backend
        self.reason = reason
        self.fallback = fallback


def _sentence_spans(text: str) -> list[dict]:
    """按句子结束符切**码点偏移**（与 ``novel_segments`` 同一坐标口径）。"""
    spans: list[dict] = []
    start = 0
    for index, char in enumerate(text):
        if char not in _SENTENCE_ENDERS and char != "\n":
            continue
        spans.extend(_emit(text, start, index + 1))
        start = index + 1
    spans.extend(_emit(text, start, len(text)))
    return spans


def _emit(text: str, begin: int, end: int) -> list[dict]:
    chunk = text[begin:end]
    if not chunk.strip():
        return []
    lead = len(chunk) - len(chunk.lstrip())
    body = chunk.strip()
    return [{"start": begin + lead, "end": begin + lead + len(body), "text": body}]


def _require_text(text) -> str:
    if not isinstance(text, str):
        raise ValueError(f"analyze 只接受 str，收到 {type(text).__name__}")
    return text


def _normalize_matrix(value, label: str, backend: str) -> list[list[str]]:
    """把 ``[[token,...], ...]`` 形状核出来；不合形状就拒，不猜。"""
    if not isinstance(value, list) or not value or not all(
            isinstance(row, list) and all(isinstance(item, str) for item in row)
            for row in value):
        raise OptionalBackendUnavailable(
            backend, f"{label} 返回形状不合预期（要 [[str,...], ...]），"
                     f"实际 {type(value).__name__}")
    return [[str(item) for item in row] for row in value]


class NoneProvider:
    """内置后端：只做句子级切分，词/词性显式报未覆盖。"""

    backend = "none"

    def analyze(self, text: str) -> dict:
        body = _require_text(text)
        spans = _sentence_spans(body)
        return {"schema": ANALYZE_SCHEMA, "backend": self.backend,
                "sentences": spans, "tokens": None, "pos": None,
                "coverage": dict(_COVERAGE_NONE),
                "component": None, "real_component_executed": False}

    def capability(self) -> dict:
        return capability(self.backend)


class ComponentProvider:
    """薄适配：构造时只解析模块，**首次 analyze 才**按上游文档好的入口取实例。

    取实例的时机放在 analyze 之后是有意的：``hanlp.load(id)`` 与 ``LTP(id)``
    都会去取权重，选择后端不等于授权下载。实例只取一次并留住，重复 analyze
    不再触发第二次加载。
    """

    def __init__(self, backend: str, module, component: str):
        self.backend = backend
        self._module = module
        self.component = component
        self._target = None
        self._real_component = _import_is_real(backend)

    def _run(self, spec, body: str):
        if self._target is None:
            self._target = getattr(self._module, spec["factory"])(self.component)
        if spec["instantiates"] == "class":
            pipeline = getattr(self._target, "pipeline", None)
            if not callable(pipeline):
                raise OptionalBackendUnavailable(
                    self.backend, "入口形状不符：实例上没有可调的 "
                                  f"pipeline(...)，不猜别的写法")
            return pipeline([body], tasks=list(spec["result_keys"]))
        if not callable(self._target):
            raise OptionalBackendUnavailable(
                self.backend, "入口形状不符：load(...) 返回的不是可调对象")
        return self._target([body])

    def analyze(self, text: str) -> dict:
        body = _require_text(text)
        spec = OPTIONAL_COMPONENTS[self.backend]
        raw = self._run(spec, body)
        keys = spec["result_keys"]
        if isinstance(raw, dict):
            got = tuple(key for key in keys if key in raw)
        else:
            got = tuple(key for key in keys if hasattr(raw, key))
        if got != tuple(keys):
            raise OptionalBackendUnavailable(
                self.backend, f"结果形状不符：缺 {'、'.join(k for k in keys if k not in got)}")
        tokens = _normalize_matrix(_pick(raw, keys[0]), keys[0], self.backend)
        pos = _normalize_matrix(_pick(raw, keys[1]), keys[1], self.backend)
        return {"schema": ANALYZE_SCHEMA, "backend": self.backend,
                "sentences": _sentence_spans(body), "tokens": tokens, "pos": pos,
                "coverage": dict(_COVERAGE_FULL), "component": self.component,
                "real_component_executed": self._real_component}

    def capability(self) -> dict:
        return capability(self.backend)


def _pick(raw, key: str):
    return raw[key] if isinstance(raw, dict) else getattr(raw, key)


def _import_is_real(backend: str) -> bool:
    """本次是否真的用了用户装好的组件（假后端注入时 find_spec 认不出来）。"""
    spec = OPTIONAL_COMPONENTS.get(backend)
    if spec is None:
        return False
    return importlib.util.find_spec(spec["module"]) is not None


def _resolve(backend: str, importer: Callable[[str], object]):
    spec = OPTIONAL_COMPONENTS[backend]
    try:
        module = importer(spec["module"])
    except ImportError as exc:
        raise OptionalBackendUnavailable(
            backend, f"组件未安装或导入失败（{exc}）；本包不捆绑该组件，"
                     "需要请自行安装并显式选择") from exc
    if not callable(getattr(module, spec["factory"], None)):
        raise OptionalBackendUnavailable(
            backend, f"入口形状不符：期望 {spec['module']}.{spec['factory']}"
                     f"{'（类）' if spec['instantiates'] == 'class' else '（函数）'}"
                     "可调，实际取不到")
    return module


def build_provider(backend: str = DEFAULT_BACKEND, *, component: str | None = None,
                   importer: Callable[[str], object] | None = None,
                   license_ack: bool = False):
    """按后端名构造 provider；``none`` 之外任何选择都逐条过闸，缺一条即拒。

    ``component`` 是**用户显式给出的权重/模型标识**：本层不知道哪个权重合适，
    也不许自己挑（一挑就变成隐式下载）。``license_ack`` 只用于许可未声明的组件，
    代表用户看过 ``THIRD_PARTY_NOTICES.md`` 里那一条并确认可以在此场景使用。
    """
    if backend not in BACKENDS:
        raise ValueError(f"backend 只能是 {' / '.join(BACKENDS)}，收到 {backend!r}")
    if backend == DEFAULT_BACKEND:
        return NoneProvider()
    spec = OPTIONAL_COMPONENTS[backend]
    if not spec["component_license_declared"] and license_ack is not True:
        raise OptionalBackendUnavailable(
            backend, "许可状态未确认：该组件的许可在上游元数据里未声明，"
                     "商用/分发限制未核；请阅读 THIRD_PARTY_NOTICES.md 的「"
                     + spec["notice_anchor"] + "」一条后显式传 license_ack=True")
    if component is None:
        raise OptionalBackendUnavailable(
            backend, "未给出权重/模型标识：本层不替用户选模型，也不隐式下载")
    if not isinstance(component, str) or not component.strip():
        raise OptionalBackendUnavailable(
            backend, f"权重/模型标识必须是非空字符串，收到 {component!r}")
    module = _resolve(backend, importer or importlib.import_module)
    return ComponentProvider(backend, module, component.strip())


def capability(backend: str = DEFAULT_BACKEND) -> dict:
    """能力与许可分别披露（只查可得性，不执行组件、不取权重）。"""
    if backend not in BACKENDS:
        raise ValueError(f"backend 只能是 {' / '.join(BACKENDS)}，收到 {backend!r}")
    if backend == DEFAULT_BACKEND:
        return {"backend": "none", "installed": True, "bundled": True,
                "available": True, "reason": "",
                "license_status": "本包自带实现，无第三方组件",
                "component_license_declared": True,
                "model_license_commercial_ok": True,
                "service_license_declared": True,
                "real_component_executed": False,
                "model_download_allowed": False, "upstream": "",
                "notice_anchor": ""}
    spec = OPTIONAL_COMPONENTS[backend]
    installed = importlib.util.find_spec(spec["module"]) is not None
    parts = [] if installed else ["组件未安装（本包未捆绑，不自动下载）"]
    if not spec["component_license_declared"]:
        parts.append("许可未声明：需显式确认后才允许选择")
    if not spec["model_license_commercial_ok"]:
        parts.append("权重许可含非商业/付费限制：商用前必须单独核对")
    if not spec["service_license_declared"]:
        parts.append("服务条款未核对")
    return {"backend": backend, "installed": installed, "bundled": False,
            "available": installed,
            "reason": "；".join(parts),
            "license_status": "已声明" if spec["component_license_declared"] else "未声明",
            "component_license_declared": spec["component_license_declared"],
            "model_license_commercial_ok": spec["model_license_commercial_ok"],
            "service_license_declared": spec["service_license_declared"],
            "real_component_executed": False,
            "model_download_allowed": False,
            "upstream": spec["upstream"],
            "notice_anchor": spec["notice_anchor"]}
