"""Lazy adapter to the pinned TradingAgents v0.5.1 public interface."""

from __future__ import annotations

import copy
import os
from datetime import datetime, time, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from zoneinfo import ZoneInfo

from .core import connect, decision_fields, iso_utc, record_decision, status, valid_ticker


def after_close(day: str, now: datetime | None = None) -> None:
    clock = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    if clock.date().isoformat() != day or clock.time() < time(16, 15):
        raise ValueError("real analysis and bar collection require today's date after 16:15 New York time")
    if clock.weekday() > 4:
        raise ValueError("today is a weekend; no completed regular stock session")


def analyze(data_dir: Path, symbols: list[str], day: str) -> list[dict]:
    """Research only. Imports no Alpaca client and has no submit operation."""
    after_close(day)
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required for actual TradingAgents research")
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
            output.append({"ticker": ticker, "status": "FAILED", "error": f"{type(exc).__name__}: {exc}"})
    db.close()
    if output and all(item["status"] != "FAILED" for item in output):
        from .signal_feed import export_feed
        from .signal_bucket import publish_if_configured
        publish_if_configured(export_feed(data_dir, day, symbols))
    return output
