from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.research_validation import (
    ResearchValidationError,
    parse_research_time,
    validate_research_payload,
)
from app.core.settings import Settings
from app.core.time import iso_now, now_shanghai

ResearchImportError = ResearchValidationError


def _append_import_audit(settings: Settings, payload: dict[str, Any], target: Path) -> None:
    audit_path = settings.state_dir / "research-import-audit.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            stream.seek(0)
            lines = [line for line in stream.read().splitlines() if line.strip()]
            previous_hash: str | None = None
            if lines:
                previous = json.loads(lines[-1])
                previous_hash = str(previous.get("event_hash"))
            event: dict[str, Any] = {
                "event_id": str(uuid4()),
                "run_id": f"{now_shanghai():%Y%m%d-%H%M%S}-research-import",
                "event_type": "REPORT_RENDERED",
                "actor": "local-cli",
                "occurred_at": iso_now(),
                "payload": {
                    "payload_ref": str(target.relative_to(settings.research_dir.parent.parent)),
                    "payload_hash": hashlib.sha256(_canonical_bytes(payload)).hexdigest(),
                    "versions": {"research_schema": str(payload["schema_version"])},
                    "redactions": [],
                },
                "prev_event_hash": previous_hash,
            }
            event_hash = hashlib.sha256(
                (
                    f"{previous_hash or ''}{json.dumps(event, ensure_ascii=False, sort_keys=True)}"
                ).encode()
            ).hexdigest()
            event["event_hash"] = event_hash
            stream.seek(0, os.SEEK_END)
            stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def import_research_card(settings: Settings, source: Path) -> Path:
    try:
        payload = json.loads(source.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchImportError(f"无法读取研究卡: {exc}") from exc
    if not isinstance(payload, dict):
        raise ResearchImportError("研究卡根节点必须是对象")
    return import_research_payload(settings, payload)


def import_research_payload(settings: Settings, payload: dict[str, Any]) -> Path:
    validate_research_payload(settings, payload)
    symbol = str(payload["symbol"])
    as_of = parse_research_time(payload["as_of"], "as_of")
    content = _canonical_bytes(payload)
    digest = hashlib.sha256(content).hexdigest()[:12]
    directory = settings.research_dir / symbol
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{as_of:%Y%m%dT%H%M%S}-{digest}.json"
    if target.exists():
        if target.read_bytes() == content:
            return target
        raise ResearchImportError("目标版本名冲突；拒绝覆盖旧研究")
    temporary = directory / f".{target.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    except FileExistsError as exc:
        raise ResearchImportError("目标研究版本已存在；拒绝覆盖") from exc
    finally:
        temporary.unlink(missing_ok=True)
    _append_import_audit(settings, payload, target)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="校验并追加导入 ChatGPT 结构化研究卡")
    parser.add_argument("file", type=Path, help="待导入 JSON 文件")
    args = parser.parse_args()
    try:
        target = import_research_card(Settings(), args.file)
    except ResearchImportError as exc:
        parser.error(str(exc))
    print(target)


if __name__ == "__main__":
    main()
