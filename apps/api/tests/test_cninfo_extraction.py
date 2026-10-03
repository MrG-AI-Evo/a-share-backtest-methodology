from __future__ import annotations

import importlib.util
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "extract_cninfo_quality_facts.py"
SPEC = importlib.util.spec_from_file_location("extract_cninfo_quality_facts", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_document_timeout_returns_auditable_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow_extract(_row: dict[str, str]) -> None:
        time.sleep(2)
        raise AssertionError("timeout did not interrupt extraction")

    monkeypatch.setattr(MODULE, "_extract_one_unbounded", slow_extract)
    row = {"symbol": "000001", "announcement_id": "test", "path": "/tmp/test.pdf"}

    started = time.monotonic()
    result = MODULE.extract_one(row, timeout_seconds=0.05)

    assert time.monotonic() - started < 1
    assert result == {
        "_failed": True,
        "symbol": "000001",
        "announcement_id": "test",
        "path": "/tmp/test.pdf",
        "error": "DOCUMENT_TIMEOUT: exceeded 0.05 seconds",
    }


def test_document_timeout_is_disarmed_after_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(MODULE, "_extract_one_unbounded", lambda row: {"ok": row["symbol"]})

    assert MODULE.extract_one({"symbol": "000001"}, timeout_seconds=0.05) == {"ok": "000001"}
    time.sleep(0.08)


def test_unmodified_opinion_paragraph_is_classified_without_literal_label() -> None:
    pages = [
        "三、审计意见 我们认为，该公司财务报表在所有重大方面按照企业会计准则编制，"
        "公允反映了报告期财务状况和经营成果。"
    ]

    result = MODULE.audit_opinion(pages, readable=True)

    assert result["classification"] == "STANDARD_UNQUALIFIED"
    assert result["evidence_page"] == 1


def test_parent_profit_variants_are_extracted() -> None:
    results = MODULE.metric_candidates(
        ["报告期归属于母公司所有者净利润 5,596,633,222.72 元。"]
    )

    value = next(item for item in results if item["metric_id"] == "net_profit_parent")
    assert value["raw_value"] == "5,596,633,222.72"


def test_parent_profit_percentage_narrative_is_skipped() -> None:
    results = MODULE.metric_candidates(
        [
            "现金分红占归属于母公司所有者净利润的35%。",
            "归属于母公司所有者净利润 5,596,633,222.72 元。",
        ]
    )

    value = next(item for item in results if item["metric_id"] == "net_profit_parent")
    assert value["raw_value"] == "5,596,633,222.72"


def test_pdf_line_wrap_inside_chinese_metric_label_is_normalized() -> None:
    results = MODULE.metric_candidates(
        ["归属于上市公司股 东的净利润 5,596,633,222.73 元。"]
    )

    value = next(item for item in results if item["metric_id"] == "net_profit_parent")
    assert value["raw_value"] == "5,596,633,222.73"
