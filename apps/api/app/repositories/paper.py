from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import aiosqlite

from app.core.settings import Settings
from app.core.time import iso_now, now_shanghai
from app.models import (
    AuditEvent,
    CorporateAction,
    NavPoint,
    PaperFill,
    PaperOrder,
    PaperPosition,
    PaperSummary,
    WatchlistCreate,
    WatchlistItem,
)

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    base_currency TEXT NOT NULL DEFAULT 'CNY',
    initial_cash REAL NOT NULL CHECK (initial_cash >= 0),
    cash REAL NOT NULL CHECK (cash >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    available_quantity INTEGER NOT NULL CHECK (available_quantity >= 0),
    cost_price REAL NOT NULL CHECK (cost_price >= 0),
    last_price REAL NOT NULL CHECK (last_price >= 0),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS position_lots (
    lot_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    acquired_date TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    remaining_quantity INTEGER NOT NULL CHECK (remaining_quantity >= 0),
    cost_price REAL NOT NULL CHECK (cost_price > 0),
    fill_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_orders (
    order_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    order_type TEXT NOT NULL CHECK (order_type = 'LIMIT_SIM'),
    limit_price REAL NOT NULL CHECK (limit_price > 0),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    filled_quantity INTEGER NOT NULL DEFAULT 0 CHECK (filled_quantity >= 0),
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    research_version TEXT,
    expected_horizon TEXT NOT NULL,
    risk_notes_json TEXT NOT NULL,
    invalidation_conditions_json TEXT NOT NULL,
    invalidation_price REAL,
    ruleset_version TEXT NOT NULL,
    fee_schedule_version TEXT NOT NULL,
    risk_checks_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    earliest_fill_at TEXT NOT NULL,
    approved_at TEXT,
    rejected_at TEXT,
    rejection_reason TEXT
);
CREATE INDEX IF NOT EXISTS paper_orders_created_idx ON paper_orders(created_at DESC);
CREATE TABLE IF NOT EXISTS paper_fills (
    fill_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES paper_orders(order_id),
    filled_at TEXT NOT NULL,
    price REAL NOT NULL CHECK (price > 0),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    gross_amount_cny REAL NOT NULL,
    commission_cny REAL NOT NULL,
    stamp_duty_cny REAL NOT NULL,
    transfer_fee_cny REAL NOT NULL,
    slippage_cny REAL NOT NULL,
    total_fees_cny REAL NOT NULL,
    source TEXT NOT NULL,
    source_timestamp TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS corporate_actions_applied (
    action_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    action_type TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    cash_effect_cny REAL NOT NULL DEFAULT 0,
    quantity_before INTEGER NOT NULL DEFAULT 0,
    quantity_after INTEGER NOT NULL DEFAULT 0,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nav_snapshots (
    as_of TEXT PRIMARY KEY,
    total_assets REAL NOT NULL,
    cash REAL NOT NULL,
    market_value REAL NOT NULL,
    daily_pnl REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS watchlist (
    symbol TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    stage TEXT NOT NULL,
    note TEXT NOT NULL,
    added_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    prev_event_hash TEXT,
    event_hash TEXT NOT NULL UNIQUE
);
"""


class PaperLedgerError(RuntimeError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@asynccontextmanager
async def _connect(database: Path) -> AsyncIterator[aiosqlite.Connection]:
    db = await aiosqlite.connect(database)
    await db.execute("PRAGMA foreign_keys = ON")
    await db.execute("PRAGMA busy_timeout = 5000")
    try:
        yield db
    finally:
        await db.close()


def _same_idempotent_request(existing: PaperOrder, incoming: PaperOrder) -> bool:
    fields = (
        "symbol",
        "name",
        "side",
        "order_type",
        "limit_price",
        "quantity",
        "reason",
        "research_version",
        "expected_horizon",
        "risk_notes",
        "invalidation_conditions",
        "invalidation_price",
    )
    return all(getattr(existing, field) == getattr(incoming, field) for field in fields)


async def _append_audit(
    db: aiosqlite.Connection,
    event_type: str,
    payload: dict[str, Any],
    actor: str = "local-user",
    run_id: str | None = None,
) -> None:
    previous = await (
        await db.execute("SELECT event_hash FROM audit_events ORDER BY rowid DESC LIMIT 1")
    ).fetchone()
    prev_hash = str(previous[0]) if previous else None
    occurred_at = iso_now()
    event_id = str(uuid4())
    event = {
        "event_id": event_id,
        "run_id": run_id or f"{now_shanghai():%Y%m%d-%H%M%S}-paper",
        "event_type": event_type,
        "actor": actor,
        "occurred_at": occurred_at,
        "payload": payload,
        "prev_event_hash": prev_hash,
    }
    event_hash = hashlib.sha256(f"{prev_hash or ''}{_json(event)}".encode()).hexdigest()
    await db.execute(
        """
        INSERT INTO audit_events
        (event_id, run_id, event_type, actor, occurred_at, payload_json,
         prev_event_hash, event_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            event["run_id"],
            event_type,
            actor,
            occurred_at,
            _json(payload),
            prev_hash,
            event_hash,
        ),
    )


class PaperRepository:
    def __init__(self, settings: Settings) -> None:
        self._database = settings.paper_database

    async def initialize(self, initial_cash: float = 1_000_000) -> None:
        self._database.parent.mkdir(parents=True, exist_ok=True)
        async with _connect(self._database) as db:
            await db.executescript(SCHEMA)
            timestamp = iso_now()
            await db.execute(
                """
                INSERT OR IGNORE INTO accounts (id, initial_cash, cash, created_at, updated_at)
                VALUES (1, ?, ?, ?, ?)
                """,
                (initial_cash, initial_cash, timestamp, timestamp),
            )
            await db.execute(
                """
                INSERT OR IGNORE INTO nav_snapshots
                (as_of, total_assets, cash, market_value, daily_pnl)
                VALUES (?, ?, ?, 0, 0)
                """,
                (timestamp, initial_cash, initial_cash),
            )
            await db.commit()

    async def _summary_with_db(self, db: aiosqlite.Connection) -> PaperSummary:
        db.row_factory = aiosqlite.Row
        account = await (await db.execute("SELECT * FROM accounts WHERE id = 1")).fetchone()
        positions = await (
            await db.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(quantity * last_price), 0) AS value "
                "FROM positions WHERE quantity > 0"
            )
        ).fetchone()
        snapshots = list(
            await (await db.execute("SELECT * FROM nav_snapshots ORDER BY as_of")).fetchall()
        )
        if account is None or positions is None:
            raise PaperLedgerError("模拟账户未初始化或聚合失败")
        market_value = float(positions["value"])
        cash = float(account["cash"])
        total_assets = cash + market_value
        initial_cash = float(account["initial_cash"])
        running_peak = 0.0
        max_drawdown = 0.0
        for snapshot in snapshots:
            nav = float(snapshot["total_assets"])
            running_peak = max(running_peak, nav)
            if running_peak:
                max_drawdown = max(max_drawdown, (running_peak - nav) / running_peak * 100)
        if running_peak:
            max_drawdown = max(max_drawdown, (running_peak - total_assets) / running_peak * 100)
        latest = snapshots[-1] if snapshots else None
        return PaperSummary(
            initial_cash=initial_cash,
            total_assets=total_assets,
            cash=cash,
            market_value=market_value,
            total_return_pct=((total_assets / initial_cash) - 1) * 100 if initial_cash else 0.0,
            daily_pnl=float(latest["daily_pnl"]) if latest else 0.0,
            max_drawdown_pct=max_drawdown,
            position_pct=(market_value / total_assets) * 100 if total_assets else 0.0,
            holding_count=int(positions["count"]),
            as_of=str(latest["as_of"]) if latest else str(account["updated_at"]),
        )

    async def summary(self) -> PaperSummary:
        async with _connect(self._database) as db:
            return await self._summary_with_db(db)

    async def positions(self, as_of: date | None = None) -> list[PaperPosition]:
        trade_date = (as_of or now_shanghai().date()).isoformat()
        async with _connect(self._database) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT p.*, COALESCE((
                        SELECT SUM(l.remaining_quantity) FROM position_lots l
                        WHERE l.symbol = p.symbol AND l.acquired_date < ?
                    ), 0) AS sellable
                    FROM positions p WHERE p.quantity > 0 ORDER BY p.symbol
                    """,
                    (trade_date,),
                )
            ).fetchall()
        result: list[PaperPosition] = []
        for row in rows:
            value = float(row["quantity"]) * float(row["last_price"])
            pnl = (float(row["last_price"]) - float(row["cost_price"])) * int(row["quantity"])
            result.append(
                PaperPosition(
                    symbol=str(row["symbol"]),
                    name=str(row["name"]),
                    quantity=int(row["quantity"]),
                    sellable_quantity=int(row["sellable"]),
                    cost_price=float(row["cost_price"]),
                    last_price=float(row["last_price"]),
                    market_value_cny=value,
                    unrealized_pnl_cny=pnl,
                    unrealized_pnl_pct=(float(row["last_price"]) / float(row["cost_price"]) - 1)
                    * 100,
                    updated_at=str(row["updated_at"]),
                )
            )
        return result

    async def new_positions_opened_on(self, trade_date: date) -> int:
        """Count currently open positions whose active lot cycle began on the date."""
        async with _connect(self._database) as db:
            row = await (
                await db.execute(
                    """
                    SELECT COUNT(*) FROM (
                        SELECT p.symbol
                        FROM positions p
                        JOIN position_lots l
                          ON l.symbol = p.symbol AND l.remaining_quantity > 0
                        WHERE p.quantity > 0
                        GROUP BY p.symbol
                        HAVING MIN(l.acquired_date) = ?
                    )
                    """,
                    (trade_date.isoformat(),),
                )
            ).fetchone()
        return int(row[0]) if row else 0

    async def mark_to_market(
        self,
        prices: dict[str, tuple[float, str]],
        as_of: str,
    ) -> PaperSummary:
        if not prices:
            return await self.summary()
        trade_date = as_of[:10]
        async with _connect(self._database) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            for symbol, (price, source_timestamp) in prices.items():
                if price <= 0:
                    raise PaperLedgerError(f"{symbol} 估值价格必须大于 0")
                await db.execute(
                    """
                    UPDATE positions SET last_price = ?, updated_at = ?
                    WHERE symbol = ? AND quantity > 0
                    """,
                    (price, source_timestamp, symbol),
                )
            account = await (await db.execute("SELECT * FROM accounts WHERE id = 1")).fetchone()
            market_row = await (
                await db.execute(
                    "SELECT COALESCE(SUM(quantity * last_price), 0) FROM positions "
                    "WHERE quantity > 0"
                )
            ).fetchone()
            if account is None or market_row is None:
                raise PaperLedgerError("模拟账户估值聚合失败")
            cash = float(account["cash"])
            market_value = float(market_row[0])
            total_assets = cash + market_value
            reference = await (
                await db.execute(
                    """
                    SELECT total_assets FROM nav_snapshots
                    WHERE substr(as_of, 1, 10) < ?
                    ORDER BY as_of DESC LIMIT 1
                    """,
                    (trade_date,),
                )
            ).fetchone()
            if reference is None:
                reference = await (
                    await db.execute(
                        "SELECT total_assets FROM nav_snapshots ORDER BY as_of LIMIT 1"
                    )
                ).fetchone()
            reference_assets = float(reference[0]) if reference else total_assets
            daily_pnl = total_assets - reference_assets
            await db.execute(
                """
                INSERT INTO nav_snapshots
                (as_of, total_assets, cash, market_value, daily_pnl)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(as_of) DO UPDATE SET
                    total_assets=excluded.total_assets,
                    cash=excluded.cash,
                    market_value=excluded.market_value,
                    daily_pnl=excluded.daily_pnl
                """,
                (as_of, total_assets, cash, market_value, daily_pnl),
            )
            await db.commit()
            return await self._summary_with_db(db)

    async def nav(self, limit: int = 260) -> list[NavPoint]:
        async with _connect(self._database) as db:
            db.row_factory = aiosqlite.Row
            rows = list(
                await (
                    await db.execute(
                        "SELECT * FROM nav_snapshots ORDER BY as_of DESC LIMIT ?",
                        (limit,),
                    )
                ).fetchall()
            )
        return [
            NavPoint(
                as_of=str(row["as_of"]),
                total_assets=float(row["total_assets"]),
                cash=float(row["cash"]),
                market_value=float(row["market_value"]),
                daily_pnl=float(row["daily_pnl"]),
            )
            for row in reversed(rows)
        ]

    async def create_order(self, order: PaperOrder, idempotency_key: str) -> PaperOrder:
        async with _connect(self._database) as db:
            await db.execute("BEGIN IMMEDIATE")
            existing = await (
                await db.execute(
                    "SELECT order_id FROM paper_orders WHERE idempotency_key = ?",
                    (idempotency_key,),
                )
            ).fetchone()
            if existing:
                found = await self.get_order(str(existing[0]))
                if found is None:
                    raise PaperLedgerError("幂等订单索引损坏")
                if not _same_idempotent_request(found, order):
                    raise PaperLedgerError("同一幂等键对应了不同订单内容，拒绝复用")
                return found
            await db.execute(
                """
                INSERT INTO paper_orders (
                    order_id, idempotency_key, symbol, name, side, order_type, limit_price,
                    quantity, filled_quantity, status, reason, research_version,
                    expected_horizon, risk_notes_json, invalidation_conditions_json,
                    invalidation_price, ruleset_version, fee_schedule_version,
                    risk_checks_json, created_at, earliest_fill_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.order_id,
                    idempotency_key,
                    order.symbol,
                    order.name,
                    order.side,
                    order.order_type,
                    order.limit_price,
                    order.quantity,
                    0,
                    order.status,
                    order.reason,
                    order.research_version,
                    order.expected_horizon,
                    _json(order.risk_notes),
                    _json(order.invalidation_conditions),
                    order.invalidation_price,
                    order.ruleset_version,
                    order.fee_schedule_version,
                    _json([check.model_dump(mode="json") for check in order.risk_checks]),
                    order.created_at,
                    order.earliest_fill_at,
                ),
            )
            await _append_audit(
                db,
                "PROPOSAL_CREATED",
                {"order_id": order.order_id, "symbol": order.symbol, "side": order.side},
            )
            await db.commit()
        return order

    @staticmethod
    def _fill_from_row(row: aiosqlite.Row) -> PaperFill:
        return PaperFill(
            fill_id=str(row["fill_id"]),
            order_id=str(row["order_id"]),
            filled_at=str(row["filled_at"]),
            price=float(row["price"]),
            quantity=int(row["quantity"]),
            gross_amount_cny=float(row["gross_amount_cny"]),
            commission_cny=float(row["commission_cny"]),
            stamp_duty_cny=float(row["stamp_duty_cny"]),
            transfer_fee_cny=float(row["transfer_fee_cny"]),
            slippage_cny=float(row["slippage_cny"]),
            total_fees_cny=float(row["total_fees_cny"]),
            source=str(row["source"]),
            source_timestamp=str(row["source_timestamp"]),
        )

    @staticmethod
    def _order_from_row(row: aiosqlite.Row, fills: list[PaperFill]) -> PaperOrder:
        data = dict(row)
        return PaperOrder(
            order_id=str(data["order_id"]),
            symbol=str(data["symbol"]),
            name=str(data["name"]),
            side=cast(Any, data["side"]),
            order_type="LIMIT_SIM",
            limit_price=float(data["limit_price"]),
            quantity=int(data["quantity"]),
            filled_quantity=int(data["filled_quantity"]),
            status=cast(Any, data["status"]),
            reason=str(data["reason"]),
            research_version=str(data["research_version"]) if data["research_version"] else None,
            expected_horizon=str(data["expected_horizon"]),
            risk_notes=json.loads(str(data["risk_notes_json"])),
            invalidation_conditions=json.loads(str(data["invalidation_conditions_json"])),
            invalidation_price=(
                float(data["invalidation_price"])
                if data["invalidation_price"] is not None
                else None
            ),
            ruleset_version=str(data["ruleset_version"]),
            fee_schedule_version=str(data["fee_schedule_version"]),
            risk_checks=json.loads(str(data["risk_checks_json"])),
            created_at=str(data["created_at"]),
            earliest_fill_at=str(data["earliest_fill_at"]),
            approved_at=str(data["approved_at"]) if data["approved_at"] else None,
            rejected_at=str(data["rejected_at"]) if data["rejected_at"] else None,
            rejection_reason=(str(data["rejection_reason"]) if data["rejection_reason"] else None),
            fills=fills,
        )

    async def get_order(self, order_id: str) -> PaperOrder | None:
        async with _connect(self._database) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute("SELECT * FROM paper_orders WHERE order_id = ?", (order_id,))
            ).fetchone()
            if row is None:
                return None
            fill_rows = await (
                await db.execute(
                    "SELECT * FROM paper_fills WHERE order_id = ? ORDER BY filled_at",
                    (order_id,),
                )
            ).fetchall()
        return self._order_from_row(row, [self._fill_from_row(item) for item in fill_rows])

    async def orders(self, limit: int = 200) -> list[PaperOrder]:
        async with _connect(self._database) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT * FROM paper_orders ORDER BY created_at DESC LIMIT ?", (limit,)
                )
            ).fetchall()
            fill_rows = await (
                await db.execute("SELECT * FROM paper_fills ORDER BY filled_at")
            ).fetchall()
        fills: dict[str, list[PaperFill]] = {}
        for row in fill_rows:
            fill = self._fill_from_row(row)
            fills.setdefault(fill.order_id, []).append(fill)
        return [self._order_from_row(row, fills.get(str(row["order_id"]), [])) for row in rows]

    async def update_order_status(
        self,
        order_id: str,
        expected_status: str,
        status: str,
        reason: str,
    ) -> PaperOrder:
        timestamp = iso_now()
        approved_at = timestamp if status == "QUEUED" else None
        rejected_at = timestamp if status == "REJECTED" else None
        async with _connect(self._database) as db:
            result = await db.execute(
                """
                UPDATE paper_orders SET status = ?, approved_at = COALESCE(?, approved_at),
                    rejected_at = COALESCE(?, rejected_at),
                    rejection_reason = CASE WHEN ? = 'REJECTED' THEN ? ELSE rejection_reason END
                WHERE order_id = ? AND status = ?
                """,
                (status, approved_at, rejected_at, status, reason, order_id, expected_status),
            )
            if result.rowcount != 1:
                raise PaperLedgerError("订单状态已变化，拒绝重复操作")
            await _append_audit(
                db,
                "APPROVED" if status == "QUEUED" else "REJECTED",
                {"order_id": order_id, "reason": reason, "new_status": status},
            )
            await db.commit()
        order = await self.get_order(order_id)
        if order is None:
            raise PaperLedgerError("订单更新后无法读取")
        return order

    async def record_fill(self, order_id: str, fill: PaperFill) -> PaperOrder:
        trade_date = fill.filled_at[:10]
        async with _connect(self._database) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            order = await (
                await db.execute("SELECT * FROM paper_orders WHERE order_id = ?", (order_id,))
            ).fetchone()
            account = await (await db.execute("SELECT * FROM accounts WHERE id = 1")).fetchone()
            if order is None or account is None:
                raise PaperLedgerError("订单或账户不存在")
            if order["status"] not in {"QUEUED", "PARTIAL"}:
                raise PaperLedgerError("只有已人工批准的排队/部分成交订单可以模拟成交")
            remaining_order_quantity = int(order["quantity"]) - int(order["filled_quantity"])
            if fill.quantity > remaining_order_quantity:
                raise PaperLedgerError("本次成交数量超过订单剩余数量")
            gross = fill.gross_amount_cny
            fees = fill.total_fees_cny
            cash = float(account["cash"])
            position = await (
                await db.execute(
                    "SELECT * FROM positions WHERE symbol = ?", (str(order["symbol"]),)
                )
            ).fetchone()
            if order["side"] == "BUY":
                required = gross + fees
                if cash + 1e-6 < required:
                    raise PaperLedgerError("模拟账户现金不足")
                old_quantity = int(position["quantity"]) if position else 0
                old_cost = float(position["cost_price"]) if position else 0.0
                new_quantity = old_quantity + fill.quantity
                new_cost = (old_quantity * old_cost + gross + fees) / new_quantity
                await db.execute(
                    """
                    INSERT INTO positions
                    (symbol, name, quantity, available_quantity, cost_price, last_price, updated_at)
                    VALUES (?, ?, ?, 0, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        name=excluded.name, quantity=excluded.quantity,
                        available_quantity=positions.available_quantity,
                        cost_price=excluded.cost_price, last_price=excluded.last_price,
                        updated_at=excluded.updated_at
                    """,
                    (
                        str(order["symbol"]),
                        str(order["name"]),
                        new_quantity,
                        new_cost,
                        fill.price,
                        fill.filled_at,
                    ),
                )
                await db.execute(
                    """
                    INSERT INTO position_lots
                    (lot_id, symbol, acquired_date, quantity, remaining_quantity,
                     cost_price, fill_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        str(order["symbol"]),
                        trade_date,
                        fill.quantity,
                        fill.quantity,
                        (gross + fees) / fill.quantity,
                        fill.fill_id,
                    ),
                )
                new_cash = cash - required
            else:
                if position is None or int(position["quantity"]) < fill.quantity:
                    raise PaperLedgerError("模拟持仓数量不足")
                lots = await (
                    await db.execute(
                        """
                        SELECT * FROM position_lots
                        WHERE symbol = ? AND acquired_date < ? AND remaining_quantity > 0
                        ORDER BY acquired_date, rowid
                        """,
                        (str(order["symbol"]), trade_date),
                    )
                ).fetchall()
                if sum(int(lot["remaining_quantity"]) for lot in lots) < fill.quantity:
                    raise PaperLedgerError("T+1 可卖数量不足")
                remaining = fill.quantity
                for lot in lots:
                    used = min(remaining, int(lot["remaining_quantity"]))
                    await db.execute(
                        "UPDATE position_lots SET remaining_quantity = remaining_quantity - ? "
                        "WHERE lot_id = ?",
                        (used, str(lot["lot_id"])),
                    )
                    remaining -= used
                    if remaining == 0:
                        break
                new_quantity = int(position["quantity"]) - fill.quantity
                await db.execute(
                    """
                    UPDATE positions SET quantity = ?,
                        available_quantity = MAX(0, available_quantity - ?),
                        last_price = ?, updated_at = ? WHERE symbol = ?
                    """,
                    (
                        new_quantity,
                        fill.quantity,
                        fill.price,
                        fill.filled_at,
                        str(order["symbol"]),
                    ),
                )
                new_cash = cash + gross - fees
            await db.execute(
                "UPDATE accounts SET cash = ?, updated_at = ? WHERE id = 1",
                (new_cash, fill.filled_at),
            )
            await db.execute(
                """
                INSERT INTO paper_fills
                (fill_id, order_id, filled_at, price, quantity, gross_amount_cny,
                commission_cny, stamp_duty_cny, transfer_fee_cny, slippage_cny,
                total_fees_cny, source, source_timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fill.fill_id,
                    fill.order_id,
                    fill.filled_at,
                    fill.price,
                    fill.quantity,
                    fill.gross_amount_cny,
                    fill.commission_cny,
                    fill.stamp_duty_cny,
                    fill.transfer_fee_cny,
                    fill.slippage_cny,
                    fill.total_fees_cny,
                    fill.source,
                    fill.source_timestamp,
                ),
            )
            total_filled = int(order["filled_quantity"]) + fill.quantity
            new_status = "FILLED" if total_filled == int(order["quantity"]) else "PARTIAL"
            await db.execute(
                "UPDATE paper_orders SET status = ?, filled_quantity = ? WHERE order_id = ?",
                (new_status, total_filled, order_id),
            )
            market_value_row = await (
                await db.execute(
                    "SELECT COALESCE(SUM(quantity * last_price), 0) "
                    "FROM positions WHERE quantity > 0"
                )
            ).fetchone()
            market_value = float(market_value_row[0]) if market_value_row else 0.0
            total_assets = new_cash + market_value
            await db.execute(
                """
                INSERT INTO nav_snapshots (as_of, total_assets, cash, market_value, daily_pnl)
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(as_of) DO UPDATE SET total_assets=excluded.total_assets,
                    cash=excluded.cash, market_value=excluded.market_value
                """,
                (fill.filled_at, total_assets, new_cash, market_value),
            )
            await _append_audit(
                db,
                "ORDER_STATE_CHANGED",
                {
                    "order_id": order_id,
                    "fill_id": fill.fill_id,
                    "fill_quantity": fill.quantity,
                    "filled_quantity": total_filled,
                    "new_status": new_status,
                },
            )
            await db.commit()
        updated = await self.get_order(order_id)
        if updated is None:
            raise PaperLedgerError("成交后订单无法读取")
        return updated

    async def apply_corporate_action(self, action: CorporateAction) -> dict[str, Any]:
        async with _connect(self._database) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            existing = await (
                await db.execute(
                    "SELECT * FROM corporate_actions_applied WHERE action_id = ?",
                    (action.action_id,),
                )
            ).fetchone()
            if existing is not None:
                return dict(existing)
            position = await (
                await db.execute("SELECT * FROM positions WHERE symbol = ?", (action.symbol,))
            ).fetchone()
            quantity_before = int(position["quantity"]) if position else 0
            quantity_after = quantity_before
            cash_effect = 0.0
            applied_at = iso_now()
            if action.action_type == "CASH_DIVIDEND":
                if action.cash_per_share_cny is None:
                    raise PaperLedgerError("现金分红缺少每股金额")
                cash_effect = round(quantity_before * action.cash_per_share_cny, 2)
                await db.execute(
                    "UPDATE accounts SET cash = cash + ?, updated_at = ? WHERE id = 1",
                    (cash_effect, applied_at),
                )
            elif action.action_type == "SHARE_MULTIPLIER":
                if action.share_multiplier is None:
                    raise PaperLedgerError("送转/拆股缺少股份乘数")
                raw_quantity = quantity_before * action.share_multiplier
                quantity_after = round(raw_quantity)
                if abs(raw_quantity - quantity_after) > 1e-8:
                    raise PaperLedgerError("公司行动产生非整数股份，缺少零股处理规则")
                if position is not None and quantity_before > 0:
                    await db.execute(
                        """
                        UPDATE positions SET quantity = ?,
                            available_quantity = ROUND(available_quantity * ?),
                            cost_price = cost_price / ?, updated_at = ?
                        WHERE symbol = ?
                        """,
                        (
                            quantity_after,
                            action.share_multiplier,
                            action.share_multiplier,
                            applied_at,
                            action.symbol,
                        ),
                    )
                    await db.execute(
                        """
                        UPDATE position_lots SET
                            quantity = ROUND(quantity * ?),
                            remaining_quantity = ROUND(remaining_quantity * ?),
                            cost_price = cost_price / ?
                        WHERE symbol = ?
                        """,
                        (
                            action.share_multiplier,
                            action.share_multiplier,
                            action.share_multiplier,
                            action.symbol,
                        ),
                    )
            await db.execute(
                """
                INSERT INTO corporate_actions_applied
                (action_id, symbol, action_type, effective_date, payload_json,
                 cash_effect_cny, quantity_before, quantity_after, applied_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action.action_id,
                    action.symbol,
                    action.action_type,
                    action.effective_date,
                    _json(action.model_dump(mode="json")),
                    cash_effect,
                    quantity_before,
                    quantity_after,
                    applied_at,
                ),
            )
            await _append_audit(
                db,
                "CORPORATE_ACTION_APPLIED",
                {
                    "action_id": action.action_id,
                    "symbol": action.symbol,
                    "action_type": action.action_type,
                    "cash_effect_cny": cash_effect,
                    "quantity_before": quantity_before,
                    "quantity_after": quantity_after,
                },
            )
            await db.commit()
        return {
            "action_id": action.action_id,
            "symbol": action.symbol,
            "action_type": action.action_type,
            "effective_date": action.effective_date,
            "cash_effect_cny": cash_effect,
            "quantity_before": quantity_before,
            "quantity_after": quantity_after,
            "applied_at": applied_at,
        }

    async def reconcile(self) -> dict[str, Any]:
        errors: list[str] = []
        async with _connect(self._database) as db:
            db.row_factory = aiosqlite.Row
            account = await (await db.execute("SELECT * FROM accounts WHERE id = 1")).fetchone()
            if account is None or float(account["cash"]) < -1e-6:
                errors.append("ACCOUNT_CASH_INVALID")
            positions = list(await (await db.execute("SELECT * FROM positions")).fetchall())
            for position in positions:
                lot_row = await (
                    await db.execute(
                        "SELECT COALESCE(SUM(remaining_quantity), 0) AS quantity "
                        "FROM position_lots WHERE symbol = ?",
                        (str(position["symbol"]),),
                    )
                ).fetchone()
                lot_quantity = int(lot_row["quantity"]) if lot_row else 0
                if int(position["quantity"]) != lot_quantity:
                    errors.append(f"POSITION_LOT_MISMATCH:{position['symbol']}")
            orders = list(await (await db.execute("SELECT * FROM paper_orders")).fetchall())
            for order in orders:
                fill_row = await (
                    await db.execute(
                        "SELECT COALESCE(SUM(quantity), 0) AS quantity "
                        "FROM paper_fills WHERE order_id = ?",
                        (str(order["order_id"]),),
                    )
                ).fetchone()
                fill_quantity = int(fill_row["quantity"]) if fill_row else 0
                if int(order["filled_quantity"]) != fill_quantity:
                    errors.append(f"ORDER_FILL_MISMATCH:{order['order_id']}")
                expected_status = (
                    "FILLED"
                    if fill_quantity == int(order["quantity"])
                    else "PARTIAL"
                    if fill_quantity > 0
                    else None
                )
                if expected_status and str(order["status"]) != expected_status:
                    errors.append(f"ORDER_STATUS_MISMATCH:{order['order_id']}")
            audit_rows = list(
                await (await db.execute("SELECT * FROM audit_events ORDER BY rowid")).fetchall()
            )
            previous_hash: str | None = None
            for row in audit_rows:
                event = {
                    "event_id": str(row["event_id"]),
                    "run_id": str(row["run_id"]),
                    "event_type": str(row["event_type"]),
                    "actor": str(row["actor"]),
                    "occurred_at": str(row["occurred_at"]),
                    "payload": json.loads(str(row["payload_json"])),
                    "prev_event_hash": previous_hash,
                }
                expected_hash = hashlib.sha256(
                    f"{previous_hash or ''}{_json(event)}".encode()
                ).hexdigest()
                if row["prev_event_hash"] != previous_hash or row["event_hash"] != expected_hash:
                    errors.append(f"AUDIT_HASH_MISMATCH:{row['event_id']}")
                previous_hash = str(row["event_hash"])
        return {
            "reconciled": not errors,
            "errors": errors,
            "checked_at": iso_now(),
            "account_count": 1 if account is not None else 0,
            "position_count": len(positions),
            "order_count": len(orders),
            "audit_event_count": len(audit_rows),
        }

    async def watchlist(self) -> list[WatchlistItem]:
        async with _connect(self._database) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute("SELECT * FROM watchlist ORDER BY updated_at DESC")
            ).fetchall()
        return [WatchlistItem(**dict(row)) for row in rows]

    async def upsert_watchlist(self, item: WatchlistCreate) -> WatchlistItem:
        timestamp = iso_now()
        async with _connect(self._database) as db:
            await db.execute(
                """
                INSERT INTO watchlist (symbol, name, stage, note, added_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, stage=excluded.stage,
                    note=excluded.note, updated_at=excluded.updated_at
                """,
                (item.symbol, item.name, item.stage, item.note, timestamp, timestamp),
            )
            await _append_audit(
                db,
                "ORDER_STATE_CHANGED",
                {"resource": "watchlist", "symbol": item.symbol, "action": "UPSERT"},
            )
            await db.commit()
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute("SELECT * FROM watchlist WHERE symbol = ?", (item.symbol,))
            ).fetchone()
        if row is None:
            raise PaperLedgerError("自选保存失败")
        return WatchlistItem(**dict(row))

    async def remove_watchlist(self, symbol: str) -> None:
        async with _connect(self._database) as db:
            result = await db.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol,))
            if result.rowcount != 1:
                raise PaperLedgerError("自选标的不存在")
            await _append_audit(
                db,
                "ORDER_STATE_CHANGED",
                {"resource": "watchlist", "symbol": symbol, "action": "DELETE"},
            )
            await db.commit()

    async def audit_events(self, limit: int = 300) -> list[AuditEvent]:
        async with _connect(self._database) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute("SELECT * FROM audit_events ORDER BY rowid DESC LIMIT ?", (limit,))
            ).fetchall()
        return [
            AuditEvent(
                event_id=str(row["event_id"]),
                run_id=str(row["run_id"]),
                event_type=str(row["event_type"]),
                actor=str(row["actor"]),
                occurred_at=str(row["occurred_at"]),
                payload=json.loads(str(row["payload_json"])),
                prev_event_hash=(str(row["prev_event_hash"]) if row["prev_event_hash"] else None),
                event_hash=str(row["event_hash"]),
            )
            for row in rows
        ]
