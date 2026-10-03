from app.sources.tencent import INDEX_SPECS, classify_quote_freshness, parse_tencent_indices


def test_parse_tencent_indices() -> None:
    statements = []
    for index, spec in enumerate(INDEX_SPECS):
        fields = [""] * 40
        fields[1] = spec.fallback_name
        fields[2] = spec.symbol[:6]
        fields[3] = str(3000 + index)
        fields[30] = "20260824150000"
        fields[31] = "10.50"
        fields[32] = "0.35"
        fields[37] = "123456.78"
        statements.append(f'v_{spec.provider_symbol}="{"~".join(fields)}";')

    quotes = parse_tencent_indices("\n".join(statements).encode("gb18030"))

    assert len(quotes) == 6
    assert quotes[0].symbol == "000001.SH"
    assert quotes[0].amount_cny == 1_234_567_800
    assert quotes[0].updated_at == "2026-08-24T15:00:00+08:00"
    assert quotes[0].market_status is None


def test_quote_freshness_thresholds() -> None:
    assert classify_quote_freshness(300) == "live"
    assert classify_quote_freshness(301) == "delayed"
    assert classify_quote_freshness(86_400) == "delayed"
    assert classify_quote_freshness(86_401) == "stale"
