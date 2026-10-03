#!/usr/bin/env python3
"""Download one user-approved public reference into append-only staging.

This is a foreground-only deterministic utility. It never schedules itself and
refuses to overwrite either the raw asset or its receipt/audit event.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-class", default="SECONDARY_PUBLIC_REFERENCE")
    parser.add_argument("--timeout-seconds", type=int, default=90)
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    args = parse_args()
    output = args.output.resolve()
    if PROJECT_ROOT not in output.parents:
        raise RuntimeError("output must be inside the project")
    receipt_dir = PROJECT_ROOT / "data" / "backtests" / "staging" / f"reference-download-{args.run_id}"
    receipt_path = receipt_dir / "download-receipt.json"
    audit_path = PROJECT_ROOT / "data" / "audit" / f"{args.run_id}-reference-download.json"
    if output.exists() or receipt_dir.exists() or audit_path.exists():
        raise RuntimeError("refusing to overwrite existing output, receipt, or audit event")
    output.parent.mkdir(parents=True, exist_ok=True)
    started_at = utc_now()
    process = subprocess.run(
        [
            "/usr/bin/curl", "--fail", "--location", "--retry", "2",
            "--connect-timeout", "15", "--max-time", str(args.timeout_seconds),
            "--silent", "--show-error", "--user-agent", "A-Share-Research-Audit/1.0",
            "--output", str(output), args.url,
        ],
        capture_output=True,
        check=False,
    )
    if process.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        if output.exists():
            output.unlink()
        error = process.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"download failed exit={process.returncode}: {error}")
    completed_at = utc_now()
    receipt = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "workflow": "BACKTEST_REFERENCE_EVIDENCE_DOWNLOAD",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "source_class": args.source_class,
        "source_uri": args.url,
        "raw_file": str(output.relative_to(PROJECT_ROOT)),
        "bytes": output.stat().st_size,
        "sha256": sha256(output),
        "started_at": started_at,
        "completed_at": completed_at,
    }
    receipt_dir.mkdir(parents=True, exist_ok=False)
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit = {
        "schema_version": "1.0.0",
        "event_type": "BACKTEST_REFERENCE_EVIDENCE_DOWNLOADED",
        "run_id": args.run_id,
        "status": "COMPLETE",
        "receipt": str(receipt_path.relative_to(PROJECT_ROOT)),
        "receipt_sha256": sha256(receipt_path),
        "created_at": completed_at,
    }
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"status": "FAILED", "error_type": type(error).__name__, "message": str(error)}, ensure_ascii=False))
        raise SystemExit(1)
