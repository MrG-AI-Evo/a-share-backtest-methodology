#!/usr/bin/env python3
"""Repair one timed-out CNInfo extraction with Poppler, without mutating the parent run."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract_cninfo_quality_facts import (
    FORMAL_STATUS,
    audit_opinion,
    going_concern,
    load_jsonl,
    metric_candidates,
    report_year,
    sha256_file,
)

PARSER_VERSION = "cninfo-annual-report-text-v3-poppler-repair"


def now() -> str:
    return datetime.now(UTC).isoformat()


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def split_pages(text: str) -> list[str]:
    pages = text.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return pages


def keyed(record: dict[str, Any]) -> tuple[str, str]:
    if record.get("_failed"):
        return str(record["symbol"]), str(record["announcement_id"])
    return str(record["source"]["symbol"]), str(record["source"]["announcement_id"])


def unresolved_keys(records: list[dict[str, Any]]) -> set[tuple[str, str]]:
    successful = {keyed(record) for record in records if not record.get("_failed")}
    failed = {keyed(record) for record in records if record.get("_failed")}
    return failed - successful


def select_input(plan_path: Path, manifest_path: Path, key: tuple[str, str]) -> dict[str, Any]:
    plans = {keyed(row): row for row in load_jsonl(plan_path)}
    downloads = {
        keyed(row): row
        for row in load_jsonl(manifest_path)
        if row.get("status") != "FAILED"
    }
    if key not in plans or key not in downloads:
        raise RuntimeError(f"repair key is absent from plan or successful download manifest: {key}")
    row = {**plans[key], **downloads[key]}
    source = Path(row["path"]).resolve()
    if not source.is_file():
        raise RuntimeError(f"source PDF is missing: {source}")
    if source.stat().st_size != int(row["bytes"]) or sha256_file(source) != row["sha256"]:
        raise RuntimeError(f"source PDF integrity mismatch: {key}")
    return {**row, "path": str(source)}


def poppler_version(binary: Path) -> str:
    result = subprocess.run([str(binary), "-v"], capture_output=True, text=True, timeout=15, check=False)
    output = (result.stdout + "\n" + result.stderr).strip().splitlines()
    return output[0][:300] if output else "UNKNOWN"


def extract_with_poppler(row: dict[str, Any], binary: Path, timeout_seconds: float) -> tuple[dict[str, Any], dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="cninfo-poppler-repair-") as temp_dir:
        text_path = Path(temp_dir) / "document.txt"
        result = subprocess.run(
            [str(binary), "-layout", "-enc", "UTF-8", row["path"], str(text_path)],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        if result.returncode != 0 or not text_path.is_file():
            raise RuntimeError(
                f"pdftotext failed with exit {result.returncode}: {result.stderr[:1000]}"
            )
        pages = split_pages(text_path.read_text(encoding="utf-8", errors="replace"))

    page_count = len(pages)
    text_chars = sum(len(value) for value in pages)
    readable = text_chars >= max(1000, page_count * 20)
    year = report_year(row, pages)
    record = {
        "schema_version": "1.0.0",
        "parser_version": PARSER_VERSION,
        "source": {
            "symbol": row["symbol"],
            "issuer_name": row["issuer_name"],
            "announcement_id": row["announcement_id"],
            "title": row["title"],
            "known_at": row["known_at"],
            "pdf_url": row["pdf_url"],
            "path": row["path"],
            "bytes": int(row["bytes"]),
            "sha256": row["sha256"],
        },
        "document": {
            "page_count": page_count,
            "text_char_count": text_chars,
            "report_year": year,
            "extractable": readable,
            "encrypted": False,
            "page_errors": 0,
        },
        "audit_opinion": audit_opinion(pages, readable),
        "going_concern": going_concern(pages, readable),
        "metric_candidates": metric_candidates(pages) if readable else [],
        "temporal": {
            "period_end": f"{year}-12-31" if year else None,
            "known_at": row["known_at"],
            "revision_role": "ORIGINAL_OR_UNRESOLVED",
            "supersedes_announcement_id": None,
        },
        "formal_status": FORMAL_STATUS,
    }
    diagnostics = {
        "exit_code": result.returncode,
        "stderr": result.stderr[:4000],
        "page_separators": page_count,
        "text_bytes": len("\f".join(pages).encode("utf-8")),
    }
    return record, diagnostics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--parent-run-id", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--announcement-id", required=True)
    parser.add_argument("--pdftotext", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        raise RuntimeError("timeout-seconds must be positive")

    key = (args.symbol, args.announcement_id)
    parent_dir = PROJECT_ROOT / "data" / "backtests" / "staging" / args.parent_run_id
    parent_receipt_path = parent_dir / "receipt.json"
    parent_raw_path = parent_dir / "extractions-append.jsonl"
    parent_audit_path = PROJECT_ROOT / "data" / "audit" / f"{args.parent_run_id}-cninfo-extraction.json"
    if not parent_receipt_path.is_file() or not parent_raw_path.is_file() or not parent_audit_path.is_file():
        raise RuntimeError("parent run receipt, raw append evidence, or audit event is missing")

    parent_receipt = json.loads(parent_receipt_path.read_text(encoding="utf-8"))
    parent_audit = json.loads(parent_audit_path.read_text(encoding="utf-8"))
    if sha256_file(parent_receipt_path) != parent_audit["receipt"]["sha256"]:
        raise RuntimeError("parent receipt hash no longer matches its audit event")
    parent_records = load_jsonl(parent_raw_path)
    unresolved = unresolved_keys(parent_records)
    if unresolved != {key}:
        raise RuntimeError(f"parent unresolved key set is not exactly the requested repair: {sorted(unresolved)}")

    output_dir = PROJECT_ROOT / "data" / "backtests" / "staging" / args.run_id
    receipt_path = output_dir / "receipt.json"
    audit_path = PROJECT_ROOT / "data" / "audit" / f"{args.run_id}-cninfo-poppler-repair.json"
    resolution_path = PROJECT_ROOT / "data" / "audit" / f"{args.parent_run_id}-resolved-by-{args.run_id}.json"
    if output_dir.exists() or audit_path.exists() or resolution_path.exists():
        raise RuntimeError("repair run is immutable; use a fresh run_id")
    output_dir.mkdir(parents=True)

    plan_path = PROJECT_ROOT / parent_receipt["plan"]
    manifest_path = PROJECT_ROOT / parent_receipt["download_manifest"]
    binary = args.pdftotext.resolve()
    if not binary.is_file():
        raise RuntimeError(f"pdftotext binary is missing: {binary}")
    row = select_input(plan_path, manifest_path, key)
    started_at = now()
    record, diagnostics = extract_with_poppler(row, binary, args.timeout_seconds)

    schema_path = PROJECT_ROOT / "schemas" / "cninfo-annual-report-extraction.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(record))
    if errors:
        raise RuntimeError("repair record schema validation failed: " + "; ".join(error.message for error in errors[:5]))

    record_path = output_dir / "annual-report-fact-candidate.json"
    write_json(record_path, record)
    identity = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "workflow": "BACKTEST_CNINFO_ANNUAL_REPORT_POPPLER_REPAIR",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "parent_run_id": args.parent_run_id,
        "parent_receipt_sha256": sha256_file(parent_receipt_path),
        "repair_key": list(key),
        "parser_version": PARSER_VERSION,
        "pdftotext": str(binary),
        "pdftotext_version": poppler_version(binary),
        "timeout_seconds": args.timeout_seconds,
        "source_pdf_sha256": row["sha256"],
        "schema_sha256": sha256_file(schema_path),
    }
    identity_path = output_dir / "identity.json"
    write_json(identity_path, identity)
    parent_successes = len({keyed(item) for item in parent_records if not item.get("_failed")})
    receipt = {
        **identity,
        "status": "STAGING_REPAIR_COMPLETE_NOT_FORMAL",
        "started_at": started_at,
        "completed_at": now(),
        "summary": {
            "repaired": 1,
            "schema_validation_errors": 0,
            "parent_unique_successes": parent_successes,
            "combined_unique_successes": parent_successes + 1,
            "combined_expected": int(parent_receipt["summary"]["downloaded_joined"]),
            "combined_unresolved_failures": 0,
        },
        "diagnostics": diagnostics,
        "artifacts": {
            "identity": {"path": str(identity_path.relative_to(PROJECT_ROOT)), "sha256": sha256_file(identity_path)},
            "record": {"path": str(record_path.relative_to(PROJECT_ROOT)), "sha256": sha256_file(record_path)},
        },
        "formal_backtest_eligibility": {
            "eligible": False,
            "reasons": parent_receipt["formal_backtest_eligibility"]["reasons"],
        },
    }
    write_json(receipt_path, receipt)
    audit = {
        "schema_version": "1.0.0",
        "event_type": "BACKTEST_CNINFO_ANNUAL_REPORT_POPPLER_REPAIRED",
        "run_id": args.run_id,
        "parent_run_id": args.parent_run_id,
        "status": receipt["status"],
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "receipt": {"path": str(receipt_path.relative_to(PROJECT_ROOT)), "sha256": sha256_file(receipt_path)},
        "created_at": receipt["completed_at"],
    }
    write_json(audit_path, audit)
    resolution = {
        "schema_version": "1.0.0",
        "event_type": "BACKTEST_CNINFO_EXTRACTION_FAILURE_RESOLVED",
        "parent_run_id": args.parent_run_id,
        "repair_run_id": args.run_id,
        "resolved_key": list(key),
        "parent_receipt": {"path": str(parent_receipt_path.relative_to(PROJECT_ROOT)), "sha256": sha256_file(parent_receipt_path)},
        "repair_receipt": {"path": str(receipt_path.relative_to(PROJECT_ROOT)), "sha256": sha256_file(receipt_path)},
        "combined_unique_successes": parent_successes + 1,
        "combined_expected": int(parent_receipt["summary"]["downloaded_joined"]),
        "combined_unresolved_failures": 0,
        "formal_status": "STAGING_COMPLETE_NOT_FORMAL",
        "created_at": receipt["completed_at"],
    }
    write_json(resolution_path, resolution)
    print(json.dumps({"receipt": receipt, "resolution": resolution}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
