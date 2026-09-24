"""The scheduled entry point derives the New York session date from UTC."""

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from ta_paper_lab.scheduler import main


class SchedulerTests(unittest.TestCase):
    @patch.dict(os.environ, {"TA_PAPER_TICKERS": "SPY,QQQ", "TA_PAPER_DATA_DIR": "/data"})
    @patch("ta_paper_lab.scheduler.cli_main", return_value=0)
    @patch("ta_paper_lab.scheduler.datetime")
    def test_utc_midnight_does_not_shift_us_trading_date(self, clock, cli):
        clock.now.return_value = datetime(2026, 9, 25, 2, 0, tzinfo=timezone.utc)
        self.assertEqual(main(), 0)
        cli.assert_called_once_with(["--data-dir", "/data", "run-day", "--date", "2026-09-24", "--tickers", "SPY,QQQ"])


if __name__ == "__main__":
    unittest.main()
