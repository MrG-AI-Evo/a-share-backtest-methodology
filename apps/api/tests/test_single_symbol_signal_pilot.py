import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "run_single_symbol_signal_pilot.py"
SPEC = importlib.util.spec_from_file_location("run_single_symbol_signal_pilot", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_signal_pilot_fails_closed_and_only_marks_upper_bound_candidates() -> None:
    assert MODULE.classify(close=None, d_ttm=0.2, cd_rate=0.03) == "BLOCKED_PRICE"
    assert MODULE.classify(close=4.0, d_ttm=0.2, cd_rate=None) == "BLOCKED_CD_RATE"
    assert (
        MODULE.classify(close=4.0, d_ttm=0.0, cd_rate=0.02)
        == "DEFINITELY_INELIGIBLE_NO_TTM_DIVIDEND"
    )
    assert (
        MODULE.classify(close=10.0, d_ttm=0.2, cd_rate=0.02) == "DEFINITELY_INELIGIBLE_UPPER_BOUND"
    )
    assert (
        MODULE.classify(close=4.0, d_ttm=0.3, cd_rate=0.02)
        == "POTENTIAL_ENTRY_REQUIRES_D_MED3_AND_QUALITY_GATE"
    )
