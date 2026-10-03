#!/usr/bin/env python3
"""Extract auditable fact candidates from downloaded CNInfo annual reports.

This foreground-only task is deterministic and resumable. It classifies audit
opinions, flags going-concern language, captures a small set of raw metric
candidates, and derives report revision lineage. It never makes trading
decisions or promotes candidates to formal point-in-time facts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import threading
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from pypdf import PdfReader
from pypdf.errors import FileNotDecryptedError, PdfReadError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PARSER_VERSION = "cninfo-annual-report-text-v2-process-pool"
FORMAL_STATUS = "STAGING_ONLY_NOT_FORMAL_BACKTEST_INPUT"
DEFAULT_DOCUMENT_TIMEOUT_SECONDS = 120.0
REVISION_TERMS = ("修订", "更正", "更新后", "补充后")
GOING_CONCERN_PATTERNS = (
    "与持续经营相关的重大不确定性",
    "持续经营能力存在重大不确定性",
    "持续经营存在重大不确定性",
    "持续经营重大不确定性",
)
AUDIT_PATTERNS = (
    ("DISCLAIMER", ("无法表示意见", "无法发表意见")),
    ("ADVERSE", ("否定意见",)),
    ("UNQUALIFIED_WITH_EMPHASIS", ("带强调事项段的无保留意见", "带持续经营重大不确定性段的无保留意见")),
    ("STANDARD_UNQUALIFIED", ("标准无保留意见", "标准无保留审计意见")),
    ("QUALIFIED", ("保留意见",)),
)
METRIC_PATTERNS = {
    "revenue": ("营业收入",),
    "net_profit_parent": (
        "归属于上市公司股东的净利润",
        "归属于母公司股东的净利润",
        "归属于母公司股东净利润",
        "归属于母公司所有者净利润",
    ),
    "operating_cash_flow": ("经营活动产生的现金流量净额",),
    "basic_eps": ("基本每股收益",),
    "weighted_roe": ("加权平均净资产收益率",),
    "debt_asset_ratio": ("资产负债率",),
}
VALUE_PATTERN = re.compile(r"(?<![0-9])([\(（]?[-+]?[0-9][0-9,]*(?:\.[0-9]+)?[%％]?[\)）]?)(?![0-9])")


def now() -> str:
    return datetime.now(UTC).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def compact_for_search(value: str) -> str:
    """Collapse PDF line-wrap spaces that split a Chinese label mid-word."""
    result = compact(value)
    return re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", result)


def excerpt(text: str, start: int, width: int = 520) -> str:
    return compact(text[max(0, start - 100) : start + width])[:800]


def report_year(row: dict[str, Any], pages: list[str]) -> int | None:
    for value in (str(row.get("title") or ""), *(pages[:5])):
        match = re.search(r"(20[0-9]{2})\s*年(?:度|年度)?报告", value)
        if match:
            return int(match.group(1))
    return None


def audit_opinion(pages: list[str], readable: bool) -> dict[str, Any]:
    if not readable:
        return {"classification": "UNREADABLE", "evidence_page": None, "evidence_excerpt": None, "evidence_sha256": None}
    hits: list[tuple[int, int, str, str]] = []
    for page_number, text in enumerate(pages, 1):
        compacted = compact_for_search(text)
        for priority, (classification, phrases) in enumerate(AUDIT_PATTERNS):
            for phrase in phrases:
                position = compacted.find(phrase)
                if position >= 0:
                    if classification == "QUALIFIED" and position > 0 and compacted[position - 1] == "无":
                        continue
                    hits.append((priority, page_number, classification, excerpt(compacted, position)))
    if not hits:
        # Older annual reports sometimes omit the literal label “标准无保留意见”
        # but include the canonical unmodified opinion paragraph. Only use this
        # fallback when no adverse/qualified phrase matched anywhere above.
        for page_number, text in enumerate(pages, 1):
            compacted = compact(text)
            markers = ("审计意见", "我们认为", "在所有重大方面", "公允反映")
            if all(marker in compacted for marker in markers):
                position = compacted.find("我们认为")
                evidence = excerpt(compacted, position)
                return {
                    "classification": "STANDARD_UNQUALIFIED",
                    "evidence_page": page_number,
                    "evidence_excerpt": evidence,
                    "evidence_sha256": sha256_text(evidence),
                }
        return {"classification": "NOT_FOUND", "evidence_page": None, "evidence_excerpt": None, "evidence_sha256": None}
    _, page_number, classification, evidence = min(hits)
    return {
        "classification": classification,
        "evidence_page": page_number,
        "evidence_excerpt": evidence,
        "evidence_sha256": sha256_text(evidence),
    }


def going_concern(pages: list[str], readable: bool) -> dict[str, Any]:
    if not readable:
        return {"status": "UNREADABLE", "evidence_pages": [], "evidence_excerpts": []}
    hits: list[tuple[int, str]] = []
    for page_number, text in enumerate(pages, 1):
        compacted = compact(text)
        for phrase in GOING_CONCERN_PATTERNS:
            position = compacted.find(phrase)
            if position >= 0:
                hits.append((page_number, excerpt(compacted, position)))
                break
        if len(hits) >= 5:
            break
    return {
        "status": "FLAG_FOUND" if hits else "NOT_FOUND_IN_EXTRACTED_TEXT",
        "evidence_pages": sorted({page for page, _ in hits}),
        "evidence_excerpts": [value for _, value in hits],
    }


def unit_context(text: str, position: int) -> str | None:
    before = compact(text[max(0, position - 1200) : position])
    matches = list(re.finditer(r"(?:金额|货币)?单位[：:]\s*([^，。；;]{1,40})", before))
    return matches[-1].group(1).strip() if matches else None


def metric_candidates(pages: list[str]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for page_number, text in enumerate(pages[:80], 1):
        compacted = compact_for_search(text)
        for metric_id, labels in METRIC_PATTERNS.items():
            if any(item["metric_id"] == metric_id for item in results):
                continue
            for label in labels:
                position = compacted.find(label)
                if position < 0:
                    continue
                value_match = VALUE_PATTERN.search(compacted, position + len(label), position + len(label) + 180)
                if not value_match:
                    continue
                raw_value = value_match.group(1)
                if metric_id in {"revenue", "net_profit_parent", "operating_cash_flow"} and raw_value.endswith(("%", "％")):
                    # Narrative dividend/payout sentences often say “占归母净利润的
                    # 35%”. That percentage is not the profit amount; keep scanning
                    # later pages for the actual statement/table value.
                    continue
                results.append(
                    {
                        "metric_id": metric_id,
                        "raw_value": raw_value,
                        "unit_context": unit_context(compacted, position),
                        "page": page_number,
                        "status": "CANDIDATE_NEEDS_VALIDATION",
                    }
                )
                break
    return sorted(results, key=lambda item: item["metric_id"])


class DocumentExtractionTimeout(RuntimeError):
    """Raised when one PDF exceeds the deterministic extraction time budget."""


def _timeout_handler(_signum: int, _frame: Any) -> None:
    raise DocumentExtractionTimeout("document extraction exceeded its time budget")


def _extract_one_unbounded(row: dict[str, Any]) -> dict[str, Any]:
    path = Path(row["path"])
    pages: list[str] = []
    page_errors = 0
    encrypted = False
    try:
        reader = PdfReader(path, strict=False)
        encrypted = bool(reader.is_encrypted)
        if encrypted and reader.decrypt("") == 0:
            return {
                "_failed": True,
                "symbol": row["symbol"],
                "announcement_id": row["announcement_id"],
                "error": "encrypted PDF cannot be opened with an empty password",
            }
        page_count = len(reader.pages)
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except (PdfReadError, FileNotDecryptedError, OSError, ValueError, KeyError, TypeError):
                pages.append("")
                page_errors += 1
    except (PdfReadError, FileNotDecryptedError, OSError, ValueError, KeyError, TypeError) as exc:
        return {"_failed": True, "symbol": row["symbol"], "announcement_id": row["announcement_id"], "error": f"{type(exc).__name__}: {exc}"[:500]}
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
            "encrypted": encrypted,
            "page_errors": page_errors,
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
    return record


def extract_one(row: dict[str, Any], timeout_seconds: float = DEFAULT_DOCUMENT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Extract one document while preventing a malformed PDF from blocking a worker forever.

    Process-pool workers execute this function on their main thread on macOS, so a
    POSIX real-time timer can interrupt both pypdf traversal and Python regex work.
    Successful extraction semantics remain unchanged; a timeout becomes an
    explicit, auditable failure record that can be retried from a repaired input.
    """
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
        raise RuntimeError("per-document hard timeout requires POSIX signal support")

    previous_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        return _extract_one_unbounded(row)
    except DocumentExtractionTimeout:
        return {
            "_failed": True,
            "symbol": row["symbol"],
            "announcement_id": row["announcement_id"],
            "path": row["path"],
            "error": f"DOCUMENT_TIMEOUT: exceeded {timeout_seconds:g} seconds",
        }
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def apply_revision_lineage(records: list[dict[str, Any]]) -> None:
    groups: dict[tuple[str, int | None], list[dict[str, Any]]] = {}
    for record in records:
        key = (record["source"]["symbol"], record["document"]["report_year"])
        groups.setdefault(key, []).append(record)
    for values in groups.values():
        values.sort(key=lambda item: (item["source"]["known_at"], item["source"]["announcement_id"]))
        prior: dict[str, Any] | None = None
        for record in values:
            title = record["source"]["title"]
            if prior is not None and any(term in title for term in REVISION_TERMS):
                prior["temporal"]["revision_role"] = "SUPERSEDED"
                record["temporal"]["revision_role"] = "REVISION"
                record["temporal"]["supersedes_announcement_id"] = prior["source"]["announcement_id"]
            prior = record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--download-manifest", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--document-timeout-seconds", type=float, default=DEFAULT_DOCUMENT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--repair-overrides",
        type=Path,
        help="Optional audited JSONL replacements keyed by symbol and announcement_id.",
    )
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    if not 1 <= args.workers <= 8:
        raise RuntimeError("workers must be between 1 and 8")
    if args.document_timeout_seconds <= 0:
        raise RuntimeError("document-timeout-seconds must be positive")

    output_dir = PROJECT_ROOT / "data" / "backtests" / "staging" / args.run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    identity_path = output_dir / "identity.json"
    raw_path = output_dir / "extractions-append.jsonl"
    normalized_path = output_dir / "annual-report-fact-candidates.jsonl"
    receipt_path = output_dir / "receipt.json"
    audit_path = PROJECT_ROOT / "data" / "audit" / f"{args.run_id}-cninfo-extraction.json"
    if receipt_path.exists() or audit_path.exists():
        raise RuntimeError("completed run is immutable; use a new run_id")

    plan_path = args.plan.resolve()
    manifest_path = args.download_manifest.resolve()
    plan_rows = load_jsonl(plan_path)
    if args.limit:
        plan_rows = plan_rows[: args.limit]
    downloads = {(row["symbol"], row["announcement_id"]): row for row in load_jsonl(manifest_path) if row["status"] != "FAILED"}
    rows = []
    for row in plan_rows:
        download = downloads.get((row["symbol"], row["announcement_id"]))
        if download:
            rows.append({**row, **download})

    repair_overrides: dict[tuple[str, str], dict[str, Any]] = {}
    repair_override_path: Path | None = None
    if args.repair_overrides:
        repair_override_path = args.repair_overrides.resolve()
        for override in load_jsonl(repair_override_path):
            key = (str(override["symbol"]), str(override["announcement_id"]))
            if key in repair_overrides:
                raise RuntimeError(f"duplicate repair override: {key}")
            replacement = Path(override["path"]).resolve()
            if not replacement.is_file():
                raise RuntimeError(f"repair override file missing: {replacement}")
            actual_bytes = replacement.stat().st_size
            actual_sha256 = sha256_file(replacement)
            if actual_bytes != int(override["bytes"]) or actual_sha256 != override["sha256"]:
                raise RuntimeError(f"repair override integrity mismatch: {key}")
            repair_overrides[key] = {**override, "path": str(replacement)}

        joined_keys = {(row["symbol"], row["announcement_id"]) for row in rows}
        unknown = sorted(set(repair_overrides) - joined_keys)
        if unknown:
            raise RuntimeError(f"repair override does not match downloaded input: {unknown[:5]}")
        rows = [
            {**row, **repair_overrides.get((row["symbol"], row["announcement_id"]), {})}
            for row in rows
        ]

    identity = {
        "run_id": args.run_id,
        "workflow": "BACKTEST_CNINFO_ANNUAL_REPORT_EXTRACTION",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "parser_version": PARSER_VERSION,
        "plan": str(plan_path.relative_to(PROJECT_ROOT)),
        "plan_sha256": sha256_file(plan_path),
        "download_manifest": str(manifest_path.relative_to(PROJECT_ROOT)),
        "download_manifest_sha256": sha256_file(manifest_path),
        "limit": args.limit,
    }
    if identity_path.exists():
        if json.loads(identity_path.read_text(encoding="utf-8")) != identity:
            raise RuntimeError("resume identity does not match")
    else:
        identity_path.write_text(json.dumps(identity, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    completed: set[tuple[str, str]] = set()
    if raw_path.exists():
        for record in load_jsonl(raw_path):
            if not record.get("_failed"):
                completed.add((record["source"]["symbol"], record["source"]["announcement_id"]))
    pending = [row for row in rows if (row["symbol"], row["announcement_id"]) not in completed]
    started_at = now()
    processed = 0
    failures = 0
    lock = threading.Lock()
    with raw_path.open("a", encoding="utf-8") as output, ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(extract_one, row, args.document_timeout_seconds): row
            for row in pending
        }
        for future in as_completed(futures):
            row = futures[future]
            try:
                record = future.result()
            except Exception as exc:  # worker crashes must be evidence, not a silent run abort
                record = {
                    "_failed": True,
                    "symbol": row["symbol"],
                    "announcement_id": row["announcement_id"],
                    "path": row["path"],
                    "error": f"WORKER_FAILURE: {type(exc).__name__}: {exc}"[:500],
                }
            with lock:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
                output.flush()
                os.fsync(output.fileno())
                processed += 1
                failures += bool(record.get("_failed"))
                if processed % 100 == 0 or processed == len(pending):
                    print(json.dumps({"processed_this_run": processed, "pending_at_start": len(pending), "failed_this_run": failures}), flush=True)

    raw_records = load_jsonl(raw_path)
    records = [record for record in raw_records if not record.get("_failed")]
    successful_keys = {(record["source"]["symbol"], record["source"]["announcement_id"]) for record in records}
    failure_events = [record for record in raw_records if record.get("_failed")]
    unresolved_failure_keys = sorted(
        {
            (record["symbol"], record["announcement_id"])
            for record in failure_events
            if (record["symbol"], record["announcement_id"]) not in successful_keys
        }
    )
    apply_revision_lineage(records)
    schema = json.loads((PROJECT_ROOT / "schemas" / "cninfo-annual-report-extraction.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    validation_errors = 0
    with normalized_path.open("w", encoding="utf-8") as output:
        for record in sorted(records, key=lambda item: (item["source"]["symbol"], item["source"]["known_at"], item["source"]["announcement_id"])):
            errors = list(validator.iter_errors(record))
            validation_errors += len(errors)
            output.write(json.dumps(record, ensure_ascii=False) + "\n")

    summary = {
        "planned": len(plan_rows),
        "downloaded_joined": len(rows),
        "extracted": len(records),
        "failed_this_invocation": failures,
        "failure_events_total": len(failure_events),
        "unresolved_failures": len(unresolved_failure_keys),
        "unresolved_failure_keys": [list(key) for key in unresolved_failure_keys],
        "document_timeout_seconds": args.document_timeout_seconds,
        "repair_overrides_applied": len(repair_overrides),
        "schema_validation_errors": validation_errors,
        "extractable": sum(record["document"]["extractable"] for record in records),
        "audit_opinion_found": sum(record["audit_opinion"]["classification"] not in {"NOT_FOUND", "UNREADABLE"} for record in records),
        "going_concern_flags": sum(record["going_concern"]["status"] == "FLAG_FOUND" for record in records),
        "metric_candidate_records": sum(bool(record["metric_candidates"]) for record in records),
        "revision_records": sum(record["temporal"]["revision_role"] == "REVISION" for record in records),
    }
    status = "STAGING_COMPLETE_NOT_FORMAL" if not unresolved_failure_keys and not validation_errors and len(records) == len(rows) else "STAGING_PARTIAL_NOT_FORMAL"
    receipt = {
        "schema_version": "1.0.0",
        **identity,
        "status": status,
        "started_at": started_at,
        "completed_at": now(),
        "summary": summary,
        "artifacts": {
            "raw_append": {"path": str(raw_path.relative_to(PROJECT_ROOT)), "sha256": sha256_file(raw_path)},
            "normalized": {"path": str(normalized_path.relative_to(PROJECT_ROOT)), "sha256": sha256_file(normalized_path)},
        },
        "formal_backtest_eligibility": {
            "eligible": False,
            "reasons": [
                "Metric values remain candidates until table units, period columns and cross-source values validate.",
                "NOT_FOUND_IN_EXTRACTED_TEXT is not proof that a filing contains no going-concern issue.",
                "Numerical quality thresholds and effective dates are not frozen.",
            ],
        },
    }
    if repair_override_path is not None:
        receipt["artifacts"]["repair_overrides"] = {
            "path": str(repair_override_path.relative_to(PROJECT_ROOT)),
            "sha256": sha256_file(repair_override_path),
        }
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps({"schema_version": "1.0.0", "event_type": "BACKTEST_CNINFO_ANNUAL_REPORTS_EXTRACTED", "run_id": args.run_id, "status": status, "scheduler_status": "DISABLED_MANUAL_ONLY", "receipt": {"path": str(receipt_path.relative_to(PROJECT_ROOT)), "sha256": sha256_file(receipt_path)}, "created_at": receipt["completed_at"]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if status == "STAGING_COMPLETE_NOT_FORMAL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
