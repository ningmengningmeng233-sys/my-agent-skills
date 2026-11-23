"""run 侧契约校验器（SC-01）：render_plan 工单结构 + 断点状态合法性。

plan 侧已有 StoryboardError（plan.py）把分镜问题收敛为纯中文错误 + 退出码 2；
本模块把同一口径补到 run 侧：破损工单、错传文件、脏断点一律收敛为 ContractError，
由 cli 统一报纯中文错误并 exit 2，不得抛裸 traceback。

定位约定：每条问题自带结构定位（文件由调用方点名，字段/镜头序号由本模块点名），
缺失命名空间或定位信息时也不得含糊——有话直说缺了什么、在哪里。
"""
from __future__ import annotations

import json
from pathlib import Path

#: 工单命名空间标记：build_plan 写死的 contract 值。校验时“认结构为主”：
#: 标记缺失的手写工单照常放行（向后兼容），标记存在但写错的才报错。
PLAN_CONTRACT = "storyboard"


class ContractError(Exception):
    """工单/断点不合契约：对外只暴露纯中文错误，不外泄 traceback。"""

    def __init__(self, problems):
        if isinstance(problems, str):
            problems = [problems]
        self.problems = list(problems)
        super().__init__("；".join(self.problems))


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_plan(data):
    """校验 render_plan 工单结构，返回中文问题列表（空即通过）。"""
    if not isinstance(data, dict):
        return ["工单顶层必须是 JSON 对象"]
    problems = []
    marker = data.get("contract")
    if marker is not None and marker != PLAN_CONTRACT:
        problems.append(f"contract 命名空间必须为 {PLAN_CONTRACT}，实为 {marker!r}")
    if "shots" not in data:
        hint = ""
        if "episodes" in data:
            hint = "（该文件含 episodes，看起来是 storyboard.json 而不是 render_plan.json）"
        problems.append(f"工单缺少必填字段 shots{hint}")
        return problems
    shots = data["shots"]
    if not isinstance(shots, list) or not shots:
        return problems + ["工单的 shots 必须是非空数组"]
    for i, shot in enumerate(shots):
        loc = f"第{i + 1}个镜头"
        if not isinstance(shot, dict):
            problems.append(f"{loc}必须是对象")
            continue
        sid = shot.get("shot_id")
        if not isinstance(sid, str) or not sid:
            problems.append(f"{loc}缺少 shot_id")
        tag = f"镜头 {sid}" if isinstance(sid, str) and sid else loc
        dur = shot.get("duration")
        if not _is_num(dur) or dur < 0:
            problems.append(f"{tag}缺少合法 duration（必须为 ≥0 的数值）")
    return problems


def check_plan(data):
    """validate_plan 的抛错版：不合契约即抛 ContractError，合契约返回原数据。"""
    problems = validate_plan(data)
    if problems:
        raise ContractError(problems)
    return data


def load_failed_items(path):
    """断点状态加载前验合法性：返回 {shot_id: error}；文件缺席视为无断点。

    脏状态（非法 JSON、顶层非数组、条目缺 shot_id）一律抛 ContractError，
    调用方不得部分消费——要么全认，要么明确报错退出。
    """
    p = Path(path)
    if not p.exists():
        return {}
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractError(f"断点文件 {p} 不可读：{exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ContractError(
            f"断点文件 {p} 不是合法 JSON：{exc}；已拒绝使用脏状态"
            "（删掉该文件可重跑全量）") from exc
    if not isinstance(data, list):
        raise ContractError(f"断点文件 {p} 顶层必须是数组，实为 {type(data).__name__}")
    out = {}
    for i, item in enumerate(data):
        loc = f"断点文件 {p} 第{i + 1}项"
        if not isinstance(item, dict):
            raise ContractError(f"{loc}必须是对象")
        sid = item.get("shot_id")
        if not isinstance(sid, str) or not sid:
            raise ContractError(f"{loc}缺少 shot_id")
        err = item.get("error", "")
        out[sid] = err if isinstance(err, str) else str(err)
    return out
