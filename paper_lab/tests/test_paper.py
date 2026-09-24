"""Risk, timing, audit and replay checks using synthetic inputs only."""

import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ta_paper_lab.agents import after_close
from ta_paper_lab.core import Bar, connect, load_bars, paper_step, record_decision, status


def decision(db, ticker="SPY", day="2026-09-21", rating="Buy", action="Buy", stop="98.00"):
    return record_decision(db, ticker, day, day + "T21:00:00Z", {
        "final_trade_decision": f"**Rating**: {rating}\n**Investment Thesis**: Fixture.",
        "trader_investment_plan": f"**Action**: {action}\n**Stop Loss**: {stop}",
    }, rating, "SYNTHETIC_TEST")


def bar(ticker="SPY", day="2026-09-22", open="100", high="101", low="99", close="100"):
    return Bar(day, ticker, *map(__import__("decimal").Decimal, (open, high, low, close)),
               day + "T21:00:00+00:00", "SYNTHETIC_TEST")


class PaperLedgerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.folder.name) / "paper.db")

    def tearDown(self):
        self.db.close()
        self.folder.cleanup()

    def step(self, day, bars, digest="example"):
        return paper_step(self.db, day, {b.ticker: b for b in bars}, digest,
                          now=datetime(2026, 9, 25, tzinfo=timezone.utc))

    def test_demo_trade_sizing_and_replay(self):
        decision(self.db)
        first = self.step("2026-09-22", [bar()])
        self.assertEqual(first["cash"], "3899.45")
        self.assertEqual(first["positions"][0]["quantity"], 11)
        self.assertEqual(first["fills"][0]["price"], "100.05")
        self.assertTrue(self.step("2026-09-22", [bar()])["idempotent"])
        with self.assertRaisesRegex(ValueError, "already finalized"):
            self.step("2026-09-22", [bar()], digest="changed")
        self.assertEqual(len(status(self.db)["fills"]), 1)

    def test_same_day_decision_waits_until_next_open(self):
        decision(self.db, day="2026-09-22")
        first = self.step("2026-09-22", [bar()])
        self.assertEqual(first["fills"], [])
        self.assertEqual(status(self.db)["decisions"][0]["status"], "PENDING")
        later = self.step("2026-09-23", [bar(day="2026-09-23")], digest="next")
        self.assertEqual(later["fills"][0]["side"], "BUY")

    def test_gap_below_stop_can_exceed_risk_cap(self):
        decision(self.db)
        self.step("2026-09-22", [bar()])
        result = self.step("2026-09-23", [bar(day="2026-09-23", open="90", high="92", low="89", close="91")])
        self.assertEqual(result["positions"], [])
        self.assertEqual(result["fills"][0]["reason"], "STOP")
        self.assertEqual(result["fills"][0]["reference_open"], "90")
        self.assertLess(float(result["equity"]), 4975)

    def test_stop_on_existing_position_cannot_reenter_at_past_open(self):
        decision(self.db)
        self.step("2026-09-22", [bar()])
        decision(self.db, day="2026-09-22", stop="96")
        result = self.step("2026-09-23", [bar(day="2026-09-23", open="100", high="102", low="97", close="101")])
        self.assertEqual([f["side"] for f in result["fills"]], ["SELL"])
        self.assertEqual(result["decisions"][0]["result"], "POSITION_EXISTS")

    def test_missing_bar_refuses_entire_session(self):
        decision(self.db, ticker="SPY")
        decision(self.db, ticker="QQQ")
        with self.assertRaisesRegex(ValueError, "QQQ"):
            self.step("2026-09-22", [bar()])
        self.assertEqual(status(self.db)["cash"], "5000.00")
        self.assertEqual(status(self.db)["fills"], [])
        self.assertTrue(all(d["status"] == "PENDING" for d in status(self.db)["decisions"]))

    def test_conflicting_and_unparseable_signals_cannot_buy(self):
        decision(self.db, ticker="SPY", action="Hold")
        record_decision(self.db, "QQQ", "2026-09-21", "2026-09-21T21:00:00Z",
                        {"final_trade_decision": "Rating unclear", "trader_investment_plan": "**Action**: Buy"},
                        "REVIEW", "SYNTHETIC_TEST")
        result = self.step("2026-09-22", [bar("SPY")])
        self.assertEqual(result["fills"], [])
        self.assertEqual([d["result"] for d in result["decisions"]], ["UNPARSEABLE", "TRADER_DISAGREES"])

    def test_only_newest_decision_acts(self):
        decision(self.db, day="2026-09-21", stop="98")
        decision(self.db, day="2026-09-22", rating="Hold", action="Hold")
        result = self.step("2026-09-23", [])
        self.assertEqual(result["fills"], [])
        self.assertEqual([d["result"] for d in result["decisions"]], ["SUPERSEDED", "NO_ACTION"])

    def test_old_signal_expires(self):
        decision(self.db, day="2026-09-10")
        result = self.step("2026-09-22", [])
        self.assertEqual(result["decisions"][0]["result"], "EXPIRED")

    def test_signal_created_after_next_open_does_not_get_past_fill(self):
        record_decision(self.db, "SPY", "2026-09-22", "2026-09-23T14:00:00Z", {
            "final_trade_decision": "**Rating**: Buy", "trader_investment_plan": "**Action**: Buy\n**Stop Loss**: 98",
        }, "Buy", "SYNTHETIC_TEST")
        result = self.step("2026-09-23", [])
        self.assertEqual(result["fills"], [])
        self.assertEqual(result["decisions"][0]["result"], "AFTER_SESSION_OPEN")

    def test_immutable_decision_and_date_order(self):
        decision(self.db)
        with self.assertRaisesRegex(ValueError, "immutable"):
            decision(self.db, rating="Hold")
        self.step("2026-09-22", [bar()])
        with self.assertRaisesRegex(ValueError, "increasing date"):
            self.step("2026-09-21", [])

    def test_bad_ohlc_and_future_observation_refused(self):
        path = Path(self.folder.name) / "bad.csv"
        path.write_text("date,ticker,open,high,low,close,observed_at,source\n"
                        "2026-09-22,SPY,100,99,98,100,2026-09-22T21:00:00Z,synthetic\n")
        with self.assertRaisesRegex(ValueError, "inconsistent OHLC"):
            load_bars(path, now=datetime(2026, 9, 25, tzinfo=timezone.utc))
        path.write_text("date,ticker,open,high,low,close,observed_at,source\n"
                        "2026-09-22,SPY,100,101,98,100,2026-09-26T21:00:00Z,synthetic\n")
        with self.assertRaisesRegex(ValueError, "observed after"):
            load_bars(path, now=datetime(2026, 9, 25, tzinfo=timezone.utc))

    def test_after_close_gate(self):
        with self.assertRaisesRegex(ValueError, "after 16:15"):
            after_close("2026-09-23", datetime(2026, 9, 23, 15, tzinfo=timezone.utc))
        after_close("2026-09-23", datetime(2026, 9, 23, 21, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
