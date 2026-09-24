"""CLI entry points for isolated research and retrospective paper simulation."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

from .core import connect, load_bars, paper_step, record_decision, status, valid_ticker


def _tickers(value: str) -> list[str]:
    symbols = list(dict.fromkeys(valid_ticker(s) for s in value.split(",")))
    if not symbols:
        raise ValueError("specify at least one ticker")
    return symbols


def _simulate(data_dir: Path, csv_path: Path) -> dict:
    day, bars, digest = load_bars(csv_path)
    db = connect(data_dir / "paper.db")
    try:
        return paper_step(db, day, bars, digest)
    finally:
        db.close()


def demo(data_dir: Path) -> dict:
    """Clearly labelled fixture, using no market/LLM data and no broker."""
    data_dir.mkdir(parents=True, exist_ok=True)
    db = connect(data_dir / "paper.db")
    try:
        record_decision(db, "SPY", "2026-09-22", "2026-09-22T21:00:00Z", {
            "final_trade_decision": "**Rating**: Buy\n\n**Investment Thesis**: Synthetic demo fixture.",
            "trader_investment_plan": "**Action**: Buy\n\n**Stop Loss**: 98.00",
            "market_report": "Synthetic demo only", "fundamentals_report": "Synthetic demo only",
            "news_report": "Synthetic demo only",
        }, "Buy", "SYNTHETIC_DEMO_NO_REAL_REPORT")
    finally:
        db.close()
    sample = data_dir / "synthetic_bars.csv"
    with sample.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["date", "ticker", "open", "high", "low", "close", "observed_at", "source"])
        writer.writeheader()
        writer.writerow({"date": "2026-09-23", "ticker": "SPY", "open": "100.00", "high": "102.00", "low": "99.00", "close": "101.00", "observed_at": "2026-09-23T21:00:00Z", "source": "SYNTHETIC_DEMO"})
    return _simulate(data_dir, sample)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradingAgents advisory research + isolated paper simulation")
    parser.add_argument("--data-dir", type=Path, default=Path(os.getenv("TA_PAPER_DATA_DIR", "./data")))
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("analyze", help="run real agents after the US market close")
    a.add_argument("--date", required=True, help="current New York trading date YYYY-MM-DD")
    a.add_argument("--tickers", required=True, help="comma-separated US tickers")
    b = sub.add_parser("collect-bars", help="download today's raw daily OHLC after 16:15 ET")
    b.add_argument("--date", required=True)
    b.add_argument("--tickers", required=True)
    s = sub.add_parser("paper-step", help="simulate next-session fills using completed CSV bars")
    s.add_argument("--bars", required=True, type=Path)
    d = sub.add_parser("run-day", help="collect bars, settle yesterday's decisions, then analyze today's tickers")
    d.add_argument("--date", required=True)
    d.add_argument("--tickers", required=True)
    sub.add_parser("status", help="show immutable decisions, positions and paper fills")
    sub.add_parser("demo", help="run a clearly synthetic, no-API example in an empty directory")
    args = parser.parse_args(argv)
    try:
        if args.command == "analyze":
            from .agents import analyze
            result = analyze(args.data_dir, _tickers(args.tickers), args.date)
        elif args.command == "collect-bars":
            from .quotes import collect
            result = {"bars": str(collect(args.data_dir, _tickers(args.tickers), args.date))}
        elif args.command == "paper-step":
            result = _simulate(args.data_dir, args.bars)
        elif args.command == "run-day":
            from .agents import after_close, analyze
            from .quotes import collect
            after_close(args.date)
            if not os.environ.get("OPENAI_API_KEY"):
                raise RuntimeError("OPENAI_API_KEY is required for actual TradingAgents research")
            universe = _tickers(args.tickers)
            db = connect(args.data_dir / "paper.db")
            try:
                tracked = {row[0] for row in db.execute("SELECT ticker FROM positions")}
                tracked |= {row[0] for row in db.execute("SELECT ticker FROM decisions WHERE status='PENDING'")}
            finally:
                db.close()
            bar_file = args.data_dir / "bars" / f"{args.date}.csv"
            if not bar_file.exists():
                bar_file = collect(args.data_dir, sorted(set(universe) | tracked), args.date)
            simulation = _simulate(args.data_dir, bar_file)
            research = analyze(args.data_dir, universe, args.date)
            result = {"bars": str(bar_file), "paper": simulation, "research": research}
        elif args.command == "demo":
            if (args.data_dir / "paper.db").exists():
                raise ValueError("demo requires a new data directory so its fake prices never mix with real research")
            result = {"mode": "SYNTHETIC_DEMO", **demo(args.data_dir)}
        else:
            db = connect(args.data_dir / "paper.db")
            try:
                result = status(db)
            finally:
                db.close()
        print(json.dumps(result, indent=2, default=str))
        if args.command in ("analyze", "run-day"):
            research = result if args.command == "analyze" else result["research"]
            return 2 if any(r["status"] == "FAILED" for r in research) else 0
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
