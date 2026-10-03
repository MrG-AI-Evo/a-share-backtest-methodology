from __future__ import annotations

import json
from datetime import datetime

from jsonschema import Draft202012Validator, FormatChecker

from app.core.settings import Settings
from app.models import CorporateAction


class CorporateActionError(RuntimeError):
    pass


class CorporateActionRepository:
    def __init__(self, settings: Settings) -> None:
        self._root = settings.corporate_action_dir
        schema = json.loads(
            (settings.schema_dir / "corporate-action.schema.json").read_text("utf-8")
        )
        self._validator = Draft202012Validator(schema, format_checker=FormatChecker())

    def for_symbol(self, symbol: str, as_of: datetime) -> tuple[list[CorporateAction], list[str]]:
        if as_of.tzinfo is None:
            raise CorporateActionError("公司行动查询 as_of 必须带时区")
        actions: list[CorporateAction] = []
        warnings: list[str] = []
        for path in sorted((self._root / symbol).glob("*.json")):
            try:
                payload = json.loads(path.read_text("utf-8"))
                errors = sorted(
                    self._validator.iter_errors(payload), key=lambda error: list(error.path)
                )
                if errors:
                    warnings.append(f"{path.name}: {errors[0].message}")
                    continue
                action = CorporateAction.model_validate(payload)
                announced = datetime.fromisoformat(action.announced_at)
                if announced.tzinfo is None:
                    warnings.append(f"{path.name}: announced_at 缺少时区")
                    continue
                if announced <= as_of and action.effective_date <= as_of.date().isoformat():
                    actions.append(action)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                warnings.append(f"{path.name}: 无法读取（{exc}）")
        actions.sort(key=lambda item: (item.effective_date, item.action_id))
        return actions, warnings
