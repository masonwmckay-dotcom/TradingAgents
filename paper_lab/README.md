# TradingAgents paper lab

A separate research bot built around [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) v0.5.1, pinned to commit `35543d0248bf89fcb92b17a15858ad0c0e940687` (September 24, 2026). This project does **not** alter `masonwmckay-dotcom/alpaca-stock-agent` or use its Alpaca credentials. TradingAgents is a Python multi-agent research framework; the linked repository is not a TradingView Pine script.

## What it does

1. After 4:15 p.m. New York time, run TradingAgents' **market, fundamentals, and news** analysts, trader, and portfolio manager for each selected US ticker. Save its complete report under `data/upstream_reports` and one immutable dated decision in `data/paper.db`.
2. Fetch the day's raw daily OHLC prices with `yfinance`, or supply the same CSV format yourself. Each bar records the provider name and when it was observed.
3. At the **end of the next session**, simulate the prior decision at that session's **opening** price, including five basis points of slippage each way. Check daily lows against stops, mark open positions at the close, and write every simulated fill to the SQLite ledger. This is a retrospective daily-bar simulation. It never sends a paper or live order to a broker.

The order of operations for a daily run is: collect today's completed bars → settle prior decisions in the paper ledger → run today's research for tomorrow's simulation. The first day therefore has research but no fills.

## Decision feed for review

When **all** requested tickers finish real analysis, the lab also writes one immutable metadata file at `data/signal_feed/YYYY-MM-DD.json` (or `/data/signal_feed/` on Railway). It contains each ticker's dated final rating, trader action, stop, decision timestamp and payload hash; no analyst prose, API key or broker order. Failed or incomplete runs produce no new feed. Re-running the same decisions accepts identical bytes and refuses a changed dated file. The synthetic `demo` cannot export a feed.

This is a review artifact. Its SHA-256 detects modification but does not authenticate who sent it. The existing Alpaca bot has a **different Railway project and volume**, so it cannot read this path directly. The optional S3 bridge publishes the file as `tradingagents/v1/YYYY-MM-DD.json` in a private Railway bucket. It uses a conditional first write and byte-for-byte verification, and refuses to replace a changed dated object. No broker submit function is added here, and this file is not an approved order.

The S3 bridge activates only when all four variables are present: `TA_SIGNAL_S3_BUCKET`, `TA_SIGNAL_S3_ENDPOINT` (HTTPS base endpoint), `TA_SIGNAL_S3_ACCESS_KEY_ID`, and `TA_SIGNAL_S3_SECRET_ACCESS_KEY`. `TA_SIGNAL_S3_REGION` defaults to `auto`. An incomplete configuration fails the research run after the local decision is saved, so the same immutable decision can be retried. Enter secrets as Railway service variables, not in GitHub. The current deployed service has none of these variables; this draft does not publish to a bucket until reviewed and configured.

## Set up

Requires Python 3.12 or later, `git`, network access for the market/LLM services, and a paid or otherwise usable **OpenAI API** key. A ChatGPT subscription by itself is not that API key.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[agents]'

# Set real keys privately in your shell or deployment environment.
export OPENAI_API_KEY='your-openai-api-key'
# Optional: the existing Railway variable name is accepted here.
export ALPHAVANTAGE_API_KEY='your-existing-alpha-vantage-key'
```

When `ALPHAVANTAGE_API_KEY` is set, the adapter maps it to TradingAgents' `ALPHA_VANTAGE_API_KEY` and uses Alpha Vantage for fundamentals and news. Without it, the upstream default is `yfinance`. Prices for the paper ledger use raw daily OHLC from `yfinance` in either case. The new bot needs its **own** environment variables; no keys are copied from the current Railway service.

Verify the ledger without keys, brokers, or outside data:

```bash
python -m ta_paper_lab --data-dir ./demo_data demo
python -m ta_paper_lab --data-dir ./demo_data status
python -m unittest discover -s tests -v
```

The demo uses an invented $100 SPY price and clearly labels its source `SYNTHETIC_DEMO`. Keep `demo_data` separate from real research.

For a real daily run, execute this **after 4:15 p.m. New York time**, on a US market weekday, with the current New York date:

```bash
python -m ta_paper_lab --data-dir ./data run-day \
  --date 2026-09-24 --tickers SPY,QQQ,IWM,XLK,XLF
python -m ta_paper_lab --data-dir ./data status
```

Replace the date on future sessions. `run-day` reuses an existing dated bar file and refuses to overwrite a finalized paper session. If a model or provider fails, that ticker gets a `FAILED` result; the command exits nonzero. After addressing the failure, rerun the same date. Successful ticker/date decisions cannot be replaced with different output.

For separate steps, use `collect-bars --date YYYY-MM-DD --tickers ...`, `paper-step --bars ./data/bars/YYYY-MM-DD.csv`, and `analyze --date YYYY-MM-DD --tickers ...`. This lets you review prices before processing the paper session. A supplied CSV must have precisely these columns:

| date | ticker | open | high | low | close | observed_at | source |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| 2026-09-23 | SPY | 100.00 | 102.00 | 99.00 | 101.00 | 2026-09-23T21:00:00Z | synthetic example only |

Use only **completed, source-checked** daily bars. `observed_at` is a UTC timestamp after 4:15 p.m. New York time on the bar's date. The example values are invented; do not treat them as actual SPY prices. Imported CSV prices and timestamps are checked for shape and timing, but this program cannot prove a person entered truthful market data.

## Rules in this version

| Rule | Setting |
| --- | ---: |
| Starting simulated cash | $5,000 |
| Maximum intended loss at a stop, per entry | $25 |
| Maximum entry value | $1,150 |
| Maximum simultaneous positions | 3 |
| Slippage | 0.05% on each side |
| Eligible direction | Long only, whole shares, no margin |
| Signal expiry | 7 calendar days |

The simulator opens only when the **final** rating is `Buy`/`Overweight`, the trader action is `Buy`, and the trader supplied a valid numeric stop below the *actual* simulated entry price. `Sell` exits and `Underweight` reduces a held position only when the trader action is `Sell`. `Hold`, `REVIEW`, contradictions, missing stops, stale signals, unavailable buying power, and position-limit breaches do not open trades. If there are several pending decisions for a ticker, only the newest acts; older decisions are marked `SUPERSEDED`.

The decision must exist **before 9:30 a.m. New York time on the simulated fill date**. A decision created after that open is recorded as `AFTER_SESSION_OPEN` and cannot claim a past opening fill.

The $25 rule sizes the position using the entry-to-stop distance. A gap through a stop can lose **more** than $25. An overnight gap down uses the worse opening price in the simulation. Intraday order, spread, partial fills, borrow costs, taxes and corporate actions are not modeled. The demo's account value and any future paper results do not establish profitable real trading.

## Isolation and next validation gate

This lab has no broker submit function or Alpaca package. It does not edit, deploy, or feed signals to the existing private bot. `/data` can be used as `TA_PAPER_DATA_DIR` on a separate Railway service with a persistent volume. Keep the two services' data paths and environment variables separate. A real multi-agent run requires `OPENAI_API_KEY`; that key is absent from this work session, so **live analyst output and real-provider fills have not been verified here**. The tests and demo exercise synthetic fixtures only.

Before judging signal quality, capture several forward sessions, inspect the analyst reports, check that data actually arrived on time, and compare the paper ledger to a simple benchmark using the same execution assumptions. No target return is built into this bot.

TradingAgents is an external Apache-2.0 project; see its [license](https://github.com/TauricResearch/TradingAgents/blob/main/LICENSE). This adapter uses its public `TradingAgentsGraph.propagate()`, `save_reports()`, and portfolio context interface without modifying upstream source.

## Separate Railway service

The included `Dockerfile` and `python -m ta_paper_lab.scheduler` provide a one-shot deployment entry point. In your `masonwmckay-dotcom/TradingAgents` fork, set the new Railway service **Root Directory** to `/paper_lab` so it uses this folder's Dockerfile. Create it as a separate service or project from `alpaca-stock-agent`. In the new Railway service:

1. Attach a persistent volume mounted at `/data`.
2. Set `OPENAI_API_KEY` and `TA_PAPER_TICKERS=SPY,QQQ,IWM,XLK,XLF` in service variables. Optionally set `ALPHAVANTAGE_API_KEY` to the value already used in the old bot; it is read only by this service.
3. Set the service's **Cron Schedule** to `0 22 * * 1-5` (22:00 UTC on weekdays). That is after the New York market close in both daylight and standard time. The scheduler computes the current New York date and exits when done. Review the first actual run's logs, reports, data provenance and paper ledger before relying on the schedule.

Railway runs [cron jobs in UTC](https://docs.railway.com/cron-jobs) and requires the process to exit. A [volume](https://docs.railway.com/volumes) retains `/data` across runs. This package cannot create the new service or insert your keys from this workspace; its Docker image and schedule entry point are prepared for that step.
