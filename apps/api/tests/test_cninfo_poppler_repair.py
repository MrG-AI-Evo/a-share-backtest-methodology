from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[3] / "scripts" / "repair_cninfo_extraction_with_poppler.py"
)
SPEC = importlib.util.spec_from_file_location("repair_cninfo_extraction_with_poppler", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_split_pages_drops_only_trailing_separator() -> None:
    assert MODULE.split_pages("first\fsecond\f") == ["first", "second"]
    assert MODULE.split_pages("first\f\fthird") == ["first", "", "third"]


def test_unresolved_keys_closes_failure_with_later_success() -> None:
    records = [
        {"_failed": True, "symbol": "600875", "announcement_id": "1216301205"},
        {"source": {"symbol": "600875", "announcement_id": "1216301205"}},
        {"_failed": True, "symbol": "000001", "announcement_id": "missing"},
    ]
    assert MODULE.unresolved_keys(records) == {("000001", "missing")}
