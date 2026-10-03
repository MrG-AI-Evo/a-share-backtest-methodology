from app.backtests.policy import excluded_symbols, load_backtest_policy
from app.core.settings import Settings


def test_default_policy_excludes_user_approved_symbol() -> None:
    policy = load_backtest_policy(Settings())
    assert policy["policy_version"] == "dividend-hurdle-core20-2026-09-12-draft-v4"
    assert excluded_symbols(policy) == frozenset({"600875"})
