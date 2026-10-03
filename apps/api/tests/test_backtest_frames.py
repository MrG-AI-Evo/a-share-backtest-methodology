from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from app.backtests.frames import HistoricalFrameBuilder, HistoricalFrameError
from app.backtests.historical import HistoricalDataRepository
from app.backtests.runner import DeterministicHistoryRunner


@pytest.mark.asyncio
async def test_point_in_time_tables_build_deterministic_frames(tmp_path: Path) -> None:
    database = tmp_path / "backtests.duckdb"
    repository = HistoricalDataRepository(database, tmp_path / "parquet", tmp_path)
    await repository.initialize()
    with duckdb.connect(str(database)) as db:
        db.execute(
            """
            INSERT INTO bt_prices VALUES
            ('601288','SSE','2020-01-02','2020-01-02T15:01:00+08:00',10,10,10,10,1000,100000,'price',NULL,false),
            ('601288','SSE','2020-01-03','2020-01-03T15:01:00+08:00',10,10,10,10,1000,100000,'price',NULL,false)
            """
        )
        db.execute(
            """
            INSERT INTO bt_security_master VALUES
            ('601288','SSE','2010-01-01','2010-01-01T00:00:00+08:00','农业银行',
             'ORDINARY_A_SHARE','CENTRAL_SOE','2010-07-15',NULL,'master',NULL,false)
            """
        )
        db.execute(
            """
            INSERT INTO bt_security_status VALUES
            ('601288','SSE','2010-01-01','2010-01-01T00:00:00+08:00','NORMAL','TRADABLE','status',NULL,false)
            """
        )
        db.execute(
            """
            INSERT INTO bt_financial_facts VALUES
            ('601288','SSE','2019-12-31','2020-01-01T00:00:00+08:00','2019-12-31','BANK',
             'QUALITY_GATE_PASS',1,'BOOLEAN','quality',NULL,false)
            """
        )
        for fiscal_year in (2016, 2017, 2018):
            db.execute(
                """
                INSERT INTO bt_dividend_fiscal_years VALUES
                ('601288','SSE',?,'2019-12-31T00:00:00+08:00',?,0.5,true,'fy',NULL,false)
                """,
                [f"{fiscal_year + 1}-06-01", fiscal_year],
            )
        db.execute(
            """
            INSERT INTO bt_dividends VALUES
            ('601288','SSE','2019-06-01','2019-05-01T00:00:00+08:00',2018,'2019-05-01',
             '2019-06-01','2019-06-10',0.5,'ORDINARY','dividend',NULL,false)
            """
        )
        for bank, rate in (("ICBC", "0.018"), ("ABC", "0.020")):
            db.execute(
                """
                INSERT INTO bt_cd_rates VALUES
                (?,5,'2019-01-01','2019-01-01T00:00:00+08:00','2020-12-31',?,200000,NULL,NULL,
                 'cd',NULL,false)
                """,
                [bank, rate],
            )
        fee_rule_json = '{"buy_slippage_bps":0,"sell_slippage_bps":0}'
        fee_rows = (
            ("BOTH", "BROKER_COMMISSION", "0.0003", "5"),
            ("SELL", "STAMP_DUTY", "0.001", None),
            ("BOTH", "TRANSFER_FEE", "0.00001", None),
            ("BOTH", "EXCHANGE_AND_REGULATORY_FEES", "0", None),
        )
        for side, component, rate, minimum in fee_rows:
            db.execute(
                """
                INSERT INTO bt_fee_rules VALUES
                ('SSE',?,'2016-01-01','2016-01-01T00:00:00+08:00','2025-12-31',?,?,?,?,'fees',NULL,false)
                """,
                [side, component, rate, minimum, fee_rule_json],
            )
    frames = HistoricalFrameBuilder(database).build("2020-01-02", "2020-01-03")
    assert len(frames.days) == 2
    assert frames.days[0].is_first_trading_day_of_month is True
    security = frames.days[0].securities[0]
    assert security.name == "农业银行"
    assert security.d_ttm == Decimal("0.500000")
    assert security.d_med3 == Decimal("0.500000")
    assert security.cd_rate == Decimal("0.01900000")
    result = DeterministicHistoryRunner("fixture-frame-run", "policy").run(frames.days)
    assert result.ledger.positions["601288"].quantity == 1000


@pytest.mark.asyncio
async def test_future_known_price_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "backtests.duckdb"
    repository = HistoricalDataRepository(database, tmp_path / "parquet", tmp_path)
    await repository.initialize()
    with duckdb.connect(str(database)) as db:
        db.execute(
            """
            INSERT INTO bt_prices VALUES
            ('601288','SSE','2020-01-02','2020-01-03T15:01:00+08:00',10,10,10,10,1000,100000,'price',NULL,false)
            """
        )
    with pytest.raises(HistoricalFrameError, match="known_at 晚于信号时点"):
        HistoricalFrameBuilder(database).build("2020-01-02", "2020-01-02")


@pytest.mark.asyncio
async def test_explicitly_excluded_symbol_never_enters_historical_frames(tmp_path: Path) -> None:
    database = tmp_path / "backtests.duckdb"
    repository = HistoricalDataRepository(database, tmp_path / "parquet", tmp_path)
    await repository.initialize()
    with duckdb.connect(str(database)) as db:
        db.execute(
            """
            INSERT INTO bt_prices VALUES
            ('600875','SSE','2020-01-02','2020-01-02T15:01:00+08:00',10,10,10,10,1000,100000,'price',NULL,false)
            """
        )
    with pytest.raises(HistoricalFrameError, match="主窗口没有真实未复权行情"):
        HistoricalFrameBuilder(database).build(
            "2020-01-02", "2020-01-02", frozenset({"600875"})
        )
