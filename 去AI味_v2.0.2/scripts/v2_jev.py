#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""v2_jev.py - 官方 TypeSafe SDK 的 Jev 裁判适配层（v2.0.0 alpha.3 P09）。

只做三件事，**不实现第二套判定、第二套重试、第二套凭据系统**：

  1. 把 ``v2_judge.build_questions`` 出的五个分项翻成官方 ``Noul``/``Choice``
     问题，用 ``TypeSafeClient.system_one`` 取答案，再翻回分项答案表；
     结论一律交给 alpha.2 的 ``v2_judge.combine_judgment``。
  2. 把官方异常分成两类：**认证类（401/403）一次都不重试**；
     允许类（429/超时/连接/5xx）由官方 ``RetryPolicy`` **有界**重试，
     耗尽即抛故障，由会话层进入 ``PAUSED_JUDGE_UNAVAILABLE``。
  3. 凭据只以引用形式出现：安全后端（keyring/Secret Store）优先，缺后端时用
     注入的进程级安全输入；**绝不写 .env、绝不把明文 Key 放进返回值、日志、
     receipt、manifest 或报告**。

三条不得越过的线：

  - **Noul 是「yes 的概率」，不是独立置信度**（官方字段就叫 ``noul``）。
    落册只写 ``probability`` + ``probability_kind``，落在不确定带里一律
    ``unknown``；只有 ``Choice`` 答案自带 ``confidence`` 时才登记 confidence。
  - **不静默换裁判**：裁判失败只抛故障，绝不产出另一个 provider 的判定，
    也不动 ``judge_epoch``；换裁判必须是用户当前明确事件（P13 接线）。
  - **mock 不等于联通**：注入客户端（``httpx2.MockTransport``）只证接口形状，
    回执里必须写明 ``judge_transport`` 与 ``real_endpoint_verified``。
    构造真实网络客户端需要显式授权参数。

实际模型取响应体 ``model``，不是请求里写的那个名字——请求名可能是别名。
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

import v2_contracts as vc
import v2_judge as vj

JEV_PROVIDER = "jev"
HOST_PROVIDER = "host"
PROVIDER_NAMES: tuple[str, ...] = (JEV_PROVIDER, HOST_PROVIDER)

#: Noul 概率的形状名：读的人必须能看出这不是独立置信度。
NOUL_PROBABILITY_KIND = "noul_yes_probability"
#: 分段规则（不是校准事实）：带内一律 unknown，unknown 在 A21 合同里就是证据不足，
#: 所以这道带不可能伪造出 rewrite。
NOUL_YES_MIN = 0.75
NOUL_NO_MAX = 0.25

#: 允许故障的有界重试（官方 RetryPolicy 的 max_retries；总尝试 = 该值 + 1）。
JEV_MAX_RETRIES = 2

_KEYRING_KIND = "keyring"
_CREDENTIAL_KINDS: tuple[str, ...] = (_KEYRING_KIND,)
#: 探测用的单问问题：只为确认「这个 Key 能用、服务端回的是哪个 model」。
_PROBE_QUESTION = "lieflat_v2_credential_probe"


def _sdk():
    """官方 SDK 只在实际调用裁判时导入。

    Jev 是用户显式选择的可选项：接收方没装 typesafe-sdk 时，整包的 import 烟测
    不能被这一层拖垮；缺依赖由调用处报「凭据/裁判不可用」，不是 ImportError。
    """
    import typesafe_sdk

    return typesafe_sdk


class CredentialUnavailable(RuntimeError):
    """安全后端没有这一项、没有可用后端，或未获进程级输入。

    只带引用形状的信息，绝不带 Key 明文。
    """

    def __init__(self, credential_ref: str, reason: str) -> None:
        super().__init__(f"凭据不可用（{credential_ref}）：{reason}")
        self.credential_ref = credential_ref
        self.reason = reason


class JudgeCallError(RuntimeError):
    """裁判调用故障基类：带尝试次数与故障类别，供会话层决定暂停原因。"""

    kind = "unknown"

    def __init__(self, *, kind: str | None = None, attempts: int,
                 credential_ref: str, detail: str = "") -> None:
        if kind is not None:
            self.kind = kind
        parts = [f"kind={self.kind}", f"attempts={attempts}",
                 f"credential_ref={credential_ref}"]
        if detail:
            parts.append(f"detail={detail[:160]}")
        super().__init__(" ".join(parts))
        self.attempts = attempts
        self.credential_ref = credential_ref
        self.detail = detail

    def describe(self) -> dict:
        return {"kind": self.kind, "attempts": self.attempts,
                "credential_ref": self.credential_ref, "detail": self.detail}


class JudgeAuthenticationError(JudgeCallError):
    """401/403：认证失败一次都不重试，交会话层进入裁判不可用暂停。"""

    kind = "authentication"


class JudgeUnavailableError(JudgeCallError):
    """允许故障有界重试耗尽：进入 PAUSED_JUDGE_UNAVAILABLE，等用户明确事件。"""

    kind = "unavailable"


class JudgeResponseShapeError(RuntimeError):
    """响应形状不对（缺实际 model、答案不可解析）：不猜、不降级成默认值。"""


# ---------------------------------------------------------------- 凭据

def _reference_problems(reference) -> list[str]:
    if not isinstance(reference, Mapping):
        return [f"reference 必须是映射，收到 {type(reference).__name__}"]
    problems: list[str] = []
    kind = reference.get("kind")
    if kind not in _CREDENTIAL_KINDS:
        problems.append(f"不支持的凭据引用类型 {kind!r}（只认 {_CREDENTIAL_KINDS}；"
                        "环境变量与配置文件都不是安全后端）")
    for field in ("service", "username"):
        value = reference.get(field)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"reference.{field} 必须是非空字符串")
    return problems


def credential_ref_of(reference: Mapping) -> str:
    """引用标识（永远不含 Key 明文）：``keyring::<service>/<username>``。"""
    return f"{reference.get('kind')}::{reference.get('service')}/{reference.get('username')}"


def _keyring_backend():
    """取系统安全后端；取不到返回 None，由调用方按「无后端」处理（不回落硬编码）。"""
    try:
        import keyring
    except Exception:  # pragma: no cover - 无后端环境
        return None
    return keyring


def resolve_credential(reference: dict, *, backend=None, reader=None,
                       allow_prompt: bool = True) -> str:
    """按引用取出 Key 明文，只在调用方内存里存在——不落盘、不返回引用以外的信息。

    次序：安全后端（keyring/Secret Store）→ 注入的进程级安全输入。
    没有后端、后端没有这一项、又不给输入通路时，一律 ``CredentialUnavailable``；
    绝不写 .env，绝不接受默认 Key，绝不沿用上一条。
    """
    problems = _reference_problems(reference)
    if problems:
        raise ValueError("；".join(problems))
    ref = credential_ref_of(reference)
    active = backend if backend is not None else _keyring_backend()
    if active is None:
        raise CredentialUnavailable(ref, "没有可用安全后端（keyring 不可用）")
    try:
        secret = active.get_password(reference["service"], reference["username"])
    except Exception as exc:  # 后端本身故障：不猜、不当成「没有」
        raise CredentialUnavailable(ref, f"安全后端读取失败：{type(exc).__name__}") from exc
    if isinstance(secret, str) and secret.strip():
        return secret
    if reader is None or not allow_prompt:
        raise CredentialUnavailable(ref, "安全后端里没有这一项，且未提供进程级安全输入")
    prompt = (f"请输入 {reference['service']}/{reference['username']} 的 Jev API Key"
              "（不会写入文件或日志）：")
    secret = reader(prompt)
    if not isinstance(secret, str) or not secret.strip():
        raise CredentialUnavailable(ref, "进程级安全输入为空")
    return secret


def describe_credential(reference: dict, *, secret: str) -> dict:
    """凭据的可外发部分：只有引用与不可逆指纹，Key 明文不进返回值。"""
    return {"credential_ref": credential_ref_of(reference),
            "key_fingerprint": hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]}


def credential_receipt(reference: dict, *, secret: str, provider: str, model: str,
                       verified: bool, validation_id: str,
                       runtime_identity: Mapping | None = None) -> dict:
    """CredentialReceipt 的形状（引用 + 指纹进 runtime_identity，绝不含明文）。"""
    described = describe_credential(reference, secret=secret)
    identity = dict(runtime_identity or {})
    identity["key_fingerprint"] = described["key_fingerprint"]
    return {"provider": provider, "model": model,
            "credential_ref": described["credential_ref"],
            "validation_id": validation_id, "verified": bool(verified),
            "runtime_identity": identity}


# ---------------------------------------------------------------- 问答形状

def value_from_probability(probability: float) -> str:
    """Noul 概率 → 分项结论；带内一律 unknown（不猜 no）。"""
    if probability >= NOUL_YES_MIN:
        return "yes"
    if probability <= NOUL_NO_MAX:
        return "no"
    return "unknown"


def entry_from_answer(question_id: str, answer: Any) -> dict:
    """官方答案 → 分项条目。Noul 只登记概率；Choice 才有 confidence。"""
    sdk = _sdk()
    if isinstance(answer, sdk.NoulAnswer):
        probability = float(answer.noul)
        return {"value": value_from_probability(probability),
                "probability": probability,
                "probability_kind": NOUL_PROBABILITY_KIND}
    if isinstance(answer, sdk.ChoiceAnswer):
        probabilities = dict(answer.probabilities or {})
        choice = str(answer.choice)
        if choice not in vj.ANSWER_VALUES:
            return {"value": "unknown", "rejected_choice": choice,
                    "conflict": "选项名不在 yes/no/unknown 之内"}
        if probabilities:
            top = max(probabilities, key=lambda name: probabilities[name])
            if top != choice:
                return {"value": "unknown", "declared_choice": choice,
                        "probabilities": {k: float(v) for k, v in probabilities.items()},
                        "conflict": f"返回选项与概率最高项冲突（概率峰={top}）"}
        entry = {"value": choice,
                 "confidence": float(answer.confidence),
                 "probabilities": {k: float(v) for k, v in probabilities.items()}}
        return entry
    raise JudgeResponseShapeError(f"分项 {question_id} 收到未知答案类型 {type(answer).__name__}")


def entries_from_response(questions: dict, response: Any) -> tuple[dict, list[str]]:
    """五个在册分项逐个取答案；缺项按 unknown，形状冲突留原因。"""
    answers = dict(getattr(response, "answers", {}) or {})
    entries: dict[str, dict] = {}
    claims: list[str] = []
    for item in questions.get("questions", ()):
        question_id = item["id"]
        answer = answers.get(question_id)
        if answer is None:
            entries[question_id] = {"value": "unknown"}
            claims.append(f"裁判未回答分项「{vj.QUESTION_TITLES[question_id]}」")
            continue
        entry = entry_from_answer(question_id, answer)
        entries[question_id] = entry
        if entry.get("conflict"):
            claims.append(f"分项「{vj.QUESTION_TITLES[question_id]}」"
                          f"答案形状冲突：{entry['conflict']}")
    for question_id in vj.QUESTION_ORDER:
        entries.setdefault(question_id, {"value": "unknown"})
    return entries, claims


def sdk_questions(questions: dict) -> dict:
    """出题面 → 官方问题对象；每个分项一条，不合并、不新增分项。"""
    sdk = _sdk()
    built: dict[str, Any] = {}
    for item in questions.get("questions", ()):
        must_check = tuple(item.get("must_check") or ())
        context = "；需核对：" + "、".join(must_check) if must_check else ""
        form = item.get("answer_form", "noul")
        if form == "choice":
            built[item["id"]] = sdk.Choice(
                instructions=f"{item['ask']}{context}",
                criteria={"yes": "是", "no": "否", "unknown": "不确定"})
        else:
            built[item["id"]] = sdk.Noul(
                instructions=f"{item['ask']}{context}",
                criteria={"true": "是", "false": "否"})
    return built


def provider_name_for(task: vc.TaskSpec) -> str:
    """按规格选裁判；名字不在册就报错，绝不回落到宿主或规则裁判。"""
    provider = str(task.judge_ref.get("provider", "")).strip().lower()
    if provider not in PROVIDER_NAMES:
        raise ValueError(f"未知裁判 provider={provider!r}（在册：{PROVIDER_NAMES}）；"
                         "换裁判需要用户明确事件，不在这里回落")
    return provider


def _classify(exc: Exception, *, attempts: int, credential_ref: str) -> JudgeCallError:
    """官方异常 → 本层故障类别；认证类一次都不重试，允许类带上有界尝试数。"""
    sdk = _sdk()
    detail = str(exc)
    if isinstance(exc, sdk.TypeSafeAPIResponseValidationError):
        # 响应体不合官方模型：这是形状问题，不重试也不暂停，直接申报响应不合规。
        return JudgeResponseShapeError(f"响应形状不合官方模型: {detail[:200]}")
    if isinstance(exc, (sdk.TypeSafeAuthenticationError,
                        sdk.TypeSafePermissionDeniedError)):
        return JudgeAuthenticationError(attempts=1, credential_ref=credential_ref,
                                        detail=detail)
    if isinstance(exc, sdk.TypeSafeRateLimitError):
        kind = "rate_limited"
    elif isinstance(exc, sdk.TypeSafeAPITimeoutError):
        kind = "timeout"
    elif isinstance(exc, sdk.TypeSafeAPIConnectionError):
        kind = "connection"
    elif isinstance(exc, sdk.TypeSafeInternalServerError):
        kind = "server_error"
    elif isinstance(exc, (sdk.TypeSafeNotFoundError, sdk.TypeSafeBadRequestError)):
        kind = "bad_request"
    else:
        kind = "api_error"
    return JudgeUnavailableError(kind=kind, attempts=attempts,
                                 credential_ref=credential_ref, detail=detail)


# ---------------------------------------------------------------- 裁判提供方

class JevJudgeProvider:
    """官方 SDK 的 Jev 裁判：问答转换 + 故障分类，判定组合仍交给 v2_judge。"""

    provider = JEV_PROVIDER

    def __init__(self, *, client, credential_ref: str, retry=None,
                 calibration: dict | None = None) -> None:
        self._client = client
        self.credential_ref = credential_ref
        self.retry = retry if retry is not None else _sdk().RetryPolicy(
            max_retries=JEV_MAX_RETRIES)
        self.calibration = calibration if isinstance(calibration, dict) else {}

    @property
    def max_attempts(self) -> int:
        return int(self.retry.max_retries) + 1

    def evaluate(self, candidate: vc.CandidateRecord, *, state: dict,
                 questions: dict, task: vc.TaskSpec) -> vc.JudgmentRecord:
        if not isinstance(candidate, vc.CandidateRecord):
            raise ValueError("candidate 必须是 CandidateRecord")
        if not isinstance(questions, Mapping) or "questions" not in questions:
            raise ValueError("questions 必须来自 v2_judge.build_questions")
        response = self._call(state, questions)
        actual_model = getattr(response, "model", None)
        if not isinstance(actual_model, str) or not actual_model.strip():
            raise JudgeResponseShapeError("响应没有实际 model，不能拿请求名代替")
        entries, claims = entries_from_response(questions, response)
        judgment = vj.combine_judgment(
            candidate, entries, provider=self.provider,
            actual_model=actual_model, judge_epoch=task.judge_epoch,
            calibration=self.calibration, extra_claims=tuple(claims))
        return judgment

    def _call(self, state, questions):
        try:
            return self._client.system_one(state, sdk_questions(questions),
                                           retry=self.retry)
        except Exception as exc:  # 官方异常按类别分流，不吞掉任何一种
            error = _classify(exc, attempts=self.max_attempts,
                              credential_ref=self.credential_ref)
            raise error from exc


class HostJudgeProvider:
    """宿主模型裁判：分项答案由宿主给，缺项一律 unknown，不造结论。"""

    provider = HOST_PROVIDER

    def __init__(self, *, credential_ref: str, answers: Mapping | None = None,
                 model: str | None = None, calibration: dict | None = None) -> None:
        self.credential_ref = credential_ref
        self.answers = dict(answers or {})
        self.model = model
        self.calibration = calibration if isinstance(calibration, dict) else {}

    def evaluate(self, candidate: vc.CandidateRecord, *, state: dict,
                 questions: dict, task: vc.TaskSpec) -> vc.JudgmentRecord:
        entries: dict[str, dict] = {}
        for question_id in vj.QUESTION_ORDER:
            given = self.answers.get(question_id)
            if isinstance(given, Mapping):
                entries[question_id] = dict(given)
            elif isinstance(given, str):
                entries[question_id] = {"value": given}
            else:
                entries[question_id] = {"value": "unknown"}
        claims = () if self.answers else ("宿主未给出分项答案，不得代答",)
        return vj.combine_judgment(
            candidate, entries, provider=self.provider,
            actual_model=self.model or str(task.judge_ref.get("model")
                                           or task.model_version),
            judge_epoch=task.judge_epoch, calibration=self.calibration,
            extra_claims=claims)


# ---------------------------------------------------------------- 配置校验

def _probe(client) -> str:
    """一次最小探测：只为确认「Key 可用」与「服务端实际用的 model 名」。"""
    response = client.system_one({"lieflat_v2_probe": True},
                                 {_PROBE_QUESTION: _sdk().Noul()},
                                 retry=None)
    model = getattr(response, "model", None)
    if not isinstance(model, str) or not model.strip():
        raise JudgeResponseShapeError("探测响应没有实际 model")
    return model


def validate_jev_configuration(reference: dict, model: str, *, client=None,
                               client_factory=None, authorize_network: bool = False,
                               backend=None, reader=None) -> dict:
    """校验凭据与裁判配置；返回可直接交 ``apply_credential_result`` 的字典。

    - 给了 ``client``（测试注入的 MockTransport 客户端）：可校验形状，但
      ``real_endpoint_verified`` 必为假——mock 不证联通；
    - 只给了 ``client_factory``：必须显式 ``authorize_network=True`` 才构造，
      否则 ``failure.kind = "not_authorized"``，一个请求都不发；
    - 认证失败不重试；结果与异常文本都不含 Key 明文。
    """
    problems = _reference_problems(reference)
    if problems:
        raise ValueError("；".join(problems))
    described = {"credential_ref": credential_ref_of(reference)}
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model 必须是非空字符串")

    if client is None:
        if not authorize_network:
            return {"provider": JEV_PROVIDER, "model": model,
                    "credential_ref": described["credential_ref"],
                    "validation_id": "not-authorized", "verified": False,
                    "real_endpoint_verified": False,
                    "failure": {"kind": "not_authorized",
                                "message": "未授权真实端点验证，不构造网络客户端"},
                    "runtime_identity": {"judge_transport": "none"}}
        client = client_factory(api_key=resolve_credential(
            reference, backend=backend, reader=reader))

    try:
        actual_model = _probe(client)
    except JudgeAuthenticationError as exc:
        return _failure(described, model, exc)
    except JudgeCallError as exc:
        return _failure(described, model, exc)
    except Exception as exc:
        error = _classify(exc, attempts=1, credential_ref=described["credential_ref"])
        return _failure(described, model, error)

    return {"provider": JEV_PROVIDER, "model": actual_model,
            "credential_ref": described["credential_ref"],
            "validation_id": "val-" + hashlib.sha256(
                f"{described['credential_ref']}|{actual_model}".encode("utf-8")
            ).hexdigest()[:12],
            "verified": True,
            "real_endpoint_verified": bool(authorize_network),
            "runtime_identity": {"judge_transport": "network" if authorize_network
                                 else "mock"}}


def _failure(described: Mapping, model: str, error: JudgeCallError) -> dict:
    return {"provider": JEV_PROVIDER, "model": model,
            "credential_ref": described["credential_ref"],
            "validation_id": "failed", "verified": False,
            "real_endpoint_verified": False,
            "failure": error.describe(),
            "runtime_identity": {"judge_transport": "mock"}}
