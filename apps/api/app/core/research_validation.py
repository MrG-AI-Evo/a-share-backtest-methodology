from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from app.core.settings import Settings
from app.core.time import SHANGHAI, now_shanghai


class ResearchValidationError(RuntimeError):
    pass


def parse_research_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ResearchValidationError(f"{field} 必须是 ISO 日期时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchValidationError(f"{field} 不是有效日期时间") from exc
    if parsed.tzinfo is None:
        raise ResearchValidationError(f"{field} 必须包含时区")
    return parsed.astimezone(SHANGHAI)


def _reference_ids(payload: dict[str, Any]) -> set[str]:
    references: set[str] = set()
    for group in (
        "core_thesis",
        "bull_case",
        "bear_case",
        "catalysts",
        "risk_flags",
        "confirmed_facts",
    ):
        rows = payload.get(group)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("evidence_ids"), list):
                references.update(str(value) for value in row["evidence_ids"])
    inferences = payload.get("ai_inferences")
    if isinstance(inferences, list):
        for row in inferences:
            if not isinstance(row, dict):
                continue
            for field in ("supporting_evidence_ids", "counter_evidence_ids"):
                if isinstance(row.get(field), list):
                    references.update(str(value) for value in row[field])
    return references


def validate_research_semantics(payload: dict[str, Any]) -> None:
    as_of = parse_research_time(payload.get("as_of"), "as_of")
    created_at = parse_research_time(payload.get("created_at"), "created_at")
    future_limit = now_shanghai() + timedelta(minutes=5)
    if as_of > future_limit or created_at > future_limit:
        raise ResearchValidationError("研究时间晚于当前时间，疑似时间穿越")
    valid_until = payload.get("valid_until")
    if valid_until is not None and parse_research_time(valid_until, "valid_until") < as_of:
        raise ResearchValidationError("valid_until 不能早于 as_of")

    evidence = payload.get("evidence")
    if not isinstance(evidence, list):
        raise ResearchValidationError("evidence 必须是数组")
    evidence_ids = [
        str(item["evidence_id"])
        for item in evidence
        if isinstance(item, dict) and "evidence_id" in item
    ]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ResearchValidationError("evidence_id 不得重复")
    missing = _reference_ids(payload) - set(evidence_ids)
    if missing:
        raise ResearchValidationError(f"证据引用不存在: {', '.join(sorted(missing))}")


def validate_research_payload(settings: Settings, payload: dict[str, Any]) -> None:
    schema = json.loads((settings.schema_dir / "research-card.schema.json").read_text("utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
    if errors:
        path = ".".join(str(item) for item in errors[0].path) or "root"
        raise ResearchValidationError(f"Schema 校验失败 {path}: {errors[0].message}")
    validate_research_semantics(payload)
