import pytest

from app.sources.sina import SinaDataError, parse_sector_text


def test_parse_sina_sector_jsonp() -> None:
    text = (
        "var S_Finance_bankuai_sinaindustry = {"
        '"new_dl":"new_dl,电力,100,10.00,0.25,2.50,1000,0,123000000"'
        "};"
    )
    sectors = parse_sector_text(text, "industry")
    assert len(sectors) == 1
    assert sectors[0].name == "电力"
    assert sectors[0].change_pct == 2.5
    assert sectors[0].amount_cny == 123_000_000
    assert sectors[0].heat_score is not None


def test_parse_sina_sector_rejects_non_jsonp() -> None:
    with pytest.raises(SinaDataError, match="格式异常"):
        parse_sector_text("temporarily unavailable", "concept")
