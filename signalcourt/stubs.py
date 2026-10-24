"""Stub implementations of all seven stages.

Each stub writes schema-valid records under the right Author, so later stages and verify_run work
unchanged. Free text is "STUB" or digit-free, so the unbacked-numbers gate never fires. A sprint that
makes a stage real registers ``real`` elsewhere; nothing here is deleted.
"""

from __future__ import annotations

from html import escape

from signalcourt.ledger import Author, RecordType
from signalcourt.ledger.schema import (
    ChallengePayload,
    ClaimPayload,
    DrafterRef,
    FactPayload,
    Falsification,
    FindingElements,
    FindingPayload,
    SignalPayload,
    TextItem,
    VerdictPayload,
)
from signalcourt.pipeline import STAGE_ORDER, RunContext, register

STUB = "STUB"
ELEMENTS = ("discovery", "why", "evidence", "timeline", "numbers", "counterargument", "falsification", "strength")
LIST_ELEMENTS = {"why", "evidence", "timeline", "numbers"}


def _entity(ctx: RunContext) -> str:
    return ctx.symbol or ctx.company


@register("harvest", "stub")
def harvest(ctx: RunContext) -> None:
    ctx.subtype = ctx.subtype or "bank"  # the real harvest reads it from the filing format
    ctx.doc_shas = []


@register("measure", "stub")
def measure(ctx: RunContext) -> None:
    fact = FactPayload(
        entity=_entity(ctx), metric="stub", value=0.0, unit="stub", period_end=ctx.as_of.isoformat(),
        basis="standalone", span_rid="stub", extractor="dataset", value_as_written="stub",
        recon_status="single_source",
    )
    ctx.ledger.append(RecordType.FACT, fact, Author.code("extract"))


@register("sense", "stub")
def sense(ctx: RunContext) -> None:
    facts = [e.rid for e in ctx.ledger.query(RecordType.FACT)]
    sig = SignalPayload(
        sensor="stub", entity=_entity(ctx), metrics=["stub"], direction="none", specificity_source="na", inputs=facts
    )
    ctx.signals.append(ctx.ledger.append(RecordType.SIGNAL, sig, Author.code("sensors")).rid)


@register("prereg", "stub")
def prereg(ctx: RunContext) -> None:
    if not ctx.signals:
        return
    claim = ClaimPayload(
        hypothesis_index=0, statement=STUB, mechanism=STUB, signal_rid=ctx.signals[0],
        falsification=Falsification(
            metric="stub", comparator="none", threshold=0.0, deadline_period=ctx.as_of.isoformat(), source_rule="stub"
        ),
        drafter=DrafterRef(model="stub", family="code"),
    )
    ctx.claims.append(ctx.ledger.append(RecordType.CLAIM, claim, Author.code("prereg")).rid)
    ctx.ledger.seal_head("prereg_head")


@register("court", "stub")
def court(ctx: RunContext) -> None:
    for claim_rid in ctx.claims:
        ch = ChallengePayload(claim_rid=claim_rid, alt_type="stub", argument=STUB, severity="minor")
        env = ctx.ledger.append(RecordType.CHALLENGE, ch, Author(component="court", role="redteam"))
        ctx.court.setdefault(claim_rid, []).append(env.rid)


@register("judge", "stub")
def judge(ctx: RunContext) -> None:
    for claim_rid in ctx.claims:
        v = VerdictPayload(claim_rid=claim_rid, grade="Weak", reasons=["stub judge"])
        ctx.ledger.append(RecordType.VERDICT, v, Author.code("judge"))
        ctx.verdicts[claim_rid] = v.grade


@register("report", "stub")
def report(ctx: RunContext) -> None:
    item = TextItem(text=STUB)
    elements = FindingElements(**{e: [item] if e in LIST_ELEMENTS else item for e in ELEMENTS})
    footer = {"run_id": ctx.ledger.run_id, "impl": dict(ctx.impl)}
    for claim_rid in ctx.claims:
        fd = FindingPayload(claim_rid=claim_rid, elements=elements, footer=footer, writer="template")
        ctx.findings.append(ctx.ledger.append(RecordType.FINDING, fd, Author.code("writer")).rid)
    ctx.ledger.seal_head("run_head")
    ctx.card_path = ctx.run_dir / "finding.html"
    ctx.card_path.write_text(render_stub_card(ctx), encoding="utf-8")


def render_stub_card(ctx: RunContext) -> str:
    """Minimal standalone card: eight STUB elements and a footer saying which implementation ran."""
    heads = ctx.ledger.heads()
    sections = "\n".join(f"<section><h2>{e.capitalize()}</h2><p>{STUB}</p></section>" for e in ELEMENTS)
    rows = "\n".join(f"<tr><td>{s}</td><td>{escape(ctx.impl.get(s, 'stub'))}</td></tr>" for s in STAGE_ORDER)
    prereg_head = heads.get("prereg_head", (0, "none"))[1]
    run_head = heads.get("run_head", (0, "none"))[1]
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Signal Court finding</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 760px; margin: 2rem auto; padding: 0 1rem; color: #111; }}
h1 {{ font-size: 1.4rem; }} h2 {{ font-size: 1rem; margin: 1.2rem 0 0.3rem; }}
footer {{ margin-top: 2rem; border-top: 1px solid #ccc; padding-top: 1rem; font-size: 0.85rem; }}
td {{ padding: 0.1rem 0.8rem 0.1rem 0; }} code {{ word-break: break-all; }}
</style>
</head>
<body>
<h1>{escape(ctx.company)}</h1>
{sections}
<footer>
<table>
<tr><th>stage</th><th>impl</th></tr>
{rows}
</table>
<p>run <code>{escape(ctx.ledger.run_id or "")}</code></p>
<p>prereg_head <code>{prereg_head}</code></p>
<p>run_head <code>{run_head}</code></p>
</footer>
</body>
</html>
"""
