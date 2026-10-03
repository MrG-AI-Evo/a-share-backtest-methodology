from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
from uuid import uuid4

import duckdb

from app.core.settings import PROJECT_ROOT, Settings
from app.core.time import iso_now, now_shanghai


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_backup(settings: Settings, root: Path = PROJECT_ROOT) -> Path:
    backup = root / "backups" / f"{now_shanghai():%Y%m%dT%H%M%S}-{uuid4().hex[:8]}"
    backup.mkdir(parents=True, exist_ok=False)
    files: list[Path] = []

    if settings.paper_database.exists():
        paper_target = backup / "paper.sqlite3"
        with (
            sqlite3.connect(settings.paper_database) as source_database,
            sqlite3.connect(paper_target) as target_database,
        ):
            source_database.backup(target_database)
        files.append(paper_target)

    if settings.analytics_database.exists():
        with duckdb.connect(str(settings.analytics_database)) as connection:
            connection.execute("CHECKPOINT")
        analytics_target = backup / "analytics.duckdb"
        shutil.copy2(settings.analytics_database, analytics_target)
        files.append(analytics_target)

    for config_source in (
        root / "config" / "policy.yaml",
        root / "rules" / "a-share-rules.yaml",
    ):
        target = backup / config_source.name
        shutil.copy2(config_source, target)
        files.append(target)

    manifest = {
        "schema_version": "1.0.0",
        "created_at": iso_now(),
        "source_project": str(root),
        "restore_policy": "停止 API 后人工核对版本并恢复；禁止运行中覆盖数据库。",
        "files": [
            {
                "name": path.name,
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for path in files
        ],
    }
    (backup / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        "utf-8",
    )
    return backup


def main() -> None:
    print(create_backup(Settings()))


if __name__ == "__main__":
    main()
