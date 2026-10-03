from app.sources.eastmoney import parse_market_breadth, parse_sector_rows


def test_parse_market_breadth_aggregates_exchange_groups() -> None:
    payload = {
        "data": {
            "diff": [
                {"f104": 10, "f105": 20, "f106": 2, "f6": 1_000_000},
                {"f104": 30, "f105": 40, "f106": 3, "f6": 2_000_000},
            ]
        }
    }
    breadth = parse_market_breadth(payload)
    assert breadth.advancing == 40
    assert breadth.declining == 60
    assert breadth.unchanged == 5
    assert breadth.total_amount_cny == 3_000_000
    assert breadth.limit_up is None


def test_parse_sector_rows_skips_invalid_values() -> None:
    payload = {
        "data": {
            "diff": [
                {"f14": "半导体", "f3": 2.5, "f6": 10_000_000_000},
                {"f14": "缺失", "f3": "-", "f6": "-"},
            ]
        }
    }
    sectors = parse_sector_rows(payload, "industry")
    assert len(sectors) == 1
    assert sectors[0].name == "半导体"
    assert sectors[0].heat_score is not None
