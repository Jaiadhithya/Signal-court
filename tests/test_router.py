"""Router tests: fake transport only, never the network."""
import json

import pytest
from conftest import fixed_clock, run_payload
from pydantic import BaseModel

from signalcourt.extract.llm_extract import LlmPort
from signalcourt.ledger import Ledger
from signalcourt.llm.keys import KeyRing, redact
from signalcourt.llm.router import (
    AllProvidersFailed,
    BudgetExhausted,
    CacheMiss,
    DeadlineExceeded,
    NoDeployment,
    Router,
    RouterError,
    TransportError,
    TransportResponse,
)

MSG = [{"role": "user", "content": "go"}]
OK = '{"ok": true}'


class Out(BaseModel):
    ok: bool


def cfg(**roles):
    dep = lambda p, m, f: [p, m, f, 100000, 6000, None, None]  # noqa: E731
    return {
        "providers": {
            "pa": {"base_url": "https://a.test/v1", "keys_env": "PA_KEYS"},
            "pb": {"base_url": "https://b.test/v1", "keys_env": "PB_KEYS"},
            "pc": {"base_url": "https://c.test/v1", "keys_env": "PC_KEYS"},
        },
        "deployments": {
            "a1": dep("pa", "model-a1", "fam_a"),
            "a2": dep("pa", "model-a2", "fam_a"),
            "b1": dep("pb", "model-b1", "fam_b"),
            "c1": dep("pc", "model-c1", "fam_c"),
            "v1": dep("pa", "<verify: model-v1>", "fam_a"),
        },
        "roles": {"r": ["a1", "b1", "c1"], **roles},
    }


KEYS = {"PA_KEYS": ["KEY-A0-secret", "KEY-A1-secret"], "PB_KEYS": ["KEY-B0-secret"], "PC_KEYS": ["KEY-C0-secret"]}


class Fake:
    """Scripted transport: ``script[model]`` is a list consumed per call; an Exception instance is raised."""

    def __init__(self, script=None, default=OK):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.default = default
        self.calls = []

    def __call__(self, base_url, api_key, model, messages, max_tokens, timeout_s):
        self.calls.append({"base_url": base_url, "key": api_key, "model": model, "messages": messages,
                           "max_tokens": max_tokens, "timeout_s": timeout_s})
        queue = self.script.get(model)
        item = queue.pop(0) if queue else self.default
        if isinstance(item, Exception):
            raise item
        return TransportResponse(text=item, tokens_in=11, tokens_out=7)

    @property
    def models(self):
        return [c["model"] for c in self.calls]


def make(tmp_path, transport, mode="live", config=None, keys=None, **kw):
    kd = KEYS if keys is None else keys
    kw.setdefault("sleep", lambda s: None)
    return Router(config or cfg(), lambda env: list(kd.get(env, [])), tmp_path / "llm.sqlite", mode,
                  transport=transport, **kw)


def new_ledger(tmp_path):
    return Ledger.create(tmp_path / "run", run_payload(), clock=fixed_clock())


def test_falls_through_chain_on_429(tmp_path):
    fake = Fake({"model-a1": [TransportError(429, "slow down")] * 2})
    res = make(tmp_path, fake).call("r", "t", MSG, Out)
    assert res.parsed.ok is True
    assert (res.model, res.family, res.provider, res.cache_hit) == ("model-b1", "fam_b", "pb", False)
    assert fake.models == ["model-a1", "model-a1", "model-b1"]  # both provider-a keys tried, then next deployment


def test_429_cools_down_key_and_tries_next_key_first(tmp_path):
    now = [100.0]
    fake = Fake({"model-a1": [TransportError(429, "slow")]})
    r = make(tmp_path, fake, clock=lambda: now[0])
    first = r.call("r", "t", MSG, Out)
    assert first.model == "model-a1"  # key 0 limited, key 1 answered in the same deployment
    assert [c["key"] for c in fake.calls] == ["KEY-A0-secret", "KEY-A1-secret"]
    fake.calls.clear()
    r.call("r", "t", [{"role": "user", "content": "other"}], Out)
    assert [c["key"] for c in fake.calls] == ["KEY-A1-secret"]  # key 0 still cooling
    now[0] += 61
    fake.calls.clear()
    r.call("r", "t", [{"role": "user", "content": "third"}], Out)
    assert fake.calls[0]["key"] == "KEY-A0-secret"  # cooldown over, round robin is back on key 0


def test_401_drops_key_for_the_run(tmp_path):
    fake = Fake({"model-a1": [TransportError(401, "bad key")]})
    r = make(tmp_path, fake)
    r.call("r", "t", MSG, Out)
    assert [c["key"] for c in fake.calls] == ["KEY-A0-secret", "KEY-A1-secret"]
    fake.calls.clear()
    for i in range(3):
        r.call("r", "t", [{"role": "user", "content": f"m{i}"}], Out)
    assert {c["key"] for c in fake.calls} == {"KEY-A1-secret"}


def test_skips_verify_placeholder_and_keyless_providers(tmp_path):
    c = cfg(r2=["v1", "b1", "a1"])
    fake = Fake()
    res = make(tmp_path, fake, config=c, keys={"PA_KEYS": ["K"]}).call("r2", "t", MSG, Out)
    assert res.model == "model-a1"  # placeholder v1 and keyless b1 never reach the transport
    assert fake.models == ["model-a1"]
    r = make(tmp_path / "k", Fake(), config=c, keys={})  # fresh cache: a cached reply would still be served
    with pytest.raises(NoDeployment):
        r.call("r2", "t", MSG, Out)
    status = dict(make(tmp_path, Fake(), config=c, keys={"PA_KEYS": ["K"]}).chain_status("r2"))
    assert status["v1"].startswith("skipped") and status["b1"].startswith("skipped") and status["a1"] == "usable"


def test_family_guard_skips_excluded_family(tmp_path):
    fake = Fake()
    r = make(tmp_path, fake)
    res = r.call("r", "t", MSG, Out, exclude_families=frozenset({"fam_a"}))
    assert res.family == "fam_b" and fake.models == ["model-b1"]
    with pytest.raises(NoDeployment):
        r.call("r", "t", MSG, Out, exclude_families=frozenset({"fam_a", "fam_b", "fam_c"}))
    assert r.families_used("r") == {"fam_b"}
    assert r.families_used("never") == set()


def test_demo_override_chain_used_when_demo_true(tmp_path):
    c = cfg(**{"r.demo": ["c1", "a1"]})
    f1 = Fake()
    assert make(tmp_path, f1, config=c, demo=True).call("r", "t", MSG, Out).model == "model-c1"
    f2 = Fake()
    assert make(tmp_path / "x", f2, config=c, demo=False).call("r", "t", MSG, Out).model == "model-a1"


def test_warm_writes_cache_then_second_call_is_cache_hit_without_transport(tmp_path):
    fake = Fake()
    r = make(tmp_path, fake, mode="warm")
    first = r.call("r", "t", MSG, Out, max_tokens=50)
    assert first.cache_hit is False and len(fake.calls) == 1 and fake.calls[0]["max_tokens"] == 50
    again = r.call("r", "t", MSG, Out, max_tokens=50)
    assert again.cache_hit is True and again.parsed.ok is True and again.model == "model-a1"
    assert len(fake.calls) == 1 and r.calls_used == 1
    r.call("r", "t", MSG, Out, max_tokens=51)  # max_tokens is part of the key
    assert len(fake.calls) == 2


def test_replay_reads_cache_and_never_calls_transport(tmp_path):
    make(tmp_path, Fake(), mode="warm").call("r", "t", MSG, Out)
    fake = Fake()
    r = make(tmp_path, fake, mode="replay", keys={})  # replay needs no keys
    res = r.call("r", "t", MSG, Out)
    assert res.cache_hit is True and res.model == "model-a1" and fake.calls == []


def test_replay_cache_miss_raises(tmp_path):
    fake = Fake()
    with pytest.raises(CacheMiss):
        make(tmp_path, fake, mode="replay").call("r", "t", MSG, Out)
    assert fake.calls == []


def test_bad_json_repaired_once_then_next_deployment(tmp_path):
    fake = Fake({"model-a1": ["not json", OK]})
    res = make(tmp_path, fake).call("r", "t", MSG, Out)
    assert res.model == "model-a1" and len(fake.calls) == 2
    repair = fake.calls[1]["messages"]
    assert repair[:1] == MSG and len(repair) == 3 and "not json" in repair[1]["content"]
    assert "JSON" in repair[2]["content"]
    fake2 = Fake({"model-a1": ["nope", "still nope"]})
    res2 = make(tmp_path / "y", fake2).call("r", "t", MSG, Out)
    assert res2.model == "model-b1" and fake2.models == ["model-a1", "model-a1", "model-b1"]


def test_schema_violation_counts_as_bad_json(tmp_path):
    fake = Fake({"model-a1": ['{"ok": "maybe"}', '{"wrong": 1}']})
    assert make(tmp_path, fake).call("r", "t", MSG, Out).model == "model-b1"


def test_fenced_json_is_parsed(tmp_path):
    fake = Fake(default='Here you go:\n```json\n{"ok": true}\n```\nHope that helps.')
    assert make(tmp_path, fake).call("r", "t", MSG, Out).parsed.ok is True
    fake2 = Fake(default='Sure! {"ok": false} -- done')
    assert make(tmp_path / "z", fake2).call("r", "t", MSG, Out).parsed.ok is False


def test_echoed_example_object_before_the_answer_is_skipped(tmp_path):
    fake = Fake(default='Format: {"example": 1}. Answer: {"ok": true}')
    r = make(tmp_path, fake)
    assert r.call("r", "t", MSG, Out).parsed.ok is True
    assert len(fake.calls) == 1  # no repair round needed


def test_budget_exhausted_counts_real_calls_only(tmp_path):
    fake = Fake()
    r = make(tmp_path, fake, mode="warm", call_budget=2)
    r.call("r", "t", MSG, Out)
    r.call("r", "t", MSG, Out)  # cache hit, free
    r.call("r", "t", [{"role": "user", "content": "two"}], Out)
    assert r.calls_used == 2
    with pytest.raises(BudgetExhausted):
        r.call("r", "t", [{"role": "user", "content": "three"}], Out)
    r.call("r", "t", MSG, Out)  # cached answers still flow when the budget is gone
    assert len(fake.calls) == 2


def test_deadline_stops_calls(tmp_path):
    now = [0.0]
    fake = Fake()
    r = make(tmp_path, fake, clock=lambda: now[0], deadline=10.0, per_call_timeout_s=25.0)
    r.call("r", "t", MSG, Out)
    assert fake.calls[0]["timeout_s"] == pytest.approx(10.0)  # never longer than the time left
    now[0] = 10.0
    with pytest.raises(DeadlineExceeded):
        r.call("r", "t", [{"role": "user", "content": "late"}], Out)
    assert len(fake.calls) == 1


def test_per_call_timeout_is_passed_to_transport(tmp_path):
    fake = Fake()
    make(tmp_path, fake, per_call_timeout_s=7.5).call("r", "t", MSG, Out)
    assert fake.calls[0]["timeout_s"] == 7.5


def test_all_providers_failed_lists_attempts(tmp_path):
    boom = TransportError(503, "down KEY-A0-secret")
    fake = Fake({"model-a1": [boom], "model-b1": [TransportError(None, "timeout")], "model-c1": [boom]})
    with pytest.raises(AllProvidersFailed) as ei:
        make(tmp_path, fake).call("r", "t", MSG, Out)
    assert [d for d, _ in ei.value.attempts] == ["a1", "b1", "c1"]
    assert "503" in ei.value.attempts[0][1] and "timeout" in ei.value.attempts[1][1]
    assert "KEY-A0-secret" not in str(ei.value) and isinstance(ei.value, RouterError)


def test_5xx_moves_to_next_deployment_without_burning_second_key(tmp_path):
    fake = Fake({"model-a1": [TransportError(500, "oops")]})
    make(tmp_path, fake).call("r", "t", MSG, Out)
    assert fake.models == ["model-a1", "model-b1"]


def test_llmcall_record_per_attempt_has_no_key_material(tmp_path):
    led = new_ledger(tmp_path)
    fake = Fake({"model-a1": [TransportError(429, "slow KEY-A0-secret"), "garbage", OK]})
    r = make(tmp_path, fake, mode="warm", ledger=led)
    res = r.call("r", "tpl", MSG, Out)
    r.call("r", "tpl", MSG, Out)  # cache hit record
    recs = led.query("LLMCALL")
    p = [e.payload for e in recs]
    assert len(p) == 4
    assert p[0]["error"].startswith("429") and p[0]["key_alias"] == "pa#0" and p[0]["response_hash"] is None
    assert p[1]["error"].startswith("parse") and p[1]["key_alias"] == "pa#1"
    assert p[2]["error"] is None and p[2]["response_hash"] and p[2]["tokens_in"] == 11 and p[2]["tokens_out"] == 7
    assert p[3]["cache_hit"] is True and p[3]["key_alias"] is None
    assert {x["prompt_template"] for x in p} == {"tpl"} and len({x["prompt_hash"] for x in p}) == 1
    assert p[2]["role"] == "r" and p[2]["provider"] == "pa" and p[2]["family"] == "fam_a"
    assert res.llmcall_rid == recs[2].rid and recs[0].author.writer == "router"
    led.close()
    blob = (tmp_path / "run" / "ledger.jsonl").read_text(encoding="utf-8")
    assert "secret" not in blob and "KEY-" not in blob
    assert "secret" not in (tmp_path / "llm.sqlite").read_bytes().decode("latin-1")


def test_complete_json_satisfies_llm_port(tmp_path):
    fake = Fake(default='```json\n{"facts": [{"a": 1}], "n": 2}\n```')
    r = make(tmp_path, fake)
    port: LlmPort = r
    out = port.complete_json("r", "extract things")
    assert out == {"facts": [{"a": 1}], "n": 2}
    assert fake.calls[0]["messages"] == [{"role": "user", "content": "extract things"}]


def test_keyring_round_robin_and_redact():
    ring = KeyRing({"pa": ["k1-aaaaaa", "k2-bbbbbb"]}, clock=lambda: 0.0)
    assert [ring.acquire("pa")[0] for _ in range(4)] == [0, 1, 0, 1]
    text = "failed with k1-aaaaaa and Bearer abc.def and gsk_1234567890abcdef"
    out = redact(text, ring.secrets())
    assert "k1-aaaaaa" not in out and "abc.def" not in out and "gsk_1234567890abcdef" not in out


def test_json_payload_roundtrip_is_plain(tmp_path):
    r = make(tmp_path, Fake(default=json.dumps({"ok": True})))
    assert r.call("r", "t", MSG, Out).parsed == Out(ok=True)
