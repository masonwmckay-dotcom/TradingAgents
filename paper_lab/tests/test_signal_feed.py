"""The review feed must be complete, immutable, and based on real report paths."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from ta_paper_lab.core import connect, record_decision
from ta_paper_lab.signal_feed import export_feed


DAY = "2026-09-24"
CREATED = "2026-09-24T21:30:00Z"


def insert(root: Path, ticker: str, report_path: Path | None = None) -> None:
    report = report_path or root / "upstream_reports" / DAY / ticker / "complete_report.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("Saved analyst report", encoding="utf-8")
    db = connect(root / "paper.db")
    try:
        record_decision(db, ticker, DAY, CREATED, {
            "market_report": "Market", "fundamentals_report": "Fundamentals", "news_report": "News",
            "final_trade_decision": "**Rating**: Buy",
            "trader_investment_plan": "**Action**: Buy\n**Stop Loss**: 98.00",
        }, "Buy", str(report))
    finally:
        db.close()


class SignalFeedTests(unittest.TestCase):
    def test_complete_feed_contains_only_decision_metadata_and_is_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            insert(root, "SPY")
            insert(root, "QQQ")
            path = export_feed(root, DAY, ["SPY", "QQQ"])
            payload = json.loads(path.read_text())
            digest = payload.pop("content_sha256")
            body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
            self.assertEqual(digest, hashlib.sha256(body).hexdigest())
            self.assertEqual(payload["universe"], ["QQQ", "SPY"])
            self.assertEqual(payload["decisions"][1]["stop_loss"], "98.00")
            self.assertNotIn("Saved analyst report", path.read_text())
            self.assertEqual(export_feed(root, DAY, ["QQQ", "SPY"]), path)
            unchanged = path.read_bytes()
            db = connect(root / "paper.db")
            with db:
                db.execute("UPDATE decisions SET payload_hash=? WHERE ticker='SPY'", ("0" * 64,))
            db.close()
            with self.assertRaisesRegex(ValueError, "refusing overwrite"):
                export_feed(root, DAY, ["SPY", "QQQ"])
            self.assertEqual(path.read_bytes(), unchanged)

    def test_partial_universe_and_demo_report_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            insert(root, "SPY")
            with self.assertRaisesRegex(ValueError, "every ticker"):
                export_feed(root, DAY, ["SPY", "QQQ"])
            self.assertFalse((root / "signal_feed" / f"{DAY}.json").exists())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            insert(root, "SPY", root / "synthetic_demo.md")
            with self.assertRaisesRegex(ValueError, "saved real analyst report"):
                export_feed(root, DAY, ["SPY"])

    def test_before_close_decision_cannot_be_exported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            insert(root, "SPY")
            db = connect(root / "paper.db")
            with db:
                db.execute("UPDATE decisions SET created_at=?", ("2026-09-24T18:00:00Z",))
            db.close()
            with self.assertRaisesRegex(ValueError, "completed New York session"):
                export_feed(root, DAY, ["SPY"])


if __name__ == "__main__":
    unittest.main()
