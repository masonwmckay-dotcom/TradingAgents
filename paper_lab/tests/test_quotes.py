"""Verify the real-data adapter's completed-day row and provenance format."""

import importlib.util
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from ta_paper_lab.core import load_bars


@unittest.skipUnless(importlib.util.find_spec("yfinance"), "install pinned TradingAgents dependency")
class QuoteCollectionTests(unittest.TestCase):
    def test_yfinance_row_is_auditable_and_refuses_overwrite(self):
        import pandas as pd

        from ta_paper_lab.quotes import collect

        frame = pd.DataFrame({"Open": [100.0], "High": [102.0], "Low": [99.0], "Close": [101.0]},
                             index=pd.to_datetime(["2026-09-23"]))
        with tempfile.TemporaryDirectory() as folder:
            with patch("ta_paper_lab.quotes.after_close"), \
                 patch("ta_paper_lab.quotes.iso_utc", return_value="2026-09-23T21:00:00Z"), \
                 patch("ta_paper_lab.quotes.load_bars"), \
                 patch("yfinance.download", return_value=frame) as download:
                path = collect(Path(folder), ["SPY"], "2026-09-23")
                self.assertTrue(path.exists())
                download.assert_called_once()
                with self.assertRaisesRegex(ValueError, "already exists"):
                    collect(Path(folder), ["SPY"], "2026-09-23")
            day, bars, _ = load_bars(path, now=datetime(2026, 9, 24, tzinfo=timezone.utc))
            self.assertEqual(day, "2026-09-23")
            self.assertEqual(bars["SPY"].source, "yfinance:raw_daily_ohlc")
            self.assertEqual(str(bars["SPY"].close), "101.00")


if __name__ == "__main__":
    unittest.main()
