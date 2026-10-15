"""v1 兼容包装：把旧 `transcripts` 记录转发给 v2 主文件渲染器。

v2 编排器（Task 11 起）直接使用 `scripts/artifacts.py` 渲染「主文件 + 原始记录」
两个文件；本模块只保留旧入口，供遗留调用与历史记录重渲染使用。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from scripts.artifacts import final_paths, write_prepared_text
from scripts.models import CorrectionStatus, QualityStatus

__all__ = ["RenderError", "render_record", "record_to_bundle"]


class RenderError(Exception):
    """TXT 渲染失败。"""


def record_to_bundle(record: dict) -> dict:
    """把 v1 `transcripts` 行适配成 v2 版本 bundle。"""
    try:
        segments = json.loads(record.get("segments_json") or "[]")
    except json.JSONDecodeError as exc:
        raise RenderError("segments_json 不是合法 JSON，拒绝丢失时间戳渲染") from exc
    if not isinstance(segments, list):
        raise RenderError("segments_json 不是片段数组")
    aweme_id = str(record.get("aweme_id") or "").strip()
    if not aweme_id:
        raise RenderError("记录缺少 aweme_id")
    status = str(record.get("status") or "")
    quality = (QualityStatus.NEEDS_REVIEW if status in ("transcribed", "summarized")
               else QualityStatus.NEEDS_REVIEW)
    return {
        "aweme_id": aweme_id,
        "canonical_url": record.get("canonical_url") or "",
        "title": record.get("title") or "",
        "author": record.get("author") or "",
        "publish_date": record.get("publish_date") or "",
        "duration_seconds": record.get("duration_seconds") or 0,
        "version_id": 0,
        "version_no": 1,
        "lifecycle_status": "legacy",
        "is_current": False,
        "quality_status": str(quality),
        "quality_score": 0.0,
        "quality_report": "{}",
        "raw_text": record.get("transcript_text") or "",
        "corrected_text": "",
        "correction_status": str(CorrectionStatus.PENDING),
        "summary": record.get("summary") or "",
        "summary_status": record.get("summary_status") or "pending",
        "summary_mode": "normal",
        "summary_meta": "{}",
        "processing_signature": "legacy-v1",
        "segments": segments,
        "attempts": [],
        "selected_attempt": None,
        "ocr_observations": [],
        "speaker_turns": [],
        "correction_changes": [],
        "degradations": [],
        "created_at": record.get("updated_at") or "",
        "published_at": record.get("updated_at") or "",
    }


def render_record(record: dict, output_root: Path) -> str:
    """渲染并写出 TXT，返回绝对路径字符串（兼容 v1 调用签名）。"""
    from scripts.artifacts import _main_body

    bundle = record_to_bundle(record)
    main, _ = final_paths(bundle, Path(output_root))
    prepared = write_prepared_text(main, _main_body(bundle))
    os.replace(prepared.path, main)
    return str(main.resolve())
