#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第一集旧库非破坏迁移预演器（阶段 7 工单 7.1，规格十二）。

三条命令，三件事，互不越权：

- `scan`：**只读**登记库内图片（相对路径、字节数、SHA-256、魔数 MIME、像素宽高），
  再为计划里的每项任务算候选匹配分数。**歧义不自动绑定**：同分或低于阈值一律标 `AMBIGUOUS`，
  留给人工/主模型决定。
- `apply`：只对**唯一匹配**的候选做复制——复制进任务轮次目录并登记为历史候选。
  绝不覆盖、移动、重命名、删除用户原文件；绝不自动通过（登记后状态为「需要重新检查」）。
- `verify`：与迁移前清单逐条比对，确认原始路径与哈希未变、已迁移副本与原图一致。

用法：
    python scripts/migrate_visual_library.py scan   --lib <资产库> --out 迁移提案.json
    python scripts/migrate_visual_library.py apply  --lib <资产库> --proposal 迁移提案.json
    python scripts/migrate_visual_library.py verify --lib <资产库> --baseline 迁移前清单.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import replace
from pathlib import Path

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))
sys.path.insert(0, str(SKILL_DIR / "scripts" / "engine"))
sys.path.insert(0, str(SKILL_DIR))

from engine.visual_schema import (  # noqa: E402
    ASPECT_BY_KIND,
    load_visual_plan,
    normalize_rel_path,
    write_visual_plan,
)
from engine.visual_store import StoreError, import_manual_result  # noqa: E402

SCHEMA_VERSION = 1
PLAN_REL = ("视觉资产", "计划.toml")
MIGRATION_LOG_REL = ("视觉资产", "迁移记录.json")
ASSET_DIR = "视觉资产"
INPUT_DIR = "参考输入"
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")

# 候选打分阈值：达到阈值且**严格唯一**才自动绑定
MATCH_THRESHOLD = 50
# 信号下限：低于此分只说明「画幅相像」之类，不构成身份证据 → 记未匹配而不是歧义
SIGNAL_FLOOR = 30
SCORE_EXISTING_RECORD = 100   # 计划里已引用过这张图：最强证据
SCORE_ASSET_IN_PATH = 30      # 路径里出现资产名（或其中文名片段）
SCORE_PRIMARY = 40            # 命中该任务种类的主词（方向 / 部位）
SCORE_BONUS = 10              # 正式产物加分（高精度CG / 正式 / 最终）
SCORE_CONFLICT = -40          # 命中方向冲突词（例如「全身正面」任务撞上「背面」）
SCORE_ASPECT_CLOSE = 5        # 画幅接近该任务的期望比例

# 任务种类 → 主词 / 加分词 / 冲突词（对着真实老库的命名习惯写，见工单 7.1）。
# 老库文件名同时编码「资产 + 方向或部位 + 质量 + 画幅」，方向词是区分同类多图的关键；
# 命中冲突词必须把候选压到信号下限以下，否则「背面」会跟「正面」并列变成歧义。
FORMAL_WORDS = ("正式", "高精度CG", "最终")
KIND_PRIMARY = {
    "正脸": ("正脸", "身份近景"),
    "后脑": ("后脑", "后脑勺"),
    "服装独立正面": ("独立正面",),
    "服装独立背面": ("独立背面",),
    "人物全身正面": ("全身立绘", "正面立绘"),
    "人物全身背面": ("背面正式立绘", "背面立绘", "全身背面"),
    "场景母版": ("场景母版", "全景"),
    "道具": ("道具参考", "材质参考"),
    "剧情剧照": ("剧照", "镜头"),
    "动作参考": ("动作", "action"),
    "材质特写": ("材质", "特写"),
}
KIND_BONUS = {
    "服装独立正面": FORMAL_WORDS + ("服装",),
    "服装独立背面": FORMAL_WORDS + ("服装",),
}
KIND_CONFLICT = {
    "正脸": ("后脑", "全身", "独立正面", "独立背面", "落难", "脸部特写"),
    "后脑": ("正脸", "全身", "独立正面", "独立背面", "落难"),
    "服装独立正面": ("独立背面", "全身", "正脸", "后脑", "落难"),
    "服装独立背面": ("独立正面", "全身", "正脸", "后脑", "落难"),
    "人物全身正面": ("背面", "独立正面", "独立背面", "后脑", "落难"),
    "人物全身背面": ("独立正面", "独立背面", "后脑", "落难", "正脸"),
    "场景母版": ("人物", "立绘"),
    "道具": ("人物", "立绘"),
    "剧情剧照": ("立绘", "场景母版", "道具参考"),
    "动作参考": ("立绘", "场景母版"),
    "材质特写": ("立绘", "场景母版"),
}


def _bonus_words(kind: str) -> tuple:
    return KIND_BONUS.get(kind, FORMAL_WORDS)


MIGRATION_PROPOSAL_UNREADABLE = "MIGRATION_PROPOSAL_UNREADABLE"
MIGRATION_BASELINE_UNREADABLE = "MIGRATION_BASELINE_UNREADABLE"
MIGRATION_PLAN_MISSING = "MIGRATION_PLAN_MISSING"
MIGRATION_SOURCE_MISSING = "MIGRATION_SOURCE_MISSING"
MIGRATION_SOURCE_CHANGED = "MIGRATION_SOURCE_CHANGED"
MIGRATION_COPY_MISMATCH = "MIGRATION_COPY_MISMATCH"


class MigrationError(RuntimeError):
    """迁移失败：code 为稳定原因码。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}：{detail}" if detail else code)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sniff_mime(data: bytes) -> str:
    """按魔数嗅探（不看扩展名，老库里有伪装后缀的历史文件）。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _image_dimensions(data: bytes) -> tuple[int, int]:
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            return int(img.width), int(img.height)
    except Exception:  # noqa: BLE001 - 旧库可能有坏图：登记为空尺寸而不是崩掉
        return 0, 0


def _iter_images(lib: Path):
    """递归列出库内图片（只读）。"""
    for path in sorted(lib.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        yield path


def _bucket_of(rel: str) -> str:
    head = rel.split("/", 1)[0]
    return head if head in (ASSET_DIR, INPUT_DIR) else "其他"


def scan_library(lib: Path) -> dict:
    """只读扫描：登记图片 + 为每项任务给出候选与判定（不写任何文件）。"""
    lib = Path(lib)
    files: list[dict] = []
    for path in _iter_images(lib):
        data = path.read_bytes()
        rel = path.relative_to(lib).as_posix()
        width, height = _image_dimensions(data)
        files.append({"path": rel, "bytes": len(data), "sha256": _sha256(data),
                      "mime": sniff_mime(data), "width": width, "height": height})

    buckets: dict[str, int] = {}
    for row in files:
        name = _bucket_of(row["path"])
        buckets[name] = buckets.get(name, 0) + 1

    plan_path = lib / PLAN_REL[0] / PLAN_REL[1]
    tasks: list[dict] = []
    if plan_path.is_file():
        plan = load_visual_plan(plan_path)
        tasks = [_match_task(task, files) for task in plan.sorted_tasks()]

    counts = {"images": len(files), "tasks": len(tasks),
              "matched": sum(1 for t in tasks if t["decision"] == "MATCHED"),
              "ambiguous": sum(1 for t in tasks if t["decision"] == "AMBIGUOUS"),
              "unmatched": sum(1 for t in tasks if t["decision"] == "UNMATCHED")}
    return {"schema_version": SCHEMA_VERSION, "library_label": lib.name,
            "scan_mode": "read_only", "file_count": len(files), "buckets": buckets,
            "files": files, "tasks": tasks, "counts": counts}


_NAME_BOUNDARY = r"(?![A-Za-z0-9一-鿿])"


def _has_asset_token(path: str, token: str) -> bool:
    """资产名必须落在「名字边界」上：`栓子` 命中 `栓子_正脸.png`，但不命中 `栓子娘_正脸.png`。"""
    if not token:
        return False
    return re.search(re.escape(token) + _NAME_BOUNDARY, path) is not None


def _score_candidate(task, row: dict, aspect: str = "") -> tuple[int, list[str]]:
    """给一个候选打分：方向感知（主词加分、冲突词扣分），分数可解释。"""
    score = 0
    reasons: list[str] = []
    rel = row["path"]
    lower = rel.lower()

    referenced = {normalize_rel_path(a.image) for a in task.attempts if a.image}
    if rel in referenced:
        score += SCORE_EXISTING_RECORD
        reasons.append("计划已引用该图")

    asset_token = str(task.asset_id).split("_", 1)[-1]
    for token in (str(task.asset_id), asset_token):
        if _has_asset_token(rel, token):
            score += SCORE_ASSET_IN_PATH
            reasons.append(f"路径含资产名 {token}")
            break

    for keyword in KIND_PRIMARY.get(task.kind, ()):
        if keyword.lower() in lower:
            score += SCORE_PRIMARY
            reasons.append(f"命中主词 {keyword}")
            break
    for keyword in _bonus_words(task.kind):
        if keyword.lower() in lower:
            score += SCORE_BONUS
            reasons.append(f"正式产物关键词 {keyword}")
            break
    for keyword in KIND_CONFLICT.get(task.kind, ()):
        if keyword.lower() in lower:
            score += SCORE_CONFLICT
            reasons.append(f"方向冲突词 {keyword}")
            break

    if aspect and row.get("width") and row.get("height"):
        want_w, _, want_h = aspect.partition(":")
        try:
            target = float(want_w) / float(want_h)
            actual = float(row["width"]) / float(row["height"])
        except (TypeError, ValueError, ZeroDivisionError):
            target = actual = 0.0
        if target and abs(actual - target) / target <= 0.2:
            score += SCORE_ASPECT_CLOSE
            reasons.append("画幅接近该任务的期望比例")
    return score, reasons


def _match_task(task, files: list[dict]) -> dict:
    """给一项任务挑候选；只有「达到阈值且严格唯一」才自动绑定。"""
    aspect = str(getattr(task, "aspect_ratio", "") or "") or ASPECT_BY_KIND.get(
        task.kind, "")
    candidates = []
    for row in files:
        if _bucket_of(row["path"]) != ASSET_DIR:
            continue  # 参考输入是用户输入，不是正式资产
        score, reasons = _score_candidate(task, row, aspect)
        if score > 0:
            candidates.append({"path": row["path"], "score": score, "reasons": reasons})
    candidates.sort(key=lambda c: (-c["score"], c["path"]))

    decision = "UNMATCHED"
    chosen = ""
    # 能参与判定的候选必须「认得出是这个资产」：路径里出现该资产名（或计划已引用）。
    # 只靠方向词与画幅命中的候选属于「同类图」，认不出是谁，不得参与自动绑定。
    meaningful = [c for c in candidates
                  if c["score"] >= SIGNAL_FLOOR
                  and any(r.startswith(("路径含资产名", "计划已引用"))
                          for r in c["reasons"])]
    if meaningful:
        top = meaningful[0]
        tied = [c for c in meaningful[1:] if c["score"] == top["score"]]
        if top["score"] >= MATCH_THRESHOLD and not tied:
            decision = "MATCHED"
            chosen = top["path"]
        else:
            decision = "AMBIGUOUS"
    return {"task_id": task.task_id, "asset_id": task.asset_id, "kind": task.kind,
            "status": task.status, "decision": decision, "chosen": chosen,
            "candidates": candidates}


def _load_proposal(proposal: dict | Path) -> dict:
    if isinstance(proposal, dict):
        data = proposal
    else:
        path = Path(proposal)
        if not path.is_file():
            raise MigrationError(MIGRATION_PROPOSAL_UNREADABLE, f"提案不存在：{path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MigrationError(MIGRATION_PROPOSAL_UNREADABLE,
                                 f"提案不可读：{type(exc).__name__}") from exc
    if not isinstance(data, dict) or data.get("scan_mode") != "read_only":
        raise MigrationError(MIGRATION_PROPOSAL_UNREADABLE,
                             "提案必须来自 scan（缺 scan_mode = read_only）")
    if not isinstance(data.get("tasks"), list):
        raise MigrationError(MIGRATION_PROPOSAL_UNREADABLE, "提案缺少 tasks")
    return data


def _next_round_manifest(task, source_rel: str, sha: str, mime: str) -> dict:
    return {"task_id": task.task_id, "source": "migration",
            "source_path": source_rel, "source_sha256": sha,
            "source_mime": mime, "note": "旧库非破坏迁移：原文件未改动"}


def apply_proposal(lib: Path, proposal: dict | Path) -> dict:
    """把唯一匹配的候选复制进任务轮次目录并登记为历史候选（非破坏、不自动通过）。"""
    lib = Path(lib)
    data = _load_proposal(proposal)
    plan_path = lib / PLAN_REL[0] / PLAN_REL[1]
    if not plan_path.is_file():
        raise MigrationError(MIGRATION_PLAN_MISSING, f"视觉计划不存在：{plan_path}")
    plan = load_visual_plan(plan_path)

    files_by_path = {row["path"]: row for row in data.get("files", [])
                     if isinstance(row, dict)}
    applied: list[dict] = []
    skipped: list[dict] = []
    already = 0

    for row in data["tasks"]:
        if not isinstance(row, dict):
            continue
        task_id = str(row.get("task_id") or "")
        task = plan.by_id(task_id)
        if task is None:
            skipped.append({"task_id": task_id, "reason": "计划里找不到该任务"})
            continue
        source_rel = str(row.get("chosen") or "")
        if row.get("decision") != "MATCHED" or not source_rel:
            skipped.append({"task_id": task_id, "reason": str(row.get("decision"))})
            continue
        source = lib / source_rel
        if not source.is_file():
            skipped.append({"task_id": task_id, "reason": "候选原文件不存在"})
            continue
        info = files_by_path.get(source_rel, {})
        digest = _sha256(source.read_bytes())
        if any(a.image_sha256 == digest for a in task.attempts):
            already += 1
            continue
        try:
            record = import_manual_result(
                lib, task_id, source, existing=task.attempts,
                request_manifest=_next_round_manifest(task, source_rel, digest,
                                                      str(info.get("mime", ""))))
        except StoreError as exc:
            skipped.append({"task_id": task_id, "reason": exc.code})
            continue
        # 登记为历史候选：等主模型复审，绝不自动通过（工单 7.2）
        updated = replace(task, attempts=(*task.attempts, record),
                          status="NEEDS_REVIEW",
                          notes=(task.notes or "") + "｜旧库迁移登记，等待主模型复审")
        plan = plan.with_task(updated)
        applied.append({"task_id": task_id, "round": int(record.round),
                        "image": record.image, "sha256": record.image_sha256,
                        "source": source_rel})

    if applied:
        write_visual_plan(plan, plan_path)
    log = {"schema_version": SCHEMA_VERSION, "library_label": lib.name,
           "applied": applied, "skipped": skipped}
    log_path = lib / MIGRATION_LOG_REL[0] / MIGRATION_LOG_REL[1]
    _write_json(log_path, log)
    return {"copied": len(applied), "already": already, "skipped": skipped,
            "applied": applied, "log": MIGRATION_LOG_REL[0] + "/" + MIGRATION_LOG_REL[1]}


def verify_migration(lib: Path, baseline: dict | Path) -> list[dict]:
    """与迁移前清单比对：原文件未变 + 已迁移副本与原图一致。"""
    lib = Path(lib)
    if isinstance(baseline, dict):
        data = baseline
    else:
        path = Path(baseline)
        if not path.is_file():
            raise MigrationError(MIGRATION_BASELINE_UNREADABLE, f"清单不存在：{path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MigrationError(MIGRATION_BASELINE_UNREADABLE,
                                 f"清单不可读：{type(exc).__name__}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        raise MigrationError(MIGRATION_BASELINE_UNREADABLE, "清单缺少 files 列表")

    issues: list[dict] = []
    for row in data["files"]:
        if not isinstance(row, dict):
            continue
        rel = str(row.get("path") or "")
        target = lib / rel
        if not target.is_file():
            issues.append({"code": MIGRATION_SOURCE_MISSING, "path": rel})
            continue
        digest = _sha256(target.read_bytes())
        if str(row.get("sha256") or "") and digest != str(row["sha256"]):
            issues.append({"code": MIGRATION_SOURCE_CHANGED, "path": rel})

    plan_path = lib / PLAN_REL[0] / PLAN_REL[1]
    if plan_path.is_file():
        plan = load_visual_plan(plan_path)
        expected = {row["path"]: row.get("sha256") for row in data["files"]
                    if isinstance(row, dict)}
        for task in plan.sorted_tasks():
            for attempt in task.attempts:
                rel = normalize_rel_path(attempt.image)
                if not rel or "/任务/" not in "/" + rel:
                    continue
                target = lib / rel
                if not target.is_file():
                    issues.append({"code": MIGRATION_SOURCE_MISSING, "path": rel})
                    continue
                digest = _sha256(target.read_bytes())
                if attempt.image_sha256 and digest != attempt.image_sha256:
                    issues.append({"code": MIGRATION_COPY_MISMATCH, "path": rel})
                    continue
                record = _migration_source(lib, task.task_id, rel)
                if record and expected.get(record) and \
                        digest != expected.get(record):
                    issues.append({"code": MIGRATION_COPY_MISMATCH, "path": rel})
    return issues


def _migration_source(lib: Path, task_id: str, image_rel: str) -> str:
    """从迁移记录里查出某轮次对应的原图路径（没有记录就返回空）。"""
    log_path = lib / MIGRATION_LOG_REL[0] / MIGRATION_LOG_REL[1]
    if not log_path.is_file():
        return ""
    try:
        log = json.loads(log_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    for row in log.get("applied", []):
        if isinstance(row, dict) and row.get("task_id") == task_id and \
                row.get("image") == image_rel:
            return str(row.get("source") or "")
    return ""


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="旧库非破坏迁移：scan / apply / verify")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, help_text in (("scan", "只读登记图片并给出候选匹配（不写库内文件）"),
                            ("apply", "把唯一匹配的候选复制进轮次目录（非破坏、不自动通过）"),
                            ("verify", "与迁移前清单比对，确认原文件未变、副本一致")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--lib", required=True)
        if name == "scan":
            p.add_argument("--out", required=True, help="迁移提案 JSON 输出路径")
        if name == "apply":
            p.add_argument("--proposal", required=True, help="scan 产出的迁移提案 JSON")
        if name == "verify":
            p.add_argument("--baseline", required=True, help="迁移前清单 JSON")
    args = parser.parse_args()
    lib = Path(args.lib)

    if args.cmd == "scan":
        proposal = scan_library(lib)
        out = _write_json(Path(args.out), proposal)
        counts = proposal["counts"]
        print(f"✅ 只读扫描完成：图片 {counts['images']} 张｜任务 {counts['tasks']} 项"
              f"（唯一匹配 {counts['matched']}｜歧义 {counts['ambiguous']}｜"
              f"未匹配 {counts['unmatched']}）")
        print(f"   提案已写出：{out}（库内文件未改动）")
        return 0

    if args.cmd == "apply":
        try:
            summary = apply_proposal(lib, Path(args.proposal))
        except MigrationError as e:
            print(f"❌ {e.code}：{e.detail}")
            return 1
        print(f"✅ 迁移登记完成：复制 {summary['copied']} 项｜已登记过 {summary['already']} 项｜"
              f"跳过 {len(summary['skipped'])} 项")
        for row in summary["skipped"]:
            print(f"   ⚠️ 跳过 {row['task_id']}：{row['reason']}")
        print(f"   记录：{summary['log']}（原文件未改动、未自动通过）")
        return 0

    try:
        issues = verify_migration(lib, Path(args.baseline))
    except MigrationError as e:
        print(f"❌ {e.code}：{e.detail}")
        return 1
    if issues:
        print(f"❌ 迁移校验未通过：{len(issues)} 项问题")
        for row in issues:
            print(f"   - {row['code']}：{row['path']}")
        return 1
    print("✅ 迁移校验通过：原文件路径与哈希未变，已迁移副本与原图一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
