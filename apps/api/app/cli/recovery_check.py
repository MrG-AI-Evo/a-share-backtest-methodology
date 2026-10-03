from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import duckdb

from app.core.settings import PROJECT_ROOT, Settings
from app.repositories.paper import PaperRepository


class RecoveryCheckError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _latest_backup(root: Path) -> Path:
    candidates = sorted((root / "backups").glob("*/manifest.json"))
    if not candidates:
        raise RecoveryCheckError("没有可用备份")
    return candidates[-1].parent


async def check_backup(backup: Path) -> dict[str, Any]:
    try:
        manifest = json.loads((backup / "manifest.json").read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryCheckError(f"无法读取备份清单: {exc}") from exc
    with tempfile.TemporaryDirectory(prefix="ashare-recovery-") as temporary:
        restore = Path(temporary)
        verified: list[str] = []
        for item in manifest.get("files", []):
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise RecoveryCheckError("备份清单文件项无效")
            source = backup / item["name"]
            if not source.is_file() or _sha256(source) != item.get("sha256"):
                raise RecoveryCheckError(f"备份哈希不匹配: {source.name}")
            shutil.copy2(source, restore / source.name)
            verified.append(source.name)

        paper_path = restore / "paper.sqlite3"
        ledger: dict[str, Any] | None = None
        if paper_path.exists():
            with sqlite3.connect(paper_path) as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise RecoveryCheckError("SQLite integrity_check 未通过")
            settings = Settings(state_dir=restore)
            repository = PaperRepository(settings)
            await repository.initialize()
            ledger = await repository.reconcile()
            if not ledger["reconciled"]:
                raise RecoveryCheckError("恢复后的模拟账本未闭合")

        analytics_path = restore / "analytics.duckdb"
        if analytics_path.exists():
            with duckdb.connect(str(analytics_path), read_only=True) as connection:
                connection.execute("SELECT 1").fetchone()
        return {
            "backup": str(backup),
            "verified_files": verified,
            "sqlite_integrity": "ok" if paper_path.exists() else "not_present",
            "duckdb_open": analytics_path.exists(),
            "ledger": ledger,
            "restore_target": "temporary_directory_removed_after_check",
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="在临时目录执行备份恢复与账本闭合演练")
    parser.add_argument("backup", nargs="?", type=Path)
    args = parser.parse_args()
    backup = args.backup or _latest_backup(PROJECT_ROOT)
    try:
        result = asyncio.run(check_backup(backup))
    except RecoveryCheckError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
