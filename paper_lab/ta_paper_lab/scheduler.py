"""One-shot Railway cron entry point; exits at the end of a daily run."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .cli import main as cli_main


def main() -> int:
    tickers = os.environ.get("TA_PAPER_TICKERS", "").strip()
    if not tickers:
        print("ERROR: TA_PAPER_TICKERS must list the research universe", file=sys.stderr)
        return 2
    day = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York")).date().isoformat()
    data_dir = os.environ.get("TA_PAPER_DATA_DIR", "/data")
    return cli_main(["--data-dir", data_dir, "run-day", "--date", day, "--tickers", tickers])


if __name__ == "__main__":
    raise SystemExit(main())
