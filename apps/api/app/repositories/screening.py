from __future__ import annotations

import json
import os
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker

from app.core.settings import Settings
from app.models import ScreeningInvalidation, ScreeningRun


class ScreeningArtifactError(RuntimeError):
    pass


class ScreeningRepository:
    def __init__(self, settings: Settings) -> None:
        self._root = settings.screening_dir
        self._invalidation_root = settings.screening_invalidation_dir
        schema = json.loads((settings.schema_dir / "screening-run.schema.json").read_text("utf-8"))
        self._validator = Draft202012Validator(schema, format_checker=FormatChecker())
        invalidation_schema = json.loads(
            (settings.schema_dir / "screening-invalidation.schema.json").read_text("utf-8")
        )
        self._invalidation_validator = Draft202012Validator(
            invalidation_schema,
            format_checker=FormatChecker(),
        )

    def append(self, run: ScreeningRun) -> ScreeningRun:
        trade_date = run.as_of[:10]
        directory = self._root / trade_date
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{run.run_id}.json"
        if target.exists():
            raise ScreeningArtifactError("同一 run_id 的筛选产物已存在，禁止覆盖")
        relative = target.relative_to(self._root.parent.parent)
        persisted = run.model_copy(update={"artifact_path": str(relative)})
        payload = persisted.model_dump(mode="json")
        errors = sorted(self._validator.iter_errors(payload), key=lambda error: list(error.path))
        if errors:
            raise ScreeningArtifactError(f"筛选产物 Schema 校验失败: {errors[0].message}")
        temporary = directory / f".{target.name}.{uuid4().hex}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise ScreeningArtifactError(f"无法追加筛选产物: {exc}") from exc
        return persisted

    def latest(self) -> ScreeningRun | None:
        invalidated = self.invalidated_run_ids()
        for path in reversed(sorted(self._root.glob("*/*.json"))):
            run = ScreeningRun.model_validate_json(path.read_text("utf-8"))
            if run.run_id not in invalidated:
                return run
        return None

    def invalidated_run_ids(self) -> set[str]:
        output: set[str] = set()
        for path in sorted(self._invalidation_root.glob("*/*.json")):
            item = ScreeningInvalidation.model_validate_json(path.read_text("utf-8"))
            output.add(item.run_id)
        return output

    def invalidate(self, item: ScreeningInvalidation) -> None:
        directory = self._invalidation_root / item.invalidated_at[:10]
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{item.invalidation_id}.json"
        if target.exists():
            raise ScreeningArtifactError("同一筛选失效事件已存在，禁止覆盖")
        payload = item.model_dump(mode="json")
        errors = sorted(
            self._invalidation_validator.iter_errors(payload),
            key=lambda error: list(error.path),
        )
        if errors:
            raise ScreeningArtifactError(f"筛选失效事件 Schema 校验失败: {errors[0].message}")
        temporary = directory / f".{target.name}.{uuid4().hex}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise ScreeningArtifactError(f"无法追加筛选失效事件: {exc}") from exc
