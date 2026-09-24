"""Immutable, broker-free decision feed for a separately reviewed consumer.

This is a file contract, not a transport or order submission API. A consumer
must authenticate its transport and independently validate every trade.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import date, time
from pathlib import Path
from zoneinfo import ZoneInfo

from .core import connect, utc_time, valid_ticker

SCHEMA = "ta-paper-decision-v1"
SOURCE = "tradingagents-paper-lab"
UPSTREAM_COMMIT = "35543d0248bf89fcb92b17a15858ad0c0e940687"
ET = ZoneInfo("America/New_York")


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def export_feed(data_dir: Path, day: str, tickers: list[str]) -> Path:
    """Export a complete date/universe once; refuse partial or changed output.

    It includes only decisions already committed by real analysis. The demo
    report path is outside ``upstream_reports`` and cannot pass this check.
    """
    session = date.fromisoformat(day)
    universe = sorted({valid_ticker(ticker) for ticker in tickers})
    if not universe or len(universe) != len(tickers):
        raise ValueError("signal feed requires a nonempty, unique ticker universe")
    data_dir = Path(data_dir)
    db = connect(data_dir / "paper.db")
    try:
        rows = [dict(row) for row in db.execute(
            "SELECT ticker,trade_date,created_at,rating,action,stop_loss,"
            "report_path,payload_hash,status FROM decisions WHERE trade_date=? ORDER BY ticker", (day,)
        )]
    finally:
        db.close()
    if [row["ticker"] for row in rows] != universe or any(row["status"] != "PENDING" for row in rows):
        raise ValueError("signal feed needs every ticker's current pending decision")

    decisions = []
    for row in rows:
        created = utc_time(row["created_at"])
        ny_time = created.astimezone(ET)
        if ny_time.date() != session or ny_time.time() < time(16, 15):
            raise ValueError("signal decision must follow its completed New York session")
        report_file = (data_dir / "upstream_reports" / day / row["ticker"] / "complete_report.md").resolve()
        if Path(row["report_path"]).resolve() != report_file or not report_file.is_file():
            raise ValueError("signal feed requires the saved real analyst report")
        if len(row["payload_hash"]) != 64 or any(ch not in "0123456789abcdef" for ch in row["payload_hash"]):
            raise ValueError("signal decision has no valid immutable payload hash")
        decisions.append({
            "ticker": row["ticker"], "analysis_date": day,
            "created_at": created.isoformat().replace("+00:00", "Z"),
            "rating": row["rating"], "trader_action": row["action"],
            "stop_loss": row["stop_loss"], "payload_sha256": row["payload_hash"],
        })

    body = {"schema": SCHEMA, "source": SOURCE, "upstream_commit": UPSTREAM_COMMIT,
            "analysis_date": day, "universe": universe, "decisions": decisions}
    feed = {**body, "content_sha256": hashlib.sha256(_canonical(body)).hexdigest()}
    contents = _canonical(feed) + b"\n"
    destination = data_dir / "signal_feed" / f"{day}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".signal-", delete=False) as tmp:
        temp_path = Path(tmp.name)
        try:
            tmp.write(contents)
            tmp.flush()
            os.fsync(tmp.fileno())
            try:
                os.link(temp_path, destination)  # atomic create; never replace a dated feed
                directory_fd = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except FileExistsError:
                if destination.read_bytes() != contents:
                    raise ValueError("existing dated signal feed differs; refusing overwrite")
        finally:
            temp_path.unlink(missing_ok=True)
    return destination
