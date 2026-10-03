from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from app.core.settings import PROJECT_ROOT, Settings


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} 必须是对象")
    return cast(dict[str, Any], value)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_backtest_policy(settings: Settings) -> dict[str, Any]:
    payload = _object(yaml.safe_load(settings.backtest_policy_file.read_text("utf-8")), "回测策略")
    schema_path = PROJECT_ROOT / "schemas" / "dividend-hurdle-backtest-run-policy.schema.json"
    schema = _object(json.loads(schema_path.read_text("utf-8")), "回测策略 Schema")
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload),
        key=lambda error: list(error.path),
    )
    if errors:
        where = ".".join(str(value) for value in errors[0].path) or "root"
        raise RuntimeError(f"回测策略不符合 Schema: {where}: {errors[0].message}")
    base = _object(payload["base_policy"], "base_policy")
    base_path = PROJECT_ROOT / str(base["path"])
    actual_hash = sha256_file(base_path)
    if actual_hash != base["sha256"]:
        raise RuntimeError(
            f"回测基线哈希不一致: {base_path}; expected={base['sha256']}; actual={actual_hash}"
        )
    return payload


def excluded_symbols(policy: dict[str, Any]) -> frozenset[str]:
    """Return explicit whole-window exclusions from the validated policy snapshot."""
    raw = policy.get("universe_exclusions", [])
    if not isinstance(raw, list):
        raise RuntimeError("universe_exclusions 必须是数组")
    return frozenset(
        str(item["symbol"])
        for value in raw
        for item in [_object(value, "universe_exclusions item")]
        if item.get("scope") == "ALL_SIMULATION_WINDOWS"
    )
