"""Audit ledger and conservative daily-bar paper simulator.

The only writes are to a local SQLite database. There is no brokerage code.
All buy entries are next-session open fills against externally supplied, completed
OHLC bars. These are retrospective simulated fills, not executable broker orders.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
CENT = Decimal("0.01")
START_CASH = Decimal("5000.00")
MAX_STOP_RISK = Decimal("25.00")
MAX_POSITION_VALUE = Decimal("1150.00")
MAX_POSITIONS = 3
SLIPPAGE = Decimal("0.0005")  # five basis points each side
MAX_SIGNAL_AGE_DAYS = 7
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,11}$")
ACTION_RE = re.compile(r"^\*\*Action\*\*:\s*(Buy|Hold|Sell)\s*$", re.I | re.M)
STOP_RE = re.compile(r"^\*\*Stop Loss\*\*:\s*([0-9]+(?:\.[0-9]+)?)\s*$", re.I | re.M)
RATING_RE = re.compile(r"^\*\*Rating\*\*:\s*(Buy|Overweight|Hold|Underweight|Sell)\s*$", re.I | re.M)


def money(value: Decimal | str | float) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def valid_ticker(value: str) -> str:
    symbol = value.strip().upper()
    if not TICKER_RE.fullmatch(symbol):
        raise ValueError(f"unsupported ticker: {value!r}")
    return symbol


def utc_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid UTC timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError("observed_at must have an explicit UTC offset")
    return parsed.astimezone(timezone.utc)


def iso_utc(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def decision_fields(final_state: dict, signal: str) -> tuple[str, str, Decimal | None]:
    """Require upstream's labelled final rating, not a stray word in rationale."""
    final = str(final_state.get("final_trade_decision") or "")
    trader = str(final_state.get("trader_investment_plan") or "")
    rating_match = RATING_RE.search(final)
    rating = rating_match.group(1).capitalize() if rating_match else "REVIEW"
    if signal != rating:
        rating = "REVIEW"
    action_match = ACTION_RE.search(trader)
    action = action_match.group(1).capitalize() if action_match else "REVIEW"
    stop_match = STOP_RE.search(trader)
    stop = money(stop_match.group(1)) if stop_match else None
    return rating, action, stop


@dataclass(frozen=True)
class Bar:
    day: str
    ticker: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    observed_at: str
    source: str


def load_bars(path: Path, now: datetime | None = None) -> tuple[str, dict[str, Bar], str]:
    """Require honest completed bars; an incomplete/malformed file changes nothing."""
    current = now or datetime.now(timezone.utc)
    raw = path.read_bytes()
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        expected = {"date", "ticker", "open", "high", "low", "close", "observed_at", "source"}
        if not reader.fieldnames or set(reader.fieldnames) != expected:
            raise ValueError(f"bars CSV columns must be exactly {', '.join(sorted(expected))}")
        rows = list(reader)
    if not rows:
        raise ValueError("bars CSV is empty")
    bars = {}
    for row in rows:
        if None in row or any(value is None for value in row.values()):
            raise ValueError("malformed bars CSV row")
        day = date.fromisoformat(row["date"])
        if day.weekday() > 4:
            raise ValueError(f"{day}: US stock bar cannot be dated on a weekend")
        ticker = valid_ticker(row["ticker"])
        if (day.isoformat(), ticker) in bars:
            raise ValueError(f"duplicate bar: {ticker} {day}")
        observed = utc_time(row["observed_at"])
        close_cutoff = datetime.combine(day, time(16, 15), ET).astimezone(timezone.utc)
        if observed < close_cutoff or observed > current:
            raise ValueError(f"{ticker} {day}: bar must be observed after 16:15 ET and no later than now")
        prices = {}
        for key in ("open", "high", "low", "close"):
            try:
                price = Decimal(row[key])
            except Exception as exc:
                raise ValueError(f"{ticker}: invalid {key}") from exc
            if not price.is_finite() or price <= 0:
                raise ValueError(f"{ticker}: {key} must be finite and positive")
            prices[key] = money(price)
        if prices["low"] > min(prices["open"], prices["close"]) or prices["high"] < max(prices["open"], prices["close"]) or prices["low"] > prices["high"]:
            raise ValueError(f"{ticker}: inconsistent OHLC values")
        if not row["source"].strip():
            raise ValueError(f"{ticker}: source is required")
        bars[(day.isoformat(), ticker)] = Bar(day.isoformat(), ticker, **prices, observed_at=iso_utc(observed), source=row["source"].strip())
    days = {day for day, _ in bars}
    if len(days) != 1:
        raise ValueError("one simulation step accepts exactly one market date")
    return days.pop(), {symbol: bar for (_, symbol), bar in bars.items()}, hashlib.sha256(raw).hexdigest()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS decisions (
            ticker TEXT NOT NULL, trade_date TEXT NOT NULL, created_at TEXT NOT NULL,
            rating TEXT NOT NULL, action TEXT NOT NULL, stop_loss TEXT,
            report_path TEXT NOT NULL, payload_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING', reason TEXT,
            PRIMARY KEY (ticker, trade_date)
        );
        CREATE TABLE IF NOT EXISTS positions (
            ticker TEXT PRIMARY KEY, quantity INTEGER NOT NULL CHECK(quantity > 0),
            avg_price TEXT NOT NULL, stop_loss TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, ticker TEXT NOT NULL,
            side TEXT NOT NULL, quantity INTEGER NOT NULL, price TEXT NOT NULL,
            reference_open TEXT NOT NULL, reason TEXT NOT NULL, source TEXT NOT NULL,
            decision_date TEXT
        );
        CREATE TABLE IF NOT EXISTS days (
            day TEXT PRIMARY KEY, csv_sha256 TEXT NOT NULL, cash TEXT NOT NULL,
            equity TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    db.execute("INSERT OR IGNORE INTO config(key,value) VALUES('cash',?)", (str(START_CASH),))
    db.commit()
    return db


def record_decision(db: sqlite3.Connection, ticker: str, trade_date: str, created_at: str,
                    state: dict, signal: str, report_path: str) -> bool:
    ticker = valid_ticker(ticker)
    date.fromisoformat(trade_date)
    created = utc_time(created_at)
    if created.date().isoformat() < trade_date:
        raise ValueError("a decision cannot be recorded before its analysis date")
    rating, action, stop = decision_fields(state, signal)
    payload = json.dumps({"state": state, "signal": signal}, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    with db:
        existing = db.execute("SELECT payload_hash FROM decisions WHERE ticker=? AND trade_date=?", (ticker, trade_date)).fetchone()
        if existing:
            if existing["payload_hash"] != digest:
                raise ValueError("decision for this ticker/date is immutable; a different rerun is refused")
            return False
        db.execute("""INSERT INTO decisions(ticker,trade_date,created_at,rating,action,stop_loss,report_path,payload_hash)
                      VALUES(?,?,?,?,?,?,?,?)""",
                   (ticker, trade_date, iso_utc(created), rating, action, str(stop) if stop else None, report_path, digest))
    return True


def _cash(db: sqlite3.Connection) -> Decimal:
    return Decimal(db.execute("SELECT value FROM config WHERE key='cash'").fetchone()[0])


def _set_cash(db: sqlite3.Connection, value: Decimal) -> None:
    db.execute("UPDATE config SET value=? WHERE key='cash'", (str(money(value)),))


def _fill(db: sqlite3.Connection, bar: Bar, side: str, quantity: int, price: Decimal,
          reference: Decimal, reason: str, decision_date: str | None) -> None:
    db.execute("""INSERT INTO fills(day,ticker,side,quantity,price,reference_open,reason,source,decision_date)
                  VALUES(?,?,?,?,?,?,?,?,?)""",
               (bar.day, bar.ticker, side, quantity, str(price), str(reference), reason, bar.source, decision_date))


def _exit(db: sqlite3.Connection, bar: Bar, pos: sqlite3.Row, quantity: int, reference: Decimal,
          reason: str, decision_date: str | None = None) -> None:
    price = money(reference * (1 - SLIPPAGE))
    cash = _cash(db) + money(price * quantity)
    _set_cash(db, cash)
    left = pos["quantity"] - quantity
    if left:
        db.execute("UPDATE positions SET quantity=? WHERE ticker=?", (left, bar.ticker))
    else:
        db.execute("DELETE FROM positions WHERE ticker=?", (bar.ticker,))
    _fill(db, bar, "SELL", quantity, price, reference, reason, decision_date)


def paper_step(db: sqlite3.Connection, day: str, bars: dict[str, Bar], digest: str,
               now: datetime | None = None) -> dict:
    """Commit one completed session atomically; refuse missing holdings prices."""
    today = date.fromisoformat(day)
    current = now or datetime.now(timezone.utc)
    if any(bar.day != day or bar.ticker != ticker for ticker, bar in bars.items()):
        raise ValueError("bar map does not match the simulation date")
    with db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT csv_sha256,cash,equity FROM days WHERE day=?", (day,)).fetchone()
        if existing:
            if existing["csv_sha256"] != digest:
                raise ValueError("session already finalized using a different bars file")
            return {"day": day, "cash": existing["cash"], "equity": existing["equity"], "idempotent": True}
        latest = db.execute("SELECT MAX(day) FROM days").fetchone()[0]
        if latest and day <= latest:
            raise ValueError("paper sessions must be processed in increasing date order")
        positions = {r["ticker"]: r for r in db.execute("SELECT * FROM positions")}
        pending = db.execute("SELECT * FROM decisions WHERE status='PENDING' AND trade_date<? ORDER BY trade_date,ticker", (day,)).fetchall()
        session_open = datetime.combine(today, time(9, 30), ET).astimezone(timezone.utc)
        # Only the latest decision for a ticker may act. Older decisions remain
        # visible with a SUPERSEDED audit status; they cannot queue up buys.
        latest_by_ticker = {r["ticker"]: r["trade_date"] for r in pending}
        latest = [r for r in pending if latest_by_ticker[r["ticker"]] == r["trade_date"]]
        active = [r for r in latest if (today - date.fromisoformat(r["trade_date"])).days <= MAX_SIGNAL_AGE_DAYS
                  and utc_time(r["created_at"]) < session_open]
        actionable = [r for r in active if r["rating"] in ("Buy", "Overweight", "Underweight", "Sell")]
        required = set(positions) | {r["ticker"] for r in actionable}
        missing = sorted(required - set(bars))
        if missing:
            raise ValueError(f"missing completed bar(s) for: {', '.join(missing)}")
        for ticker in positions:
            if bars[ticker].observed_at and utc_time(bars[ticker].observed_at) > current:
                raise ValueError(f"{ticker}: future bar")
        for decision in active:
            bar = bars.get(decision["ticker"])
            if bar and utc_time(bar.observed_at) < utc_time(decision["created_at"]):
                raise ValueError(f"{decision['ticker']}: bar was observed before the decision")

        # Sell/trim at the next open first; remaining existing positions get a
        # stop check using the session low. Gap-down stops execute at the worse
        # opening price, with slippage (a stop does not guarantee the $25 cap).
        consumed = set()
        for d in active:
            ticker = d["ticker"]
            if ticker in consumed or ticker not in positions or d["rating"] not in ("Sell", "Underweight"):
                continue
            bar = bars[ticker]
            pos = db.execute("SELECT * FROM positions WHERE ticker=?", (ticker,)).fetchone()
            if not pos:
                continue
            if d["action"] != "Sell":
                continue
            qty = pos["quantity"] if d["rating"] == "Sell" else max(1, (pos["quantity"] + 1) // 2)
            _exit(db, bar, pos, qty, bar.open, d["rating"].upper(), d["trade_date"])
            consumed.add(ticker)
        for ticker in positions:
            pos = db.execute("SELECT * FROM positions WHERE ticker=?", (ticker,)).fetchone()
            if pos and bars[ticker].low <= Decimal(pos["stop_loss"]):
                bar = bars[ticker]
                stop = Decimal(pos["stop_loss"])
                _exit(db, bar, pos, pos["quantity"], min(bar.open, stop), "STOP")

        statuses = []
        for d in pending:
            ticker = d["ticker"]
            age = (today - date.fromisoformat(d["trade_date"])).days
            rating, action = d["rating"], d["action"]
            reason = "NO_ACTION"
            if latest_by_ticker[ticker] != d["trade_date"]:
                reason = "SUPERSEDED"
            elif age > MAX_SIGNAL_AGE_DAYS:
                reason = "EXPIRED"
            elif utc_time(d["created_at"]) >= session_open:
                reason = "AFTER_SESSION_OPEN"
            elif ticker in consumed and rating in ("Sell", "Underweight"):
                reason = "EXITED" if rating == "Sell" else "REDUCED"
            elif rating == "REVIEW" or action == "REVIEW":
                reason = "UNPARSEABLE"
            elif rating in ("Buy", "Overweight"):
                if action != "Buy":
                    reason = "TRADER_DISAGREES"
                elif ticker in positions or db.execute("SELECT 1 FROM positions WHERE ticker=?", (ticker,)).fetchone():
                    reason = "POSITION_EXISTS"
                elif db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] >= MAX_POSITIONS:
                    reason = "POSITION_LIMIT"
                elif d["stop_loss"] is None:
                    reason = "MISSING_STOP"
                else:
                    bar = bars[ticker]
                    entry = money(bar.open * (1 + SLIPPAGE))
                    stop = Decimal(d["stop_loss"])
                    if stop <= 0 or stop >= entry:
                        reason = "INVALID_STOP"
                    else:
                        budget = min(MAX_POSITION_VALUE, _cash(db))
                        stopped_price = money(stop * (1 - SLIPPAGE))
                        qty_risk = (MAX_STOP_RISK / (entry - stopped_price)).to_integral_value(rounding=ROUND_DOWN)
                        qty_budget = (budget / entry).to_integral_value(rounding=ROUND_DOWN)
                        qty = int(min(qty_risk, qty_budget))
                        if qty < 1:
                            reason = "INSUFFICIENT_RISK_OR_CASH"
                        else:
                            _set_cash(db, _cash(db) - money(entry * qty))
                            db.execute("INSERT INTO positions(ticker,quantity,avg_price,stop_loss) VALUES(?,?,?,?)",
                                       (ticker, qty, str(entry), str(stop)))
                            _fill(db, bar, "BUY", qty, entry, bar.open, rating.upper(), d["trade_date"])
                            reason = "BOUGHT"
                            # The intraday low may touch the stop after the open.
                            # Do not use the close to decide whether a stop hit.
                            if bar.low <= stop:
                                pos = db.execute("SELECT * FROM positions WHERE ticker=?", (ticker,)).fetchone()
                                _exit(db, bar, pos, qty, min(bar.open, stop), "STOP_ON_ENTRY", d["trade_date"])
            elif rating in ("Sell", "Underweight"):
                reason = "TRADER_DISAGREES" if action != "Sell" else "NO_POSITION"
            db.execute("UPDATE decisions SET status='CONSUMED',reason=? WHERE ticker=? AND trade_date=?",
                       (reason, ticker, d["trade_date"]))
            statuses.append({"ticker": ticker, "decision_date": d["trade_date"], "result": reason})

        equity = _cash(db)
        for p in db.execute("SELECT ticker,quantity FROM positions"):
            equity += money(bars[p["ticker"]].close * p["quantity"])
        equity = money(equity)
        db.execute("INSERT INTO days(day,csv_sha256,cash,equity,created_at) VALUES(?,?,?,?,?)",
                   (day, digest, str(_cash(db)), str(equity), iso_utc(current)))
        return {"day": day, "cash": str(_cash(db)), "equity": str(equity), "decisions": statuses,
                "positions": [dict(r) for r in db.execute("SELECT * FROM positions ORDER BY ticker")],
                "fills": [dict(r) for r in db.execute("SELECT * FROM fills WHERE day=? ORDER BY id", (day,))]}


def status(db: sqlite3.Connection) -> dict:
    return {
        "cash": str(_cash(db)),
        "positions": [dict(r) for r in db.execute("SELECT * FROM positions ORDER BY ticker")],
        "sessions": [dict(r) for r in db.execute("SELECT day,cash,equity FROM days ORDER BY day")],
        "decisions": [dict(r) for r in db.execute("SELECT ticker,trade_date,rating,action,stop_loss,status,reason,report_path FROM decisions ORDER BY trade_date,ticker")],
        "fills": [dict(r) for r in db.execute("SELECT day,ticker,side,quantity,price,reason FROM fills ORDER BY id")],
    }
