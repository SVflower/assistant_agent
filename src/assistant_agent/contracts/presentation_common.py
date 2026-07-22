"""图表 Artifact 版本共享的稳定编码与标识。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

MAX_ARTIFACT_BYTES = 512 * 1024
MAX_RUN_ARTIFACTS = 16
MAX_RUN_ARTIFACT_BYTES = 2 * 1024 * 1024


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def stable_message_id(run_id: str) -> str:
    digest = hashlib.sha256(f"message:{run_id}".encode()).hexdigest()[:24]
    return f"msg_{digest}"
