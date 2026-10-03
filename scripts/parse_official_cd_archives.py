#!/usr/bin/env python3
"""Parse downloaded four-bank personal CD archives into auditable observations.

This is a foreground-only, deterministic staging task. It never fetches remote
content, mutates policy, closes a formal gate, or runs a backtest. Raw files are
kept immutable and every normalized row points back to the source asset hash.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
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
DEFAULT_TRADING_CALENDAR_PARQUET = (
    PROJECT_ROOT
    / "data"
    / "backtests"
    / "staging"
    / "nas-daily-20260910-155000-nas-daily-staging"
    / "shsz-daily-raw-bars-staging-v1.parquet"
)
MAIN_START = date(2016, 1, 4)
MAIN_END = date(2025, 12, 31)

SOURCE_URLS = {
    "abc-2016.html": "https://www.abchina.com/cn/PersonalServices/Deposit/decdcp/201601/t20160108_821291.htm",
    "abc-2019-2021.html": "https://www.abchina.com/cn/PersonalServices/Deposit/decdcp/201812/t20181229_1806590.htm",
    "abc-2022-2023.html": "https://www.abchina.com/cn/PersonalServices/Deposit/decdcp/202101/t20210121_1954832.htm",
    "abc-2024-2025.html": "https://www.abchina.com/cn/PersonalServices/Deposit/decdcp/202401/t20240102_2379368.htm",
    "boc-2016-issue-1.html": "https://www.boc.cn/pbservice/bi2/201602/t20160226_6458172.html",
    "boc-2016-issue-2.html": "https://www.boc.cn/pbservice/bi2/201605/t20160531_6987108.html",
    "boc-2016-issue-3.html": "https://www.boc.cn/pbservice/bi2/201607/t20160706_7250592.html",
    "boc-2016-issue-4.html": "https://www.boc.cn/pbservice/bi2/201609/t20160927_7738399.html",
    "boc-2025-issue-1.html": "https://www.boc.cn/pbservice/bi2/202505/t20250520_25356443.html",
    "ccb-2016-sample.html": "https://www.ccb.com/cn/finance/productnews/newsdetail/20160329_631785152.html",
    "ccb-2015-2016-index.html": "https://www.ccb.com/cn/public/20150619_1434694765.html",
    "ccb-2019-q1-3y.pdf": "https://www.ccb.com/cn/finance/upload/productInfo/20190111_1547186546/20190111140134386736.pdf",
    "ccb-2019-q2q3-3y.pdf": "https://www.ccb.com/cn/html1/finance/19/03/47.pdf",
    "ccb-2023-3y.pdf": "https://www.ccb.com/cn/html1/finance/22/12/29/de2311.pdf",
    "ccb-2024-issue-16.pdf": "https://www.ccb.com/cn/html1/finance/23/12/28/cd16.pdf",
    "icbc-personal-cd.html": "https://www.icbc.com.cn/page/721854746150600716.html",
    "icbc-2022-issue-1-6.doc": "https://v.icbc.com.cn/userfiles/Resources/ICBC/grjr/download/2021/grdecd202201.doc",
}
DISCOVERED_SOURCE_URLS: dict[str, str] = {}


def now() -> str:
    return datetime.now(UTC).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--audit-dir", type=Path, default=PROJECT_ROOT / "data" / "audit")
    parser.add_argument(
        "--trading-calendar-parquet",
        type=Path,
        default=DEFAULT_TRADING_CALENDAR_PARQUET,
        help="Candidate SH/SZ daily-bar parquet used only to identify monthly trading checkpoints.",
    )
    return parser.parse_args()


def ensure_new(*paths: Path) -> None:
    collisions = [str(path) for path in paths if path.exists()]
    if collisions:
        raise RuntimeError("refusing to overwrite immutable artifacts: " + ", ".join(collisions))


def parse_date(value: object) -> date | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    match = re.search(r"(20\d{2})\s*[年/\-.]\s*(\d{1,2})\s*[月/\-.]\s*(\d{1,2})", text)
    if not match:
        return None
    return date(*(int(part) for part in match.groups()))


def parse_date_range(text: str) -> tuple[date | None, date | None]:
    matches = re.findall(r"(20\d{2})\s*[年/\-.]\s*(\d{1,2})\s*[月/\-.]\s*(\d{1,2})", text)
    parsed = [date(*(int(part) for part in item)) for item in matches]
    if not parsed:
        return None, None
    return parsed[0], parsed[1] if len(parsed) > 1 else parsed[0]


def rate_decimal(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", str(value))
    return None if not match else round(float(match.group(1)) / 100.0, 8)


def amount_cny(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*万", str(value))
    if match:
        return float(match.group(1)) * 10_000
    match = re.search(r"(\d+(?:\.\d+)?)", str(value).replace(",", ""))
    return None if not match else float(match.group(1))


def tenor_years(value: object) -> float | None:
    text = re.sub(r"\s+", "", str(value).strip())
    aliases = {
        "五年": 5.0,
        "5年": 5.0,
        "三年": 3.0,
        "3年": 3.0,
        "二年": 2.0,
        "两年": 2.0,
        "2年": 2.0,
        "一年": 1.0,
        "1年": 1.0,
        "18个月": 1.5,
        "九个月": 0.75,
        "9个月": 0.75,
        "六个月": 0.5,
        "6个月": 0.5,
        "三个月": 0.25,
        "3个月": 0.25,
        "一个月": 1 / 12,
        "1个月": 1 / 12,
    }
    for key, result in aliases.items():
        if text == key:
            return result
    if re.match(r"^5年(?:期|[A-Z款（(].*)?$", text):
        return 5.0
    if re.match(r"^3年(?:期|[A-Z款（(].*)?$", text):
        return 3.0
    if re.match(r"^2年(?:期|[A-Z款（(].*)?$", text):
        return 2.0
    if re.match(r"^1年(?:期|[A-Z款（(].*)?$", text):
        return 1.0
    return None


def file_timestamp(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()


def source_record(path: Path) -> dict[str, Any]:
    relative = str(path.relative_to(PROJECT_ROOT))
    return {
        "raw_file": relative,
        "source_uri": SOURCE_URLS.get(path.name) or DISCOVERED_SOURCE_URLS.get(relative),
        "source_asset_sha256": sha256_file(path),
        "bytes": path.stat().st_size,
        "retrieved_at": file_timestamp(path),
    }


def observation(
    *,
    bank: str,
    product_name: str,
    tenor: float | None,
    rate: float | None,
    valid_from: date | None,
    valid_to: date | None,
    minimum_cny: float | None,
    payment_method: str | None,
    early_withdrawal: str | None,
    customer_scope: str,
    raw: dict[str, Any],
    window_evidence: str,
) -> dict[str, Any]:
    target_tenor = tenor in {3.0, 5.0}
    complete = all(item is not None for item in (tenor, rate, valid_from, valid_to))
    return {
        "bank": bank,
        "product_name": product_name,
        "customer_scope": customer_scope,
        "tenor_years": tenor,
        "annual_rate_decimal": rate,
        "annual_rate_percent": None if rate is None else round(rate * 100.0, 6),
        "valid_from": None if valid_from is None else valid_from.isoformat(),
        "valid_to": None if valid_to is None else valid_to.isoformat(),
        "known_at_candidate": None if valid_from is None else valid_from.isoformat(),
        "minimum_purchase_cny": minimum_cny,
        "payment_method": payment_method,
        "early_withdrawal_terms": early_withdrawal,
        "window_evidence": window_evidence,
        "target_tenor": target_tenor,
        "observation_eligible_candidate": bool(target_tenor and complete),
        "formal_backtest_eligible": False,
        **raw,
    }


def parse_abc(path: Path) -> list[dict[str, Any]]:
    frame = pd.read_html(path)[0]
    columns = [str(item).strip() for item in frame.iloc[0].tolist()]
    frame = frame.iloc[1:].copy()
    frame.columns = columns
    frame = frame.dropna(how="all")
    raw = source_record(path)
    rows: list[dict[str, Any]] = []
    for _, item in frame.iterrows():
        product_name = str(item.get("产品名称", "")).strip()
        if not product_name or product_name.lower() == "nan" or str(item.get("发售对象", "")).strip() != "对私":
            continue
        start = parse_date(item.get("认购起始日"))
        end = parse_date(item.get("认购结束日"))
        if start is None or end is None or end < MAIN_START or start > MAIN_END:
            continue
        rows.append(
            observation(
                bank="ABC",
                product_name=product_name,
                tenor=tenor_years(item.get("期限（天）")),
                rate=rate_decimal(item.get("利率")),
                valid_from=max(start, MAIN_START),
                valid_to=min(end, MAIN_END),
                minimum_cny=amount_cny(item.get("认购起点(万元)")),
                payment_method=None if pd.isna(item.get("付息方式")) else str(item.get("付息方式")),
                early_withdrawal=None if pd.isna(item.get("提前支取利率")) else str(item.get("提前支取利率")),
                customer_scope="PERSONAL",
                raw=raw,
                window_evidence="SOURCE_EXPLICIT_SUBSCRIPTION_WINDOW",
            )
        )
    return rows


def flatten_columns(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    if isinstance(result.columns, pd.MultiIndex):
        result.columns = [str(column[-1]).strip() for column in result.columns]
    else:
        result.columns = [str(column).strip() for column in result.columns]
    return result


def parse_boc(path: Path) -> list[dict[str, Any]]:
    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
    text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
    start_match = re.search(r"我行将于(20\d{2}年\d{1,2}月\d{1,2}日)(?:起)?发售", text)
    observed_on = parse_date(start_match.group(1)) if start_match else None
    title = next((line for line in text.splitlines() if "关于发售" in line and "大额存单" in line), path.stem)
    frame = flatten_columns(pd.read_html(path)[0])
    tenor_column = next((column for column in frame.columns if "期限" in column), None)
    minimum_column = next((column for column in frame.columns if "起点金额" in column), None)
    rate_column = next((column for column in frame.columns if "年化利率" in column), None)
    if tenor_column is None or minimum_column is None or rate_column is None:
        raise RuntimeError(f"BOC product columns not found: {path.name}")
    frame[tenor_column] = frame[tenor_column].ffill()
    raw = source_record(path)
    rows: list[dict[str, Any]] = []
    for _, item in frame.iterrows():
        tenor_value = item.get(tenor_column)
        minimum = item.get(minimum_column)
        rate_value = item.get(rate_column)
        rows.append(
            observation(
                bank="BOC",
                product_name=title,
                tenor=tenor_years(tenor_value),
                rate=rate_decimal(rate_value),
                valid_from=observed_on,
                valid_to=observed_on,
                minimum_cny=amount_cny(minimum),
                payment_method=None if "付息规则" not in item else str(item.get("付息规则")),
                early_withdrawal=None if "付息规则" not in item else str(item.get("付息规则")),
                customer_scope="PERSONAL",
                raw=raw,
                window_evidence="EXACT_ISSUE_DATE_ONLY_NO_END_DATE",
            )
        )
    return rows


def parse_ccb_product(path: Path) -> list[dict[str, Any]]:
    tables = pd.read_html(path)
    pairs = next((table for table in tables if table.shape[1] == 2 and "产品名称" in set(table.iloc[:, 0].astype(str))), None)
    if pairs is None:
        raise RuntimeError(f"CCB product table not found: {path.name}")
    values = {str(row.iloc[0]).strip(): str(row.iloc[1]).strip() for _, row in pairs.iterrows()}
    start, end = parse_date_range(values.get("发行时间", ""))
    raw = source_record(path)
    return [
        observation(
            bank="CCB",
            product_name=values.get("产品名称", path.stem),
            tenor=tenor_years(values.get("存单期限")),
            rate=rate_decimal(values.get("年利率（%）")),
            valid_from=start,
            valid_to=end,
            minimum_cny=amount_cny(values.get("认购起点金额")),
            payment_method=values.get("付息方式"),
            early_withdrawal=values.get("提前支取条款"),
            customer_scope="PERSONAL",
            raw=raw,
            window_evidence="SOURCE_EXPLICIT_ISSUE_WINDOW",
        )
    ]


def extract_pdf_text(path: Path) -> str:
    python = (
        Path.home()
        / ".cache"
        / "codex-runtimes"
        / "codex-primary-runtime"
        / "dependencies"
        / "python"
        / "bin"
        / "python3"
    )
    if not python.is_file():
        raise RuntimeError("bundled PDF extraction runtime is unavailable")
    code = (
        "from pathlib import Path; from pypdf import PdfReader; import sys; "
        "p=Path(sys.argv[1]); r=PdfReader(p); "
        "print('\\n'.join((page.extract_text() or '') for page in r.pages))"
    )
    process = subprocess.run([str(python), "-c", code, str(path)], check=True, capture_output=True, text=True)
    return process.stdout


def pdf_field(text: str, label: str, next_labels: tuple[str, ...]) -> str | None:
    escaped_next = "|".join(re.escape(item) for item in next_labels)
    match = re.search(rf"{re.escape(label)}\s+(.+?)(?=\s+(?:{escaped_next})\s+|$)", text, flags=re.S)
    if not match:
        return None
    return " ".join(match.group(1).split())


def compact_pdf_field(text: str, label: str, next_labels: tuple[str, ...]) -> str | None:
    """Read old CCB PDFs whose extractor separates every glyph with whitespace.

    Several 2018 product sheets encode ``2018`` as four individually positioned
    glyphs.  Whitespace-sensitive parsing therefore loses the date, tenor and
    rate even though the PDF text is complete.  This fallback removes only
    layout whitespace and still requires the same surrounding field labels.
    """
    compact = re.sub(r"\s+", "", text)
    escaped_next = "|".join(re.escape(item) for item in next_labels)
    match = re.search(rf"{re.escape(label)}(.+?)(?=(?:{escaped_next})|$)", compact, flags=re.S)
    return None if not match else match.group(1)


def parse_ccb_pdf(path: Path) -> list[dict[str, Any]]:
    text = extract_pdf_text(path)
    labels = (
        "产品编号",
        "发行分行",
        "发售对象",
        "币种",
        "发行渠道",
        "存单期限",
        "发行时间",
        "认购起点金额",
        "最小递增金额",
        "年利率（%）",
        "计息类型",
        "利息计算方式",
        "是否可转让",
        "付息方式",
        "起息日",
        "到期日",
        "兑付日",
        "提前支取条款",
        "附属条款",
        "税款",
    )
    product_name = pdf_field(text, "产品名称", labels)
    tenor = pdf_field(text, "存单期限", labels)
    issue_window = pdf_field(text, "发行时间", labels)
    rate = pdf_field(text, "年利率（%）", labels)
    minimum = pdf_field(text, "认购起点金额", labels)
    payment = pdf_field(text, "付息方式", labels)
    early = pdf_field(text, "提前支取条款", labels)
    start, end = parse_date_range(issue_window or "")
    if not product_name or tenor_years(tenor) is None or rate_decimal(rate) is None or start is None or end is None:
        product_name = compact_pdf_field(text, "产品名称", labels)
        tenor = compact_pdf_field(text, "存单期限", labels)
        issue_window = compact_pdf_field(text, "发行时间", labels)
        rate = compact_pdf_field(text, "年利率（%）", labels)
        minimum = compact_pdf_field(text, "认购起点金额", labels)
        payment = compact_pdf_field(text, "付息方式", labels)
        early = compact_pdf_field(text, "提前支取条款", labels)
        start, end = parse_date_range(issue_window or "")
    if not product_name or tenor_years(tenor) is None or rate_decimal(rate) is None or start is None or end is None:
        raise RuntimeError(f"required CCB PDF product fields not found: {path.name}")
    return [
        observation(
            bank="CCB",
            product_name=product_name,
            tenor=tenor_years(tenor),
            rate=rate_decimal(rate),
            valid_from=start,
            valid_to=end,
            minimum_cny=amount_cny(minimum),
            payment_method=payment,
            early_withdrawal=early,
            customer_scope="PERSONAL",
            raw=source_record(path),
            window_evidence="SOURCE_EXPLICIT_ISSUE_WINDOW",
        )
    ]


def split_terms(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[/／]", value) if item.strip()]


def parse_icbc_doc(path: Path) -> list[dict[str, Any]]:
    process = subprocess.run(
        ["/usr/bin/textutil", "-convert", "txt", "-stdout", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    text = process.stdout
    blocks = re.split(r"(?=中国工商银行2022年第[一二三四五六]期个人大额存单\n产品名称)", text)
    raw = source_record(path)
    rows: list[dict[str, Any]] = []
    for block in blocks:
        if "产品名称\n" not in block or "产品期限\n" not in block or "产品利率\n" not in block:
            continue
        fields: dict[str, str] = {}
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        known_fields = {
            "产品名称",
            "发行期",
            "起息日",
            "产品期限",
            "产品起存金额",
            "产品利率",
            "付息方式",
            "提前支取计息方式",
        }
        for index, line in enumerate(lines[:-1]):
            if line in known_fields:
                fields[line] = lines[index + 1]
        start, end = parse_date_range(fields.get("发行期", ""))
        terms = split_terms(fields.get("产品期限", ""))
        rates = split_terms(fields.get("产品利率", ""))
        if len(terms) != len(rates):
            continue
        minimum_default = amount_cny(fields.get("产品起存金额"))
        for term, rate_value in zip(terms, rates, strict=True):
            rows.append(
                observation(
                    bank="ICBC",
                    product_name=fields.get("产品名称", path.stem),
                    tenor=tenor_years(term),
                    rate=rate_decimal(rate_value),
                    valid_from=start,
                    valid_to=end,
                    minimum_cny=minimum_default,
                    payment_method=fields.get("付息方式"),
                    early_withdrawal=fields.get("提前支取计息方式"),
                    customer_scope="PERSONAL_OR_NAMED_SEGMENT",
                    raw=raw,
                    window_evidence="SOURCE_EXPLICIT_ISSUE_WINDOW",
                )
            )
    return rows


def discover_ccb_links(path: Path) -> list[dict[str, str]]:
    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
    discovered: list[dict[str, str]] = []
    for anchor in soup.find_all("a", href=True):
        title = " ".join(anchor.get_text(" ", strip=True).split())
        href = str(anchor["href"]).strip()
        if "大额存单" not in title or "newsdetail" not in href:
            continue
        if href.startswith("http://"):
            href = "https://" + href.removeprefix("http://")
        discovered.append({"title": title, "source_uri": href})
    unique = {(item["title"], item["source_uri"]): item for item in discovered}
    return [unique[key] for key in sorted(unique)]


def load_download_receipts() -> None:
    for path in sorted((PROJECT_ROOT / "data" / "backtests" / "staging").glob("official-cd-download-*/download-receipt.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for record in payload.get("records", []):
            raw_file = record.get("raw_file")
            source_uri = record.get("source_uri")
            if raw_file and source_uri and record.get("status") in {"DOWNLOADED", "ALREADY_PRESENT"}:
                DISCOVERED_SOURCE_URLS[str(raw_file)] = str(source_uri)


def each_day(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def coverage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [
        row
        for row in rows
        if row["observation_eligible_candidate"]
        and row["valid_from"] is not None
        and row["valid_to"] is not None
    ]
    intervals: dict[tuple[str, float], list[tuple[date, date, float]]] = defaultdict(list)
    for row in usable:
        intervals[(row["bank"], row["tenor_years"])].append(
            (date.fromisoformat(row["valid_from"]), date.fromisoformat(row["valid_to"]), row["annual_rate_decimal"])
        )

    counts = Counter()
    uncovered_ranges: list[dict[str, str]] = []
    open_gap: date | None = None
    prior_gap: date | None = None
    for current in each_day(MAIN_START, MAIN_END):
        preferred = 3.0 if current < date(2018, 1, 1) else 5.0
        fallback = None if preferred == 3.0 else 3.0
        chosen: float | None = None
        chosen_banks: list[str] = []
        for candidate in (preferred, fallback):
            if candidate is None:
                continue
            banks: list[str] = []
            for bank in ("ICBC", "ABC", "BOC", "CCB"):
                rates = {
                    rate
                    for start, end, rate in intervals.get((bank, candidate), [])
                    if start <= current <= end
                }
                if len(rates) == 1:
                    banks.append(bank)
            if len(banks) >= 2:
                chosen = candidate
                chosen_banks = banks
                break
        if chosen is None:
            counts["uncovered_calendar_days"] += 1
            if open_gap is None:
                open_gap = current
            prior_gap = current
        else:
            counts["covered_calendar_days"] += 1
            counts[f"covered_tenor_{int(chosen)}y_days"] += 1
            counts[f"covered_{len(chosen_banks)}_bank_days"] += 1
            if open_gap is not None and prior_gap is not None:
                uncovered_ranges.append({"start": open_gap.isoformat(), "end": prior_gap.isoformat()})
            open_gap = None
            prior_gap = None
    if open_gap is not None and prior_gap is not None:
        uncovered_ranges.append({"start": open_gap.isoformat(), "end": prior_gap.isoformat()})
    return {
        **dict(sorted(counts.items())),
        "coverage_basis": "CALENDAR_DAY_SOURCE_EXPLICIT_WINDOWS_EXACT_ONE_UNIQUE_RATE_PER_BANK_TENOR",
        "uncovered_range_count": len(uncovered_ranges),
        "uncovered_ranges": uncovered_ranges,
        "formal_signal_series_created": False,
    }


def first_trading_days_from_parquet(path: Path) -> list[date]:
    """Return the first observed SH/SZ session in each month.

    The NAS daily bars do not currently carry a formal known_at or accepted
    license, so this calendar is explicitly candidate-only. It is useful for
    measuring the real strategy checkpoint gap without making the rate series
    or the price data formally eligible.
    """

    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"trading calendar parquet missing or empty: {path}")
    try:
        import duckdb
    except ImportError as error:
        raise RuntimeError("duckdb is required to read the candidate trading calendar") from error
    connection = duckdb.connect()
    try:
        result = connection.execute(
            """
            SELECT min(effective_date) AS signal_date
            FROM read_parquet(?)
            WHERE effective_date BETWEEN ? AND ?
              AND exchange IN ('SSE', 'SZSE')
              AND close IS NOT NULL
            GROUP BY date_trunc('month', effective_date)
            ORDER BY signal_date
            """,
            [str(path), MAIN_START, MAIN_END],
        ).fetchall()
    finally:
        connection.close()
    return [item[0] for item in result]


def rates_for_checkpoint(
    current: date,
    intervals: dict[tuple[str, float], list[tuple[date, date, float]]],
) -> dict[str, Any]:
    preferred = 3.0 if current < date(2018, 1, 1) else 5.0
    fallback = None if preferred == 3.0 else 3.0
    candidate_detail: list[dict[str, Any]] = []
    for candidate in (preferred, fallback):
        if candidate is None:
            continue
        bank_rates: dict[str, list[float]] = {}
        eligible_banks: list[str] = []
        for bank in ("ICBC", "ABC", "BOC", "CCB"):
            rates = sorted(
                {
                    rate
                    for start, end, rate in intervals.get((bank, candidate), [])
                    if start <= current <= end
                }
            )
            if rates:
                bank_rates[bank] = rates
            if len(rates) == 1:
                eligible_banks.append(bank)
        candidate_detail.append(
            {
                "tenor_years": candidate,
                "bank_rates": bank_rates,
                "eligible_banks": eligible_banks,
            }
        )
        if len(eligible_banks) >= 2:
            return {
                "covered": True,
                "chosen_tenor_years": candidate,
                "eligible_banks": eligible_banks,
                "candidates": candidate_detail,
            }
    return {
        "covered": False,
        "chosen_tenor_years": None,
        "eligible_banks": [],
        "candidates": candidate_detail,
    }


def monthly_checkpoint_coverage(rows: list[dict[str, Any]], calendar_parquet: Path) -> dict[str, Any]:
    usable = [
        row
        for row in rows
        if row["observation_eligible_candidate"]
        and row["valid_from"] is not None
        and row["valid_to"] is not None
    ]
    intervals: dict[tuple[str, float], list[tuple[date, date, float]]] = defaultdict(list)
    for row in usable:
        intervals[(row["bank"], row["tenor_years"])].append(
            (date.fromisoformat(row["valid_from"]), date.fromisoformat(row["valid_to"]), row["annual_rate_decimal"])
        )

    checkpoints = first_trading_days_from_parquet(calendar_parquet)
    details: list[dict[str, Any]] = []
    for checkpoint in checkpoints:
        result = rates_for_checkpoint(checkpoint, intervals)
        details.append({"signal_date": checkpoint.isoformat(), **result})
    covered = sum(bool(item["covered"]) for item in details)
    return {
        "status": "CANDIDATE_ONLY_NOT_FORMAL",
        "calendar_source": str(calendar_parquet.relative_to(PROJECT_ROOT)),
        "calendar_source_sha256": sha256_file(calendar_parquet),
        "calendar_formal_backtest_eligible": False,
        "calendar_limitations": [
            "The user-supplied NAS daily bars do not yet have an accepted license status.",
            "known_at is not populated, so this calendar can measure checkpoints but cannot close the point-in-time gate.",
        ],
        "checkpoint_definition": "FIRST_OBSERVED_SSE_OR_SZSE_TRADING_DAY_OF_MONTH",
        "checkpoint_count": len(details),
        "covered_checkpoint_count": covered,
        "uncovered_checkpoint_count": len(details) - covered,
        "coverage_ratio": 0.0 if not details else round(covered / len(details), 6),
        "checkpoints": details,
        "formal_signal_series_created": False,
    }


def ccb_wrapper_pdf_links(path: Path) -> list[str]:
    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
    return sorted(
        {
            str(anchor["href"]).strip()
            for anchor in soup.find_all("a", href=True)
            if str(anchor["href"]).lower().split("?", 1)[0].endswith(".pdf")
        }
    )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = list(rows[0]) if rows else []
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def deduplicate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key = (
            row["bank"],
            row["product_name"],
            row["tenor_years"],
            row["annual_rate_decimal"],
            row["valid_from"],
            row["valid_to"],
            row["minimum_purchase_cny"],
            row["source_uri"],
        )
        unique[key] = row
    return list(unique.values())


def main() -> int:
    args = parse_args()
    raw_dir = args.raw_dir.resolve()
    if not raw_dir.is_dir():
        raise RuntimeError(f"raw directory missing: {raw_dir}")
    output_dir = (
        args.output_dir
        or PROJECT_ROOT / "data" / "backtests" / "staging" / f"official-cd-normalized-{args.run_id}"
    ).resolve()
    json_path = output_dir / "observations.json"
    csv_path = output_dir / "observations.csv"
    manifest_path = output_dir / "source-manifest.json"
    coverage_path = output_dir / "coverage-report.json"
    links_path = output_dir / "ccb-discovered-links.json"
    errors_path = output_dir / "parse-errors.json"
    wrappers_path = output_dir / "ccb-wrapper-pages.json"
    audit_path = args.audit_dir.resolve() / f"{args.run_id}-official-cd-archive-audit.json"
    ensure_new(
        output_dir,
        json_path,
        csv_path,
        manifest_path,
        coverage_path,
        links_path,
        errors_path,
        wrappers_path,
        audit_path,
    )

    required = list(SOURCE_URLS)
    missing = [name for name in required if not (raw_dir / name).is_file() or (raw_dir / name).stat().st_size == 0]
    if missing:
        raise RuntimeError("required raw files missing or empty: " + ", ".join(missing))

    load_download_receipts()

    observations: list[dict[str, Any]] = []
    parse_errors: list[dict[str, Any]] = []
    wrapper_pages: list[dict[str, Any]] = []
    for name in ("abc-2016.html", "abc-2019-2021.html", "abc-2022-2023.html", "abc-2024-2025.html"):
        observations.extend(parse_abc(raw_dir / name))
    for name in (
        "boc-2016-issue-1.html",
        "boc-2016-issue-2.html",
        "boc-2016-issue-3.html",
        "boc-2016-issue-4.html",
        "boc-2025-issue-1.html",
    ):
        observations.extend(parse_boc(raw_dir / name))
    for path in sorted((raw_dir / "boc-products").glob("*.html")):
        try:
            observations.extend(parse_boc(path))
        except Exception as error:
            parse_errors.append({**source_record(path), "error_type": type(error).__name__, "message": str(error)})
    observations.extend(parse_ccb_product(raw_dir / "ccb-2016-sample.html"))
    for path in sorted((raw_dir / "ccb-products").glob("*.html")):
        try:
            observations.extend(parse_ccb_product(path))
        except Exception as error:
            pdf_links = ccb_wrapper_pdf_links(path)
            if pdf_links:
                wrapper_pages.append({**source_record(path), "linked_pdf_urls": pdf_links})
            else:
                parse_errors.append({**source_record(path), "error_type": type(error).__name__, "message": str(error)})
    observations.extend(parse_icbc_doc(raw_dir / "icbc-2022-issue-1-6.doc"))
    for path in sorted(raw_dir.glob("ccb-*.pdf")):
        try:
            observations.extend(parse_ccb_pdf(path))
        except Exception as error:
            parse_errors.append({**source_record(path), "error_type": type(error).__name__, "message": str(error)})
    for path in sorted((raw_dir / "ccb-product-pdfs").glob("*.pdf")):
        try:
            observations.extend(parse_ccb_pdf(path))
        except Exception as error:
            parse_errors.append({**source_record(path), "error_type": type(error).__name__, "message": str(error)})
    observations = deduplicate(observations)
    observations.sort(
        key=lambda item: (
            item["valid_from"] or "",
            item["bank"],
            item["tenor_years"] or 0,
            item["product_name"],
            item["annual_rate_decimal"] or 0,
        )
    )

    source_paths = [raw_dir / name for name in sorted(SOURCE_URLS)]
    source_paths.extend(sorted((raw_dir / "boc-products").glob("*.html")))
    source_paths.extend(sorted((raw_dir / "ccb-products").glob("*.html")))
    source_paths.extend(sorted((raw_dir / "ccb-product-pdfs").glob("*.pdf")))
    source_manifest = [source_record(path) for path in source_paths]
    ccb_links = discover_ccb_links(raw_dir / "ccb-2015-2016-index.html")
    coverage_report = coverage(observations)
    per_bank = Counter(row["bank"] for row in observations)
    per_bank_target = Counter(row["bank"] for row in observations if row["target_tenor"])
    status = "OFFICIAL_ARCHIVE_PARTIAL_NOT_SIGNAL_READY"
    started_at = now()
    input_payload = {
        "source_asset_sha256": {item["raw_file"]: item["source_asset_sha256"] for item in source_manifest},
        "parser_sha256": sha256_file(Path(__file__).resolve()),
        "main_window": {"start": MAIN_START.isoformat(), "end": MAIN_END.isoformat()},
    }

    output_dir.mkdir(parents=True, exist_ok=False)
    args.audit_dir.resolve().mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(observations, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_csv(csv_path, observations)
    manifest_path.write_text(json.dumps(source_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    links_path.write_text(json.dumps(ccb_links, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    errors_path.write_text(json.dumps(parse_errors, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    wrappers_path.write_text(json.dumps(wrapper_pages, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    monthly_coverage = monthly_checkpoint_coverage(observations, args.trading_calendar_parquet.resolve())
    coverage_payload = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "workflow": "BACKTEST_SOURCE_ARCHIVE_NORMALIZATION",
        "status": status,
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "input": {**input_payload, "input_sha256": sha256_bytes(json.dumps(input_payload, sort_keys=True).encode())},
        "summary": {
            "observation_count": len(observations),
            "target_tenor_observation_count": sum(row["target_tenor"] for row in observations),
            "per_bank_observation_count": dict(sorted(per_bank.items())),
            "per_bank_target_tenor_count": dict(sorted(per_bank_target.items())),
            "ccb_product_links_discovered": len(ccb_links),
            "ccb_product_pages_present": len(list((raw_dir / "ccb-products").glob("*.html"))),
            "ccb_wrapper_page_count": len(wrapper_pages),
            "parse_failure_count": len(parse_errors),
        },
        "coverage": coverage_report,
        "monthly_checkpoint_coverage": monthly_coverage,
        "formal_backtest_eligibility": {
            "eligible": False,
            "blocker": "CD_HISTORY",
            "reasons": [
                "The downloaded official archive is not yet a complete four-bank 2016-2025 panel.",
                "Coverage is measured on source-explicit windows only; no unverified carry or inferred values are used.",
                "Multiple products for one bank/date/tenor require an explicit deterministic selection rule before a formal signal is produced.",
            ],
        },
        "artifacts": {
            "observations_json": str(json_path.relative_to(PROJECT_ROOT)),
            "observations_csv": str(csv_path.relative_to(PROJECT_ROOT)),
            "source_manifest": str(manifest_path.relative_to(PROJECT_ROOT)),
            "ccb_discovered_links": str(links_path.relative_to(PROJECT_ROOT)),
            "parse_errors": str(errors_path.relative_to(PROJECT_ROOT)),
            "ccb_wrapper_pages": str(wrappers_path.relative_to(PROJECT_ROOT)),
        },
        "completed_at": now(),
    }
    coverage_path.write_text(json.dumps(coverage_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "event_type": "BACKTEST_OFFICIAL_CD_ARCHIVE_NORMALIZED",
                "run_id": args.run_id,
                "status": status,
                "input_sha256": coverage_payload["input"]["input_sha256"],
                "report": str(coverage_path.relative_to(PROJECT_ROOT)),
                "created_at": coverage_payload["completed_at"],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "run_id": args.run_id,
                "status": status,
                **coverage_payload["summary"],
                "covered_calendar_days": coverage_report.get("covered_calendar_days", 0),
                "uncovered_calendar_days": coverage_report.get("uncovered_calendar_days", 0),
                "covered_monthly_checkpoints": monthly_coverage["covered_checkpoint_count"],
                "uncovered_monthly_checkpoints": monthly_coverage["uncovered_checkpoint_count"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(
            json.dumps(
                {"status": "FAILED", "error_type": type(error).__name__, "message": str(error)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1)
