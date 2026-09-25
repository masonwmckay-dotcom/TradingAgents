"""Actual TradingAgents import and API adapter, with external calls stubbed."""

import importlib.util
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from ta_paper_lab.agents import _verify_analyst_prices, analyze
from ta_paper_lab.core import connect, status


class ConnectionDiagnosticsTests(unittest.TestCase):
    def test_analyst_raw_bar_must_match_saved_ohlc(self):
        expected = SimpleNamespace(open=Decimal("99.00"), high=Decimal("102.00"),
                                   low=Decimal("98.00"), close=Decimal("101.00"))
        prices = pd.DataFrame([{"Date":pd.Timestamp("2026-09-24"), "Open":99.0,
                                "High":102.0, "Low":98.0, "Close":101.0}])
        with patch("ta_paper_lab.agents.load_bars", return_value=("2026-09-24", {"SPY":expected}, "hash")), \
             patch("tradingagents.dataflows.vendors.yahoo.ohlcv.load_ohlcv", return_value=prices):
            _verify_analyst_prices(Path("/unused"), "SPY", "2026-09-24", {})
            bad = prices.assign(Close=100.0)
            with patch("tradingagents.dataflows.vendors.yahoo.ohlcv.load_ohlcv", return_value=bad):
                with self.assertRaisesRegex(ValueError, "analyst close differs"):
                    _verify_analyst_prices(Path("/unused"), "SPY", "2026-09-24", {})

    def test_cause_chain_omits_exception_messages(self):
        from ta_paper_lab.agents import _safe_cause_chain

        try:
            try:
                raise OSError(101, "secret-value-must-not-appear")
            except OSError as root:
                raise ConnectionError("another-secret") from root
        except ConnectionError as outer:
            chain = _safe_cause_chain(outer)
        self.assertEqual(chain, [{"type": "OSError", "errno": 101}])
        self.assertNotIn("secret", str(chain))


    def test_key_normalization_and_internal_line_break(self):
        from ta_paper_lab.agents import _normalize_openai_key

        with patch.dict(os.environ, {"OPENAI_API_KEY": "  fake-test-key\n"}):
            _normalize_openai_key()
            self.assertEqual(os.environ["OPENAI_API_KEY"], "fake-test-key")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake\nother"}):
            with self.assertRaisesRegex(RuntimeError, "internal line break"):
                _normalize_openai_key()


    def test_invalid_header_category_redacts_value(self):
        from ta_paper_lab.agents import _safe_cause_chain

        class LocalProtocolError(Exception):
            pass

        try:
            try:
                raise LocalProtocolError("Illegal header value b'Bearer secret-value'")
            except LocalProtocolError as root:
                raise ConnectionError("connection failed") from root
        except ConnectionError as outer:
            chain = _safe_cause_chain(outer)
        self.assertEqual(chain, [{"type": "LocalProtocolError",
                                  "category": "invalid_http_header_value"}])
        self.assertNotIn("secret-value", str(chain))


@unittest.skipUnless(importlib.util.find_spec("tradingagents"), "install pinned TradingAgents dependency")
class GraphContractTests(unittest.TestCase):
    @patch.dict(os.environ, {"OPENAI_API_KEY": "fake-test-value", "ALPHAVANTAGE_API_KEY": "fake-test-value"})
    @patch("ta_paper_lab.agents.after_close")
    @patch("ta_paper_lab.agents._verify_analyst_prices")
    def test_three_agents_vendor_alias_and_saved_report(self, _verify, _clock):
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
                (path / "summary.md").write_text(state["market_report"])
                return path

        self_test = self
        with tempfile.TemporaryDirectory() as folder:
            with patch("tradingagents.graph.trading_graph.TradingAgentsGraph", FakeGraph):
                rows = analyze(Path(folder), ["SPY"], "2026-09-24")
            self.assertEqual(rows[0]["status"], "RECORDED")
            self.assertEqual(rows[0]["rating"], "Buy")
            self.assertTrue((Path(rows[0]["report"]) / "summary.md").exists())
            db = connect(Path(folder) / "paper.db")
            try:
                self.assertEqual(status(db)["decisions"][0]["status"], "PENDING")
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
