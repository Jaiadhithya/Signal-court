import re

from conftest import make_ctx

from signalcourt import stubs  # noqa: F401  (registers the stub stages)
from signalcourt.ledger import verify_run
from signalcourt.pipeline import STAGE_ORDER, run_pipeline

ALL_STUBS = {s: "stub" for s in STAGE_ORDER}


def test_all_stubs_produce_a_verifiable_ledger(tmp_path):
    ctx = make_ctx(tmp_path, impl=ALL_STUBS)
    run_pipeline(ctx)
    types = [e.type for e in ctx.ledger.iter_records()]
    assert types[0] == "RUN" and "CLAIM" in types and "FINDING" in types
    ctx.ledger.close()
    rep = verify_run(ctx.run_dir)
    assert rep.ok, rep.problems
    assert (ctx.run_dir / "finding.html").exists()


def test_stub_records_types_authors_and_heads(tmp_path):
    ctx = make_ctx(tmp_path, impl=ALL_STUBS)
    run_pipeline(ctx)
    led = ctx.ledger
    assert [e.type.value for e in led.iter_records()] == [
        "RUN", "FACT", "SIGNAL", "CLAIM", "CHALLENGE", "VERDICT", "FINDING"
    ]
    assert [(r.stage, r.impl, r.records) for r in ctx.stage_runs] == [
        ("harvest", "stub", 0), ("measure", "stub", 1), ("sense", "stub", 1), ("prereg", "stub", 1),
        ("court", "stub", 1), ("judge", "stub", 1), ("report", "stub", 1),
    ]
    ch = led.query("CHALLENGE")[0]
    assert ch.author.role == "redteam" and ch.payload["severity"] == "minor"
    assert led.query("SIGNAL")[0].payload["specificity_source"] == "na"
    assert led.query("VERDICT")[0].payload["grade"] == "Weak"
    heads = led.heads()
    assert set(heads) == {"prereg_head", "run_head"}
    assert heads["run_head"][1] == led.head()[1]
    assert ctx.claims and ctx.verdicts and ctx.findings and ctx.card_path == ctx.run_dir / "finding.html"
    led.close()


def test_stub_free_text_has_no_digits(tmp_path):
    ctx = make_ctx(tmp_path, impl=ALL_STUBS)
    run_pipeline(ctx)
    fd = ctx.ledger.query("FINDING")[0].payload["elements"]
    texts = [v["text"] for v in fd.values() if isinstance(v, dict)]
    texts += [i["text"] for v in fd.values() if isinstance(v, list) for i in v]
    assert texts and all(t == "STUB" for t in texts)
    for rtype, keys in {"CLAIM": ("statement", "mechanism"), "CHALLENGE": ("argument",), "VERDICT": ("reasons",)}.items():
        p = ctx.ledger.query(rtype)[0].payload
        for k in keys:
            assert not re.search(r"\d", str(p[k])), (rtype, k)
    ctx.ledger.close()


def test_card_shows_eight_stub_elements_and_footer(tmp_path):
    ctx = make_ctx(tmp_path, impl=ALL_STUBS)
    run_pipeline(ctx)
    html = (ctx.run_dir / "finding.html").read_text(encoding="utf-8")
    for h in ("Discovery", "Why", "Evidence", "Timeline", "Numbers", "Counterargument", "Falsification", "Strength"):
        assert f">{h}<" in html
    assert html.count("STUB") >= 8
    heads = ctx.ledger.heads()
    assert heads["prereg_head"][1] in html and heads["run_head"][1] in html
    assert ctx.ledger.run_id in html
    for s in STAGE_ORDER:
        assert f"<td>{s}</td>" in html
    ctx.ledger.close()
