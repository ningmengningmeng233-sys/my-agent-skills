from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from . import fingerprints
from .audit import append_audit, now_iso, read_audit
from .jsonio import read_json, write_json_atomic
from .locking import ProjectLock
from .paths import RULES_PATH
from .schemas import validate_manifest
from .state_machine import require_transition


def _last_audit_state(audit_path: Path) -> dict[str, str]:
    """从审计记录中反查每个章节最后进入的状态。"""
    last: dict[str, str] = {}
    try:
        for record in read_audit(audit_path):
            if record.get("type") == "state_change_detail":
                chapter_id = record.get("chapter_id")
                target = record.get("to")
                if isinstance(chapter_id, str) and isinstance(target, str):
                    last[chapter_id] = target
            elif isinstance(record.get("chapters"), list):
                for item in record["chapters"]:
                    if isinstance(item, dict) and isinstance(item.get("chapter_id"), str):
                        last[item["chapter_id"]] = item.get("state", "")
    except ValueError:
        return {}
    return last


class ManifestStore:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.path = workspace / "manifest.json"
        self.audit_path = workspace / "audit.jsonl"

    def load(self) -> dict[str, Any]:
        value = read_json(self.path)
        validate_manifest(value)
        return value

    def validate_state_history(self, manifest: dict[str, Any] | None = None) -> list[str]:
        """检查当前状态是否有对应的系统审计记录。"""
        manifest = manifest or self.load()
        last: dict[str, str] = {}
        try:
            for record in read_audit(self.audit_path):
                if record.get("type") == "state_change_detail":
                    chapter_id = record.get("chapter_id")
                    target = record.get("to")
                    if isinstance(chapter_id, str) and isinstance(target, str):
                        last[chapter_id] = target
                elif isinstance(record.get("chapters"), list):
                    for item in record["chapters"]:
                        if isinstance(item, dict) and isinstance(item.get("chapter_id"), str):
                            last[item["chapter_id"]] = item.get("state", "")
        except ValueError as exc:
            return [str(exc)]
        issues: list[str] = []
        for chapter in manifest.get("chapters", []):
            chapter_id = chapter["id"]
            state = chapter["state"]
            if state in {"new", "awaiting_boundary"}:
                continue
            if last.get(chapter_id) != state:
                issues.append(f"章节 {chapter_id} 的 manifest 状态缺少匹配系统审计记录: {state}")
        return issues

    def update(self, mutate: Callable[[dict[str, Any]], None], *, event: str) -> dict[str, Any]:
        with ProjectLock(self.workspace):
            manifest = self.load()
            before_revision = manifest["manifest_revision"]
            updated = deepcopy(manifest)
            mutate(updated)
            updated["manifest_revision"] = before_revision + 1
            updated["updated_at"] = now_iso()
            validate_manifest(updated)
            write_json_atomic(self.path, updated)
            append_audit(
                self.audit_path,
                event,
                manifest_revision=updated["manifest_revision"],
                chapters=[
                    {"chapter_id": chapter["id"], "state": chapter["state"]}
                    for chapter in updated.get("chapters", [])
                ],
            )
            return updated

    def transition_chapter(self, chapter_id: str, target: str, *, reason: str) -> dict[str, Any]:
        def mutate(manifest: dict[str, Any]) -> None:
            chapter = next((item for item in manifest["chapters"] if item["id"] == chapter_id), None)
            if chapter is None:
                raise KeyError(f"章节不存在: {chapter_id}")
            current = chapter["state"]
            require_transition(current, target)
            chapter["state"] = target
            chapter["updated_at"] = now_iso()

        updated = self.update(mutate, event="state_change")
        append_audit(
            self.audit_path,
            "state_change_detail",
            chapter_id=chapter_id,
            to=target,
            reason=reason,
            manifest_revision=updated["manifest_revision"],
        )
        return updated

    def sanitize_tampered_states(self) -> list[str]:
        """将手改 passed/delivered 且无审计记录的章节降级为 needs_review。

        返回被降级章节 ID 列表；无篡改时返回空列表。
        """
        manifest = self.load()
        last = _last_audit_state(self.audit_path)
        tampered_map: dict[str, str] = {}
        for chapter in manifest.get("chapters", []):
            chapter_id = chapter["id"]
            state = chapter["state"]
            if state in {"passed", "delivered"} and last.get(chapter_id) != state:
                tampered_map[chapter_id] = state
        if not tampered_map:
            return []

        def mutate(updated: dict[str, Any]) -> None:
            for chapter in updated["chapters"]:
                cid = chapter["id"]
                original = tampered_map.get(cid)
                if original is None:
                    continue
                target = "needs_review"
                require_transition(chapter["state"], target)
                chapter["state"] = target
                chapter["blockers"] = [
                    b
                    for b in chapter.get("blockers", [])
                    if b.get("code") != "MANIFEST_TAMPERED"
                ] + [
                    {
                        "code": "MANIFEST_TAMPERED",
                        "kind": "manual",
                        "message": (
                            f"manifest 状态 {original} 缺少匹配系统审计记录，"
                            "已降级到 needs_review"
                        ),
                        "opened_at": now_iso(),
                    }
                ]
                chapter["updated_at"] = now_iso()

        updated = self.update(mutate, event="manifest_tampered_sanitized")
        for chapter_id, original_state in tampered_map.items():
            append_audit(
                self.audit_path,
                "state_change_detail",
                chapter_id=chapter_id,
                to="needs_review",
                reason=(
                    f"manifest tampered: {original_state} state without audit record"
                ),
                manifest_revision=updated["manifest_revision"],
            )
        return list(tampered_map.keys())

    def refresh_stale_chapters(
        self, manifest: dict[str, Any] | None = None
    ) -> tuple[list[tuple[str, str, set[str]]], list[tuple[str, set[str]]]]:
        """检测输入指纹变化，对旧状态章节降级或仅刷新指纹。

        - awaiting_checks / awaiting_review -> rework
        - passed / delivered -> ready_for_rewrite
        - 更早的状态只刷新指纹，不清除现有阻塞项
        """
        manifest = manifest or self.load()
        stale = fingerprints.stale_input_chapters(manifest, self.workspace)
        downgraded: list[tuple[str, str, set[str]]] = []
        refreshed: list[tuple[str, set[str]]] = []
        if not stale:
            return downgraded, refreshed

        for chapter_id, changed in stale:
            chapter = next(
                (c for c in manifest["chapters"] if c["id"] == chapter_id), None
            )
            if chapter is None:
                continue
            state = chapter["state"]
            if state in {"awaiting_checks", "awaiting_review"}:
                target = "rework"
            elif state in {"passed", "delivered"}:
                target = "ready_for_rewrite"
            else:
                target = ""

            def make_mutate(
                cid: str,
                changed_keys: set[str],
                target_state: str,
            ) -> Callable[[dict[str, Any]], None]:
                def mutate(updated: dict[str, Any]) -> None:
                    ch = next(
                        (c for c in updated["chapters"] if c["id"] == cid), None
                    )
                    if ch is None:
                        return
                    paths = {
                        "source": self.workspace / ch["paths"]["source"],
                        "events": self.workspace / ch["paths"]["events"],
                        "project_decisions": self.workspace
                        / updated["project_decisions"]["path"],
                        "chapter_decisions": self.workspace / ch["paths"]["decisions"],
                        "rules": RULES_PATH,
                        "output": self.workspace / ch["paths"]["output"],
                        "checks": self.workspace / ch["paths"]["checks"],
                    }
                    ch["updated_at"] = now_iso()
                    if target_state:
                        ch["fingerprints"] = fingerprints.chapter_fingerprints(**paths)
                        require_transition(ch["state"], target_state)
                        ch["state"] = target_state
                        ch["blockers"] = [
                            b
                            for b in ch.get("blockers", [])
                            if b.get("code") != "STALE_INPUT_FINGERPRINT"
                        ] + [
                            {
                                "code": "STALE_INPUT_FINGERPRINT",
                                "kind": "fingerprint",
                                "message": (
                                    f"输入指纹变化：{sorted(changed_keys)}；"
                                    f"已降级到 {target_state}"
                                ),
                                "opened_at": now_iso(),
                            }
                        ]
                    elif ch["state"] == "approved":
                        # 已批准章节的输入漂移**不得被静默洗掉**（ADR：防止旧正文冒充
                        # 新决策结果）。刻意保留记录的旧指纹，使 merge 的哈希复核继续
                        # 拒绝该章；且不在 approved 上写 blocker（schema 要求
                        # approved/delivered 无阻塞项）。撤销须走显式 rollback，
                        # 以免在事实源已提交的情况下造成半批准状态。
                        pass
                    else:
                        ch["fingerprints"] = fingerprints.chapter_fingerprints(**paths)
                        ch["blockers"] = [
                            b
                            for b in ch.get("blockers", [])
                            if b.get("code") != "STALE_INPUT_FINGERPRINT"
                        ]

                return mutate

            self.update(
                make_mutate(chapter_id, changed, target),
                event="stale_fingerprint_checked",
            )
            if target:
                append_audit(
                    self.audit_path,
                    "state_change_detail",
                    chapter_id=chapter_id,
                    to=target,
                    reason=f"stale fingerprints: {sorted(changed)}",
                )
                downgraded.append((chapter_id, target, changed))
            else:
                refreshed.append((chapter_id, changed))
        return downgraded, refreshed
