"""改稿下游失效链（v1.5.0 T4；方案 §6 编排纪律 + fbs 磁盘契约）。

链条：`script`(定稿剧本) → `plan`(导演规划) → `shots`(镜头组) → `supervision`(逐集监督)。
**上游任一件内容变了，下游整链失效、必须重跑，绝不复用**（Review Focus #1 的要害）。
判据是内容指纹，不是时间戳——每级落盘时把「当时上游各件的指纹」写进自己的
`_meta.upstreamHashes`，`chain_stale()` 就是拿记录与当前上游在库件对账。

指纹口径复用 v1.4.0 `ep_content_hash`（去 `_meta` + 规范化键序的 md5）：**盖章写进 `_meta`
不会改变本件自己的指纹**，链不会自激。

在库路径约定（T7 工作流按此读写，人工重做也照此放）::

    script      → <workdir>/state/episodes/epNNN.json      （v1.4.0 既有）
    plan        → <workdir>/state/plans/epNNN.json
    shots       → <workdir>/state/shotgroups/epNNN.json
    supervision → <workdir>/state/supervision/epNNN.json

「件不存在」与「件失效」是两件事：本模块只管后者；有没有归盘库管。

CLI::

    python -m dramacraft.artifact_chain check  --workdir W --ep N [--stage all] --json R
        退出码 0 = 链新鲜；3 = 有失效环节（R 里列 stale 名单）；1 = 用法错或输入不可用（不落报告）
    python -m dramacraft.artifact_chain emit   --workdir W --ep N --stage plan --json R
        把当前上游指纹盖进本阶段落盘件（本阶段件不存在 = 用法错）；退出码 0 / 1
    python -m dramacraft.artifact_chain supervise --workdir W --ep N --verdict pass|fail --json R [--finding 文本]...
        监督双件落盘：核人读件 `state/supervision/epNNN.md`、写机读件 `epNNN.json` 并在放行时盖章；退出码 0 / 1
    python -m dramacraft.artifact_chain snapshot --json <清单> <文件>...
        对任意文件列表算 sha256 入清单；文件缺失 = 用法错，退出码 0 / 1
    python -m dramacraft.artifact_chain verify   --manifest <清单> --json R
        逐项复核；退出码 0 = 全一致；5 = 有变动/丢失；1 = 用法错或输入不可用
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from dramacraft.continuity_lint import ep_content_hash

STAGES: Tuple[str, ...] = ("plan", "shots", "supervision")

# 阶段中文名（单源）：终端与报告里给人看时一律用这个，不吐 plan/shots 这类英文键名
STAGE_LABELS: Dict[str, str] = {
    "script": "定稿剧本",
    "plan": "导演规划",
    "shots": "镜头组",
    "supervision": "逐集监督",
}

# 每级要盯住的上游件（顺序即依赖顺序）
UPSTREAM: Dict[str, Tuple[str, ...]] = {
    "plan": ("script",),
    "shots": ("script", "plan"),
    "supervision": ("script", "plan", "shots"),
}

# 各级在库位置：stage → (workdir 下的子目录, 文件名模板)
_LAYOUT: Dict[str, Tuple[Tuple[str, ...], str]] = {
    "script": (("state", "episodes"), "ep{nn}.json"),
    "plan": (("state", "plans"), "ep{nn}.json"),
    "shots": (("state", "shotgroups"), "ep{nn}.json"),
    "supervision": (("state", "supervision"), "ep{nn}.json"),
}


class ChainError(Exception):
    """入参/在库件不可用——CLI exit 1（不落报告）。"""


def _as_ep(ep: Any) -> int:
    try:
        n = int(ep)
    except (TypeError, ValueError):
        raise ChainError(f"集号无法解析：{ep}")
    if n < 1:
        raise ChainError(f"集号必须从 1 起：{ep}")
    return n


def artifact_path(workdir: Any, ep: Any, stage: str) -> Path:
    if stage not in _LAYOUT:
        raise ChainError(f"未知阶段：{stage}（可用：{'、'.join(_LAYOUT)}）")
    sub, template = _LAYOUT[stage]
    return Path(str(workdir)).joinpath(*sub, template.format(nn=f"{_as_ep(ep):03d}"))


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise ChainError(f"在库件不存在：{path}")
    try:
        doc = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as err:
        raise ChainError(f"在库件不可解析：{path}（{err}）")
    if not isinstance(doc, dict):
        raise ChainError(f"在库件结构无法识别（应为 JSON 对象）：{path}")
    return doc


def file_content_hash(path: Path) -> str:
    """与剧本侧同一口径的内容指纹：读 JSON、剥 `_meta`、规范化键序。"""
    return ep_content_hash(_read_json(path))


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def emit_chain(workdir: Any, ep: Any, stage: str) -> Dict[str, Any]:
    """把当前上游指纹盖进本阶段落盘件，返回写进去的 `upstreamHashes`。

    调用时机：**本阶段件落盘之后**。上游件缺失即 ChainError——链条断了不能假装盖上。
    """
    if stage not in UPSTREAM:
        raise ChainError(f"阶段 {stage} 没有上游链（可用：{'、'.join(UPSTREAM)}）")
    own = artifact_path(workdir, ep, stage)
    doc = _read_json(own)
    hashes: Dict[str, str] = {}
    for up in UPSTREAM[stage]:
        up_path = artifact_path(workdir, ep, up)
        if not up_path.exists():
            raise ChainError(f"上游件缺失，无法盖章：{up_path}")
        hashes[up] = file_content_hash(up_path)
    meta = doc.get("_meta")
    if not isinstance(meta, dict):
        meta = {}
    meta["upstreamHashes"] = hashes
    doc["_meta"] = meta
    _atomic_write_json(own, doc)
    return hashes


SUPERVISION_SCHEMA_VERSION = 1


def supervision_report_path(workdir: Any, ep: Any) -> Path:
    """监督的人读件：与机读件同目录同名，只差后缀（`epNNN.md`）。"""
    return artifact_path(workdir, ep, "supervision").with_suffix(".md")


def write_supervision_record(workdir: Any, ep: Any, passed: bool, findings: Iterable[Any] = ()) -> Dict[str, Any]:
    """监督放行的**双件落盘**：人读报告必须在库且非空，机读件由本函数写并盖章。

    机读件固定四字段：`ep`、`schemaVersion`、`verdict`、`findings`（放行时另有 `_meta.upstreamHashes`）。
    缺人读报告或报告是空文件 = 监督员没审过，一律 ChainError——绝不写「通过」的空壳。
    未放行时**整份覆写**：上一轮的 pass 件不许留在盘上冒充本轮结论。
    """
    report = supervision_report_path(workdir, ep)
    if not report.exists():
        raise ChainError(f"监督报告（人读件）不存在，不能放行：{report}——监督员先把报告写到这个路径")
    if not report.read_text("utf-8").strip():
        raise ChainError(f"监督报告（人读件）是空文件，不能放行：{report}")
    record: Dict[str, Any] = {
        "ep": _as_ep(ep),
        "schemaVersion": SUPERVISION_SCHEMA_VERSION,
        "verdict": "pass" if passed else "fail",
        "findings": [str(x) for x in (findings or [])],
    }
    _atomic_write_json(artifact_path(workdir, ep, "supervision"), record)
    if passed:
        record["_meta"] = {"upstreamHashes": emit_chain(workdir, ep, "supervision")}
    else:
        # 未放行的件不盖链章：链章只证明「这份放行结论配得上现在的上游」。
        record["_meta"] = {}
    return record


def chain_stale(workdir: Any, ep: Any, stage: str) -> bool:
    """本阶段件是否已失效。件不存在 → False（不存在归盘库管）；上游件没了 → True。"""
    own = artifact_path(workdir, ep, stage)
    if not own.exists():
        return False
    doc = _read_json(own)
    recorded = (doc.get("_meta") or {}).get("upstreamHashes")
    if not isinstance(recorded, dict) or not recorded:
        # 老件没有链条记录：无法证明新鲜，按失效处理（保守方向 = 重跑，不是复用）
        return True
    for up in UPSTREAM[stage]:
        up_path = artifact_path(workdir, ep, up)
        if not up_path.exists():
            return True
        if recorded.get(up) != file_content_hash(up_path):
            return True
    return False


def stale_stages(workdir: Any, ep: Any, stages: Optional[Iterable[str]] = None) -> List[str]:
    wanted = list(stages) if stages else list(STAGES)
    return [s for s in wanted if chain_stale(workdir, ep, s)]


def chain_report(workdir: Any, ep: Any, stages: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """盘库/工作流用的三态报告：present（在库）/ absent（不在库）/ stale（在库但已失效）。"""
    wanted = list(stages) if stages else list(STAGES)
    present, absent, stale = [], [], []
    for stage in wanted:
        path = artifact_path(workdir, ep, stage)
        if not path.exists():
            absent.append({"stage": stage, "path": str(path)})
        else:
            present.append({"stage": stage, "path": str(path)})
            if chain_stale(workdir, ep, stage):
                stale.append({"stage": stage, "path": str(path)})
    return {
        "ep": _as_ep(ep),
        "status": "stale" if stale else "fresh",
        "stale": stale,
        "absent": [x["stage"] for x in absent],
        "present": [x["stage"] for x in present],
    }


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot_files(paths: Sequence[Any], manifest_path: Any) -> Dict[str, Any]:
    """对给定文件列表算 sha256 入清单（工作流越权硬闸的底账）。"""
    files: Dict[str, str] = {}
    for raw in paths:
        p = Path(str(raw))
        if not p.exists():
            raise ChainError(f"待快照文件不存在：{p}")
        if not p.is_file():
            raise ChainError(f"待快照路径不是文件：{p}")
        files[str(p.resolve())] = sha256_file(p)   # 绝对路径入库：换 cwd 复核也不许指错文件
    payload = {"schemaVersion": 1, "files": files}
    _atomic_write_json(Path(str(manifest_path)), payload)
    return payload


def _norm_key(raw: str) -> str:
    """把清单键归一成可比较的形状：反斜杠/正斜杠、大小写、相对路径都算同一条。"""
    try:
        return str(Path(raw).resolve()).casefold()
    except OSError:  # pragma: no cover - 非法路径
        return raw.strip().casefold()


def _load_manifest(path: Path) -> Dict[str, Any]:
    """读清单，并把「同一路径出现两次」当场判红。

    `json.loads` 对重复键静默取最后一个：一份手工拼接过的清单只要把正确摘要写在后面，
    复核就会报「逐字节一致」，把篡改伪装成全过。
    """
    try:
        text = path.read_text("utf-8")
    except OSError as err:
        raise ChainError(f"清单不可读取：{path}（{type(err).__name__}）")
    dups: List[str] = []

    def _catch(pairs):
        seen: Dict[str, int] = {}
        for key, _ in pairs:
            norm = _norm_key(str(key))
            seen[norm] = seen.get(norm, 0) + 1
            if seen[norm] == 2:
                dups.append(str(key))
        return dict(pairs)

    try:
        doc = json.loads(text, object_pairs_hook=_catch)
    except ValueError as err:
        raise ChainError(f"清单不可解析：{path}（{err}）")
    if dups:
        raise ChainError(f"清单存在重复条目：{len(dups)} 个路径被列了多次（例如 {dups[0]}），"
                         f"折叠后剩下哪一条没人知道——请用 snapshot 重新生成：{path}")
    if not isinstance(doc, dict):
        raise ChainError(f"清单结构无法识别（应为 JSON 对象）：{path}")
    return doc


def verify_files(manifest_path: Any) -> Dict[str, Any]:
    """复核清单里每个文件是否仍是原样。返回 {changed, removed, matched}。"""
    doc = _load_manifest(Path(str(manifest_path)))
    files = doc.get("files")
    if not isinstance(files, dict):
        raise ChainError(f"清单结构无法识别（缺 files）：{manifest_path}")
    if not files:
        raise ChainError(f"清单里一个文件都没有，这种「全过」没有意义：{manifest_path}")
    changed, removed, matched = [], [], []
    for raw, digest in files.items():
        p = Path(str(raw))
        if not p.is_file():                        # 不存在、或被杀成了目录，都算"丢了"
            removed.append({"path": str(p), "expected": digest})
            continue
        actual = sha256_file(p)                   # 只算一次：两次读之间文件变了会自相矛盾
        if actual != digest:
            changed.append({"path": str(p), "expected": digest, "actual": actual})
        else:
            matched.append(str(p))
    return {"changed": changed, "removed": removed, "matched": matched}


# ------------------------------------------------------------------ CLI

class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # 用法错一律 exit 1（与门族合同一致）
        self.print_usage(sys.stderr)
        print(f"artifact_chain: error: {message}", file=sys.stderr)
        raise SystemExit(1)


def _build_parser() -> _Parser:
    ap = _Parser(prog="python -m dramacraft.artifact_chain",
                 description="改稿下游失效链：查在库件是否还配得上现在的上游，或对文件清单做快照/复核")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="查某一集的下游链是否失效")
    p_check.add_argument("--workdir", required=True)
    p_check.add_argument("--ep", required=True)
    p_check.add_argument("--stage", default="all", help="plan / shots / supervision / all（默认 all）")
    p_check.add_argument("--json", required=True, dest="json_path", help="报告保存路径（JSON）")

    p_emit = sub.add_parser("emit", help="把当前上游指纹盖进本阶段落盘件（本阶段件须已在库）")
    p_emit.add_argument("--workdir", required=True)
    p_emit.add_argument("--ep", required=True)
    p_emit.add_argument("--stage", required=True, help="plan / shots / supervision")
    p_emit.add_argument("--json", required=True, dest="json_path", help="盖章结果保存路径（JSON）")

    p_sup = sub.add_parser("supervise", help="监督双件落盘：核人读件、写机读件、放行时盖章")
    p_sup.add_argument("--workdir", required=True)
    p_sup.add_argument("--ep", required=True)
    p_sup.add_argument("--verdict", required=True, choices=("pass", "fail"), help="本次监督结论")
    p_sup.add_argument("--finding", action="append", default=[], dest="findings",
                       help="问题条目（可重复；未放行时把红线级问题逐条带上）")
    p_sup.add_argument("--json", required=True, dest="json_path", help="结果保存路径（JSON）")

    p_snap = sub.add_parser("snapshot", help="对文件列表算 sha256 入清单")
    p_snap.add_argument("files", nargs="+")
    p_snap.add_argument("--json", required=True, dest="json_path", help="清单保存路径（JSON）")

    p_ver = sub.add_parser("verify", help="按清单复核文件是否原样")
    p_ver.add_argument("--manifest", required=True)
    p_ver.add_argument("--json", required=True, dest="json_path", help="复核报告保存路径（JSON）")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.cmd == "check":
            if str(args.stage).strip() == "":
                raise ChainError("--stage 不能是空串：要么写 all，要么写 plan/shots/supervision")
            stages = None if args.stage == "all" else [x.strip() for x in str(args.stage).split(",")]
            if stages:
                for s in stages:
                    if s not in STAGES:
                        raise ChainError(f"未知阶段：{s}（可用：plan、shots、supervision、all）")
            report = chain_report(args.workdir, args.ep, stages)
            _atomic_write_json(Path(str(args.json_path)), report)
            if report["stale"]:
                print(f"✗ 第 {report['ep']} 集有 {len(report['stale'])} 个后续环节与现在的剧本对不上了："
                      f"{'、'.join(STAGE_LABELS.get(x['stage'], x['stage']) for x in report['stale'])}"
                      f"——这些环节要重做，不许沿用旧的")
                return 3
            present = '、'.join(STAGE_LABELS.get(s, s) for s in report["present"]) or "无"
            absent = '、'.join(STAGE_LABELS.get(s, s) for s in report["absent"]) or "无"
            print(f"✓ 第 {report['ep']} 集的后续环节与现在的剧本一致（已有：{present}；还没做：{absent}）")
            return 0

        if args.cmd == "emit":
            hashes = emit_chain(args.workdir, args.ep, args.stage)
            payload = {"ep": _as_ep(args.ep), "stage": args.stage, "upstreamHashes": hashes}
            _atomic_write_json(Path(str(args.json_path)), payload)
            ups = '、'.join(STAGE_LABELS.get(k, k) for k in hashes)
            print(f"✓ 已记录：第 {payload['ep']} 集的{STAGE_LABELS.get(args.stage, args.stage)}"
                  f"是照着现在的{ups}做的")
            return 0

        if args.cmd == "supervise":
            record = write_supervision_record(args.workdir, args.ep, args.verdict == "pass", args.findings)
            payload = {
                "ep": record["ep"],
                "verdict": record["verdict"],
                "report": str(supervision_report_path(args.workdir, args.ep)),
                "record": str(artifact_path(args.workdir, args.ep, "supervision")),
                "upstreamHashes": (record.get("_meta") or {}).get("upstreamHashes") or {},
            }
            _atomic_write_json(Path(str(args.json_path)), payload)
            if record["verdict"] == "pass":
                print(f"✓ 第 {record['ep']} 集监督放行已入链（人读报告与机读件齐备）")
            else:
                print(f"✓ 第 {record['ep']} 集监督记为未放行：{len(record['findings'])} 条问题落盘，本轮不盖链章")
            return 0

        if args.cmd == "snapshot":
            payload = snapshot_files(args.files, args.json_path)
            print(f"✓ 已快照 {len(payload['files'])} 个文件 → {args.json_path}")
            return 0

        payload = verify_files(args.manifest)
        _atomic_write_json(Path(str(args.json_path)), payload)
        if payload["changed"] or payload["removed"]:
            print(f"✗ 文件有变动：改过 {len(payload['changed'])} 个、丢了 {len(payload['removed'])} 个")
            for item in payload["changed"]:
                print(f"   ✗ 被改动：{item['path']}")
            for item in payload["removed"]:
                print(f"   ✗ 已丢失：{item['path']}")
            return 5
        print(f"✓ {len(payload['matched'])} 个文件与快照逐字节一致")
        return 0
    except ChainError as err:
        print(f"artifact_chain: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
