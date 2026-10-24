"""API key rotation and redaction. A key never appears in logs, records, caches or exception text."""
from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterable

COOLDOWN_S = 60.0

_PATTERNS = [
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\b(?:sk|gsk|nvapi|AIza|ghp|gho|github_pat|xai)[-_][A-Za-z0-9_-]{8,}"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{20,}"),
]


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Replace known keys and anything shaped like a key or bearer token."""
    for s in sorted((s for s in secrets if s and len(s) >= 4), key=len, reverse=True):
        text = text.replace(s, "[redacted]")
    for pat in _PATTERNS:
        text = pat.sub("[redacted]", text)
    return text


class KeyRing:
    """Round-robin keys per provider, with per-key cooldowns and permanent drops."""

    def __init__(self, keys: dict[str, list[str]], clock: Callable[[], float] = time.monotonic,
                 cooldown_s: float = COOLDOWN_S):
        self._keys = {p: list(ks) for p, ks in keys.items()}
        self._next = dict.fromkeys(keys, 0)
        self._cool: dict[tuple[str, int], float] = {}
        self._dropped: set[tuple[str, int]] = set()
        self._clock = clock
        self._cooldown_s = cooldown_s
        self._lock = threading.Lock()

    def _usable(self, provider: str, i: int) -> bool:
        return (provider, i) not in self._dropped and self._cool.get((provider, i), 0.0) <= self._clock()

    def has_keys(self, provider: str) -> bool:
        """True while at least one key has not been dropped (cooling keys still count)."""
        return any((provider, i) not in self._dropped for i in range(len(self._keys.get(provider, []))))

    def available(self, provider: str) -> bool:
        with self._lock:
            return any(self._usable(provider, i) for i in range(len(self._keys.get(provider, []))))

    def acquire(self, provider: str) -> tuple[int, str] | None:
        """The next usable key as (index, key), advancing the rotation; None if every key is cooling or dropped."""
        with self._lock:
            ks = self._keys.get(provider, [])
            for step in range(len(ks)):
                i = (self._next[provider] + step) % len(ks)
                if self._usable(provider, i):
                    self._next[provider] = (i + 1) % len(ks)
                    return i, ks[i]
        return None

    def cool(self, provider: str, index: int) -> None:
        with self._lock:
            self._cool[(provider, index)] = self._clock() + self._cooldown_s

    def drop(self, provider: str, index: int) -> None:
        with self._lock:
            self._dropped.add((provider, index))

    def secrets(self) -> list[str]:
        return [k for ks in self._keys.values() for k in ks]
