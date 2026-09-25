"""Raw paper-lab prices must not inherit the adjusted Yahoo cache or yesterday's close."""
from pathlib import Path

import pandas as pd
import pytest

from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.vendors.yahoo import market, ohlcv


def frame(today_close=101.0):
    return pd.DataFrame(
        {"Open": [99.0, 100.0], "High": [100.0, 102.0],
         "Low": [98.0, 99.0], "Close": [99.5, today_close],
         "Volume": [100, 110]},
        index=pd.DatetimeIndex(["2026-09-23", "2026-09-24"], name="Date"),
    )


def test_raw_lab_refetches_incomplete_cache_and_uses_distinct_cache(monkeypatch, tmp_path):
    config = {"data_cache_dir": str(tmp_path), "paper_lab_raw_daily_ohlc": True,
              "paper_lab_require_analysis_day_close": True}
    # Pretend this cache was written today and is still within its normal TTL.
    now = pd.Timestamp("2026-09-24 19:00")
    monkeypatch.setattr(ohlcv.pd.Timestamp, "today", staticmethod(lambda: now))
    raw_cache = tmp_path / "SPY-YFin-raw-data.csv"
    frame(float("nan")).reset_index().to_csv(raw_cache, index=False)
    import os
    os.utime(raw_cache, (now.timestamp(), now.timestamp()))
    seen = []
    def download(symbol, **kwargs):
        seen.append(kwargs)
        return frame()
    monkeypatch.setattr(ohlcv.yf, "download", download)
    with run_config(config):
        result = ohlcv.load_ohlcv("SPY", "2026-09-24", fill_gaps=False)
        summary = market.get_YFin_data_online("SPY", "2026-09-23", "2026-09-24")
    assert len(seen) == 1 and seen[0]["auto_adjust"] is False
    assert result.iloc[-1]["Close"] == 101.0
    assert "2026-09-24,100.0,102.0,99.0,101.0" in summary
    assert not (tmp_path / "SPY-YFin-data.csv").exists()


def test_missing_raw_analysis_close_fails_instead_of_using_prior_day(monkeypatch, tmp_path):
    monkeypatch.setattr(ohlcv.yf, "download", lambda *a, **kw: frame(float("nan")))
    with run_config({"data_cache_dir": str(tmp_path), "paper_lab_raw_daily_ohlc": True,
                     "paper_lab_require_analysis_day_close": True}):
        with pytest.raises(NoMarketDataError, match="verified closing price unavailable"):
            ohlcv.load_ohlcv("SPY", "2026-09-24")
