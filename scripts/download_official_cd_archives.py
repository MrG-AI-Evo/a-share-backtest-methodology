#!/usr/bin/env python3
"""Manually download official bank CD pages discovered from local indexes.

The task is foreground-only and has no scheduler integration. It downloads
only URLs already present in official bank index pages, writes each raw asset
once under a URL-hash filename, and emits an append-only receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RAW_DIR = (
    PROJECT_ROOT
    / "data"
    / "backtests"
    / "staging"
    / "cd-rates-20260910-official-archive"
    / "raw"
)


def now() -> str:
    return datetime.now(UTC).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--bank", choices=("BOC", "CCB", "CCB_PDF"), required=True)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--receipt-dir", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--request-interval-seconds", type=float, default=0.15)
    return parser.parse_args()


def discover_boc(raw_dir: Path) -> list[dict[str, str]]:
    discovered: dict[str, str] = {}
    for path in sorted(raw_dir.glob("boc-index-*.html")):
        soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
        for anchor in soup.find_all("a", href=True):
            title = " ".join(anchor.get_text(" ", strip=True).split())
            if "大额存单" not in title or "发售" not in title:
                continue
            url = urljoin("https://www.boc.cn/pbservice/bi2/", str(anchor["href"]))
            if "/pbservice/bi2/20" in url:
                discovered[url] = title
    return [{"source_uri": url, "title": discovered[url]} for url in sorted(discovered)]


def discover_ccb(raw_dir: Path) -> list[dict[str, str]]:
    index = raw_dir / "ccb-2015-2016-index.html"
    soup = BeautifulSoup(index.read_text(encoding="utf-8", errors="replace"), "html.parser")
    discovered: dict[str, str] = {}
    for anchor in soup.find_all("a", href=True):
        title = " ".join(anchor.get_text(" ", strip=True).split())
        href = "".join(str(anchor["href"]).split())
        if "大额存单" not in title or "newsdetail" not in href:
            continue
        parsed = urlsplit(href)
        if parsed.path:
            href = "https://www.ccb.com" + parsed.path
        discovered[href] = title
    return [{"source_uri": url, "title": discovered[url]} for url in sorted(discovered)]


def discover_ccb_pdfs(raw_dir: Path) -> list[dict[str, str]]:
    discovered: dict[str, str] = {}
    for path in sorted((raw_dir / "ccb-products").glob("*.html")):
        soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
        for anchor in soup.find_all("a", href=True):
            title = " ".join(anchor.get_text(" ", strip=True).split())
            href = "".join(str(anchor["href"]).split())
            if not href.lower().endswith(".pdf") or "大额存单" not in title:
                continue
            discovered[urljoin("https://www.ccb.com", href)] = title
    return [{"source_uri": url, "title": discovered[url]} for url in sorted(discovered)]


def destination(raw_dir: Path, bank: str, url: str) -> Path:
    digest = sha256_bytes(url.encode("utf-8"))[:16]
    suffix = ".pdf" if bank == "CCB_PDF" else ".html"
    directory = "ccb-product-pdfs" if bank == "CCB_PDF" else f"{bank.lower()}-products"
    return raw_dir / directory / f"{digest}{suffix}"


def fetch(url: str, timeout_seconds: float) -> tuple[bytes | None, dict[str, Any]]:
    process = subprocess.run(
        [
            "/usr/bin/curl",
            "--fail",
            "--location",
            "--retry",
            "2",
            "--connect-timeout",
            str(min(timeout_seconds, 15.0)),
            "--max-time",
            str(timeout_seconds),
            "--silent",
            "--show-error",
            "--user-agent",
            "A-Share-Research-Audit/1.0",
            url,
        ],
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        error = process.stderr.decode("utf-8", errors="replace").strip()[:500]
        return None, {"http_status": None, "curl_exit_code": process.returncode, "error": error}
    return process.stdout, {"http_status": 200, "final_url": url}


def main() -> int:
    args = parse_args()
    if args.timeout_seconds <= 0 or args.request_interval_seconds < 0:
        raise RuntimeError("timeouts must be positive and interval must be non-negative")
    raw_dir = args.raw_dir.resolve()
    if not raw_dir.is_dir():
        raise RuntimeError(f"raw directory missing: {raw_dir}")
    receipt_dir = (
        args.receipt_dir
        or PROJECT_ROOT / "data" / "backtests" / "staging" / f"official-cd-download-{args.run_id}"
    ).resolve()
    receipt_path = receipt_dir / "download-receipt.json"
    audit_path = PROJECT_ROOT / "data" / "audit" / f"{args.run_id}-official-cd-download.json"
    if receipt_dir.exists() or receipt_path.exists() or audit_path.exists():
        raise RuntimeError("refusing to overwrite an existing receipt or audit event")

    if args.bank == "BOC":
        items = discover_boc(raw_dir)
    elif args.bank == "CCB":
        items = discover_ccb(raw_dir)
    else:
        items = discover_ccb_pdfs(raw_dir)
    if not items:
        raise RuntimeError(f"no {args.bank} product links discovered")
    records: list[dict[str, Any]] = []
    started_at = now()
    for index, item in enumerate(items):
        output = destination(raw_dir, args.bank, item["source_uri"])
        record: dict[str, Any] = {**item, "raw_file": str(output.relative_to(PROJECT_ROOT))}
        if output.exists():
            body = output.read_bytes()
            record.update({"status": "ALREADY_PRESENT", "bytes": len(body), "sha256": sha256_bytes(body)})
        else:
            body, result = fetch(item["source_uri"], args.timeout_seconds)
            record.update(result)
            if body is None or result.get("http_status") != 200 or not body:
                record["status"] = "FAILED"
            else:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(body)
                record.update({"status": "DOWNLOADED", "bytes": len(body), "sha256": sha256_bytes(body)})
        records.append(record)
        if index + 1 < len(items) and args.request_interval_seconds:
            time.sleep(args.request_interval_seconds)

    counts: dict[str, int] = {}
    for record in records:
        counts[record["status"]] = counts.get(record["status"], 0) + 1
    receipt = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "workflow": "BACKTEST_OFFICIAL_ARCHIVE_DOWNLOAD",
        "bank": args.bank,
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "started_at": started_at,
        "completed_at": now(),
        "summary": {"discovered": len(items), **dict(sorted(counts.items()))},
        "records": records,
    }
    receipt_dir.mkdir(parents=True, exist_ok=False)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "event_type": "BACKTEST_OFFICIAL_CD_ARCHIVE_DOWNLOADED",
                "run_id": args.run_id,
                "bank": args.bank,
                "status": "COMPLETE" if counts.get("FAILED", 0) == 0 else "PARTIAL",
                "receipt": str(receipt_path.relative_to(PROJECT_ROOT)),
                "receipt_sha256": sha256_bytes(receipt_path.read_bytes()),
                "created_at": receipt["completed_at"],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"run_id": args.run_id, "bank": args.bank, **receipt["summary"]}, ensure_ascii=False))
    return 0 if counts.get("FAILED", 0) == 0 else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"status": "FAILED", "error_type": type(error).__name__, "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
