#!/usr/bin/env python3
"""Manually fetch a public GitHub Release asset with verifiable HTTP ranges.

This tool is deliberately a one-shot, foreground data-acquisition command.  It
does not schedule itself, invoke models, import data into the backtest engine,
or declare a dataset fit for formal performance.  It writes an append-only
receipt beside the resulting raw asset so the later importer can decide whether
the source and its coverage are acceptable.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")


def request(url: str, *, byte_range: tuple[int, int] | None = None):
    headers = {"User-Agent": "A-Share-Research-Local-Data-Intake/1.0"}
    if byte_range is not None:
        headers["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
    return urlopen(Request(url, headers=headers), timeout=60)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_total_size(url: str) -> tuple[int, str | None, str | None]:
    with request(url, byte_range=(0, 0)) as response:
        content_range = response.headers.get("Content-Range", "")
        match = CONTENT_RANGE.match(content_range)
        if response.status != 206 or match is None:
            raise RuntimeError(
                "source must support byte-range responses; got "
                f"status={response.status}, Content-Range={content_range!r}"
            )
        return (
            int(match.group(3)),
            response.headers.get("ETag"),
            response.headers.get("Last-Modified"),
        )


def fetch_range(url: str, start: int, end: int) -> bytes:
    with request(url, byte_range=(start, end)) as response:
        content_range = response.headers.get("Content-Range", "")
        expected = f"bytes {start}-{end}/"
        if response.status != 206 or not content_range.startswith(expected):
            raise RuntimeError(
                f"range mismatch for {start}-{end}: status={response.status}, "
                f"Content-Range={content_range!r}"
            )
        payload = response.read()
    expected_size = end - start + 1
    if len(payload) != expected_size:
        raise RuntimeError(
            f"range length mismatch for {start}-{end}: expected={expected_size}, got={len(payload)}"
        )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Public GitHub Release download URL")
    parser.add_argument("--output", required=True, type=Path, help="New raw asset path")
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--license-status", default="REVIEW_REQUIRED")
    parser.add_argument("--chunk-mib", type=int, default=32)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume a partial file created by this command after validating its chunk boundary.",
    )
    args = parser.parse_args()

    if args.chunk_mib < 1 or args.chunk_mib > 128:
        raise SystemExit("--chunk-mib must be between 1 and 128")
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite existing output: {args.output}")

    try:
        total_size, etag, last_modified = parse_total_size(args.url)
        chunk_size = args.chunk_mib * 1024 * 1024
        args.output.parent.mkdir(parents=True, exist_ok=True)
        partial = args.output.with_suffix(args.output.suffix + ".partial")
        receipt = args.output.with_suffix(args.output.suffix + ".receipt.json")
        lock_path = partial.with_suffix(partial.suffix + ".lock")
        lock_handle = lock_path.open("a+b")
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_handle.close()
            raise RuntimeError(f"another manual acquisition owns {partial}; refusing concurrent write") from exc
        existing_size = partial.stat().st_size if partial.exists() else 0
        if existing_size and not args.resume:
            raise RuntimeError(
                f"partial file already exists; re-run with --resume after review: {partial}"
            )
        if existing_size > total_size:
            raise RuntimeError(
                f"partial file exceeds declared remote length: {existing_size} > {total_size}"
            )
        if existing_size and existing_size != total_size and existing_size % chunk_size:
            raise RuntimeError(
                f"partial file is not on a verified chunk boundary: {existing_size} bytes"
            )

        started_at = datetime.now(timezone.utc).isoformat()
        chunk_receipts: list[dict[str, object]] = []
        if existing_size:
            with partial.open("rb") as existing:
                for offset in range(0, existing_size, chunk_size):
                    end = min(existing_size - 1, offset + chunk_size - 1)
                    payload = existing.read(end - offset + 1)
                    if len(payload) != end - offset + 1:
                        raise RuntimeError(f"cannot re-read retained segment {offset}-{end}")
                    chunk_receipts.append(
                        {
                            "start": offset,
                            "end": end,
                            "bytes": len(payload),
                            "sha256": hashlib.sha256(payload).hexdigest(),
                            "retained_after_interrupted_manual_run": True,
                        }
                    )

        mode = "ab" if existing_size else "xb"
        with partial.open(mode) as handle:
            for offset in range(existing_size, total_size, chunk_size):
                end = min(total_size - 1, offset + chunk_size - 1)
                payload = fetch_range(args.url, offset, end)
                handle.write(payload)
                chunk_receipts.append(
                    {
                        "start": offset,
                        "end": end,
                        "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                )
                print(f"downloaded {end + 1}/{total_size} bytes", flush=True)
            handle.flush()
            os.fsync(handle.fileno())

        actual_size = partial.stat().st_size
        if actual_size != total_size:
            raise RuntimeError(f"assembled size mismatch: expected={total_size}, got={actual_size}")
        os.replace(partial, args.output)
        result = {
            "schema_version": "1.0.0",
            "kind": "RAW_SOURCE_ACQUISITION_RECEIPT",
            "source_id": args.source_id,
            "source_url": args.url,
            "license_status": args.license_status,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "started_at": started_at,
            "http_etag": etag,
            "http_last_modified": last_modified,
            "content_length": total_size,
            "sha256": sha256_file(args.output),
            "chunk_mib": args.chunk_mib,
            "chunks": chunk_receipts,
            "formal_backtest_eligible": False,
            "eligibility_reason": "Raw source staging only; coverage, point-in-time semantics, and data license remain under review.",
        }
        receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()
        return 0
    except (HTTPError, URLError, OSError, RuntimeError) as exc:
        print(f"acquisition failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
