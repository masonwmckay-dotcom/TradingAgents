"""Optional authenticated S3 handoff for immutable review feeds.

The bucket has no brokerage credentials and receives only the small decision
metadata file. A bucket write does not approve or submit a paper order.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

ENV_NAMES = ("TA_SIGNAL_S3_BUCKET", "TA_SIGNAL_S3_ENDPOINT",
             "TA_SIGNAL_S3_ACCESS_KEY_ID", "TA_SIGNAL_S3_SECRET_ACCESS_KEY")
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PREFIX = "tradingagents/v1/"
MAX_BYTES = 65536


def _error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str(response.get("Error", {}).get("Code", ""))
    return None


def _read(client, bucket: str, key: str) -> bytes | None:
    try:
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read(MAX_BYTES + 1)
    except Exception as exc:
        if _error_code(exc) in {"NoSuchKey", "404", "NotFound"}:
            return None
        raise RuntimeError("signal feed bucket read failed") from None
    if len(body) > MAX_BYTES:
        raise ValueError("stored signal feed exceeds the maximum size")
    return body


def publish(path: Path, bucket: str, client) -> str:
    """Conditional create and byte-for-byte reconciliation; never replace."""
    path = Path(path)
    if not DAY_RE.fullmatch(path.stem) or path.suffix != ".json":
        raise ValueError("signal feed must have a dated JSON filename")
    raw = path.read_bytes()
    if not 0 < len(raw) <= MAX_BYTES:
        raise ValueError("signal feed size is invalid")
    key = PREFIX + path.name
    old = _read(client, bucket, key)
    if old is not None:
        if old != raw:
            raise ValueError("stored dated signal feed differs; refusing overwrite")
        return key
    try:
        # A concurrent create may win between GET and PUT. No mutable overwrite.
        client.put_object(Bucket=bucket, Key=key, Body=raw,
                          ContentType="application/json", IfNoneMatch="*")
    except Exception as exc:
        if _error_code(exc) not in {"PreconditionFailed", "412"}:
            raise RuntimeError("signal feed bucket write failed") from None
    if _read(client, bucket, key) != raw:
        raise RuntimeError("signal feed bucket verification failed")
    return key


def publish_if_configured(path: Path) -> str | None:
    configured = {name: os.environ.get(name, "").strip() for name in ENV_NAMES}
    if not any(configured.values()):
        return None
    if not all(configured.values()):
        raise RuntimeError("incomplete signal feed bucket configuration")
    url = urlsplit(configured["TA_SIGNAL_S3_ENDPOINT"])
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}:
        raise RuntimeError("signal feed bucket requires a plain HTTPS endpoint")
    if not BUCKET_RE.fullmatch(configured["TA_SIGNAL_S3_BUCKET"]):
        raise RuntimeError("signal feed bucket name is invalid")
    try:
        import boto3
        from botocore.config import Config
        client = boto3.client(
            "s3", endpoint_url=configured["TA_SIGNAL_S3_ENDPOINT"],
            region_name=os.environ.get("TA_SIGNAL_S3_REGION", "auto"),
            aws_access_key_id=configured["TA_SIGNAL_S3_ACCESS_KEY_ID"],
            aws_secret_access_key=configured["TA_SIGNAL_S3_SECRET_ACCESS_KEY"],
            config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 2},
                          s3={"addressing_style": "virtual"}),
        )
    except ImportError as exc:
        raise RuntimeError("install the bridge dependency to use signal bucket transfer") from exc
    return publish(path, configured["TA_SIGNAL_S3_BUCKET"], client)
