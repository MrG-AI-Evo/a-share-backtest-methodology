#!/usr/bin/env python3
"""Download a small, fixed set of public CD-rate evidence snapshots.

Foreground/manual-only. Official bank pages are kept separate from secondary
media observations. The downloader never promotes either class to a formal
backtest input and never infers a product-validity window from a publication.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    ("OFFICIAL_BANK", "ABC", "abc-2018-2021-catalog", "https://www.abchina.com/cn/PersonalServices/Deposit/decdcp/201812/t20181229_1806590.htm"),
    ("OFFICIAL_BANK", "ABC", "abc-2022-2023-catalog", "https://www.abchina.com/cn/PersonalServices/Deposit/decdcp/202101/t20210121_1954832.htm"),
    ("OFFICIAL_BANK", "ABC", "abc-2024-2025-catalog", "https://www.abchina.com/cn/PersonalServices/Deposit/decdcp/202401/t20240102_2379368.htm"),
    ("OFFICIAL_BANK", "BOC", "boc-2021-issue-1", "https://www.boc.cn/pbservice/bi2/202101/t20210106_18866803.html"),
    ("OFFICIAL_BANK", "BOC", "boc-2021-issue-2", "https://www.boc.cn/pbservice/bi2/202106/t20210621_19564780.html"),
    ("OFFICIAL_BANK", "BOC", "boc-2022-issue-1", "https://www.boc.cn/pbservice/bi2/202204/t20220425_21049715.html"),
    ("OFFICIAL_BANK", "BOC", "boc-2023-issue-2", "https://www.boc.cn/pbservice/bi2/202306/t20230608_23194251.html"),
    ("OFFICIAL_BANK", "BOC", "boc-2023-issue-4", "https://www.boc.cn/pbservice/bi2/202312/t20231222_24285703.html"),
    ("OFFICIAL_BANK", "BOC", "boc-2024-issue-1", "https://www.boc.cn/pbservice/bi2/202407/t20240726_25118746.html"),
    ("OFFICIAL_BANK", "BOC", "boc-2024-issue-2", "https://www.boc.cn/pbservice/bi2/202410/t20241018_25175421.html"),
    ("OFFICIAL_BANK", "BOC", "boc-2025-issue-1", "https://www.boc.cn/pbservice/bi2/202505/t20250520_25356443.html"),
    ("SECONDARY_MEDIA", "MULTI", "cls-2025-05-cd-snapshot", "https://www.cls.cn/detail/2036118"),
    ("SECONDARY_MEDIA", "MULTI", "sina-2025-03-cd-snapshot", "https://k.sina.com.cn/article_7096019974_1a6f4ac0602001izlm.html"),
    ("SECONDARY_MEDIA_REPUBLISHED_ON_BANK", "MULTI", "ccb-csrc-2025-05-cd-snapshot", "https://sinfo.ccb.com/newsinfo/neiis/20250525/A10zgjsyhzx56951/A10zgjsyhzx56951.html"),
    ("OFFICIAL_BANK", "BANKWF", "bankwf-2019-10-3y", "https://www.bankwf.com/bankwf/2024-05/14/article_2024051419493669915.html"),
    ("OFFICIAL_BANK", "NCBANK", "ncbank-2020-05-3y", "https://www.ncbank.cn/ncbank/2020-05/14/article_496186.shtml"),
    ("OFFICIAL_BANK", "GRCB", "grcb-2020-06-08-3y", "https://www.grcbank.com/grcbank/gryw/xxgs/2021010416552077908/index.shtml"),
    ("OFFICIAL_BANK", "JNBANK", "jnbank-2021-full-year-3y", "https://www.jn-bank.com/jnbank/2024-01/18/article_2024011809415189837.html"),
    ("SECONDARY_AUTHORITATIVE_MEDIA", "ICBC", "cnfin-2022-12-icbc-3y", "https://www.cnfin.com/hb-lb/detail/20221226/3773897_1.html"),
)


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat()


def fetch(url: str, timeout: int) -> tuple[bytes | None, str | None]:
    result = subprocess.run(
        [
            "/usr/bin/curl",
            "--fail",
            "--location",
            "--retry",
            "2",
            "--connect-timeout",
            "10",
            "--max-time",
            str(timeout),
            "--silent",
            "--show-error",
            "--user-agent",
            "A-Share-Research-Audit/1.0",
            url,
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        return None, result.stderr.decode("utf-8", errors="replace")[:500]
    if not result.stdout:
        return None, "empty response"
    return result.stdout, None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--timeout", type=int, default=45)
    args = parser.parse_args()

    output_dir = PROJECT_ROOT / "data" / "backtests" / "staging" / args.run_id
    audit_path = PROJECT_ROOT / "data" / "audit" / f"{args.run_id}-cd-public-supplements.json"
    if output_dir.exists() or audit_path.exists():
        raise RuntimeError("refusing to overwrite immutable output")
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True)
    started_at = now()
    records = []
    for source_class, bank, slug, url in SOURCES:
        body, error = fetch(url, args.timeout)
        suffix = Path(urlsplit(url).path).suffix.lower()
        if suffix not in {".pdf", ".doc", ".docx"}:
            suffix = ".html"
        path = raw_dir / f"{slug}{suffix}"
        record = {
            "source_class": source_class,
            "bank": bank,
            "slug": slug,
            "source_uri": url,
            "retrieved_at": now(),
        }
        if body is None:
            record.update({"status": "FAILED", "error": error})
        else:
            path.write_bytes(body)
            record.update(
                {
                    "status": "DOWNLOADED",
                    "path": str(path.relative_to(PROJECT_ROOT)),
                    "bytes": len(body),
                    "sha256": sha256(body),
                }
            )
        records.append(record)

    failed = sum(record["status"] == "FAILED" for record in records)
    receipt = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "workflow": "BACKTEST_CD_PUBLIC_SUPPLEMENT_DOWNLOAD",
        "status": "STAGING_COMPLETE_NOT_FORMAL" if not failed else "STAGING_PARTIAL_NOT_FORMAL",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "started_at": started_at,
        "completed_at": now(),
        "summary": {"requested": len(records), "downloaded": len(records) - failed, "failed": failed},
        "interpretation": [
            "Official bank pages are primary product-publication evidence.",
            "Media pages are secondary point observations only.",
            "No source is assigned an undisclosed sale end date or carried to another signal date.",
            "Nothing in this package is automatically promoted to the formal CD signal series.",
        ],
        "records": records,
    }
    receipt_path = output_dir / "receipt.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "event_type": "BACKTEST_CD_PUBLIC_SUPPLEMENTS_DOWNLOADED",
                "run_id": args.run_id,
                "status": receipt["status"],
                "scheduler_status": receipt["scheduler_status"],
                "receipt": {
                    "path": str(receipt_path.relative_to(PROJECT_ROOT)),
                    "sha256": sha256(receipt_path.read_bytes()),
                },
                "created_at": receipt["completed_at"],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(receipt["summary"], ensure_ascii=False))
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
