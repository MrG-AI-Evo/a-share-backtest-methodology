#!/usr/bin/env python3
"""Manually preflight FFD PIT coverage without downloading market data.

This foreground-only audit calls the two FFD endpoints that the published
historical-data contract identifies as zero-point coverage discovery.  It
never runs a backtest, creates formal parquet assets, changes a policy, or
starts a scheduler.  The local API key is read only at request time and is
never written to an artifact, log, exception, or standard output.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ID = "FFD_STANDARD_HISTORICAL_DATA_API"
BASE_URL = "https://ffd.findesk.cn/api"
ENDPOINTS = {
    "pit_catalog": "/v1/pit/catalog",
    "historical_coverage": "/historical/coverage",
}
SENSITIVE_TOKENS = ("api_key", "apikey", "authorization", "token", "secret", "cookie", "email", "phone")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--secrets-csv", type=Path, default=PROJECT_ROOT / "secrets" / "api.csv")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--audit-dir", type=Path, default=PROJECT_ROOT / "data" / "audit")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    return parser.parse_args()


def ensure_new(*paths: Path) -> None:
    existing = [str(path) for path in paths if path.exists()]
    if existing:
        raise RuntimeError("refusing to overwrite immutable audit artifacts: " + ", ".join(existing))


def read_key(path: Path) -> str:
    if not path.is_file():
        raise RuntimeError("FFD credential CSV is missing")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise RuntimeError("FFD credential CSV has no header")
        api_field = next((field for field in reader.fieldnames if field and field.strip().lower() == "api"), None)
        if api_field is None:
            raise RuntimeError("FFD credential CSV must contain an api column")
        for row in reader:
            value = (row.get(api_field) or "").strip()
            if value:
                return value
    raise RuntimeError("FFD credential CSV contains no API value")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if any(token in str(key).lower() for token in SENSITIVE_TOKENS) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def fetch_json(api_key: str, endpoint: str, timeout_seconds: float) -> dict[str, Any]:
    request = Request(
        f"{BASE_URL}{endpoint}",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read()
            return {"http_status": response.status, "payload": redact(json.loads(body.decode("utf-8")))}
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            parsed: Any = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"non_json_body_sha256": sha256_bytes(body.encode("utf-8"))}
        return {"http_status": error.code, "payload": redact(parsed)}
    except URLError as error:
        return {"http_status": None, "transport_error": type(error.reason).__name__}


def main() -> int:
    args = parse_args()
    if args.timeout_seconds <= 0:
        raise RuntimeError("timeout-seconds must be positive")
    output_dir = (args.output_dir or PROJECT_ROOT / "data" / "backtests" / "staging" / f"ffd-pit-coverage-{args.run_id}").resolve()
    raw_path = output_dir / "raw-coverage-responses.redacted.json"
    report_path = output_dir / "coverage-audit.json"
    audit_path = args.audit_dir.resolve() / f"{args.run_id}-ffd-pit-coverage-audit.json"
    ensure_new(output_dir, raw_path, report_path, audit_path)

    # Do not include the credential path/content hash in audit inputs: neither
    # proves authority nor belongs in a portable evidence package.
    started_at = now()
    api_key = read_key(args.secrets_csv.resolve())
    responses = {name: fetch_json(api_key, endpoint, args.timeout_seconds) for name, endpoint in ENDPOINTS.items()}
    del api_key

    output_dir.mkdir(parents=True, exist_ok=False)
    args.audit_dir.resolve().mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(responses, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    successful = [name for name, response in responses.items() if response.get("http_status") == 200]
    errors = {
        name: {key: value for key, value in response.items() if key != "payload"}
        for name, response in responses.items()
        if response.get("http_status") != 200
    }
    input_payload = {
        "source_id": SOURCE_ID,
        "endpoints": {name: f"{BASE_URL}{endpoint}" for name, endpoint in ENDPOINTS.items()},
        "timeout_seconds": args.timeout_seconds,
        "script_sha256": sha256_file(Path(__file__).resolve()),
    }
    report = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "workflow": "BACKTEST_SOURCE_COVERAGE_AUDIT",
        "status": "CANDIDATE_COVERAGE_DISCOVERED_NOT_FORMAL" if len(successful) == len(ENDPOINTS) else "SOURCE_OR_ENTITLEMENT_UNAVAILABLE_NOT_FORMAL",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "source": {"source_id": SOURCE_ID, "base_url": BASE_URL, "retrieved_at": started_at},
        "input": {**input_payload, "input_sha256": sha256_bytes(json.dumps(input_payload, sort_keys=True).encode("utf-8"))},
        "summary": {
            "requested_free_coverage_endpoints": list(ENDPOINTS),
            "successful_endpoints": successful,
            "failed_endpoints": errors,
            "credential_logged": False,
        },
        "formal_backtest_eligibility": {
            "eligible": False,
            "blockers_preserved": ["CD_HISTORY", "QUALITY_GATE", "FEE_TAX_RULES", "POINT_IN_TIME_COVERAGE"],
            "reasons": [
                "Coverage discovery is not historical data delivery.",
                "No security-status, corporate-action, financial, rate, fee, or tax value is imported by this preflight.",
                "Any FFD PIT candidate must remain non-decision-eligible unless its response proves decision eligibility and complete coverage for the requested unit.",
            ],
        },
        "artifacts": {"raw_redacted": str(raw_path.relative_to(PROJECT_ROOT)), "raw_redacted_sha256": sha256_file(raw_path)},
        "completed_at": now(),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit_path.write_text(json.dumps({
        "schema_version": "1.0.0",
        "event_type": "BACKTEST_FFD_PIT_COVERAGE_AUDITED",
        "run_id": args.run_id,
        "status": report["status"],
        "input_sha256": report["input"]["input_sha256"],
        "report": str(report_path.relative_to(PROJECT_ROOT)),
        "created_at": report["completed_at"],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"run_id": args.run_id, "status": report["status"], "successful_endpoints": successful, "failed_endpoint_names": list(errors)}, ensure_ascii=False))
    return 0 if len(successful) == len(ENDPOINTS) else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"status": "FAILED", "error_type": type(error).__name__, "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
