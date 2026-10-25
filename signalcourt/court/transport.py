"""The court's provider transport: the router's OpenAI-compatible call with a hard wall-clock limit.

The openai client's timeout applies to each network read, so a provider that streams slowly can hold one call
far past it and blow the court's time cap. Here the call runs in a daemon thread and is abandoned when the limit
passes (reported to the router as a timeout, like any other). gpt-oss models are asked for low reasoning effort,
so their reply budget goes to the answer rather than to thinking.
"""

from __future__ import annotations

import threading
from typing import Any

from ..llm.keys import redact
from ..llm.router import TransportError, TransportResponse

GRACE_S = 1.0


def _low_effort(model: str) -> dict[str, Any]:
    return {"reasoning_effort": "low"} if "gpt-oss" in model else {}


def openai_call(
    base_url: str, api_key: str, model: str, messages: list[dict], max_tokens: int | None, timeout_s: float
) -> TransportResponse:
    import openai

    client = openai.OpenAI(base_url=base_url, api_key=api_key, timeout=timeout_s, max_retries=0)
    kw: dict[str, Any] = {} if max_tokens is None else {"max_tokens": max_tokens}
    extra = _low_effort(model)
    if extra:
        kw["extra_body"] = extra
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


def bounded(inner=openai_call):
    """Wrap a transport so no call outlives its ``timeout_s`` (plus a second of grace)."""

    def call(base_url, api_key, model, messages, max_tokens, timeout_s):
        box: dict[str, Any] = {}

        def work() -> None:
            try:
                box["r"] = inner(base_url, api_key, model, messages, max_tokens, timeout_s)
            except BaseException as e:  # noqa: BLE001 - handed back to the caller's thread below
                box["e"] = e

        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(max(0.0, timeout_s) + GRACE_S)
        if t.is_alive():
            raise TransportError(None, "timeout")
        if "e" in box:
            raise box["e"]
        return box["r"]

    return call
