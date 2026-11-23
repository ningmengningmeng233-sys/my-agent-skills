import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def append_audit(path: Path, event_type: str, **fields: Any) -> None:
    record = {"ts": now_iso(), "type": event_type, **fields}
    payload = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def read_audit(path: Path) -> Iterator[dict[str, Any]]:
    if not path.is_file():
        return
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"审计日志第 {line_number} 行不是合法 JSON") from exc
            if not isinstance(record, dict):
                raise ValueError(f"审计日志第 {line_number} 行根值不是对象")
            yield record
