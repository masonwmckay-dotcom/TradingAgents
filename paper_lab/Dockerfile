FROM python:3.12-slim

# git is needed to install the exact TradingAgents commit in pyproject.toml.
# tzdata is needed for the New York market-close and session-open checks.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY ta_paper_lab ./ta_paper_lab
RUN python -m pip install --no-cache-dir '.[agents,bridge]'

ENV PYTHONUNBUFFERED=1 TA_PAPER_DATA_DIR=/data
CMD ["python", "-m", "ta_paper_lab.scheduler"]
