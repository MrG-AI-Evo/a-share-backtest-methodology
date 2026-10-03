#!/usr/bin/env python3
"""Stage archived SASAC central-enterprise directory snapshots for sensitivity use."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from bs4 import BeautifulSoup


TZ = ZoneInfo("Asia/Shanghai")
CDX = "https://web.archive.org/cdx/search/cdx"
URLS = [
    "http://www.sasac.gov.cn/n4422011/n14158800/n14158998/c14159097/content.html",
    "http://www.sasac.gov.cn/n2588045/n27271785/n27271792/c14159097/content.html",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def get(session: requests.Session, url: str, retries: int = 2) -> requests.Response:
    error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = session.get(url, timeout=60)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            error = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"download failed: {url}: {error}")


def extract_names(html: str) -> tuple[str | None, list[tuple[int, str]]]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    published = None
    match = re.search(r"发布时间[：:]\s*(\d{4}-\d{2}-\d{2})", text)
    if match:
        published = match.group(1)
    names: dict[int, str] = {}
    for row in soup.find_all("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"])]
        for index in range(len(cells) - 1):
            if cells[index].isdigit() and cells[index + 1] and not cells[index + 1].isdigit():
                sequence = int(cells[index])
                if 1 <= sequence <= 200 and "企业(集团)名称" not in cells[index + 1]:
                    names[sequence] = cells[index + 1]
    return published, sorted(names.items())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--from-year", type=int, default=2016)
    parser.add_argument("--to-year", type=int, default=2025)
    args = parser.parse_args()
    started_at = datetime.now(TZ)
    root = args.output_root.resolve()
    raw_root = root / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": "A-share-backtest-audit/1.0 (manual deterministic collector)"})

    captures: dict[tuple[str, str], dict[str, str]] = {}
    cdx_records = []
    failures = []
    for original in URLS:
        query = (
            f"{CDX}?url={quote(original, safe='')}&output=json&filter=statuscode:200"
            f"&filter=mimetype:text/html&collapse=digest&from={args.from_year}&to={args.to_year}"
        )
        payload = get(session, query).json()
        header = payload[0]
        rows = [dict(zip(header, row)) for row in payload[1:]]
        cdx_records.extend(rows)
        for row in rows:
            captures[(row["digest"], row["timestamp"])] = row

    normalized_rows = []
    raw_records = []
    for (_, timestamp), row in sorted(captures.items(), key=lambda item: item[0][1]):
        archive_url = f"https://web.archive.org/web/{timestamp}id_/{row['original']}"
        try:
            response = get(session, archive_url)
            raw_path = raw_root / f"sasac-directory-{timestamp}.html"
            raw_path.write_bytes(response.content)
            published_at, names = extract_names(response.text)
            raw_records.append(
                {
                    "capture_timestamp": timestamp,
                    "original_url": row["original"],
                    "archive_url": archive_url,
                    "page_published_at": published_at,
                    "names": len(names),
                    "path": str(raw_path.relative_to(Path.cwd())),
                    "bytes": raw_path.stat().st_size,
                    "sha256": sha256_file(raw_path),
                }
            )
            for sequence, name in names:
                normalized_rows.append(
                    {
                        "archive_capture_at": datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(
                            tzinfo=ZoneInfo("UTC")
                        ),
                        "page_published_at": published_at,
                        "sequence": sequence,
                        "central_enterprise_name": name,
                        "original_url": row["original"],
                        "archive_url": archive_url,
                        "formal_backtest_eligible": False,
                        "is_synthetic": False,
                    }
                )
            print(f"{timestamp}: names={len(names)} published={published_at}", flush=True)
            time.sleep(0.2)
        except Exception as exc:
            failures.append({"capture_timestamp": timestamp, "url": archive_url, "error": str(exc)})

    frame = pd.DataFrame(normalized_rows)
    output_path = root / "sasac-central-enterprise-directory-snapshots-candidate.parquet"
    frame.to_parquet(output_path, index=False)
    completed_at = datetime.now(TZ)
    input_contract = {
        "urls": URLS,
        "from_year": args.from_year,
        "to_year": args.to_year,
        "collapse": "digest",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
    }
    receipt = {
        "schema_version": "1.0.0",
        "run_id": root.name,
        "workflow": "BACKTEST_SOURCE_EVIDENCE_COLLECTION",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_seconds": (completed_at - started_at).total_seconds(),
        "input_hash": hashlib.sha256(
            json.dumps(input_contract, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "input_contract": input_contract,
        "cdx_capture_rows": len(cdx_records),
        "downloaded_snapshots": len(raw_records),
        "failures": failures,
        "raw_records": raw_records,
        "output": {
            "path": str(output_path.relative_to(Path.cwd())),
            "bytes": output_path.stat().st_size,
            "sha256": sha256_file(output_path),
            "rows": len(frame),
            "minimum_capture_at": str(frame["archive_capture_at"].min()) if not frame.empty else None,
            "maximum_capture_at": str(frame["archive_capture_at"].max()) if not frame.empty else None,
        },
        "formal_assessment": {
            "status": "STAGING_ONLY_SENSITIVITY_SUPPORT",
            "useful_for": "Versioned parent-level central-enterprise directory evidence from archived first-party pages.",
            "not_sufficient_for": [
                "listed-company ownership chain",
                "central versus local controller classification for each security",
                "monthly decision eligibility before the first available archive capture",
            ],
            "formal_gate_closed": False,
        },
    }
    (root / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(receipt["output"], ensure_ascii=False, indent=2), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
