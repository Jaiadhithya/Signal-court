"""Write admitted findings, seal the run, and render the saved research report."""

from __future__ import annotations

import json
import time

from ..ledger import Author, RecordType
from ..ledger.schema import StopPayload
from ..pipeline import RunContext, register


@register("report", "real")
def report(ctx: RunContext) -> None:
    """Report judged claims; optional missing inputs are documented beside the ledger."""
    started = time.perf_counter()
    from ..render import render_report
    from ..writer import append_finding

    cap = float(ctx.settings.get("demo", {}).get("report_cap_s", 15))
    notes: list[str] = []
    if not (ctx.run_dir / "panel.parquet").is_file():
        notes.append("Saved panel unavailable; the report will use ledger data where possible.")
    if ctx.store is None:
        notes.append("Local document store unavailable; public source links remain available.")

    judged = {record.payload["claim_rid"] for record in ctx.ledger.query(RecordType.VERDICT)}
    ctx.findings = []
    claims = list(dict.fromkeys(ctx.claims))
    for index, claim_rid in enumerate(claims):
        if claim_rid not in judged:
            notes.append(f"Hypothesis {claim_rid} has no verdict; no finding was written for it.")
            continue
        if time.perf_counter() - started >= cap:
            notes.append("The report time cap was reached; remaining hypotheses were not written.")
            ctx.ledger.append(
                RecordType.STOP,
                StopPayload(
                    reason="time_cap",
                    calls_used=0,
                    tools_used=0,
                    elapsed_s=round(time.perf_counter() - started, 3),
                    open_items=claims[index:],
                ),
                Author.code("scheduler"),
            )
            break
        finding_rid = append_finding(
            ctx.ledger,
            claim_rid,
            panel=ctx.panel,
            settings=ctx.settings,
            impl=ctx.impl,
        )
        if finding_rid is not None:
            ctx.findings.append(finding_rid)
        else:
            notes.append(f"Hypothesis {claim_rid} did not pass the finding text gate.")

    metadata = {
        "impl": dict(ctx.impl),
        "settings": ctx.settings,
        "subtype": ctx.subtype,
        "peers": ctx.peers,
        "notes": notes,
        "report_seconds": time.perf_counter() - started,
    }
    meta_path = ctx.run_dir / "report-meta.json"
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    ctx.ledger.seal_head("run_head")
    html = render_report(ctx.run_dir, store=ctx.store)
    report_path = ctx.run_dir / "report.html"
    report_path.write_text(html, encoding="utf-8")
    (ctx.run_dir / "finding.html").write_text(html, encoding="utf-8")
    ctx.card_path = report_path
    metadata["report_seconds"] = time.perf_counter() - started
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    for note in notes:
        ctx.on_event("stage_note", {"stage": "report", "note": note})
