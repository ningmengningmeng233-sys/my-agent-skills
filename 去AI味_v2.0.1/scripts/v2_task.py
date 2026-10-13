#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2_task.py - v2 任务：正文范围发现 + 多源原 job 协调（alpha.1 P03 + 补正 Q03）。

合同（设计 4/6/9 节；补正方案 4.1/4.2/4.3；总工单五项评审重点第五项）：

  1. **目录扫描只产生候选范围**：README、人物卡、世界观、设定、大纲、笔记、
     提示词、备份等按文件名信号判为 ``support``；无信号的文件判 ``uncertain``。
     两者都不自动进入正文范围——「不能看到 .md 就全改」。``explicit_sources``
     点名的文件用户指定优先，直接进 ``body``；仅 basename 命中多件时全部标
     ``ambiguous``，不自动选中，等待用户确认范围。
  2. **来源唯一身份是规范绝对路径**（补正 R03）：不同目录的同名章节是两件
     来源，相同内容不同路径也是两件；display_path 只用于展示，不参与去重。
  3. **多源任务逐成员复用现有 job**（不建第二个任务库）：每个来源调用
     ``novel_job.create_job``，各自持有默认工作区、源认领、快照与锁；
     primary job 的 manifest ``v2`` 扩展字段保存完整规格与成员映射，
     state ``v2`` 扩展字段保存 SessionRecord——旧 v2-spec/v2-session 附属
     文件只读兼容，不再是第二可写权威。
  4. **失败只释放本次获得的成员**：集合不一致零创建；逐成员创建或扩展
     元数据写失败时，用现有 ``cleanup_job`` 释放自己已持有的锁/认领并保留
     诊断件，别人的 job 与原稿一个字节不动。
  5. **任务摘要绑定源哈希**：TaskSpec.source_files 持有订约时 sha256；
     ``source_drift_detected`` 用真实文件比对判定漂移——源被改/缺失即 True，
     摘要失效必须重新确认。布尔由比对得出，不由调用方声明。
  6. 初态按 choices 最靠前的缺失环节推导：缺 mode → WAIT_MODE；缺 jev →
     WAIT_JEV；启用未验证 → WAIT_JEV_KEY（补正 R01）；其余 →
     WAIT_TASK_CONFIRMATION。
  7. **授权配对成对落盘**（第二轮复核 S01）：manifest 的 v2.spec 与 state 的
     v2.session 是两个文件，各自原子不等于成对原子。``persist_task_session``
     先写 ``state.v2.authorization_update`` pending 标记，再写新规格，最后写新
     会话并清标记；失败尽力回退，回退不成则保留 pending 并抛
     :class:`AuthorizationRecoveryNeeded`；``require_no_pending_update`` 让入口
     在半更新期间一律拒绝推进授权，``recover_task_session`` 只回到上个一致状态，
     不猜测未完成的用户批准。

package 身份：优先安装目录 ``release-manifest.json`` 的 package_digest；开发源
树无 manifest 时退化为 SKILL.md 字节 SHA-256（确定性、可复核，不猜）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, replace
from pathlib import Path

import novel_job
import v2_contracts as vc

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPTS_DIR.parent

SPEC_FILE = "v2-spec.json"
SESSION_FILE = "v2-session.json"
# 开发阶段身份（补正方案 5.2）：**默认**任务快照与 job 身份按此绑定；
# 阶段升到 stable 只声明「这就是将交付那份字节的身份」，**不等于发布通过**：终态仍由
# run_v2_quality 的真实门读数判，缺人签/宿主/真实语料照样是开放。C34：这条值与
# pyproject 的 [tool.lemon.v2] release_stage 是一组权威，不一致由
# tests/v2/test_release_stage_default.py 判红——声明不再是装饰，测试也不再用
# 「临时传一个 stage」掩盖默认身份。
DEV_RELEASE_STAGE = "stable"
# 遗留会话迁移前的只读备份（补正方案 3.4：先保留原件再迁移，不伪造历史批准）。
LEGACY_SESSION_BACKUP = "v2-session.legacy-backup.json"
# 新授权字段集：缺失即视为旧开发会话，需显式迁移。
_SESSION_V2_FIELDS = ("choices", "task_revision", "confirmed_revision",
                      "credential_receipt", "presented_revision",
                      "presented_request_id", "presented_summary_sha256")

# 文件名信号：非正文（support）——README/设定/人物卡等（用户要求第 32 节）。
_SUPPORT_NAMES = (
    "readme", "人物卡", "世界观", "设定", "大纲", "提纲", "笔记", "备忘",
    "prompt", "提示词", "backup", "备份",
)
# 文件名信号：正文章节（第X章/回/节/卷、序章、终章、番外、尾声、楔子）。
_CHAPTER_RE = re.compile(
    r"^(?:第[0-9一二三四五六七八九十百千零〇两]+[章回节卷篇]|序章|序言|终章|"
    r"尾声|番外|楔子|终章)")
_BODY_HINT_RE = re.compile(r"[章回]")


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _classify(name: str) -> str:
    """文件名启发式：body / support / uncertain。只看文件名，不看内容。"""
    stem = name.lower()
    if stem.startswith("readme") or any(k in stem for k in _SUPPORT_NAMES):
        return "support"
    if name.endswith((".bak", ".tmp", ".orig")):
        return "support"
    if _CHAPTER_RE.match(name):
        return "body"
    if _BODY_HINT_RE.search(name):
        return "body"
    return "uncertain"


def _canonical_source_id(path) -> str:
    """来源唯一身份：经重解析的规范绝对路径，按平台大小写规则比较（补正 R03）。

    不同目录的同名章节得到不同 source_id；同一真实文件的多种写法
    （大小写、相对/绝对、重解析点）收敛为同一个 source_id。
    """
    return os.path.normcase(str(Path(path).resolve()))


def discover_sources(paths: tuple[Path, ...], *,
                     explicit_sources: tuple[str, ...] = ()) -> tuple[dict, ...]:
    """发现候选来源：每个 dict 含 ``source_id``（规范绝对路径，唯一身份）、
    ``display_path``/``path``（各扫描根内相对 POSIX 路径，仅展示）、
    ``role``（body/support/uncertain/ambiguous）与 ``sha256``。

    目录会被展开为其中的 .md/.txt 文件；显式给到的单文件按其自身判定。
    去重只按 source_id——display_path 重名不合并（补正 R03）。
    ``explicit_sources``（用户点名）按完整路径或扫描根相对路径匹配强制
    ``body``；裸 basename 命中多件时全部标 ``ambiguous``，不自动选中。
    """
    records: list[dict] = []
    by_id: dict[str, dict] = {}
    roots = [Path(p) for p in paths]
    for root in roots:
        files = (sorted(root.glob("*.md")) + sorted(root.glob("*.txt"))
                 if root.is_dir() else [root])
        base = root if root.is_dir() else root.parent
        for f in files:
            source_id = _canonical_source_id(f)
            if source_id in by_id:
                continue                     # 同一真实文件重复输入去重
            rel = f.relative_to(base).as_posix()
            record = {"path": rel, "display_path": rel,
                      "source_id": source_id, "role": _classify(f.name),
                      "sha256": _sha256_of(f)}
            records.append(record)
            by_id[source_id] = record
    for explicit in explicit_sources:
        matches = _match_explicit(explicit, records, roots)
        if len(matches) == 1:
            matches[0]["role"] = "body"
        elif len(matches) > 1:
            for record in matches:           # 重名点名：等用户确认，不自动选
                record["role"] = "ambiguous"
    return tuple(records)


def _match_explicit(explicit: str, records: list[dict],
                    roots: list[Path]) -> list[dict]:
    """显式点名 → 候选记录列表（0/1/多件）。

    带路径成分的点名按真实路径解析（当前目录、各扫描根及其父目录），
    以规范身份命中；裸 basename 按各根展示名匹配——命中多件即交给
    ambiguous 处理，绝不自动全选。
    """
    wanted = explicit.strip().replace("\\", "/")
    if not wanted:
        return []
    if "/" in wanted or Path(explicit).is_absolute():
        probe = Path(explicit)
        if probe.is_file():
            hit_id = _canonical_source_id(probe)
            return [r for r in records if r["source_id"] == hit_id]
        hit_ids: set[str] = set()
        for root in roots:
            for base in (root, root.parent):
                candidate = base / wanted
                if candidate.is_file():
                    hit_ids.add(_canonical_source_id(candidate))
        return [r for r in records if r["source_id"] in hit_ids]
    return [r for r in records if r["display_path"] == wanted]


def source_drift_detected(spec: vc.TaskSpec, root: Path) -> bool:
    """源文件被改或缺失 = True：任务摘要随之失效，必须重新确认。"""
    root = Path(root)
    for ref in spec.source_files:
        p = Path(ref.path)
        if not p.is_absolute():
            p = root / ref.path
        if not p.is_file():
            return True
        if _sha256_of(p) != ref.sha256:
            return True
    return False


def runtime_identity(root: Path | None = None, *,
                     release_stage: str | None = None) -> dict:
    """真实运行件身份（补正方案 5.2）：对声明运行件逐文件复算摘要。

    - 无 manifest（开发源树）：按 ``runtime_contract.REQUIRED_RUNTIME_FILES``
      权威清单逐文件实算字节与哈希，origin = ``source``；
    - 有 release-manifest.json（候选/安装树）：对每个声明条目核对真实字节，
      声明与真实不符一律拒绝——不信任自报 digest，origin = ``installed``；
    - 摘要调用 ``package_identity.package_digest``，不抄第二份算法；
    - release_stage 是证据绑定字段，非法取值在此拒绝。阶段权威分两层：
      **包内声明优先**（D44：新候选自带 release_stage，收件人不需要 pyproject 也能读出
      真实阶段），调用方传的别的阶段一律拒，覆写不了包内声明；旧正式 v1 包没这个字段，
      继续按调用方给的阶段（或开发默认）走，兼容规则由 ``verify_install`` 的
      ``stage_source`` 如实区分。
    - 开发源树（没有包内清单）同样**不许调用方改名**（E52）：阶段取自构建期那一份权威
      （``package_identity.read_release_stage``：pyproject 声明与包内默认值成对，冲突或
      两缺即拒），显式参数只作期望值核对。合成源树只要带着 ``scripts/v2_task.py`` 的
      默认值就仍有明确阶段来源，不需要 pyproject。
    """
    root = Path(root) if root is not None else SKILL_ROOT
    import package_identity as pi
    entries: list[dict] = []
    manifest_path = root / pi.MANIFEST_NAME
    packaged_stage: str | None = None
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if "release_stage" in manifest:
            packaged_stage = str(manifest["release_stage"])
            if release_stage is not None and release_stage != packaged_stage:
                raise ValueError(
                    f"release_stage 由包内权威声明为 {packaged_stage!r}，"
                    f"调用方传的 {release_stage!r} 覆写不了它")
        elif release_stage is None:
            packaged_stage = DEV_RELEASE_STAGE      # 旧正式包：沿用开发默认，不猜别的阶段
    else:
        # 开发源树：阶段的权威与构建期同一份（E52）。读出来就是这棵树的阶段；
        # 调用方传的只作期望值核对，异值即拒——同一份代码不能被随口改名贴到证据上。
        try:
            authoritative = pi.read_release_stage(root)
        except SystemExit as stop:      # 权威层的 [STOP] 文案换成调用方可读的拒绝
            raise ValueError(f"开发源阶段权威不成立：{stop}") from None
        if release_stage is not None and release_stage != authoritative:
            raise ValueError(
                f"release_stage 由开发源自己的权威声明为 {authoritative!r}，"
                f"调用方传的 {release_stage!r} 只能核对期望值，不能替这棵树改名")
        packaged_stage = authoritative
    stage = vc._require_enum(release_stage or packaged_stage or DEV_RELEASE_STAGE,
                             vc.RELEASE_STAGES, "release_stage")
    if manifest_path.is_file():
        declared = manifest.get("files") or []
        if not isinstance(declared, list) or not declared:
            raise ValueError(f"release-manifest.json 未声明任何文件: {root}")
        for entry in declared:
            rel = str(entry["path"])
            actual = root / rel
            data = actual.read_bytes()
            if (len(data) != int(entry["bytes"])
                    or hashlib.sha256(data).hexdigest()
                    != str(entry["sha256"]).lower()):
                raise ValueError(f"运行件声明与真实字节不符: {rel}")
            entries.append({"path": rel, "bytes": int(entry["bytes"]),
                            "sha256": str(entry["sha256"]).lower()})
        origin = "installed"
        version = manifest.get("version") or pi.read_skill_version(
            root / "SKILL.md")
    else:
        from runtime_contract import REQUIRED_RUNTIME_FILES
        for rel in REQUIRED_RUNTIME_FILES:
            data = (root / rel).read_bytes()
            entries.append({"path": rel, "bytes": len(data),
                            "sha256": hashlib.sha256(data).hexdigest()})
        origin = "source"
        version = pi.read_skill_version(root / "SKILL.md")
    return {"origin": origin, "skill_version": version,
            "release_stage": stage,
            "package_digest": pi.package_digest(entries)}


def _package_sha256() -> str:
    """包身份：真实运行件摘要（补正 R04：SKILL.md 单文件哈希不得冒充包身份）。"""
    return runtime_identity()["package_digest"]


def _initial_state(spec: vc.TaskSpec) -> str:
    choices = spec.choices
    if choices.mode is None:
        return "WAIT_MODE"
    if choices.jev is None:
        return "WAIT_JEV"
    if choices.jev == "enabled":
        # 启用 Jev 未验证凭据：必须先过凭据门（补正 R01），
        # 验证结果只能来自受信任 Provider 通路，用户事件不能替代。
        return "WAIT_JEV_KEY"
    return "WAIT_TASK_CONFIRMATION"


def _spec_to_json(spec: vc.TaskSpec) -> dict:
    from dataclasses import asdict
    payload = asdict(spec)
    payload["choices"] = asdict(spec.choices)
    return payload


def _member_source_path(member: dict) -> Path:
    """成员实际操作路径：优先用清单记的真实拼写路径（F61）。

    旧任务清单没有 ``path`` 字段时退回 ``source_id``——那是本平台下的规范比较键，
    在大小写不敏感卷上指向同一目录，在大小写敏感卷上 ``source_id`` 本就保真；
    不据此改任何物理名字，也不拿近似名猜新任务。
    """
    recorded = str(member.get("path") or "").strip()
    return Path(recorded or member["source_id"])


def _member_job(member: dict) -> Path:
    """成员 job 目录：定位用真实拼写路径，身份比较仍用规范键。"""
    return (novel_job.workspace_dir_for(_member_source_path(member))
            / novel_job.JOBS_DIRNAME / member["job_id"])


def initial_session(spec: vc.TaskSpec) -> vc.SessionRecord:
    """按规格推导初始会话记录（唯一构造点，create_task 与入口调度共用）。

    初态取 choices 最靠前的缺失环节；不伪造历史批准，也不给 READY 兜底。
    """
    return vc.SessionRecord(
        task_id=spec.task_id, interaction_state=_initial_state(spec),
        explicit_sources=tuple(ref.path for ref in spec.source_files),
        summary_digest=None, authorization_event_refs=(),
        judge_epoch=spec.judge_epoch, choices=spec.choices,
        task_revision=spec.task_revision, confirmed_revision=None,
        credential_receipt=None)


def create_task(primary: Path, *, sources: tuple[Path, ...],
                spec: vc.TaskSpec) -> Path:
    """多源任务创建（补正方案 4.2/4.3）：逐成员复用现有 job 协议。

    - **零创建一致性检查**：primary 必须属于 sources；sources 与
      TaskSpec.source_files 的规范身份集合及订约哈希完全一致（来源当前
      内容当场重算比对），任一不符直接拒绝，不留任何半成品。
    - **逐成员 create_job**：按规范路径排序创建，稳定顺序降低交叉争用；
      每源持有自己的默认工作区、源认领、快照与锁（不共享工作区，
      旧 ``_refuse_if_active`` 不会误伤跨源成员）。
    - **primary 为协调根**：manifest ``v2`` 扩展保存完整规格与成员映射，
      state ``v2`` 扩展保存会话记录；不落第二权威附属文件。
    - **失败只释放本次获得的成员**：用现有 ``cleanup_job`` 释放自己已持有
      的锁/认领并保留诊断件；别人的 job 与原稿一个字节不动。
    """
    primary_id = _canonical_source_id(primary)
    member_ids: list[str] = []
    for source in sources:
        source_id = _canonical_source_id(source)
        if source_id not in member_ids:
            member_ids.append(source_id)      # 同一真实文件重复输入去重
    if primary_id not in member_ids:
        raise ValueError(f"primary 不在 sources 中: {primary}")
    spec_hashes: dict[str, str] = {}
    for ref in spec.source_files:
        source_id = _canonical_source_id(ref.path)
        if source_id in spec_hashes and spec_hashes[source_id] != ref.sha256:
            raise ValueError(f"TaskSpec.source_files 同一来源携带不同哈希: "
                             f"{source_id}")
        spec_hashes[source_id] = ref.sha256
    if set(member_ids) != set(spec_hashes):
        raise ValueError("sources 与 TaskSpec.source_files 集合不一致："
                         f"sources={sorted(member_ids)} "
                         f"spec={sorted(spec_hashes)}")
    # source_id 只是「本平台下同一真实文件的比较键」（Windows 会把它折成小写），
    # 不能拿来当实际操作路径：那样 job 清单记的原稿名、工作区目录名都丢了真实拼写，
    # 覆盖交付的裸名与清单名对不上，正常提交就入不了册（F61 独立反例）。
    # 这里把调用方实际选中的那条路径按身份键收下来，后续一律用它读写与命名。
    real_paths: dict[str, Path] = {}
    for source in sources:
        real_paths.setdefault(_canonical_source_id(source), Path(source))
    if primary_id not in real_paths:
        raise ValueError(f"primary 的规范身份没有对应的实际来源路径: {primary}")
    for source_id in sorted(spec_hashes):
        path = real_paths[source_id]
        if not path.is_file():
            raise ValueError(f"来源不存在: {path}")
        if _sha256_of(path) != spec_hashes[source_id]:
            raise ValueError(f"来源当前内容与订约哈希不一致: {path}")
    created: dict[str, Path] = {}
    try:
        for source_id in sorted(member_ids):
            created[source_id] = novel_job.create_job(
                real_paths[source_id], strength=spec.strength,
                package_sha256=_package_sha256(),
                rules_version=spec.rule_version, model_id=spec.model_version)
        primary_job = created[primary_id]
        members = [{"source_id": source_id, "sha256": spec_hashes[source_id],
                    "path": str(real_paths[source_id]),
                    "job_id": created[source_id].name}
                   for source_id in sorted(created)]
        novel_job.update_job_extension(
            primary_job,
            manifest_extension={"v2": {"spec": _spec_to_json(spec),
                                       "members": members}})
        initial = initial_session(spec)
        novel_job.update_job_extension(
            primary_job,
            state_extension={"v2": {"session": asdict(initial)}})
        novel_job._append_event(primary_job, "create",
                                detail={"v2": spec.task_id,
                                        "members": len(members)})
        return primary_job
    except BaseException:
        # 只回收本次成功创建并归本进程持有的成员：释放锁与源认领，
        # 保留 manifest/state 作诊断；失败不掩盖原始异常。
        for source_id in sorted(created):
            try:
                novel_job.cleanup_job(created[source_id], keep_evidence=True)
            except Exception:
                pass
        raise


def load_spec(job_dir: Path) -> vc.TaskSpec:
    """任务规格：primary manifest 的 ``v2`` 扩展字段为权威；旧 v2-spec.json
    附属文件只读兼容（不再回写，不构成第二权威）。"""
    job_dir = Path(job_dir)
    payload = (novel_job.load_manifest(job_dir).get("v2") or {}).get("spec")
    if payload is not None:
        return vc.validate_task_spec(payload)
    sidecar = job_dir / SPEC_FILE
    if sidecar.is_file():
        return vc.validate_task_spec(
            json.loads(sidecar.read_text(encoding="utf-8")))
    raise FileNotFoundError(f"任务 {job_dir.name} 没有 v2 任务规格")


def _migrate_legacy_session(payload: dict) -> dict:
    """旧开发会话没有完整授权字段：显式迁移到最早缺失状态（补正方案 3.4）。

    不得自动继承 READY——历史记录里没有可核验的实际选择与摘要确认，
    反推即伪造用户批准。
    """
    migrated = dict(payload)
    if migrated.get("choices") is None:
        migrated["interaction_state"] = "WAIT_MODE"
    elif migrated.get("summary_digest") is None:
        migrated["interaction_state"] = "WAIT_TASK_CONFIRMATION"
    migrated.setdefault("choices", None)
    migrated.setdefault("task_revision", 0)
    migrated.setdefault("confirmed_revision", None)
    migrated.setdefault("credential_receipt", None)
    # G2：旧会话里没有「摘要在哪一回合展示过」的记录——不反推成已展示，
    # 迁移后必须重新展示并让用户在后续回合确认。
    migrated.setdefault("presented_revision", None)
    migrated.setdefault("presented_request_id", None)
    migrated.setdefault("presented_summary_sha256", None)
    return migrated


def load_session(job_dir: Path) -> vc.SessionRecord:
    """会话记录：primary state 的 ``v2`` 扩展字段为权威；旧 sidecar 只读
    迁移（先保留原件备份），迁移结果不回写 sidecar。"""
    job_dir = Path(job_dir)
    state = novel_job._read_json(job_dir / novel_job.STATE_NAME) or {}
    payload = (state.get("v2") or {}).get("session")
    if payload is not None:
        return vc.SessionRecord(**payload)
    sidecar = job_dir / SESSION_FILE
    if sidecar.is_file():
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        if not set(_SESSION_V2_FIELDS).issubset(payload):
            backup = job_dir / LEGACY_SESSION_BACKUP
            if not backup.exists():
                backup.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8", newline="\n")
            payload = _migrate_legacy_session(payload)
        return vc.SessionRecord(**payload)
    raise FileNotFoundError(f"任务 {job_dir.name} 没有 v2 会话记录")


def persist_session(job_dir: Path, record: vc.SessionRecord) -> None:
    """会话记录写回 primary state 的 ``v2`` 扩展字段；原子写与锁检查复用
    ``novel_job.update_job_extension``，不另写第二份。

    兼容保留：只适合规格未变的写入。**入口不再用它更新已协调变化的规格**——
    规格与会话必须成对落盘，见 ``persist_task_session``（S01 补正）。
    """
    novel_job.update_job_extension(
        Path(job_dir), state_extension={"v2": {"session": asdict(record)}})


# ---- 成对持久化与中断恢复（S01 补正，补充工单第三节）------------------------

AUTHORIZATION_UPDATE_KEY = "authorization_update"


class AuthorizationRecoveryNeeded(ValueError):
    """半更新或损坏 pending：授权一律不放行，需显式 ``recover_task_session``。"""


def _v2_state_payload(job_dir: Path) -> dict:
    state = novel_job._read_json(Path(job_dir) / novel_job.STATE_NAME) or {}
    return state.get("v2") or {}


def pending_authorization_update(job_dir: Path):
    """返回 state.v2.authorization_update（无则 None）——半更新的可见标记。"""
    return _v2_state_payload(Path(job_dir)).get(AUTHORIZATION_UPDATE_KEY)


def require_no_pending_update(job_dir: Path, action: str = "推进授权") -> None:
    """pending 未清 = 规格与会话可能不成对，拒绝据此{action}。"""
    if pending_authorization_update(Path(job_dir)) is not None:
        raise AuthorizationRecoveryNeeded(
            f"任务 {Path(job_dir).name} 有一次未完成的授权更新（pending 未清）："
            f"拒绝{action}，请先调用 recover_task_session 恢复到上个一致状态")


def _validate_persisted_pair(spec: vc.TaskSpec, record: vc.SessionRecord) -> None:
    """成对写入前的自校验：身份、版本、实际选择与摘要四项必须互相咬合。"""
    if record.task_id != spec.task_id:
        raise ValueError(f"会话 task_id={record.task_id!r} 与规格 "
                         f"task_id={spec.task_id!r} 不一致，拒绝成对写入")
    if record.task_revision != spec.task_revision:
        raise ValueError(f"会话版本 {record.task_revision} 与规格版本 "
                         f"{spec.task_revision} 不一致，拒绝成对写入")
    if record.choices != spec.choices:
        raise ValueError("会话记录的实际选择与将要写入的规格不一致："
                         "不能事件一套、落盘另一套")
    if vc.task_digest(spec) != spec.request_digest:
        raise ValueError("规格摘要与按内容重算不符，拒绝据此建立授权记录")


def _json_norm(payload):
    """按落盘形状规范化比较：内存 asdict 给元组，磁盘 JSON 给列表。

    不做这一步的话「配对是否已一致」的判断会被容器类型差骗过——
    元组 ≠ 列表 永远不相等，幂等分支形同虚设。
    """
    return json.loads(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def persist_task_session(job_dir: Path, spec: vc.TaskSpec,
                         record: vc.SessionRecord, *,
                         expected_previous_digest: str) -> None:
    """规格与会话成对落盘（补正 S01）：pending 标记 → 新规格 → 新会话并清标记。

    manifest 与 state 是两个文件，各自原子不等于成对原子，所以按补充工单第三节
    用「已有 job 元数据里的一段暂存」做局部一致性标记，不建事务引擎：

    - 先证明磁盘上的现行规格就是调用方看到的 ``expected_previous_digest``，
      不符即拒绝（防覆盖后来的任务修订）；
    - 写 ``state.v2.authorization_update`` 记下上个一致配对与目标摘要；
    - 再写 ``manifest.v2.spec``，最后写 ``state.v2.session`` 并清标记；
    - 任一步失败就尽力用同一受管通路回退到上个一致配对；回退也失败则**保留 pending**
      并抛 :class:`AuthorizationRecoveryNeeded`，绝不吞异常、绝不半个更新冒充成功；
    - 每一步都只替换自己那一个 v2 子键，``members`` 等既有字段原样带走。

    所有写入复用 ``novel_job.update_job_extension``（内含锁持有者核验与
    ``_atomic_write``），本函数不另写原子文件算法。
    """
    job_dir = Path(job_dir)
    manifest = novel_job.load_manifest(job_dir)
    manifest_v2 = dict(manifest.get("v2") or {})
    stored_payload = manifest_v2.get("spec")
    if stored_payload is None:
        raise ValueError(f"任务 {job_dir.name} 的 manifest 没有 v2 规格，"
                         "无法成对更新")
    stored = vc.validate_task_spec(stored_payload)
    if stored.request_digest != expected_previous_digest:
        raise ValueError(
            "expected_previous_digest 与磁盘当前规格摘要不符：任务已被别处修订，"
            "拒绝用旧基线覆盖（请重载规格后重试）")
    _validate_persisted_pair(spec, record)

    session_payload = asdict(record)
    current_state_v2 = dict(_v2_state_payload(job_dir))
    if (_json_norm(stored_payload) == _json_norm(_spec_to_json(spec))
            and _json_norm(current_state_v2.get("session")) == _json_norm(session_payload)
            and AUTHORIZATION_UPDATE_KEY not in current_state_v2):
        return                                   # 幂等：配对未变时不写（确认类事件）

    pending = {
        "update_id": f"{spec.task_id}-{spec.task_revision}-{record.interaction_state}",
        "task_id": spec.task_id,
        "task_revision": spec.task_revision,
        "target_digest": spec.request_digest,
        "previous_spec": stored_payload,
        "previous_session": current_state_v2.get("session"),
    }
    marked = dict(current_state_v2)
    marked[AUTHORIZATION_UPDATE_KEY] = pending
    novel_job.update_job_extension(job_dir, state_extension={"v2": marked})
    try:
        new_manifest_v2 = dict(manifest_v2)
        new_manifest_v2["spec"] = _spec_to_json(spec)
        novel_job.update_job_extension(job_dir, manifest_extension={"v2": new_manifest_v2})
        fresh = dict(_v2_state_payload(job_dir))
        fresh["session"] = session_payload
        fresh.pop(AUTHORIZATION_UPDATE_KEY, None)
        novel_job.update_job_extension(job_dir, state_extension={"v2": fresh})
    except BaseException as exc:
        try:
            _roll_back_pair(job_dir, pending)
        except BaseException as rollback_exc:
            raise AuthorizationRecoveryNeeded(
                f"成对更新失败且自动回退也失败（原错误：{type(exc).__name__}: {exc}；"
                f"回退错误：{type(rollback_exc).__name__}: {rollback_exc}）："
                "pending 已保留，请显式 recover_task_session") from exc
        raise AuthorizationRecoveryNeeded(
            f"成对更新失败，已回退到上个一致规格与会话（{type(exc).__name__}: {exc}）")


def _roll_back_pair(job_dir: Path, pending: dict) -> None:
    """按 pending 记录回到上个一致配对：先规格、后会话，回退到位才清标记。"""
    back_manifest_v2 = dict(novel_job.load_manifest(job_dir).get("v2") or {})
    back_manifest_v2["spec"] = pending["previous_spec"]
    novel_job.update_job_extension(job_dir, manifest_extension={"v2": back_manifest_v2})
    back_state_v2 = dict(_v2_state_payload(job_dir))
    back_state_v2["session"] = pending["previous_session"]
    back_state_v2.pop(AUTHORIZATION_UPDATE_KEY, None)
    novel_job.update_job_extension(job_dir, state_extension={"v2": back_state_v2})


def recover_task_session(job_dir: Path) -> None:
    """显式恢复：把半更新的授权回退到 pending 记录的上个一致状态（补充工单 3.2）。

    - 先由 ``load_manifest`` / ``update_job_extension`` 认目录并核验锁持有者，
      非持有进程一律 JobError，不代为接管；
    - pending 必须自带可验证的 ``previous_spec`` / ``previous_session`` 与任务身份；
      损坏 pending **保留不动**并报错——不猜测未完成的用户批准，也不静默重建 READY；
    - 回退完成（规格与会话都落回旧值）之后才清除 pending 标记。
    - 没有 pending 时幂等返回，不改动任何字节。
    """
    job_dir = Path(job_dir)
    novel_job.load_manifest(job_dir)              # 认目录：缺清单即 unknown_job
    pending = pending_authorization_update(job_dir)
    if pending is None:
        return
    if not isinstance(pending, dict) or not {"previous_spec", "previous_session"}.issubset(pending):
        raise ValueError(
            f"任务 {job_dir.name} 的 pending 授权更新已损坏（缺上个一致配对）："
            "拒绝猜测未完成的用户批准，保留 pending 供人工处置")
    previous = vc.validate_task_spec(pending["previous_spec"])
    if pending.get("task_id") != previous.task_id:
        raise ValueError(f"pending 记录的任务身份与上个规格不符："
                         f"{pending.get('task_id')!r} ≠ {previous.task_id!r}，拒绝回退")
    if not isinstance(pending["previous_session"], dict) \
            or pending["previous_session"].get("task_id") != previous.task_id:
        raise ValueError("pending 里的上个会话与规格任务身份不符，拒绝回退")
    _roll_back_pair(job_dir, pending)
    _append_event = getattr(novel_job, "_append_event", None)
    if _append_event is not None:
        _append_event(job_dir, "authorization_update_recovered",
                      detail={"task_id": previous.task_id,
                              "restored_digest": previous.request_digest})


def _resume_members(job_dir: Path, spec: vc.TaskSpec) -> list:
    """逐成员恢复（补正方案 4.3）：主 job 与每个成员 job 调用现有
    ``resume_job`` 重新核对源快照（sha/size/mtime）与身份一致性。

    任一成员拒绝恢复即抛 JobError——任务不得进入 RUNNING；已核实成员的
    进度原样保留。全部通过时返回 ``[(source_id, member_job, verdict)]``；
    ``verdict`` 直接取自现有 ``resume_job``，这里不重新发明判定。
    """
    job_dir = Path(job_dir)
    members = (novel_job.load_manifest(job_dir).get("v2") or {}).get("members")
    if not members:
        raise ValueError(f"任务 {job_dir.name} 的 manifest 没有 v2 成员映射")
    spec_ids = {_canonical_source_id(ref.path) for ref in spec.source_files}
    results: list[Path] = []
    for member in members:
        source_id = member["source_id"]
        if source_id not in spec_ids:
            raise ValueError(f"成员不在当前任务规格内: {source_id}")
        member_job = _member_job(member)
        identity = novel_job.load_identity(member_job)
        verdict = novel_job.resume_job(
            member_job, strength=identity.strength,
            package_sha256=identity.package_sha256,
            rules_version=identity.rules_version,
            model_id=identity.model_id)
        if verdict == "refuse":
            raise novel_job.JobError(
                "member_resume_refused",
                f"成员 {source_id} 恢复被拒（源漂移/身份变化/锁占用）："
                "任务不得继续，已核实成员的进度保留",
                member=source_id)
        results.append((source_id, member_job, verdict))
    return results


def resume_task_sources(job_dir: Path, spec: vc.TaskSpec) -> tuple[Path, ...]:
    """逐成员恢复的公开入口（补正方案 4.3）：返回各成员 job 目录（含主 job）。

    判定本体在 ``_resume_members``；本壳只取用其中的目录一项，保持既有调用方与
    测试的签名不变——P13 需要恢复结论时取同一份实现，不写第二套判定。
    """
    return tuple(job for _source_id, job, _verdict in _resume_members(job_dir, spec))

# ---------------------------------------------------------------- P13 恢复/换裁判/回滚

#: 换裁判事件的唯一确认字段：它不是三选一里的一个选项，而是一次新的授权，
#: 因此不混进 ``v2_session.EVENT_FIELDS``（那条合同管的是入口三问）。
SWITCH_EVENT_FIELDS: tuple[str, ...] = ("confirm_judge_switch",)
SWITCH_EVENT_KEYS: tuple[str, ...] = ("source", "request_id", "fields")
SWITCH_SOURCE_USER = "user"
#: 允许换裁判的状态。**RUNNING / WAIT_PATCH_CONFIRMATION 不在内**：正在跑或正在
#: 等逐项接受时改裁判＝静默换裁判，必须先由具名 trigger 暂停。
SWITCHABLE_STATES = frozenset(("WAIT_MODE", "WAIT_JEV", "WAIT_JEV_KEY",
                               "WAIT_TASK_CONFIRMATION", "READY",
                               "PAUSED_JUDGE_UNAVAILABLE", "COMPLETED", "FAILED"))
#: 还需要还原的账本状态：committed 是未回滚，failed 是上次还原没成功（仍可重试）。
RESTORE_TARGET_STATUSES: tuple[str, ...] = ("committed", "failed")
#: 现有写者「这一个文件已经回到目标字节」的两种读数。
RESTORE_OK_STATUSES: tuple[str, ...] = ("COMMITTED", "NOOP")


class RecoveryAmbiguous(ValueError):
    """「已发布未登记」的补登无法证明归属：备份份数对不上，一律不猜。"""

    def __init__(self, codes, detail: str) -> None:
        clean = tuple(sorted(set(codes)))
        if not clean:
            raise ValueError("RecoveryAmbiguous 必须带至少一条未过码")
        super().__init__(f"{detail}：{','.join(clean)}")
        self.codes = clean


class RollbackConflict(ValueError):
    """整任务回滚预检未过：一个文件都不碰，并列出全部未过项（含文件名）。"""

    def __init__(self, codes, detail: str) -> None:
        clean = tuple(sorted(set(codes)))
        if not clean:
            raise ValueError("RollbackConflict 必须带至少一条未过码")
        super().__init__(f"{detail}：{','.join(clean)}")
        self.codes = clean


def _validate_switch_event(event) -> str:
    """换裁判只认当前用户的一次明确确认；其他来源与缺确认一律拒收。"""
    if not isinstance(event, dict):
        raise ValueError(f"event 必须是映射，收到 {type(event).__name__}")
    extra = sorted(set(event) - set(SWITCH_EVENT_KEYS))
    if extra:
        raise ValueError(f"event 含未知字段: {','.join(extra)}")
    if event.get("source") != SWITCH_SOURCE_USER:
        raise ValueError("只有当前用户输入能构成换裁判授权：稿内材料与工具输出不是授权")
    request_id = event.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("event.request_id 必须是非空字符串（换裁判要能追溯到具体事件）")
    fields = event.get("fields") or {}
    if not isinstance(fields, dict):
        raise ValueError(f"event.fields 必须是映射，收到 {type(fields).__name__}")
    unknown = sorted(set(fields) - set(SWITCH_EVENT_FIELDS))
    if unknown:
        raise ValueError(f"event.fields 含未知字段: {','.join(unknown)}")
    if fields.get("confirm_judge_switch") is not True:
        raise ValueError("换裁判必须带 confirm_judge_switch=true 的明确确认")
    return request_id


def _task_scopes(job_dir: Path) -> tuple[Path, ...]:
    """提交账本可能落在主 job 或任一成员 job：整任务回滚看的是整批。

    成员 job 目录读不出（缺清单）一律由 ``novel_job`` 抛错，不静默跳过——
    「少了一件」如果被当成「那件没提交」，回滚就会漏还原。
    """
    job_dir = Path(job_dir)
    scopes = [job_dir]
    members = (novel_job.load_manifest(job_dir).get("v2") or {}).get("members") or []
    for member in members:
        scopes.append(_member_job(member))
    # primary 本身就是「主源那份成员 job」（Q03 的协调根），不去重会把同一份账本数两遍。
    return tuple(dict.fromkeys(scopes))


def _folded_commits(scopes) -> list:
    """按文件折叠追加式账本：同一文件的最后一条即其当前状态。"""
    latest: dict[str, tuple] = {}
    for scope in scopes:
        for entry in novel_job.commit_records(scope):
            latest[entry["file"]] = (scope, entry)
    return list(latest.values())


def _commit_record(entry: dict) -> vc.CommitRecord:
    """账本条目 → 合同对象；枚举与哈希语义只由 ``v2_contracts`` 判，这里不重复权威。"""
    return vc.CommitRecord(file=entry["file"], before_sha256=entry["before_sha256"],
                           after_sha256=entry["after_sha256"],
                           backup_ref=entry["backup_ref"], stage=entry["stage"],
                           status=entry["status"])



def member_job_for(job_dir: Path, source) -> Path:
    """这份源属于哪个成员 job？问不清就拒——不回退到主 job，也不写到没登记过的目录。"""
    job_dir = Path(job_dir)
    members = (novel_job.load_manifest(job_dir).get("v2") or {}).get("members") or []
    target = _canonical_source_id(source)
    for member in members:
        if _canonical_source_id(member["source_id"]) == target:
            job = _member_job(member)
            if job.is_dir():
                return job
            raise ValueError(f"成员 {target} 的 job 目录不在盘上：{job}")
    raise ValueError(f"来源 {target} 在本任务里没有成员 job：拒绝据此提交正文")


def commit_task(job_dir: Path, *, request, task: vc.TaskSpec,
                session: vc.SessionRecord, patches: tuple, bundle, channel: str,
                output_dir=None, mixed_newline_authorization: str | None = None,
                reasons: list | None = None):
    """生产提交入口：真实任务与成员 → 现有通路 → 初次逐文件登记。

    提交前先把「这份 job 是不是这个任务、这个会话是不是登记过的那份、这份源有没有
    成员 job」三件事问清；三件事之后才轮到 v2_preview 的门（请求同一性、通道值域、
    接受与验证、bundle 一致）。恢复身份取自成员 job 自己的登记，不由调用方随口给。
    """
    import v2_preview

    job_dir = Path(job_dir)
    require_no_pending_update(job_dir, "提交正文")
    stored = load_spec(job_dir)
    if stored != task:
        raise ValueError("传入的任务规格与 job 已登记的规格不一致：提交不接管另一套任务")
    stored_session = load_session(job_dir)
    if stored_session != session:
        raise ValueError("传入的会话与 job 已登记的会话不一致：授权必须成对，"
                         "先按登记过的会话重新加载再提交")
    member = member_job_for(job_dir, request.source)
    identity = novel_job.load_identity(member)
    resume_identity = {"strength": identity.strength,
                       "package_sha256": identity.package_sha256,
                       "rules_version": identity.rules_version,
                       "model_id": identity.model_id}
    return v2_preview.commit_verified(
        request, task=task, session=session, patches=patches, bundle=bundle,
        job_dir=member, channel=channel, output_dir=output_dir,
        mixed_newline_authorization=mixed_newline_authorization,
        resume_identity=resume_identity, reasons=reasons)


#: 未收尾交付的形状（发现、阻止重复与报告共用这一份分类，不各写一套判据）。
#: 写成未收尾 / 已收尾未登记 / 旧意图缺交付定位信息 / 意图记了但一个字节都没写成。
RECOVERABLE_SHAPES = ("written_unfinalized", "finalized_unregistered", "locator_missing")


def _intent_is_settled(facts, intent, output_sha: str) -> bool:
    """这条意图已经有对应的提交事实了吗？

    新账按**稳定意图身份**认——同前态的两次不同意图（另一版候选、另一个裁判纪元）不能
    互相顶掉。旧事实没有身份字段，只能按「同一前态字节 ＋ 等于任务记着的那份交付摘要」
    这一对来认；单看前态哈希会把两次交付并成一条（上一轮就是这么误报的）。
    """
    iid = str(intent.get("intent_id") or "")
    for fact in facts:
        fid = str(fact.get("intent_id") or "")
        if iid and fid == iid:
            return True
        if (not fid
                and fact.get("before_sha256") == intent.get("before_sha256")
                and (not output_sha or fact.get("after_sha256") == output_sha)):
            return True
    return False


#: 内联粘贴快照的默认名：只写进调用方指定的受管工作区，不写用户源目录或输出目录。
INLINE_SNAPSHOT_NAME = "内联粘贴.txt"


def materialize_inline(text: str, *, workspace: Path,
                       name: str = INLINE_SNAPSHOT_NAME) -> Path:
    """把用户粘贴的正文落成**受管原始来源快照**，返回路径供任务规格引用。

    这是内联任务进现有授权门的唯一入口：没有来源就没有任务，也没有摘要可确认。
    只写原始输入本身（UTF-8 逐字），不写任何生成稿；目标已存在就拒，不覆盖。
    落点必须解析进 workspace 之内——逃出去就是写进用户的源目录或输出目录。
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("内联正文必须是非空字符串：空白粘贴不构成任务来源")
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise ValueError(f"受管工作区目录不存在：{root}")
    target = (root / name).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"内联快照落在受管工作区之外：{target}")
    if target.exists():
        raise ValueError(f"内联快照目标已存在，拒绝覆盖：{target}")
    target.write_bytes(text.encode("utf-8"))
    return target


def unfinished_deliveries(job_dir: Path) -> list:
    """每条还没落成提交事实的交付意图，按形状分类返回。

    判据来自现有事件层的三条账（意图 → 交付 → 事实），不再把「状态是不是 published」
    当成存在已写盘事实的唯一条件——独立复核那份反例正是停在非发布状态上把正文改掉了，
    而恢复面报 0，等于把已发生的事当成没发生。发现不等于批准：这里只报事实与所缺证据。
    """
    found: list = []
    for scope in _task_scopes(job_dir):
        state = novel_job.load_state(scope)
        facts = novel_job.commit_records(scope)
        deliveries = {str(item.get("intent_id") or ""): item
                      for item in novel_job.commit_deliveries(scope)}
        output_sha = str((state.get("output") or {}).get("sha256") or "")
        for intent in novel_job.commit_intents(scope):
            iid = str(intent.get("intent_id") or "")
            if _intent_is_settled(facts, intent, output_sha):
                continue
            entry = {"intent_id": iid, "source": intent["file"],
                     "channel": intent["channel"], "task_id": intent["task_id"],
                     "before_sha256": intent["before_sha256"],
                     "candidate_sha256": intent["candidate_sha256"],
                     "stage": intent["stage"], "judge_epoch": intent["judge_epoch"],
                     "judge_provider": intent["judge_provider"],
                     "judge_model": intent["judge_model"],
                     "job_dir": scope, "job_state": state.get("state")}
            delivery = deliveries.get(iid) if iid else None
            if delivery is not None:
                entry.update(
                    shape=("finalized_unregistered" if state.get("state") == "published"
                           else "written_unfinalized"),
                    delivered=delivery["delivered"],
                    backup_ref=delivery["backup_ref"],
                    after_sha256=delivery["after_sha256"],
                    delivered_text_sha256=delivery["delivered_text_sha256"],
                    output_dir=delivery["output_dir"])
            elif iid:
                entry.update(shape="attempted_not_written", delivered=None,
                             backup_ref="", after_sha256="",
                             delivered_text_sha256="",
                             output_dir=str(intent.get("output_dir") or ""))
            else:
                entry.update(shape="locator_missing", delivered=None, backup_ref="",
                             after_sha256="", delivered_text_sha256="", output_dir="")
            found.append(entry)
    return found


def pending_registrations(job_dir: Path) -> list:
    """恢复面要处理的未收尾交付（可重试的「一个字节都没写成」不在内）。"""
    return [item for item in unfinished_deliveries(job_dir)
            if item["shape"] in RECOVERABLE_SHAPES]


def _finalise_delivery(scope: Path, delivered: Path, digest: str, channel: str) -> str | None:
    """用现有状态机把「已写成未收尾」的交付收尾；锁归别人或推进失败就返回问题码。

    收尾走 ``novel_output._finalize_job`` 同一条路（先入册再清理候选），不伪造 published，
    也不在这里另写一份状态文件。
    """
    import novel_output

    if not novel_job._takeover_lock(scope, novel_job.load_identity(scope)):
        return f"lock_held:{scope.name}"
    reasons: list = []
    novel_output._finalize_job(scope, Path(delivered).name, digest, channel, reasons)
    if novel_job.load_state(scope).get("state") != "published":
        return f"finalise_failed:{scope.name}"
    return None


def register_pending_commits(job_dir: Path) -> tuple:
    """把未收尾的交付补成逐文件提交事实——路径与备份只认写者当场记下的那份。

    上一版在这里靠「目标旁边等于前态摘要的 ``.bak*``」反推归属：那既是猜名字，也盖不住
    显式输出目录与冲突序号两种真实形状。现在交付事实由写者在落盘之后、终态之前亲手记下，
    本函数只做四件事，任何一件证据不成立都进 problems 并抛 :class:`RecoveryAmbiguous`，
    稿件一个字节不碰：

      1. 交付件确实在盘上，且字节摘要等于记录（不等就是用户后改，不据它补登）；
      2. 有备份的（覆盖通道）备份文件确实在，且字节等于提交前摘要；
      3. 「写成未收尾」的先走现有状态机收尾（锁归别人就拒），不在账本里留悬案；
      4. 按稳定意图身份登记事实，重复调用不再追加第二条。
    """
    job_dir = Path(job_dir)
    require_no_pending_update(job_dir, "补登提交记录")
    results = []
    problems: list[str] = []
    details: list[str] = []
    ready: list[tuple[Path, dict, str, str]] = []
    # 先全批预检，再动手：一件证据不成立就整批不登记——否则会出现「先好的那件被记了，
    # 后坏的那件把整批卡住」这种半本账，续行时也无从判断哪些已经做过（与回滚同一口径）。
    for pending in pending_registrations(job_dir):
        scope = Path(pending["job_dir"])
        if pending["shape"] == "locator_missing":
            problems.append(f"intent_without_delivery:{pending['source']}")
            details.append("这条提交意图没有交付定位信息（旧版本事件）：不扫目录猜哪份是"
                           "它的交付，恢复需要人工按源路径与备份确认后再登记")
            continue
        delivered = Path(pending["delivered"])
        if not delivered.is_file():
            problems.append(f"delivered_missing:{delivered}")
            details.append("交付事实说写成过，但盘上找不到那份文件")
            continue
        digest = _sha256_of(delivered)
        if digest != pending["after_sha256"]:
            problems.append(f"user_edited:{delivered}")
            details.append(f"交付文件当前摘要 {digest[:12]}… 与记录里的 "
                           f"{pending['after_sha256'][:12]}… 不符：已被后续改动，不据它补登")
            continue
        if pending["backup_ref"]:
            backup = Path(pending["backup_ref"])
            if not backup.is_file():
                problems.append(f"backup_missing:{backup}")
                details.append("交付事实记着备份，但那份备份不在盘上")
                continue
            if _sha256_of(backup) != pending["before_sha256"]:
                problems.append(f"backup_mismatch:{backup}")
                details.append("备份字节与登记的前态摘要对不上")
                continue
        ready.append((scope, pending, digest,
                      pending["backup_ref"] or vc.CREATED_BACKUP_REF))
    if problems:
        raise RecoveryAmbiguous(problems, "补登提交记录未通过：一个字节都不碰，账本一条都不记（"
                                          + "；".join(details) + "）")
    for scope, pending, digest, backup_ref in ready:
        if pending["shape"] == "written_unfinalized":
            problem = _finalise_delivery(scope, Path(pending["delivered"]), digest,
                                         pending["channel"])
            if problem:
                # 收尾失败不再往下记：这条交付仍是「写成未收尾」，下次调用还能看见
                problems.append(problem)
                details.append(f"{scope.name} 收尾未通过：状态机没走通就没有可登记的事实")
                continue
        record = {"file": pending["delivered"], "before_sha256": pending["before_sha256"],
                  "after_sha256": digest, "backup_ref": backup_ref,
                  "stage": pending["stage"], "status": "committed",
                  "judge_epoch": pending["judge_epoch"],
                  "judge_provider": pending["judge_provider"],
                  "judge_model": pending["judge_model"],
                  "intent_id": pending["intent_id"]}
        novel_job.record_commit(scope, record)
        results.append(_commit_record(record))
    if problems:
        raise RecoveryAmbiguous(problems, "补登中途失败：已登记的不回退，剩下的下次接着补（"
                                          + "；".join(details) + "）")
    return tuple(results)

def resume_task(job_dir: Path, *, current_spec: vc.TaskSpec) -> dict:
    """从现有 job 恢复任务：逐成员核对身份与源快照，已提交件一个字节都不重写。

    - 半更新的授权先由 ``require_no_pending_update`` 挡下（必须先显式 recover）；
    - 传入规格与 job 登记规格不一致 → 拒绝，恢复不接管另一套任务；
    - 每件成员的结论直接取自现有 ``resume_job``：已发布且交付记录重推导通过 =
      ``complete``，该路径在任何写盘之前就返回，所以「已提交件不被重写」不是靠
      事后比对，而是靠复用这条判定顺序；
    - ``committed`` 是账本里逐文件事实的合同视图（含已回滚件，如实带状态）。
    """
    job_dir = Path(job_dir)
    require_no_pending_update(job_dir, "恢复任务")
    stored = load_spec(job_dir)
    if stored != current_spec:
        raise ValueError("传入的任务规格与 job 已登记的规格不一致："
                         "恢复不接管另一套任务，请按实际内容重新确认或另起任务")
    members = _resume_members(job_dir, current_spec)
    session = load_session(job_dir)
    scopes = (job_dir,) + tuple(job for _source_id, job, _verdict in members)
    pending = pending_registrations(job_dir)
    return {"verdicts": {source_id: verdict for source_id, _job, verdict in members},
            "member_jobs": tuple(job for _source_id, job, _verdict in members),
            "committed": tuple(_commit_record(entry)
                               for _scope, entry in _folded_commits(scopes)),
            "pending_registrations": tuple(pending),
            "judge_epoch": session.judge_epoch}


def switch_judge(job_dir: Path, event: dict, *,
                 new_judge_ref: dict) -> vc.SessionRecord:
    """用户明确换裁判：纪元 +1，旧纪元的判定缓存清理，旧授权与凭据一并失效。

    四件事分开做，谁都不替代谁：
      1. 授权——只认当前用户带 ``confirm_judge_switch`` 的事件；RUNNING/预览等待中
         拒绝，不在跑动中静默改裁判；
      2. 规格——裁判绑定写进任务内容，摘要必然变（旧确认随之失效）；
      3. 会话——具名 trigger 推进（READY 先失效摘要；启用 Jev 时回到等凭据），
         ``credential_receipt`` 清空：换裁判后的凭据必须重新验证；
      4. 缓存——按登记的所属纪元清掉旧裁判条目；**已提交件的账本一字不动**，
         旧裁判溯源是历史事实，不是待清理项。
    """
    import v2_cache
    import v2_session as vs

    job_dir = Path(job_dir)
    require_no_pending_update(job_dir, "更换裁判")
    request_id = _validate_switch_event(event)
    if not isinstance(new_judge_ref, dict) or not new_judge_ref:
        raise ValueError(f"new_judge_ref 必须是非空映射，收到 {new_judge_ref!r}")
    spec = load_spec(job_dir)
    record = load_session(job_dir)
    if record.interaction_state not in SWITCHABLE_STATES:
        raise ValueError(f"当前状态 {record.interaction_state} 不允许换裁判："
                         "任务正在执行或正在等逐项接受，先暂停再换")
    content = vc.content_of_task_spec(spec)
    content["judge_ref"] = dict(new_judge_ref)
    new_spec = vc.form_task_spec(content)     # 裁判绑定不合形状当场拒
    host = vs.build_machine(record.interaction_state)
    state = record.interaction_state
    if host.trigger("invalidate_summary"):
        state = host.state
    if state == "WAIT_TASK_CONFIRMATION" and new_spec.choices.jev == "enabled":
        if host.trigger("require_credential"):
            state = host.state
    session = replace(record, interaction_state=state,
                      judge_epoch=record.judge_epoch + 1,
                      summary_digest=None, confirmed_revision=None,
                      credential_receipt=None,
                      authorization_event_refs=record.authorization_event_refs
                      + (request_id,))
    persist_task_session(job_dir, new_spec, session,
                         expected_previous_digest=spec.request_digest)
    discarded = v2_cache.discard_stale(job_dir, keep_epoch=session.judge_epoch)
    novel_job._append_event(job_dir, "judge_switched", detail={
        "from": dict(spec.judge_ref), "to": dict(new_judge_ref),
        "judge_epoch": session.judge_epoch, "cache": discarded,
        "request_id": request_id})
    return session


def _rollback_problems(entry: dict) -> list:
    """单件预检：目标还在且仍等于提交后的字节、备份还在且仍等于提交前的字节。"""
    file_path = Path(entry["file"])
    backup = Path(entry["backup_ref"])
    codes: list[str] = []
    if not file_path.is_file():
        codes.append(f"target_missing:{entry['file']}")
    elif _sha256_of(file_path) != entry["after_sha256"]:
        codes.append(f"user_edited:{entry['file']}")
    if entry["backup_ref"] == vc.CREATED_BACKUP_REF:
        # 另存交付没有前态也没有备份：撤销它意味着删掉一份交付件，本层不假装能做。
        codes.append(f"save_as_not_rollbackable:{entry['file']}")
        return codes
    if not backup.is_file():
        codes.append(f"backup_missing:{entry['file']}")
    elif _sha256_of(backup) != entry["before_sha256"]:
        codes.append(f"backup_hash_mismatch:{entry['file']}")
    return codes


def _restore_committed_file(entry: dict) -> str:
    """还原一个文件：只走现有安全提交（唯一写者），本层不做第二套原子交换。"""
    import file_commit

    try:
        payload = Path(entry["backup_ref"]).read_bytes()
        outcome = file_commit.SafeFileCommitEngine.overwrite_atomically(
            Path(entry["file"]), payload, expected_sha256=entry["after_sha256"])
    except OSError:
        return "failed"
    return ("rolled_back" if outcome.get("status") in RESTORE_OK_STATUSES
            else "failed")


def rollback_task(job_dir: Path) -> tuple[vc.CommitRecord, ...]:
    """整任务回滚：先核全批，再逐件还原，状态逐件入账——不宣称一次原子交换。

    - **先全批预检**：任何一件对不上（用户后续编辑、目标消失、备份缺失或被换）
      → 抛 :class:`RollbackConflict`，**一个文件都不碰**，不做「先恢复能恢复的」；
    - **没有账本就拒绝**：不扫目录猜哪份 ``.bak`` 属于哪次提交，备份归属只能由
      提交时登记的记录说话；
    - **逐件还原**：一件失败不牵动已完成件，账本如实记下 ``failed``，下次调用只
      处理还没完成的件（中断后重跑安全，已完成件不被第二次覆盖）；
    - 全部已完成时返回空元组（幂等 no-op）。
    """
    job_dir = Path(job_dir)
    require_no_pending_update(job_dir, "回滚任务")
    entries = _folded_commits(_task_scopes(job_dir))
    if not entries:
        raise RollbackConflict(("no_committed_record",),
                               "本任务没有逐文件提交记录：备份归属猜不得，"
                               "不扫目录找备份")
    batch = [(scope, entry) for scope, entry in entries
             if entry["status"] in RESTORE_TARGET_STATUSES]
    if not batch:
        return ()
    problems = [code for _scope, entry in batch for code in _rollback_problems(entry)]
    if problems:
        raise RollbackConflict(problems, "整批预检未通过：一个文件都不碰")
    results: list[vc.CommitRecord] = []
    for scope, entry in batch:
        record = dict(entry, status=_restore_committed_file(entry))
        novel_job.record_commit(scope, record)
        results.append(_commit_record(record))
    return tuple(results)
