from __future__ import annotations

import json
import os
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker

from app.core.settings import Settings
from app.models import UniverseSnapshot


class UniverseArtifactError(RuntimeError):
    pass


class UniverseRepository:
    def __init__(self, settings: Settings) -> None:
        self._root = settings.normalized_dir / "universe"
        schema_path = settings.schema_dir / "universe-snapshot.schema.json"
        schema = json.loads(schema_path.read_text("utf-8"))
        self._validator = Draft202012Validator(schema, format_checker=FormatChecker())

    def append(self, snapshot: UniverseSnapshot) -> UniverseSnapshot:
        directory = self._root / snapshot.as_of[:10]
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{snapshot.run_id}.json"
        if target.exists():
            raise UniverseArtifactError("同一 run_id 的全市场快照已存在，禁止覆盖")
        relative = target.relative_to(self._root.parent.parent.parent)
        persisted = snapshot.model_copy(update={"artifact_path": str(relative)})
        payload = persisted.model_dump(mode="json")
        errors = sorted(self._validator.iter_errors(payload), key=lambda error: list(error.path))
        if errors:
            raise UniverseArtifactError(f"全市场快照 Schema 校验失败: {errors[0].message}")
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
            raise UniverseArtifactError(f"无法追加全市场快照: {exc}") from exc
        return persisted
