#!/usr/bin/env python3
"""Download a precomputed CNInfo annual-report plan to NAS.

This is a resumable, foreground-only, deterministic downloader. It never
starts a scheduler, runs a backtest, or promotes downloaded PDFs to formal
facts. Each completed PDF is validated, hashed, and appended to a JSONL
manifest before it can be used by a later extraction job.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def now() -> str:
    return datetime.now(UTC).isoformat()


def sha256_file(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def load_plan(path: Path, start: str | None, end: str | None, limit: int | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            known_date = str(row.get("known_at") or "")[:10]
            if start and known_date < start:
                continue
            if end and known_date > end:
                continue
            rows.append(row)
            if limit and len(rows) >= limit:
                break
    return rows


def destination(root: Path, row: dict[str, Any]) -> Path:
    return root / str(row["symbol"]) / f"{row['announcement_id']}.pdf"


def fetch_one(root: Path, row: dict[str, Any], timeout: float, retries: int) -> dict[str, Any]:
    path = destination(root, row)
    if path.is_file() and path.stat().st_size > 1024:
        with path.open("rb") as handle:
            magic = handle.read(5)
        if magic == b"%PDF-":
            return {
                "status": "ALREADY_PRESENT_VERIFIED",
                "symbol": row["symbol"],
                "announcement_id": row["announcement_id"],
                "known_at": row.get("known_at"),
                "pdf_url": row["pdf_url"],
                "path": str(path),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
                "completed_at": now(),
            }
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".pdf.part")
    error: str | None = None
    for attempt in range(1, retries + 2):
        try:
            request = Request(row["pdf_url"], headers={"User-Agent": "A-Share-Research-Audit/1.0"})
            with urlopen(request, timeout=timeout) as response, partial.open("wb") as output:
                first = response.read(5)
                if first != b"%PDF-":
                    raise ValueError("response is not a PDF")
                output.write(first)
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
            if partial.stat().st_size <= 1024:
                raise ValueError("downloaded PDF is unexpectedly small")
            os.replace(partial, path)
            return {
                "status": "DOWNLOADED_VERIFIED",
                "symbol": row["symbol"],
                "announcement_id": row["announcement_id"],
                "known_at": row.get("known_at"),
                "pdf_url": row["pdf_url"],
                "path": str(path),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
                "attempts": attempt,
                "completed_at": now(),
            }
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]
            if partial.exists():
                partial.unlink()
            if attempt <= retries:
                time.sleep(min(2**attempt, 8))
    return {
        "status": "FAILED",
        "symbol": row["symbol"],
        "announcement_id": row["announcement_id"],
        "known_at": row.get("known_at"),
        "pdf_url": row["pdf_url"],
        "error": error,
        "attempts": retries + 1,
        "completed_at": now(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--nas-root", type=Path, required=True)
    parser.add_argument("--start-known-at")
    parser.add_argument("--end-known-at")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--minimum-free-gib", type=float, default=100.0)
    args = parser.parse_args()
    if not 1 <= args.workers <= 8:
        raise RuntimeError("workers must be between 1 and 8")

    plan_path = args.plan.resolve()
    nas_root = args.nas_root.resolve()
    state_dir = PROJECT_ROOT / "data" / "backtests" / "staging" / args.run_id
    state_dir.mkdir(parents=True, exist_ok=True)
    state_identity = state_dir / "identity.json"
    manifest_path = state_dir / "download-manifest.jsonl"
    receipt_path = state_dir / "receipt.json"
    audit_path = PROJECT_ROOT / "data" / "audit" / f"{args.run_id}-cninfo-quality-pdfs.json"
    if receipt_path.exists() or audit_path.exists():
        raise RuntimeError("completed run is immutable; use a new run_id")
    if not plan_path.is_file() or not plan_path.stat().st_size:
        raise RuntimeError("download plan is missing or empty")
    nas_root.mkdir(parents=True, exist_ok=True)
    free_gib = os.statvfs(nas_root).f_bavail * os.statvfs(nas_root).f_frsize / 1024**3
    if free_gib < args.minimum_free_gib:
        raise RuntimeError(f"NAS free space {free_gib:.2f} GiB is below required {args.minimum_free_gib:.2f} GiB")

    identity = {
        "run_id": args.run_id,
        "workflow": "BACKTEST_CNINFO_QUALITY_PDF_DOWNLOAD",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "plan": str(plan_path.relative_to(PROJECT_ROOT)),
        "plan_sha256": sha256_file(plan_path),
        "nas_root": str(nas_root),
        "start_known_at": args.start_known_at,
        "end_known_at": args.end_known_at,
        "limit": args.limit,
    }
    if state_identity.exists():
        if json.loads(state_identity.read_text(encoding="utf-8")) != identity:
            raise RuntimeError("resume identity does not match the original run")
    else:
        state_identity.write_text(json.dumps(identity, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    rows = load_plan(plan_path, args.start_known_at, args.end_known_at, args.limit)
    if not rows:
        raise RuntimeError("filtered plan is empty")
    completed_ids: set[tuple[str, str]] = set()
    previous: list[dict[str, Any]] = []
    if manifest_path.exists():
        with manifest_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                record = json.loads(line)
                previous.append(record)
                if record["status"] in {"DOWNLOADED_VERIFIED", "ALREADY_PRESENT_VERIFIED"}:
                    completed_ids.add((record["symbol"], record["announcement_id"]))
    pending = [row for row in rows if (row["symbol"], row["announcement_id"]) not in completed_ids]
    lock = threading.Lock()
    completed_now = 0
    failed_now = 0
    started_at = now()
    with manifest_path.open("a", encoding="utf-8") as manifest, ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch_one, nas_root, row, args.timeout, args.retries): row for row in pending}
        for future in as_completed(futures):
            record = future.result()
            with lock:
                manifest.write(json.dumps(record, ensure_ascii=False) + "\n")
                manifest.flush()
                os.fsync(manifest.fileno())
                completed_now += 1
                failed_now += record["status"] == "FAILED"
                if completed_now % 100 == 0 or completed_now == len(pending):
                    print(
                        json.dumps(
                            {
                                "processed_this_run": completed_now,
                                "pending_at_start": len(pending),
                                "failed_this_run": failed_now,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )

    records = []
    with manifest_path.open("r", encoding="utf-8") as handle:
        records = [json.loads(line) for line in handle]
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        latest[(record["symbol"], record["announcement_id"])] = record
    relevant = [latest.get((row["symbol"], row["announcement_id"])) for row in rows]
    failed = [record for record in relevant if not record or record.get("status") == "FAILED"]
    delivered = [record for record in relevant if record and record.get("status") != "FAILED"]
    receipt = {
        "schema_version": "1.0.0",
        **identity,
        "status": "STAGING_COMPLETE_NOT_FORMAL" if not failed else "STAGING_PARTIAL_NOT_FORMAL",
        "started_at": started_at,
        "completed_at": now(),
        "summary": {
            "planned": len(rows),
            "delivered": len(delivered),
            "failed": len(failed),
            "bytes": sum(int(record.get("bytes") or 0) for record in delivered),
            "nas_free_gib_before": round(free_gib, 3),
        },
        "manifest": {"path": str(manifest_path.relative_to(PROJECT_ROOT)), "sha256": sha256_file(manifest_path)},
        "formal_backtest_eligibility": {
            "eligible": False,
            "reasons": [
                "Downloaded filing bodies still require deterministic extraction and revision-lineage validation.",
                "The numerical quality thresholds and effective dates are not frozen.",
                "This downloader never promotes documents or generates trading decisions.",
            ],
        },
    }
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "event_type": "BACKTEST_CNINFO_QUALITY_PDFS_DOWNLOADED",
                "run_id": args.run_id,
                "status": receipt["status"],
                "scheduler_status": receipt["scheduler_status"],
                "receipt": {"path": str(receipt_path.relative_to(PROJECT_ROOT)), "sha256": sha256_file(receipt_path)},
                "created_at": receipt["completed_at"],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(receipt["summary"], ensure_ascii=False), flush=True)
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
