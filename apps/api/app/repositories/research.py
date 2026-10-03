from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, cast

from jsonschema import Draft202012Validator, FormatChecker

from app.core.research_validation import ResearchValidationError, validate_research_semantics
from app.core.settings import Settings
from app.models import ResearchCardSummary

ResearchStatus = Literal["重点观察", "观察", "谨慎", "排除"]


def _is_contract_baseline(payload: dict[str, Any]) -> bool:
    return payload.get("record_type") == "CONTRACT_BASELINE" or str(
        payload.get("research_id", "")
    ).startswith("baseline-")


class ResearchRepository:
    def __init__(self, settings: Settings) -> None:
        self._root = settings.research_dir
        schema = json.loads((settings.schema_dir / "research-card.schema.json").read_text("utf-8"))
        self._validator = Draft202012Validator(schema, format_checker=FormatChecker())

    def latest(self) -> tuple[list[ResearchCardSummary], list[str]]:
        self._root.mkdir(parents=True, exist_ok=True)
        latest_by_symbol: dict[str, tuple[Path, dict[str, Any]]] = {}
        warnings: list[str] = []

        for path in sorted(self._root.glob("*/*.json")):
            try:
                payload: dict[str, Any] = json.loads(path.read_text("utf-8"))
                errors = sorted(
                    self._validator.iter_errors(payload),
                    key=lambda error: list(error.path),
                )
                if errors:
                    warnings.append(f"{path.name}: schema 校验失败（{errors[0].message}）")
                    continue
                try:
                    validate_research_semantics(payload)
                except ResearchValidationError as exc:
                    warnings.append(f"{path.name}: 语义校验失败（{exc}）")
                    continue
                if _is_contract_baseline(payload):
                    continue
                symbol = str(payload["symbol"])
                current = latest_by_symbol.get(symbol)
                if current is None or str(payload["as_of"]) > str(current[1]["as_of"]):
                    latest_by_symbol[symbol] = (path, payload)
            except (OSError, json.JSONDecodeError, KeyError) as exc:
                warnings.append(f"{path.name}: 无法读取（{exc}）")

        result: list[ResearchCardSummary] = []
        for path, payload in latest_by_symbol.values():
            risk_flags = payload.get("risk_flags") or []
            maximum_risk = None
            if isinstance(risk_flags, list) and risk_flags:
                first = risk_flags[0]
                maximum_risk = first.get("text") if isinstance(first, dict) else str(first)
            result.append(
                ResearchCardSummary(
                    symbol=str(payload["symbol"]),
                    name=str(payload["name"]),
                    as_of=str(payload["as_of"]),
                    status=cast(ResearchStatus, payload["status"]),
                    score=float(payload["score"]),
                    confidence=float(payload["confidence"]),
                    summary=str(payload["summary"]),
                    maximum_risk=maximum_risk,
                    file_path=str(path.relative_to(self._root.parent.parent)),
                    valid_until=str(payload["valid_until"]) if payload.get("valid_until") else None,
                    data_freshness=cast(dict[str, Any], payload.get("data_freshness") or {}),
                )
            )
        return sorted(result, key=lambda card: (-card.score, card.symbol)), warnings

    def latest_for(self, symbol: str) -> tuple[dict[str, Any] | None, list[str]]:
        candidates, warnings = self.history_for(symbol)
        return (candidates[0] if candidates else None), warnings

    def history_for(self, symbol: str) -> tuple[list[dict[str, Any]], list[str]]:
        directory = self._root / symbol
        warnings: list[str] = []
        candidates: list[dict[str, Any]] = []
        for path in sorted(directory.glob("*.json")):
            try:
                payload: dict[str, Any] = json.loads(path.read_text("utf-8"))
                errors = sorted(
                    self._validator.iter_errors(payload),
                    key=lambda error: list(error.path),
                )
                if errors:
                    warnings.append(f"{path.name}: schema 校验失败（{errors[0].message}）")
                    continue
                try:
                    validate_research_semantics(payload)
                except ResearchValidationError as exc:
                    warnings.append(f"{path.name}: 语义校验失败（{exc}）")
                    continue
                if str(payload.get("symbol")) != symbol:
                    warnings.append(f"{path.name}: 文件目录与 symbol 不一致")
                    continue
                if _is_contract_baseline(payload):
                    continue
                payload["_file_path"] = str(path.relative_to(self._root.parent.parent))
                candidates.append(payload)
            except (OSError, json.JSONDecodeError) as exc:
                warnings.append(f"{path.name}: 无法读取（{exc}）")
        if not candidates:
            return [], warnings
        return sorted(candidates, key=lambda payload: str(payload["as_of"]), reverse=True), warnings

    def contract_baselines(self) -> tuple[list[dict[str, Any]], list[str]]:
        """Load regression fixtures without exposing them as formal research."""
        self._root.mkdir(parents=True, exist_ok=True)
        baselines: list[dict[str, Any]] = []
        warnings: list[str] = []
        for path in sorted(self._root.glob("*/*.json")):
            try:
                payload: dict[str, Any] = json.loads(path.read_text("utf-8"))
                errors = sorted(
                    self._validator.iter_errors(payload),
                    key=lambda error: list(error.path),
                )
                if errors:
                    warnings.append(f"{path.name}: schema 校验失败（{errors[0].message}）")
                    continue
                try:
                    validate_research_semantics(payload)
                except ResearchValidationError as exc:
                    warnings.append(f"{path.name}: 语义校验失败（{exc}）")
                    continue
                if not _is_contract_baseline(payload):
                    continue
                payload["_file_path"] = str(path.relative_to(self._root.parent.parent))
                baselines.append(payload)
            except (OSError, json.JSONDecodeError) as exc:
                warnings.append(f"{path.name}: 无法读取（{exc}）")
        return sorted(baselines, key=lambda payload: str(payload["symbol"])), warnings
