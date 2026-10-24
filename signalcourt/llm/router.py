"""LLM router: role chains, key rotation, family guard, response cache, JSON repair, LLMCALL records.

Every provider is OpenAI-compatible. A call walks the role's chain of deployments; the first reply that
parses and validates wins and is cached, so a warm run can be replayed offline. Nothing here can hang: every
real call is bounded by the per-call timeout, the run deadline and the call budget.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from ..ledger import Author, Ledger
from ..ledger.canonical import sha256_hex
from .cache import ResponseCache, cache_key
from .keys import COOLDOWN_S, KeyRing, redact

Mode = Literal["live", "warm", "replay"]
ERROR_CHARS = 200
MAX_RATE_WAIT_S = 8.0


class RouterError(Exception):
    pass


class CacheMiss(RouterError):
    pass


class BudgetExhausted(RouterError):
    pass


class DeadlineExceeded(RouterError):
    pass


class NoDeployment(RouterError):
    pass


class AllProvidersFailed(RouterError):
    def __init__(self, attempts: list[tuple[str, str]]):
        self.attempts = attempts
        super().__init__("all providers failed: " + "; ".join(f"{d}: {e}" for d, e in attempts))


class TransportError(Exception):
    """An HTTP-level failure. ``status`` is None for timeouts and connection errors."""

    def __init__(self, status: int | None, message: str):
        self.status = status
        self.message = message
        super().__init__(f"{status}: {message}")


@dataclass
class TransportResponse:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0


class Transport(Protocol):
    def __call__(
        self, base_url: str, api_key: str, model: str, messages: list[dict], max_tokens: int | None, timeout_s: float
    ) -> TransportResponse: ...


def openai_transport(
    base_url: str, api_key: str, model: str, messages: list[dict], max_tokens: int | None, timeout_s: float
) -> TransportResponse:
    import openai

    client = openai.OpenAI(base_url=base_url, api_key=api_key, timeout=timeout_s, max_retries=0)
    kw: dict[str, Any] = {} if max_tokens is None else {"max_tokens": max_tokens}
    try:
        r = client.chat.completions.create(model=model, messages=messages, **kw)
    except openai.APITimeoutError as e:
        raise TransportError(None, "timeout") from e
    except openai.APIStatusError as e:
        raise TransportError(e.status_code, redact(str(e.message), [api_key])) from e
    except openai.APIConnectionError as e:
        raise TransportError(None, "connection error") from e
    usage = r.usage
    text = (r.choices[0].message.content or "") if r.choices else ""
    return TransportResponse(text, getattr(usage, "prompt_tokens", 0) or 0, getattr(usage, "completion_tokens", 0) or 0)


@dataclass(frozen=True)
class Deployment:
    name: str
    provider: str
    model: str
    family: str
    rpm: float | None


@dataclass
class RouterResult:
    parsed: BaseModel
    llmcall_rid: str | None
    model: str
    family: str
    provider: str
    cache_hit: bool


class _AnyObject(BaseModel):
    model_config = ConfigDict(extra="allow")


_THINK = re.compile(r"<think>.*?</think>", re.S)
AUTHOR = Author.code("router")


def parse_json(text: str, schema: type[BaseModel]) -> BaseModel:
    """The first JSON object in ``text`` (fences and surrounding prose ignored), validated with ``schema``."""
    text = _THINK.sub("", text)
    dec = json.JSONDecoder()
    pos = text.find("{")
    last_err: Exception = ValueError("no JSON object in reply")
    for _ in range(20):
        if pos < 0:
            break
        try:
            obj, _end = dec.raw_decode(text, pos)
        except ValueError as e:
            last_err = e
            pos = text.find("{", pos + 1)
            continue
        return schema.model_validate(obj)
    raise ValueError(str(last_err))


class Router:
    def __init__(
        self,
        models_cfg: dict,
        keys: Callable[[str], list[str]],
        cache_path: Path,
        mode: Mode,
        *,
        ledger: Ledger | None = None,
        call_budget: int | None = None,
        deadline: float | None = None,
        per_call_timeout_s: float = 25.0,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.monotonic,
        demo: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        cooldown_s: float = COOLDOWN_S,
        max_rate_wait_s: float = MAX_RATE_WAIT_S,
    ):
        if mode not in ("live", "warm", "replay"):
            raise ValueError(f"unknown router mode {mode!r}")
        self.mode = mode
        self.ledger = ledger
        self.call_budget = call_budget
        self.deadline = deadline
        self.per_call_timeout_s = per_call_timeout_s
        self.demo = demo
        self.calls_used = 0
        self._transport: Transport = transport or openai_transport
        self._clock = clock
        self._sleep = sleep
        self._max_rate_wait_s = max_rate_wait_s
        self._providers: dict[str, dict] = models_cfg.get("providers", {})
        self._roles: dict[str, list[str]] = models_cfg.get("roles", {})
        self._deployments = {
            name: Deployment(name, v[0], str(v[1]), v[2], v[4] if len(v) > 4 else None)
            for name, v in models_cfg.get("deployments", {}).items()
        }
        self._ring = KeyRing(
            {p: list(keys(c.get("keys_env", ""))) for p, c in self._providers.items()}, clock, cooldown_s
        )
        self._cache = ResponseCache(cache_path)
        self._last_call: dict[str, float] = {}
        self._used: dict[str, set[str]] = {}
        self._lock = threading.Lock()

    # -- chain ---------------------------------------------------------------------------------------------
    def _names(self, role: str) -> list[str]:
        if self.demo and f"{role}.demo" in self._roles:
            return list(self._roles[f"{role}.demo"])
        return list(self._roles.get(role, []))

    def _static_skip(self, dep: Deployment | None, exclude: frozenset[str]) -> str | None:
        if dep is None:
            return "unknown deployment"
        if dep.model.startswith("<verify"):
            return "placeholder model id"
        if dep.family in exclude:
            return "family excluded"
        return None

    def _live_skip(self, dep: Deployment) -> str | None:
        if not self._ring.has_keys(dep.provider):
            return "no key"
        if not self._ring.available(dep.provider):
            return "cooling down"
        return None

    def _chain(self, role: str, exclude: frozenset[str]) -> list[Deployment]:
        out = []
        for n in self._names(role):
            dep = self._deployments.get(n)
            if self._static_skip(dep, exclude) is None:
                out.append(dep)
        return out

    def chain_status(self, role: str, exclude_families: frozenset[str] = frozenset()) -> list[tuple[str, str]]:
        """(deployment, "usable" | "skipped: <why>") for each entry of the role chain; never calls a provider."""
        out = []
        for n in self._names(role):
            dep = self._deployments.get(n)
            why = self._static_skip(dep, exclude_families) or (self._live_skip(dep) if self.mode != "replay" else None)
            out.append((n, "usable" if why is None else f"skipped: {why}"))
        return out

    def families_used(self, role: str) -> set[str]:
        with self._lock:
            return set(self._used.get(role, ()))

    # -- public calls --------------------------------------------------------------------------------------
    def call(
        self,
        role: str,
        template: str,
        messages: list[dict],
        schema: type[BaseModel],
        *,
        exclude_families: frozenset[str] = frozenset(),
        max_tokens: int | None = None,
    ) -> RouterResult:
        chain = self._chain(role, exclude_families)
        if not chain:
            raise NoDeployment(f"no deployment left for role {role!r}")
        for dep in chain:
            hit = self._cache.get(cache_key(dep.model, messages, max_tokens))
            if hit is None:
                continue
            try:
                parsed = parse_json(hit.text, schema)
            except ValueError:
                continue  # stale entry for an older schema; treat as a miss
            rid = self._record(
                role, template, dep, hit.key, hit.text, True, hit.tokens_in, hit.tokens_out, 0, None, None
            )
            return self._finish(role, parsed, rid, dep, True)
        if self.mode == "replay":
            raise CacheMiss(f"no cached reply for role {role!r}")
        if all(self._live_skip(d) for d in chain):
            raise NoDeployment(f"no usable deployment for role {role!r}: " + "; ".join(
                f"{n}: {s}" for n, s in self.chain_status(role, exclude_families)))
        attempts: list[tuple[str, str]] = []
        for dep in chain:
            why = self._live_skip(dep)
            if why:
                attempts.append((dep.name, f"skipped: {why}"))
                continue
            res = self._try(dep, role, template, messages, schema, max_tokens, attempts)
            if res is not None:
                return res
        raise AllProvidersFailed(attempts)

    def complete_json(self, role: str, prompt: str) -> dict[str, Any]:
        """LlmPort adapter: one user message, any JSON object back."""
        res = self.call(role, "complete_json", [{"role": "user", "content": prompt}], _AnyObject)
        return res.parsed.model_dump()

    def close(self) -> None:
        self._cache.close()

    # -- internals -----------------------------------------------------------------------------------------
    def _finish(self, role: str, parsed: BaseModel, rid: str | None, dep: Deployment, hit: bool) -> RouterResult:
        with self._lock:
            self._used.setdefault(role, set()).add(dep.family)
        return RouterResult(parsed, rid, dep.model, dep.family, dep.provider, hit)

    def _try(
        self,
        dep: Deployment,
        role: str,
        template: str,
        messages: list[dict],
        schema: type[BaseModel],
        max_tokens: int | None,
        attempts: list[tuple[str, str]],
    ) -> RouterResult | None:
        key = cache_key(dep.model, messages, max_tokens)
        sent = messages
        repaired = False
        secrets = self._ring.secrets()
        base_url = self._providers[dep.provider].get("base_url", "")
        while True:
            got = self._ring.acquire(dep.provider)
            if got is None:
                attempts.append((dep.name, "no usable key"))
                return None
            idx, secret = got
            alias = f"{dep.provider}#{idx}"
            timeout = self._reserve(dep)
            if timeout is None:
                attempts.append((dep.name, "skipped: local rate limit"))
                return None
            t0 = self._clock()
            try:
                resp = self._transport(base_url, secret, dep.model, sent, max_tokens, timeout)
            except TransportError as e:
                err = redact(f"{e.status if e.status is not None else 'error'}: {e.message}", secrets)[:ERROR_CHARS]
                self._record(role, template, dep, key, None, False, 0, 0, self._ms(t0), err, alias)
                if e.status == 429:
                    self._ring.cool(dep.provider, idx)
                    continue
                if e.status in (401, 403):
                    self._ring.drop(dep.provider, idx)
                    continue
                attempts.append((dep.name, err))
                return None
            except Exception as e:  # noqa: BLE001 - a broken transport must not take the demo down
                err = f"error: {type(e).__name__}"
                self._record(role, template, dep, key, None, False, 0, 0, self._ms(t0), err, alias)
                attempts.append((dep.name, err))
                return None
            ms = self._ms(t0)
            try:
                parsed = parse_json(resp.text, schema)
            except ValueError as e:
                err = redact(f"parse: {e}", secrets)[:ERROR_CHARS]
                self._record(role, template, dep, key, None, False, resp.tokens_in, resp.tokens_out, ms, err, alias)
                if not repaired:
                    repaired = True
                    sent = [
                        *messages,
                        {"role": "assistant", "content": resp.text},
                        {"role": "user", "content": f"That reply was not valid: {err}. "
                         "Reply again with only the corrected JSON object."},
                    ]
                    continue
                attempts.append((dep.name, err))
                return None
            self._cache.put(key, dep.model, dep.family, dep.provider, resp.text, resp.tokens_in, resp.tokens_out)
            rid = self._record(
                role, template, dep, key, resp.text, False, resp.tokens_in, resp.tokens_out, ms, None, alias
            )
            return self._finish(role, parsed, rid, dep, False)

    def _reserve(self, dep: Deployment) -> float | None:
        """Check deadline and budget, take a rate slot and count the call. Returns the transport timeout,
        or None when the deployment's rate limit would make us wait longer than we are willing to."""
        with self._lock:
            now = self._clock()
            if self.deadline is not None and now >= self.deadline:
                raise DeadlineExceeded("run deadline reached")
            if self.call_budget is not None and self.calls_used >= self.call_budget:
                raise BudgetExhausted(f"call budget of {self.call_budget} used")
            gap = 60.0 / dep.rpm if dep.rpm else 0.0
            wait = max(0.0, self._last_call.get(dep.name, float("-inf")) + gap - now)
            if wait > self._max_rate_wait_s:
                return None
            if self.deadline is not None and now + wait >= self.deadline:
                raise DeadlineExceeded("run deadline reached while waiting for the rate limit")
            self._last_call[dep.name] = now + wait
            self.calls_used += 1
            timeout = self.per_call_timeout_s
            if self.deadline is not None:
                timeout = min(timeout, self.deadline - now - wait)
        if wait > 0:
            self._sleep(wait)
        return timeout

    def _ms(self, t0: float) -> int:
        return max(0, int(round((self._clock() - t0) * 1000)))

    def _record(
        self,
        role: str,
        template: str,
        dep: Deployment,
        key: str,
        text: str | None,
        cache_hit: bool,
        tokens_in: int,
        tokens_out: int,
        latency_ms: int,
        error: str | None,
        alias: str | None,
    ) -> str | None:
        if self.ledger is None:
            return None
        env = self.ledger.append(
            "LLMCALL",
            {
                "role": role,
                "provider": dep.provider,
                "model": dep.model,
                "family": dep.family,
                "prompt_template": template,
                "prompt_hash": key,
                "response_hash": None if error or text is None else sha256_hex(text.encode("utf-8")),
                "cache_hit": cache_hit,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "latency_ms": latency_ms,
                "error": error,
                "key_alias": alias,
            },
            AUTHOR,
        )
        return env.rid
