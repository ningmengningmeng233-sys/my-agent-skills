"""双文件渲染与事务式发布（设计文档 14.2、16）。

主文件面向阅读，证据文件面向追溯；两者都先写到同目录临时文件，
校验哈希后才原子替换，任一环节失败都还原旧文件、保持旧 current 版本。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

from scripts.models import (ArtifactKind, CorrectionStatus, PreparedArtifact,
                            QualityStatus)
from scripts.transcript_db import PublishError, TranscriptDB

logger = logging.getLogger("douyin_transcript.artifacts")

MAX_NAME_CHARS = 80
_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
             *(f"lpt{i}" for i in range(1, 10))}
_HEADER_RULE = "=" * 78
NEEDS_REVIEW_BANNER = (
    "⚠️ 本稿为“需要复核（needs_review）”：识别质量未达自动发布标准，"
    "部分内容可能存在偏差，请对照原始记录文件核对。"
)
CORRECTION_PENDING_BANNER = (
    "⚠️ 以下为“未经 AI 校正原始稿”，校正版待补；补齐后主文件会由数据库重渲染。"
)


def safe_filename(name: str, limit: int = MAX_NAME_CHARS) -> str:
    """Windows 安全的文件名片段：去非法字符、避开保留名、限制长度。"""
    cleaned = _ILLEGAL.sub("_", str(name or "")).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = "未命名"
    if cleaned.split(".")[0].lower() in _RESERVED:
        cleaned = f"_{cleaned}"
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip(" ._")
    return cleaned or "未命名"


def time_label(seconds: float) -> str:
    total = int(max(0.0, float(seconds or 0)))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _month_root(output_root: Path, publish_date: str) -> Path:
    text = str(publish_date or "")
    if len(text) >= 7 and text[4] == "-":
        return Path(output_root) / text[:4] / text[5:7]
    now = datetime.now()
    return Path(output_root) / f"{now.year:04d}" / f"{now.month:02d}"


def base_name(bundle: dict) -> str:
    author = safe_filename(bundle.get("author") or "未知作者", 20)
    aweme_id = safe_filename(bundle.get("aweme_id") or "unknown", 24)
    limit = max(MAX_NAME_CHARS - len(author) - len(aweme_id) - 12, 20)
    title = safe_filename(bundle.get("title") or "无标题", limit)
    return f"{author}_{title}_{aweme_id}"


def final_paths(bundle: dict, output_root: Path) -> tuple[Path, Path]:
    directory = _month_root(Path(output_root), bundle.get("publish_date", ""))
    stem = base_name(bundle)
    return (directory / f"{stem}.txt",
            directory / f"{stem}_原始记录.txt")


def _prepared(kind: ArtifactKind, path: Path) -> PreparedArtifact:
    raw = Path(path).read_bytes()
    return PreparedArtifact(kind=str(kind), path=str(path),
                            sha256=hashlib.sha256(raw).hexdigest(),
                            size_bytes=len(raw))


def write_prepared_text(path: Path, text: str) -> PreparedArtifact:
    """渲染阶段只写临时文件，最终替换交给发布阶段统一完成。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f"{path.name}.{uuid.uuid4().hex}.tmp"
    with open(temp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    raw = temp.read_bytes()
    return PreparedArtifact(kind="", path=str(temp),
                            sha256=hashlib.sha256(raw).hexdigest(),
                            size_bytes=len(raw))


# ----------------------------------------------------------------------
# 版本数据
# ----------------------------------------------------------------------

def build_version_bundle(db: TranscriptDB, version_id: int,
                         *, degradations: Iterable[str] | None = None) -> dict:
    """把渲染一个版本需要的全部结构化证据取出来（单一入口）。"""
    version = db.get_version(version_id)
    if version is None:
        raise LookupError(f"版本不存在：{version_id}")
    video = db.get_video(version["aweme_id"]) or {}
    attempts = db.list_attempts(version_id)
    selected = None
    for attempt in attempts:
        if attempt["attempt_id"] == version.get("selected_attempt_id"):
            selected = attempt
    segments = []
    if selected is not None:
        import json as _json

        try:
            segments = _json.loads(selected.get("segments_json") or "[]")
        except (TypeError, ValueError):
            segments = []
    return {
        "aweme_id": version["aweme_id"],
        "canonical_url": video.get("canonical_url", ""),
        "title": video.get("title", ""),
        "author": video.get("author", ""),
        "publish_date": video.get("publish_date", ""),
        "duration_seconds": video.get("duration_seconds", 0),
        "version_id": version_id,
        "version_no": version.get("version_no"),
        "lifecycle_status": version.get("lifecycle_status"),
        "is_current": bool(version.get("is_current")),
        "quality_status": version.get("quality_status"),
        "quality_score": version.get("quality_score"),
        "quality_report": version.get("quality_report_json", "{}"),
        "raw_text": version.get("raw_text", ""),
        "corrected_text": version.get("corrected_text", ""),
        "correction_status": version.get("correction_status"),
        "summary": version.get("summary", ""),
        "summary_status": version.get("summary_status"),
        "summary_mode": version.get("summary_mode"),
        "summary_meta": version.get("summary_meta_json", "{}"),
        "processing_signature": version.get("processing_signature", ""),
        "segments": segments,
        "attempts": attempts,
        "selected_attempt": selected,
        "ocr_observations": db.list_ocr_observations(version_id),
        "speaker_turns": db.list_speaker_turns(version_id),
        "correction_changes": db.list_correction_changes(version_id),
        "degradations": list(degradations or []),
        "created_at": version.get("created_at", ""),
        "published_at": version.get("published_at", ""),
    }


# ----------------------------------------------------------------------
# 渲染
# ----------------------------------------------------------------------

def _line_with_speaker(segment: dict, speaker_display: dict) -> str:
    label = speaker_display.get(str(segment.get("speaker", "")), "")
    text = str(segment.get("text", ""))
    suffix = "（低置信，建议核对）" if segment.get("needs_review") else ""
    prefix = f"{label}：" if label else ""
    return (f"[{time_label(segment.get('start', 0))} → "
            f"{time_label(segment.get('end', 0))}] {prefix}{text}{suffix}")


def _speaker_display(turns: Sequence[dict]) -> dict:
    from scripts.diarization import display_label

    mapping: dict = {}
    for turn in turns or []:
        label = str(turn.get("speaker_label") or "")
        if label and label not in mapping:
            mapping[label] = display_label(label)
    return mapping


def _visual_only(bundle: dict) -> bool:
    return str(bundle.get("quality_status")) == str(QualityStatus.VISUAL_TEXT_ONLY)


def _main_body(bundle: dict) -> str:
    visual_only = _visual_only(bundle)
    lines: list[str] = []
    if str(bundle.get("quality_status")) == str(QualityStatus.NEEDS_REVIEW):
        lines += [NEEDS_REVIEW_BANNER, ""]

    lines += [_HEADER_RULE, "抖音视频转录文档" if not visual_only
              else "抖音视频画面文字记录", _HEADER_RULE, "",
              f"📹 视频标题：{bundle.get('title', '')}",
              f"🔗 抖音链接：{bundle.get('canonical_url', '')}",
              f"👤 作者：{bundle.get('author', '')}",
              f"📅 发布时间：{bundle.get('publish_date', '') or '未知'}",
              f"⏱️ 视频时长：{time_label(bundle.get('duration_seconds', 0))}",
              f"🆔 作品 ID：{bundle.get('aweme_id', '')}",
              f"📝 版本：v{bundle.get('version_no')}（version_id={bundle.get('version_id')}）",
              f"📊 质量状态：{bundle.get('quality_status')}"
              f"（{float(bundle.get('quality_score') or 0):.1f} 分）",
              f"🎙️ 校正状态：{bundle.get('correction_status')}",
              f"🧾 摘要状态：{bundle.get('summary_status')}"
              f"（{bundle.get('summary_mode')}）",
              ""]

    lines += [_HEADER_RULE, "第一部分：摘要（AI 生成）", _HEADER_RULE, ""]
    if str(bundle.get("summary_status")) == "summarized" and bundle.get("summary"):
        lines += [str(bundle.get("summary")).strip(), ""]
    else:
        lines += ["（摘要待补：可运行 scripts/maintenance.py fill-ai 补全）", ""]

    if visual_only:
        lines += [_HEADER_RULE, "第二部分：画面文字记录（非语音）", _HEADER_RULE, ""]
        observations = bundle.get("ocr_observations") or []
        if observations:
            for item in observations:
                lines.append(
                    f"[{time_label(item.get('start_seconds', 0))} → "
                    f"{time_label(item.get('end_seconds', 0))}] "
                    f"{item.get('text', '')}（{item.get('kind', '')}）")
        else:
            lines.append("（未识别到画面文字）")
        lines += ["", _HEADER_RULE, "文档结束", _HEADER_RULE, ""]
        return "\n".join(lines)

    correction_status = str(bundle.get("correction_status") or "")
    speaker_display = _speaker_display(bundle.get("speaker_turns") or [])
    is_corrected = correction_status == str(CorrectionStatus.CORRECTED) and         bool(str(bundle.get("corrected_text") or "").strip())
    segments = corrected_segments(bundle) if is_corrected         else list(bundle.get("segments") or [])
    title = ("第二部分：校正版逐字稿" if is_corrected
             else "第二部分：未经 AI 校正原始稿")
    lines += [_HEADER_RULE, title, _HEADER_RULE, ""]
    if not is_corrected:
        lines += [CORRECTION_PENDING_BANNER, ""]
    if segments:
        lines += [_line_with_speaker(item, speaker_display) for item in segments]
    else:
        lines += [str(bundle.get("corrected_text") or bundle.get("raw_text") or "")]
        lines += [CORRECTION_PENDING_BANNER, ""]
    lines += ["", _HEADER_RULE, "文档结束", _HEADER_RULE, ""]
    return "\n".join(lines)


def _evidence_body(bundle: dict) -> str:
    lines: list[str] = [_HEADER_RULE, "原始记录（可追溯证据）", _HEADER_RULE, "",
                        f"作品 ID：{bundle.get('aweme_id', '')}",
                        f"标题：{bundle.get('title', '')}",
                        f"版本：v{bundle.get('version_no')}"
                        f"（version_id={bundle.get('version_id')}）",
                        f"生命周期：{bundle.get('lifecycle_status')}"
                        f"；是否当前版：{'是' if bundle.get('is_current') else '否'}",
                        f"质量管线签名：{bundle.get('processing_signature', '')}",
                        f"创建时间：{bundle.get('created_at', '')}",
                        f"发布时间：{bundle.get('published_at', '') or '未发布'}",
                        "", "质量报告：",
                        str(bundle.get("quality_report") or "{}"), ""]

    lines += [_HEADER_RULE, "一、ASR 轮次", _HEADER_RULE]
    attempts = bundle.get("attempts") or []
    if not attempts:
        lines.append("（无 ASR 轮次记录）")
    for item in attempts:
        mark = "★选中轮次" if item.get("attempt_id") == (
            bundle.get("selected_attempt") or {}).get("attempt_id") else "  "
        lines += [f"{mark} 第 {item.get('attempt_no')} 轮："
                  f"{item.get('model')}/{item.get('device')}/"
                  f"{item.get('compute_type')}，音轨 {item.get('audio_variant')}，"
                  f"得分 {item.get('score')}，通过={'是' if item.get('passed') else '否'}",
                  f"      策略：{item.get('strategy_json')}",
                  f"      指标：{item.get('metrics_json')}",
                  f"      硬异常：{item.get('hard_failures_json')}"]
    lines.append("")

    lines += [_HEADER_RULE, "二、原始时间戳稿（不可变）", _HEADER_RULE, ""]
    for item in bundle.get("segments") or []:
        lines.append(
            f"[{time_label(item.get('start', 0))} → "
            f"{time_label(item.get('end', 0))}] {item.get('text', '')}"
            f"  | avg_logprob={item.get('avg_logprob')}"
            f" no_speech={item.get('no_speech_prob')}"
            f" 复核={','.join(item.get('review_reasons') or []) or '无'}")
    lines.append("")

    lines += [_HEADER_RULE, "三、画面文字（OCR 分类）", _HEADER_RULE, ""]
    observations = bundle.get("ocr_observations") or []
    if not observations:
        lines.append("（未识别到画面文字）")
    for item in observations:
        lines.append(
            f"[{time_label(item.get('start_seconds', 0))} → "
            f"{time_label(item.get('end_seconds', 0))}] "
            f"{item.get('kind')}（conf={item.get('confidence')}）"
            f" {item.get('text', '')}")
    lines.append("")

    lines += [_HEADER_RULE, "四、说话人（匿名）", _HEADER_RULE, ""]
    turns = bundle.get("speaker_turns") or []
    if not turns:
        lines.append("（未产出说话人标签：单人视频或分离能力降级）")
    for item in turns:
        lines.append(
            f"[{time_label(item.get('start_seconds', 0))} → "
            f"{time_label(item.get('end_seconds', 0))}] "
            f"{item.get('speaker_label')}（conf={item.get('confidence')}）")
    lines.append("")

    lines += [_HEADER_RULE, "五、重要修正", _HEADER_RULE, ""]
    changes = bundle.get("correction_changes") or []
    if not changes:
        lines.append("（无修正记录）")
    for item in changes:
        lines.append(
            f"{item.get('source_segment_ref')} [{item.get('change_type')}] "
            f"{item.get('original_text')} → {item.get('corrected_text')}"
            f"  证据={item.get('evidence_json')}"
            f" 置信度={item.get('confidence')}")
    lines.append("")

    lines += [_HEADER_RULE, "六、疑点", _HEADER_RULE]
    doubts = [f"{item.get('source_segment_ref')}：{item.get('change_type')}"
              f" {item.get('evidence_json')}"
              for item in changes if item.get("change_type") in ("conflict",)]
    doubts += [f"片段复核建议：{item.get('text', '')[:40]}"
               for item in (bundle.get("segments") or [])
               if item.get("needs_review")]
    lines += doubts or ["（无疑点）"]
    lines.append("")

    lines += [_HEADER_RULE, "七、降级说明", _HEADER_RULE]
    lines += [f"- {item}" for item in (bundle.get("degradations") or [])] or \
        ["（无降级）"]
    lines += ["", _HEADER_RULE, "文档结束", _HEADER_RULE, ""]
    return "\n".join(lines)


def corrected_segments(bundle: dict) -> list[dict]:
    """把校正结果按 source_segment_ref 回填到原始段，保留时间戳与说话人。

    原始段永不被覆盖：这里只是渲染用的派生视图。
    """
    changes = {str(item.get("source_segment_ref")): item
               for item in (bundle.get("correction_changes") or [])}
    result = []
    for index, segment in enumerate(bundle.get("segments") or []):
        ref = f"seg-{index + 1}"
        change = changes.get(ref)
        item = dict(segment)
        item["segment_id"] = ref
        if change and str(change.get("corrected_text") or "").strip():
            item["text"] = str(change["corrected_text"])
            item["corrected"] = str(change.get("change_type")) != "unchanged"
        else:
            item["corrected"] = False
        result.append(item)
    return result


def render_main(bundle: dict, output_root: Path) -> PreparedArtifact:
    """渲染主文件到同目录临时文件；不触碰现有正式文件。"""
    main, _ = final_paths(bundle, output_root)
    prepared = write_prepared_text(main, _main_body(bundle))
    return PreparedArtifact(kind=str(ArtifactKind.MAIN_TXT), path=prepared.path,
                            sha256=prepared.sha256,
                            size_bytes=prepared.size_bytes)


def render_evidence(bundle: dict, output_root: Path) -> PreparedArtifact:
    _, evidence = final_paths(bundle, output_root)
    prepared = write_prepared_text(evidence, _evidence_body(bundle))
    return PreparedArtifact(kind=str(ArtifactKind.EVIDENCE_TXT),
                            path=prepared.path, sha256=prepared.sha256,
                            size_bytes=prepared.size_bytes)


# ----------------------------------------------------------------------
# 发布
# ----------------------------------------------------------------------

@dataclass
class PublishedArtifacts:
    version_id: int
    main_file: str
    evidence_file: str
    main_sha256: str
    evidence_sha256: str
    repaired: bool = False


def _target_of(prepared: PreparedArtifact, bundle: dict,
               output_root: Path) -> Path:
    main, evidence = final_paths(bundle, output_root)
    return main if str(prepared.kind) == str(ArtifactKind.MAIN_TXT) else evidence


def _discard(prepared: Sequence[PreparedArtifact]) -> None:
    """清掉尚未发布的临时文件，绝不在输出目录留下半成品。"""
    directories = set()
    for item in prepared:
        path = Path(item.path)
        directories.add(path.parent)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("清理临时交付文件失败：%s", path.name)
    # 同目录下的其它 *.tmp 也是本管线产物（调用方传入的集合可能不完整）
    for directory in directories:
        for stale in Path(directory).glob("*.tmp"):
            try:
                stale.unlink()
            except OSError:
                logger.warning("清理残留临时文件失败：%s", stale.name)


def publish_artifacts(db: TranscriptDB, version_id: int,
                      prepared: Sequence[PreparedArtifact], *,
                      output_root: Path | None = None,
                      verifier=None, replacer=None) -> PublishedArtifacts:
    """校验 → 原子替换 → 数据库发布；任一步失败都还原旧文件与旧 current。"""
    bundle = build_version_bundle(db, version_id)
    if output_root is None:
        output_root = Path(prepared[0].path).parents[2] if prepared else Path(".")
    replacer = replacer or os.replace
    kinds = {str(item.kind) for item in prepared}
    if kinds != {str(ArtifactKind.MAIN_TXT), str(ArtifactKind.EVIDENCE_TXT)}:
        _discard(prepared)
        raise PublishError("发布必须同时提供主文件与证据文件")

    targets: dict[str, Path] = {}
    for item in prepared:
        source = Path(item.path)
        if not source.is_file():
            _discard(prepared)
            raise PublishError("待发布的交付文件不存在")
        raw = source.read_bytes()
        if len(raw) != int(item.size_bytes) or hashlib.sha256(raw).hexdigest() != \
                str(item.sha256):
            _discard(prepared)
            raise PublishError("待发布文件哈希与预期不一致")
        if verifier is not None:
            try:
                verifier(item)
            except Exception:
                _discard(prepared)
                raise
        targets[str(item.kind)] = _target_of(item, bundle, output_root)

    # 记录旧文件内容，任何后续失败都要还原
    rollback: list[tuple[Path, bytes]] = []
    for target in targets.values():
        if target.is_file():
            rollback.append((target, target.read_bytes()))

    replaced: list[Path] = []
    try:
        for item in prepared:
            target = targets[str(item.kind)]
            replacer(item.path, target)
            replaced.append(target)
        final = [
            PreparedArtifact(kind=str(item.kind),
                             path=str(targets[str(item.kind)]),
                             sha256=str(item.sha256),
                             size_bytes=int(item.size_bytes))
            for item in prepared]
        db.publish_version(version_id, final)
    except Exception as exc:
        for path, content in rollback:
            try:
                if path.is_file():
                    path.write_bytes(content)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
            except OSError:
                logger.error("回滚交付文件失败，请运行 maintenance repair-artifacts")
        for item in prepared:
            Path(item.path).unlink(missing_ok=True)
        for path, content in rollback:
            if not path.is_file():
                continue
            if path.read_bytes() != content:
                logger.error("交付文件未能完全还原：%s", path.name)
        if isinstance(exc, PublishError):
            raise
        raise PublishError(f"发布失败：{type(exc).__name__}") from exc

    main_file = targets[str(ArtifactKind.MAIN_TXT)]
    evidence_file = targets[str(ArtifactKind.EVIDENCE_TXT)]
    by_kind = {str(item.kind): item for item in prepared}
    return PublishedArtifacts(
        version_id=int(version_id), main_file=str(main_file),
        evidence_file=str(evidence_file),
        main_sha256=str(by_kind[str(ArtifactKind.MAIN_TXT)].sha256),
        evidence_sha256=str(by_kind[str(ArtifactKind.EVIDENCE_TXT)].sha256))


def repair_artifacts(db: TranscriptDB, aweme_id: str, *,
                     output_root: Path) -> PublishedArtifacts:
    """只从 SQLite 重建两个交付文件：不下载、不 ASR、不调用任何外部能力。"""
    version = db.get_current_version(aweme_id)
    if version is None:
        raise PublishError("该作品没有可重建的当前版本")
    version_id = int(version["version_id"])
    bundle = build_version_bundle(db, version_id)
    prepared = [render_main(bundle, output_root),
                render_evidence(bundle, output_root)]
    published = publish_artifacts(db, version_id, prepared,
                                 output_root=output_root)
    published.repaired = True
    return published


def render_manifest(results: list[dict], manifest_path: Path | str,
                    generator: str = "抖音转录_v2.0.4") -> Path | None:
    """生成符合 source-manifest.schema.json 契约的交付清单。"""
    import json
    manifest_path = Path(manifest_path)
    base_dir = manifest_path.parent
    items = []
    for r in results:
        if not r.get("ok") or not r.get("main_file"):
            continue
        mf = Path(r["main_file"])
        if not mf.is_file():
            continue
        aweme_id = str(r.get("aweme_id") or "")
        try:
            txt_rel = str(mf.relative_to(base_dir).as_posix())
        except ValueError:
            txt_rel = str(mf.as_posix())
        ev = Path(r["evidence_file"]) if r.get("evidence_file") else None
        ev_rel = None
        if ev and ev.is_file():
            try:
                ev_rel = str(ev.relative_to(base_dir).as_posix())
            except ValueError:
                ev_rel = str(ev.as_posix())

        raw = mf.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()

        items.append({
            "source_id": aweme_id,
            "platform": "douyin",
            "source_url": f"https://www.douyin.com/video/{aweme_id}" if aweme_id else "",
            "part_index": 1,
            "txt_path": txt_rel,
            "evidence_path": ev_rel,
            "sha256": sha,
            "status": "completed",
            "title": r.get("title") or "",
            "has_summary": bool(r.get("summary_status") == "summarized"),
        })
    if items:
        manifest_data = {
            "schema_version": "1.0.0",
            "generator": generator,
            "generated_at": datetime.now().astimezone().isoformat(),
            "items": items,
        }
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest_data, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest_path
    return None

