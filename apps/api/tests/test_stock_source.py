from app.sources.tencent import parse_tencent_bars, provider_symbol


def test_provider_symbol_routes_exchanges() -> None:
    assert provider_symbol("600519") == ("sh600519", "SSE")
    assert provider_symbol("300476") == ("sz300476", "SZSE")
    assert provider_symbol("920001") == ("bj920001", "BSE")


def test_parse_daily_bar_converts_lots_to_shares() -> None:
    bars = parse_tencent_bars(
        [["2026-08-24", "10", "11", "12", "9", "1234"]],
        "day",
    )
    assert bars[0].close == 11
    assert bars[0].volume_shares == 123_400
    assert bars[0].timestamp == "2026-08-24T15:00+08:00"
