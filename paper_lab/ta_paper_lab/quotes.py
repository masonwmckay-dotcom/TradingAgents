"""Fetch today's completed daily OHLC for retrospective paper simulation."""

from __future__ import annotations

import csv
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .agents import after_close
from .core import iso_utc, load_bars, money, valid_ticker


def collect(data_dir: Path, symbols: list[str], day: str) -> Path:
    after_close(day)
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("TradingAgents/yfinance is required: pip install -e '.[agents]'") from exc
    symbols = list(dict.fromkeys(valid_ticker(s) for s in symbols))
    if not symbols:
        raise ValueError("collect at least one symbol")
    target = data_dir / "bars" / f"{day}.csv"
    if target.exists():
        # Historical inputs are immutable. An operator must inspect a changed
        # vendor response instead of silently updating already-recorded prices.
        raise ValueError(f"bar file already exists: {target}")
    rows = []
    for ticker in symbols:
        frame = yf.download(ticker, start=day,
                            end=(date.fromisoformat(day) + timedelta(days=1)).isoformat(),
                            auto_adjust=False, multi_level_index=False, progress=False)
        if frame.empty:
            raise ValueError(f"{ticker}: no completed session returned by yfinance")
        matching = frame[frame.index.date == date.fromisoformat(day)]
        if len(matching) != 1:
            raise ValueError(f"{ticker}: expected exactly one bar for {day}")
        sample = matching.iloc[0]
        row = {"date": day, "ticker": ticker,
               **{key: str(money(sample[key.title()])) for key in ("open", "high", "low", "close")},
               "observed_at": iso_utc(), "source": "yfinance:raw_daily_ohlc"}
        rows.append(row)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".tmp")
    try:
        with temp.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["date", "ticker", "open", "high", "low", "close", "observed_at", "source"])
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        load_bars(temp)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return target
