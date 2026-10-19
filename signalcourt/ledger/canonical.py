"""Canonical JSON and the ledger hash formula.

hash = sha256(prev_hash || canonical_json(envelope without hash and rid))

The rid is derived from the hash (type prefix + first 8 hex), so it cannot be an
input to it; it is excluded together with the hash itself.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

GENESIS_HASH = hashlib.sha256(b"signal-court/v2").hexdigest()

# Envelope fields that are not hashed.
UNHASHED_FIELDS = ("hash", "rid")


def _prepare(obj: Any) -> Any:
    """Round floats to 10 decimals, map NaN/inf to None, recurse into containers."""
    if isinstance(obj, bool) or obj is None or isinstance(obj, (str, int)):
        return obj
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        r = round(obj, 10)
        # -0.0 and 0.0 must hash the same
        return 0.0 if r == 0 else r
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if not isinstance(k, str):
                raise TypeError(f"canonical_json: non-string key {k!r}")
            out[k] = _prepare(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [_prepare(v) for v in obj]
    raise TypeError(f"canonical_json: unsupported type {type(obj).__name__}")


def canonical_json(obj: Any) -> bytes:
    """Sorted keys, no whitespace, UTF-8, floats rounded to 10 places, no NaN."""
    return json.dumps(_prepare(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode(
        "utf-8"
    )


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hashable_view(envelope: dict) -> dict:
    return {k: v for k, v in envelope.items() if k not in UNHASHED_FIELDS}


def compute_hash(prev_hash: str, env_without_hash: dict) -> str:
    """sha256(prev_hash || canonical_json(envelope without hash))."""
    return sha256_hex(prev_hash.encode("ascii") + canonical_json(hashable_view(env_without_hash)))


def make_rid(prefix: str, hash_hex: str) -> str:
    return f"{prefix}-{hash_hex[:8]}"
