"""Lazy adapter to the pinned TradingAgents v0.5.1 public interface."""

from __future__ import annotations

import copy
import os
from decimal import Decimal
from datetime import datetime, time, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .core import connect, decision_fields, iso_utc, load_bars, money, record_decision, status, valid_ticker


def after_close(day: str, now: datetime | None = None) -> None:
    clock = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    if clock.date().isoformat() != day or clock.time() < time(16, 15):
        raise ValueError("real analysis and bar collection require today's date after 16:15 New York time")
    if clock.weekday() > 4:
        raise ValueError("today is a weekend; no completed regular stock session")



def _safe_cause_chain(exc: BaseException) -> list[dict]:
    """Report exception classes and numeric OS codes without logging URLs or secrets."""
    causes = []
    seen = {id(exc)}
    current = exc.__cause__ or exc.__context__
    while current is not None and id(current) not in seen and len(causes) < 6:
        seen.add(id(current))
        detail = {"type": type(current).__name__}
        if isinstance(current, OSError) and isinstance(current.errno, int):
            detail["errno"] = current.errno
        verify_code = getattr(current, "verify_code", None)
        if isinstance(verify_code, int):
            detail["verify_code"] = verify_code
        if type(current).__name__ == "LocalProtocolError":
            reason = str(current)
            if "Illegal header value" in reason or "Invalid header value" in reason:
                detail["category"] = "invalid_http_header_value"
            elif "Illegal header name" in reason or "Invalid header name" in reason:
                detail["category"] = "invalid_http_header_name"
            elif "Content-Length" in reason:
                detail["category"] = "content_length_mismatch"
            else:
                detail["category"] = "other_local_protocol_error"
        causes.append(detail)
        current = current.__cause__ or current.__context__
    return causes



def _normalize_openai_key() -> None:
    """Remove accidental surrounding whitespace without revealing credential data."""
    key = os.environ.get("OPENAI_API_KEY", "")
    cleaned = key.strip()
    if not cleaned:
        raise RuntimeError("OPENAI_API_KEY is required for actual TradingAgents research")
    if any(char in cleaned for char in "\r\n"):
        raise RuntimeError("OPENAI_API_KEY contains an internal line break; re-enter it as one line in Railway")
    if cleaned != key:
        os.environ["OPENAI_API_KEY"] = cleaned


def _verify_analyst_prices(data_dir: Path, ticker: str, day: str, config: dict) -> None:
    """Require the vendor's raw analysis-day OHLC to equal the immutable paper bar."""
    from tradingagents.dataflows.config import run_config
    from tradingagents.dataflows.vendors.yahoo.ohlcv import load_ohlcv

    stored_day, bars, _ = load_bars(data_dir / "bars" / f"{day}.csv")
    if stored_day != day or ticker not in bars:
        raise ValueError(f"{ticker} {day}: completed paper bar missing")
    with run_config(config):
        frame = load_ohlcv(ticker, day, fill_gaps=False)
    today = frame[frame["Date"] == pd.Timestamp(day)]
    if len(today) != 1:
        raise ValueError(f"{ticker} {day}: analyst close missing")
    observed = today.iloc[0]
    reference = bars[ticker]
    for field in ("open", "high", "low", "close"):
        value = Decimal(str(observed[field.title()]))
        if not value.is_finite() or money(value) != getattr(reference, field):
            raise ValueError(f"{ticker} {day}: analyst {field} differs from saved paper bar")


def analyze(data_dir: Path, symbols: list[str], day: str) -> list[dict]:
    """Research only. Imports no Alpaca client and has no submit operation."""
    after_close(day)
    _normalize_openai_key()
    try:
        installed = version("tradingagents")
    except PackageNotFoundError as exc:
        raise RuntimeError("install the pinned TradingAgents dependency: pip install -e '.[agents]'") from exc
    if installed != "0.5.1":
        raise RuntimeError(f"TradingAgents 0.5.1 is required, found {installed}")
    # The existing Railway bot names this key ALPHAVANTAGE_API_KEY. The
    # upstream framework expects ALPHA_VANTAGE_API_KEY; accept either spelling
    # without printing or storing its value in our ledger.
    if os.environ.get("ALPHAVANTAGE_API_KEY") and not os.environ.get("ALPHA_VANTAGE_API_KEY"):
        os.environ["ALPHA_VANTAGE_API_KEY"] = os.environ["ALPHAVANTAGE_API_KEY"]
    try:
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        from tradingagents.portfolio import PortfolioContext, Position
    except ImportError as exc:
        raise RuntimeError("install the pinned TradingAgents dependency: pip install -e '.[agents]'") from exc

    symbols = list(dict.fromkeys(valid_ticker(s) for s in symbols))
    db = connect(data_dir / "paper.db")
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update({
        "results_dir": str(data_dir / "upstream_reports"),
        "data_cache_dir": str(data_dir / "upstream_cache"),
        "memory_log_path": str(data_dir / "memory" / "trading_memory.md"),
        "checkpoint_enabled": False,
        # The paper bar collector uses raw Yahoo OHLC. Use the same source and
        # refuse a missing requested close instead of silently using yesterday.
        "paper_lab_raw_daily_ohlc": True,
        "paper_lab_require_analysis_day_close": True,
    })
    if os.environ.get("ALPHA_VANTAGE_API_KEY"):
        config["data_vendors"].update({"fundamental_data": "alpha_vantage", "news_data": "alpha_vantage"})
    # No social API key is needed; keep the initial trial to the three requested
    # analysts. Preserve the upstream's other data and model defaults.
    graph = TradingAgentsGraph(selected_analysts=("market", "fundamentals", "news"), config=config)
    output = []
    for ticker in symbols:
        old = db.execute("SELECT rating,report_path FROM decisions WHERE ticker=? AND trade_date=?", (ticker, day)).fetchone()
        if old:
            output.append({"ticker": ticker, "status": "ALREADY_RECORDED", "rating": old["rating"], "report": old["report_path"]})
            continue
        previous = status(db)
        context = PortfolioContext(
            cash=float(previous["cash"]), currency="USD",
            positions=[Position(ticker=p["ticker"], quantity=p["quantity"], average_price=float(p["avg_price"]))
                       for p in previous["positions"]],
        )
        try:
            _verify_analyst_prices(data_dir, ticker, day, config)
            state, signal = graph.propagate(ticker, day, portfolio=context)
            if any(not str(state.get(field) or "").strip()
                   for field in ("market_report", "fundamentals_report", "news_report")):
                # A directional final rating cannot turn missing analyst
                # evidence into an actionable simulated trade.
                signal = "REVIEW"
            report_dir = graph.save_reports(state, ticker, data_dir / "upstream_reports" / day / ticker)
            report_path = str(report_dir)
            saved = record_decision(db, ticker, day, iso_utc(), state, signal, report_path)
            rating, action, stop = decision_fields(state, signal)
            output.append({"ticker": ticker, "status": "RECORDED" if saved else "ALREADY_RECORDED",
                           "rating": rating, "trader_action": action,
                           "stop_loss": str(stop) if stop is not None else None,
                           "report": report_path})
        except Exception as exc:
            # A failed LLM/data call never produces a decision or a simulated fill.
            output.append({"ticker": ticker, "status": "FAILED", "error": f"{type(exc).__name__}: {exc}",
                           "causes": _safe_cause_chain(exc)})
    db.close()
    return output
