import time

import pytest

from signalcourt.court.transport import _low_effort, bounded
from signalcourt.llm.router import TransportError, TransportResponse


def test_bounded_transport_abandons_a_call_that_outlives_its_timeout():
    def slow(*a):
        time.sleep(5)
        return TransportResponse("{}")

    t0 = time.monotonic()
    with pytest.raises(TransportError) as e:
        bounded(slow)("u", "k", "m", [], None, 0.2)
    assert e.value.status is None and time.monotonic() - t0 < 2.5


def test_bounded_transport_passes_replies_and_errors_through():
    assert bounded(lambda *a: TransportResponse("ok"))("u", "k", "m", [], None, 1).text == "ok"

    def bad(*a):
        raise TransportError(429, "slow down")

    with pytest.raises(TransportError) as e:
        bounded(bad)("u", "k", "m", [], None, 1)
    assert e.value.status == 429


def test_low_reasoning_effort_only_for_gpt_oss():
    assert _low_effort("openai/gpt-oss-20b") == {"reasoning_effort": "low"}
    assert _low_effort("qwen/qwen3.8-27b") == {}
