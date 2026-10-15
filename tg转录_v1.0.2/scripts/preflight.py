"""环境与能力报告（Task 1）。

只做本地只读检查：不执行 tdl、不安装或升级依赖、不查 PATH、不创建目录、
不触碰会话文件、不联网。会话探测必须显式开启，且只允许用于在线动作。
每一项能力单独输出 capability/status/reason/affected_action，局部未知不被
一个总结论覆盖；reason 只点名字段与序号，不回显配置原值（会话目录、命名空间、
代理凭据、账号号码因此不会进入报告）。
"""

import os
import sys

from job import ATTACHMENT_TYPES, CLI_ACTIONS, ONLINE_ACTIONS

ALLOWED_STATUS = ("available", "unavailable", "unsupported",
                  "insufficient_source", "unchecked")
PROBE_STATUS = ("available", "unavailable")
OBSERVATION_KEYS = frozenset({"status", "reason", "affected_action"})
_MINIMUM_PYTHON = (3, 9)


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and "\x00" not in value


def _entry(capability, status, reason, affected_action):
    return {"capability": capability, "status": status, "reason": reason,
            "affected_action": affected_action}


def _selected(config, key):
    value = config.get(key)
    return list(value) if isinstance(value, list) else []


def _check_python(action):
    if sys.version_info[:2] >= _MINIMUM_PYTHON:
        return _entry("python_runtime", "available",
                      "当前 Python 解释器满足技能要求", action)
    return _entry("python_runtime", "unavailable",
                  "Python 版本低于技能要求，需升级后重跑检查", action)


def _check_selection(config, action):
    from job import validate_job
    try:
        validate_job(config)
    except ValueError as error:
        # error 文本只含字段名与不受支持的取值，不含会话/代理/账号值
        return _entry("job_selection", "unavailable", str(error), action)
    return _entry("job_selection", "available",
                  "用户选择合同校验通过（不代表已获真实账号授权）", action)


def _check_tdl(config, action):
    online = action in ONLINE_ACTIONS
    path = config.get("tdl")
    if not online:
        return _entry("tdl_executable", "unchecked",
                      "本次离线动作不需要 tdl；在线导出前的 tdl 路径仍未检查", "export")
    if not _text(path):
        return _entry("tdl_executable", "unavailable",
                      "配置未提供 tdl 可执行路径（不自动安装、不搜索 PATH）", action)
    if os.path.isfile(path):
        return _entry("tdl_executable", "available",
                      "已找到 tdl 可执行文件；本次未运行它", action)
    return _entry("tdl_executable", "unavailable",
                  "配置中的 tdl 路径不存在或不是文件（不会自动安装或替换）", action)


def _check_tdl_parameters(action):
    affected = action if action in ONLINE_ACTIONS else "export"
    return _entry("tdl_version_and_parameters", "unchecked",
                  "未运行 tdl，版本与所需参数（含导出模式）未验证，不宣称兼容", affected)


def _check_session_storage(config, action):
    affected = action if action in ONLINE_ACTIONS else "export"
    if action not in ONLINE_ACTIONS:
        return _entry("session_storage", "unchecked",
                      "本次离线动作不需要会话存储目录", affected)
    storage = config.get("storage")
    namespace = config.get("namespace")
    if not _text(storage) or not _text(namespace):
        return _entry("session_storage", "unavailable",
                      "配置未给出会话存储目录或命名空间（不创建、不重置登录）", affected)
    if not os.path.isdir(storage):
        return _entry("session_storage", "unavailable",
                      "会话存储目录不存在（不创建目录、不复位会话）", affected)
    return _entry("session_storage", "available",
                  "会话存储目录存在；内容未读取", affected)


def _normalise_probe_result(result):
    if isinstance(result, dict):
        status, reason = result.get("status"), result.get("reason")
    elif isinstance(result, tuple) and len(result) == 2:
        status, reason = result
    else:
        return "unchecked", "会话探测返回未知结构，不作为可用凭据"
    if status not in PROBE_STATUS or not _text(reason):
        return "unchecked", "会话探测返回不受支持的状态，不作为可用凭据"
    return status, reason


def _check_session_login(config, action, probe_session, session_probe):
    affected = action if action in ONLINE_ACTIONS else "export"
    if action not in ONLINE_ACTIONS:
        return (_entry("session_login", "unchecked",
                       "会话探测只允许用于在线动作；本次离线动作不探测登录", affected), False)
    if not probe_session:
        return (_entry("session_login", "unchecked",
                       "会话探测未显式开启；纯本地检查不证明登录有效", affected), False)
    if not callable(session_probe):
        return (_entry("session_login", "unchecked",
                       "已请求会话探测但未提供探测实现；未联网，不宣称登录有效", affected), False)
    try:
        status, reason = _normalise_probe_result(session_probe(config))
    except Exception as error:  # noqa: BLE001 - 探测失败只报告，不重置登录
        return (_entry("session_login", "unavailable",
                       f"会话探测未成功（{type(error).__name__}），"
                       "未重置登录或会话文件", affected), True)
    return (_entry("session_login", status, reason, affected), True)


def _check_output_path(config, action):
    destination = config.get("destination")
    if not _text(destination):
        if action in ONLINE_ACTIONS:
            return [_entry("output_path", "unavailable",
                           "配置未提供保存位置（不推荐目录代替用户选择）", action)]
        return []
    parent = os.path.dirname(destination.rstrip("/\\")) or destination
    if os.path.isdir(parent):
        return [_entry("output_path", "available",
                       "保存位置的上级目录存在；本次检查不创建目录", action)]
    return [_entry("output_path", "unavailable",
                   "保存位置的上级目录不存在（不自动创建目录）", action)]


def _check_sources(config, action):
    sources = _selected(config, "sources")
    if not sources:
        return []
    missing = [index + 1 for index, path in enumerate(sources)
               if not (isinstance(path, str) and os.path.exists(path))]
    if not missing:
        return [_entry("source_files", "available",
                       "所选本地源全部存在；内容未读取", action)]
    labels = "、".join(f"第 {number} 个" for number in missing)
    return [_entry("source_files", "unavailable",
                   f"{labels} source 路径不存在（不递归磁盘补齐未选择的源）", action)]


def _check_content_capabilities(config, action):
    entries = []
    contents = _selected(config, "contents")
    if "text" in contents:
        affected = action if action in ONLINE_ACTIONS else "export"
        entries.append(_entry("text_export", "unchecked",
                              "文字导出所需 tdl 参数与发送者字段要按实际导出 schema 核对，"
                              "本次未验证", affected))
        # W5：源数据缺发送者字段只记缺失、不编造署名（reading.py 的
        # MISSING_SENDER_NOTE / missing_sender 口径），故署名显示能力记
        # insufficient_source，而不是可用或未验证。
        entries.append(_entry("署名显示", "insufficient_source",
                              "发送者署名依赖实际导出数据的发送者字段（本次未按真实导出 "
                              "schema 核对）；缺发送者时阅读版只记缺失、不编造姓名",
                              affected))
    for category in contents:
        if category in ATTACHMENT_TYPES:
            entries.append(_entry(f"media_{category}", "unchecked",
                                  "媒体类别须按原始元数据实测判定（不以后缀冒充类型识别），"
                                  "本次未验证", "download"))
    return entries


def _validate_observations(observations, reserved):
    if observations is None:
        return []
    if not isinstance(observations, dict):
        raise ValueError("observations 必须是「能力名 -> 观察结果」的映射")
    entries = []
    for name, payload in observations.items():
        if not _text(name):
            raise ValueError("observations 条目的 capability 名称不能为空")
        if name in reserved:
            raise ValueError(f"observations 不得覆盖本地检查项：{name}")
        if not isinstance(payload, dict) or set(payload) != OBSERVATION_KEYS:
            raise ValueError("observations 条目必须只包含 status/reason/affected_action：" + name)
        status = payload["status"]
        if status not in ALLOWED_STATUS:
            raise ValueError("observations 状态不受支持（仅五种能力状态）：" + name)
        if not _text(payload["reason"]):
            raise ValueError("observations 必须给出非空 reason：" + name)
        affected = payload["affected_action"]
        if affected not in CLI_ACTIONS:
            raise ValueError("observations 的 affected_action 不是受支持的动作：" + name)
        entries.append(_entry(name, status, payload["reason"], affected))
    return entries


def inspect_environment(config, action, probe_session=False, *, session_probe=None,
                        observations=None):
    """按本次动作检查依赖与能力；probe_session 默认关闭且不联网。"""
    if not isinstance(config, dict):
        raise ValueError("config 必须是映射(dict)：环境检查需要本次任务的配置")
    if not isinstance(action, str) or action not in CLI_ACTIONS:
        raise ValueError("action 缺失或不是受支持的子命令：环境检查按动作分级")
    if probe_session and action not in ONLINE_ACTIONS:
        raise ValueError("probe_session=True 只允许用于在线动作；"
                         "离线动作不得触发会话探测（不联网、不重置登录）")

    entries = [
        _check_python(action),
        _check_selection(config, action),
        _check_tdl(config, action),
        _check_tdl_parameters(action),
    ]
    session_entry, session_probed = _check_session_login(
        config, action, bool(probe_session), session_probe)
    entries.append(_check_session_storage(config, action))
    entries.append(session_entry)
    entries.extend(_check_output_path(config, action))
    entries.extend(_check_sources(config, action))
    entries.extend(_check_content_capabilities(config, action))

    reserved = {entry["capability"] for entry in entries}
    entries.extend(_validate_observations(observations, reserved))
    return {"action": action,
            "probe_session": bool(probe_session),
            "session_probed": session_probed,
            "local_checks_only": not session_probed,
            "capabilities": entries}
