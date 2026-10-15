STATES = {
    "new",
    "awaiting_boundary",
    "parsed",
    "awaiting_decision",
    "ready_for_rewrite",
    "rewriting",
    "awaiting_checks",
    "rework",
    "awaiting_review",
    "needs_review",
    "passed",
    "approved",
    "delivered",
    "paused",
    "error",
}


class StateError(ValueError):
    pass


def transition_allowed(current: str, target: str) -> bool:
    direct = {
        ("new", "parsed"),
        ("new", "awaiting_boundary"),
        ("awaiting_boundary", "parsed"),
        ("parsed", "awaiting_decision"),
        ("parsed", "ready_for_rewrite"),
        ("awaiting_decision", "ready_for_rewrite"),
        ("rework", "ready_for_rewrite"),
        ("ready_for_rewrite", "rewriting"),
        ("rewriting", "awaiting_checks"),
        # 进程被强制终止（kill/断电）会留下 rewriting 残留态；该迁移是 recover 的
        # 收敛出口（§10.4「强制终止进程后可恢复」），否则该章成为死路。
        ("rewriting", "needs_review"),
        ("awaiting_checks", "rework"),
        ("awaiting_checks", "awaiting_review"),
        ("awaiting_checks", "passed"),
        ("rework", "rewriting"),
        ("awaiting_review", "passed"),
        ("awaiting_review", "rework"),
        ("passed", "approved"),
        ("approved", "delivered"),
        ("passed", "ready_for_rewrite"),
        ("passed", "needs_review"),
        ("approved", "ready_for_rewrite"),
        ("approved", "needs_review"),
        ("delivered", "ready_for_rewrite"),
        ("delivered", "needs_review"),
        ("error", "needs_review"),
        ("needs_review", "ready_for_rewrite"),
        ("needs_review", "rework"),
    }
    if current == target:
        return True
    if (current, target) in direct:
        return True
    if current not in {"delivered", "error"} and target == "paused":
        return True
    if current not in {"delivered", "error"} and target == "error":
        return True
    if current == "paused" and target in STATES - {"new", "awaiting_boundary", "parsed", "delivered"}:
        return True
    return False


def require_transition(current: str, target: str) -> None:
    if current not in STATES or target not in STATES:
        raise StateError(f"未知状态: {current} -> {target}")
    if not transition_allowed(current, target):
        raise StateError(f"非法状态迁移: {current} -> {target}")
