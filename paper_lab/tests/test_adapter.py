"""Actual TradingAgents import and API adapter, with external calls stubbed."""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ta_paper_lab.agents import analyze
from ta_paper_lab.core import connect, status


@unittest.skipUnless(importlib.util.find_spec("tradingagents"), "install pinned TradingAgents dependency")
class GraphContractTests(unittest.TestCase):
    @patch.dict(os.environ, {"OPENAI_API_KEY": "fake-test-value", "ALPHAVANTAGE_API_KEY": "fake-test-value"})
    @patch("ta_paper_lab.agents.after_close")
    def test_three_agents_vendor_alias_and_saved_report(self, _clock):
        class FakeGraph:
            def __init__(self, selected_analysts, config):
                self.analysts = selected_analysts
                self.config = config

            def propagate(self, ticker, day, portfolio):
                self_test.assertEqual(self.analysts, ("market", "fundamentals", "news"))
                self_test.assertEqual(self.config["data_vendors"]["news_data"], "alpha_vantage")
                self_test.assertEqual(portfolio.cash, 5000)
                return ({"market_report": "Market fixture", "fundamentals_report": "Fundamentals fixture",
                         "news_report": "News fixture", "final_trade_decision": "**Rating**: Buy",
                         "trader_investment_plan": "**Action**: Buy\n**Stop Loss**: 98"}, "Buy")

            def save_reports(self, state, ticker, path):
                path.mkdir(parents=True)
                (path / "complete_report.md").write_text(state["market_report"])
                return path / "complete_report.md"

        self_test = self
        with tempfile.TemporaryDirectory() as folder:
            with patch("tradingagents.graph.trading_graph.TradingAgentsGraph", FakeGraph), \
                 patch("ta_paper_lab.agents.iso_utc", return_value="2026-09-24T21:00:00Z"):
                rows = analyze(Path(folder), ["SPY"], "2026-09-24")
            self.assertEqual(rows[0]["status"], "RECORDED")
            self.assertEqual(rows[0]["rating"], "Buy")
            self.assertTrue(Path(rows[0]["report"]).is_file())
            self.assertTrue((Path(folder) / "signal_feed" / "2026-09-24.json").exists())
            db = connect(Path(folder) / "paper.db")
            try:
                self.assertEqual(status(db)["decisions"][0]["status"], "PENDING")
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
