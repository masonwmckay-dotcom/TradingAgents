"""Bucket handoff is immutable and optional without credentials or network."""

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ta_paper_lab.signal_bucket import publish, publish_if_configured


class FakeBucket:
    def __init__(self):
        self.objects = {}
        self.puts = 0

    def get_object(self, *, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            error = RuntimeError("missing")
            error.response = {"Error": {"Code": "NoSuchKey"}}
            raise error
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def put_object(self, *, Bucket, Key, Body, ContentType, IfNoneMatch):
        assert ContentType == "application/json" and IfNoneMatch == "*"
        if (Bucket, Key) in self.objects:
            raise AssertionError("a dated feed was overwritten")
        self.objects[(Bucket, Key)] = Body
        self.puts += 1


class BucketTests(unittest.TestCase):
    def test_first_write_is_verified_repeat_is_noop_and_conflict_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "2026-09-24.json"
            path.write_bytes(b'{"test":"first"}\n')
            fake = FakeBucket()
            key = publish(path, "review-bucket-123", fake)
            self.assertEqual(key, "tradingagents/v1/2026-09-24.json")
            self.assertEqual(publish(path, "review-bucket-123", fake), key)
            self.assertEqual(fake.puts, 1)
            path.write_bytes(b'{"test":"changed"}\n')
            with self.assertRaisesRegex(ValueError, "refusing overwrite"):
                publish(path, "review-bucket-123", fake)
            self.assertEqual(fake.objects[("review-bucket-123", key)], b'{"test":"first"}\n')

    def test_partial_configuration_fails_and_unconfigured_is_local_only(self):
        names = ("TA_SIGNAL_S3_BUCKET", "TA_SIGNAL_S3_ENDPOINT",
                 "TA_SIGNAL_S3_ACCESS_KEY_ID", "TA_SIGNAL_S3_SECRET_ACCESS_KEY")
        with patch.dict(os.environ, {name: "" for name in names}):
            self.assertIsNone(publish_if_configured(Path("unused.json")))
        with patch.dict(os.environ, {**{name: "" for name in names}, "TA_SIGNAL_S3_BUCKET": "bucket123"}):
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                publish_if_configured(Path("unused.json"))


if __name__ == "__main__":
    unittest.main()
