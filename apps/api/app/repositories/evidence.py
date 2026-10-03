from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker

from app.core.settings import Settings
from app.core.time import SHANGHAI, now_shanghai


class EvidenceBundleError(RuntimeError):
    pass


def _canonical(payload: Any) -> bytes:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (serialized + "\n").encode()


def _parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise EvidenceBundleError(f"{field} 必须是带时区的 ISO 日期时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceBundleError(f"{field} 不是有效日期时间") from exc
    if parsed.tzinfo is None:
        raise EvidenceBundleError(f"{field} 必须包含时区")
    return parsed.astimezone(SHANGHAI)


def finalize_bundle(draft: dict[str, Any]) -> dict[str, Any]:
    """Return a hashed copy; source text stays immutable once imported."""
    payload = json.loads(json.dumps(draft, ensure_ascii=False))
    evidence = payload.get("evidence")
    if not isinstance(evidence, list):
        raise EvidenceBundleError("evidence 必须是数组")
    for item in evidence:
        if not isinstance(item, dict):
            raise EvidenceBundleError("evidence 项必须是对象")
        unhashed = {key: value for key, value in item.items() if key != "content_hash"}
        item["content_hash"] = hashlib.sha256(_canonical(unhashed)).hexdigest()
    unhashed_bundle = {key: value for key, value in payload.items() if key != "content_hash"}
    payload["content_hash"] = hashlib.sha256(_canonical(unhashed_bundle)).hexdigest()
    return cast(dict[str, Any], payload)


class EvidenceBundleRepository:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        schema_path = settings.schema_dir / "evidence-bundle.schema.json"
        schema = json.loads(schema_path.read_text("utf-8"))
        self._validator = Draft202012Validator(schema, format_checker=FormatChecker())

    def validate(self, payload: dict[str, Any]) -> None:
        errors = sorted(self._validator.iter_errors(payload), key=lambda error: list(error.path))
        if errors:
            where = ".".join(str(value) for value in errors[0].path) or "root"
            raise EvidenceBundleError(f"Schema 校验失败 {where}: {errors[0].message}")
        cutoff = _parse_time(payload.get("data_cutoff"), "data_cutoff")
        as_of = _parse_time(payload.get("as_of"), "as_of")
        created_at = _parse_time(payload.get("created_at"), "created_at")
        if cutoff > as_of:
            raise EvidenceBundleError("data_cutoff 不能晚于 as_of")
        future_limit = now_shanghai() + timedelta(minutes=5)
        if as_of > future_limit or created_at > future_limit:
            raise EvidenceBundleError("证据包时间晚于当前时间，疑似时间穿越")
        evidence_ids: set[str] = set()
        for item in payload["evidence"]:
            evidence_id = str(item["evidence_id"])
            if evidence_id in evidence_ids:
                raise EvidenceBundleError(f"evidence_id 重复: {evidence_id}")
            evidence_ids.add(evidence_id)
            evidence_as_of = _parse_time(item["as_of"], f"evidence.{evidence_id}.as_of")
            if evidence_as_of > cutoff:
                raise EvidenceBundleError(f"证据 {evidence_id} 晚于 data_cutoff")
            finalized_item = finalize_bundle({**payload, "evidence": [item]})["evidence"][0]
            expected = finalized_item["content_hash"]
            if item["content_hash"] != expected:
                raise EvidenceBundleError(f"证据 {evidence_id} content_hash 不匹配")
        expected_bundle = finalize_bundle(payload)["content_hash"]
        if payload["content_hash"] != expected_bundle:
            raise EvidenceBundleError("证据包 content_hash 不匹配")
        conflict_ids = {
            str(value)
            for conflict in payload["conflicts"]
            for value in conflict.get("evidence_ids", [])
        }
        missing = conflict_ids - evidence_ids
        if missing:
            raise EvidenceBundleError(f"冲突引用未知证据: {', '.join(sorted(missing))}")

    def import_draft(self, draft: dict[str, Any]) -> Path:
        payload = finalize_bundle(draft)
        self.validate(payload)
        symbol = str(payload["symbol"])
        as_of = _parse_time(payload["as_of"], "as_of")
        digest = str(payload["content_hash"])[:12]
        directory = self._settings.evidence_dir / symbol
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{as_of:%Y%m%dT%H%M%S}-{digest}.json"
        content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode() + b"\n"
        if target.exists():
            if target.read_bytes() == content:
                return target
            raise EvidenceBundleError("目标证据包版本冲突；拒绝覆盖")
        temporary = directory / f".{target.name}.{uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, target)
        except FileExistsError as exc:
            raise EvidenceBundleError("目标证据包已存在；拒绝覆盖") from exc
        finally:
            temporary.unlink(missing_ok=True)
        return target
