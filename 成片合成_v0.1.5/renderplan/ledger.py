"""运行台账：每个 stage 向 .lemon/run-ledger.jsonl 追加一条记录。

字段是 `schemas/run-ledger.schema.json`（C8 跨技能统一台账）的超集：必填的
成本/效果字段（calls/tokens_*/cost_cny/gate_failures/retries/input_units/
model_node/skill_version/artifact_paths）本机全部如实写 0 或「未记录」——
桩阶段不调模型、不花钱，但不许缺字段，否则这条账进不了统一汇总。
状态取值遵循套件红线：SKIPPED / NOT_RUN 不算绿灯，只有真产出才 SUCCEEDED。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .plan import SOURCE_SKILL

SCHEMA_VERSION = 1
SKILL_VERSION = "0.1.3"


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def make_record(stage, status, succeeded=0, failed=0, skipped=0,
                artifacts=None, note="", run_id=None,
                input_units=0, model_node="未记录", retries=0):
    arts = [str(a) for a in (artifacts or [])]
    return {
        "schema_version": SCHEMA_VERSION,
        "ts": _now(),
        "skill": SOURCE_SKILL,
        "skill_version": SKILL_VERSION,
        "stage": stage,
        "run_id": run_id or os.environ.get("RENDER_RUN_ID") or "local",
        "status": status,
        "succeeded": int(succeeded),
        "failed": int(failed),
        "skipped": int(skipped),
        "artifacts": arts,
        "note": note,
        # ↓ 统一台账（C8）必填：本机桩链路无模型调用、无成本，如实记 0
        "model_node": model_node,
        "input_units": int(input_units),
        "calls": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_cny": 0,
        "gate_failures": int(failed),
        "retries": int(retries),
        "artifact_paths": arts,
    }


def append(record, ledger_path):
    ledger_path = Path(ledger_path)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record
