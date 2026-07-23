"""JSONL run logger backed by a public GCS bucket.

Each conversation gets a unique run id and its own object. The resulting URL is
public and wget-able so the grader can download it.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

from google.cloud import storage

import config


class RunLogger:
    """Accumulates log lines and uploads the JSONL object to GCS."""

    def __init__(self, bucket_name: str = config.GCS_LOG_BUCKET, prefix: str = config.GCS_LOG_PREFIX):
        self.run_id = uuid.uuid4().hex[:16]
        self.bucket_name = bucket_name
        self.prefix = prefix
        self.object_name = f"{prefix}run-{self.run_id}.jsonl"
        self._lines: list[dict[str, Any]] = []
        self._client: storage.Client | None = None

    def _get_client(self) -> storage.Client:
        if self._client is None:
            self._client = storage.Client()
        return self._client

    def log(self, event: str, data: dict[str, Any]) -> None:
        self._lines.append(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "run_id": self.run_id,
                "event": event,
                **data,
            }
        )

    def finalize(self) -> str:
        """Upload accumulated lines to GCS and return the public log URL."""
        if not self._lines:
            # Always write at least a heartbeat so the URL is not empty.
            self.log("finalize", {"note": "empty run"})

        body = "\n".join(json.dumps(line, ensure_ascii=False, default=str) for line in self._lines)
        blob = self._get_client().bucket(self.bucket_name).blob(self.object_name)
        blob.upload_from_file(BytesIO(body.encode("utf-8")), content_type="application/jsonlines+json")
        # Public URL for a uniform-bucket-level-access public bucket.
        return f"https://storage.googleapis.com/{self.bucket_name}/{self.object_name}"
