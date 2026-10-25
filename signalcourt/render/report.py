"""Rebuild a self-contained research note from the saved evidence ledger."""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from ..calc.panel import Panel
from ..judge.grade import admissible
from ..ledger import Ledger
from ..pipeline import STAGE_ORDER
from ..settings import load_config, load_registry
from .records import RecordLinks, day, label, number, period

GRADE_ORDER = {"Strong": 0, "Moderate": 1, "Weak": 2, "Killed": 3}
STAGE_TYPES = {
    "harvest": {"ENTITY", "DOC", "SPAN"},
    "measure": {"FACT", "CALC"},
    "sense": {"SIGNAL"},
    "prereg": {"PLAN", "CLAIM"},
    "court": {"EVIDENCE", "CHALLENGE", "REBUTTAL", "LLMCALL", "GATE", "REJECT"},
    "judge": {"VERDICT"},
    "report": {"FINDING"},
}


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _load_panel(run_dir, run, entity, notes):
    if not (run_dir / "panel.parquet").exists():
        notes.append("The saved metric panel is unavailable; source records remain accessible below.")
        return None
    try:
        df = pd.read_parquet(run_dir / "panel.parquet")
        target = entity.get("symbol") or run.get("symbol")
        peers = [str(e) for e in df.entity.unique() if e != target]
        return Panel(df, target, peers, date.fromisoformat(run["as_of"]))
    except (OSError, ValueError, KeyError, AttributeError) as exc:
        notes.append(f"The saved metric panel could not be read ({type(exc).__name__}).")
        return None


def _fallback_elements(signal, claim, verdict, prereg_stub):
    p = signal.payload if signal else {}
    metric = label((p.get("metrics") or ["metric"])[0])
    baseline = {"twin": "its synthetic twin", "peer_median": "the peer median"}.get(
        p.get("specificity_source"), "its own history"
    )
    summary = (
        f"{metric} moved {p.get('direction', 'away')} against {baseline} from {period(p.get('break_period'))}."
        if signal
        else "No signal cleared the bar"
    )
    if signal and claim is None:
        summary = "No signal cleared the bar. Strongest unclaimed candidate: " + summary
    rids = [signal.rid] if signal else []
    item = lambda text: {"text": text, "rids": rids}  # noqa: E731
    cp = claim.payload if claim else {}
    fals = cp.get("falsification", {})
    kill = "The hypothesis and kill test were not pre-registered in this run."
    if claim and not prereg_stub:
        units = {m["id"]: m.get("unit", "") for m in load_registry("metrics.yaml")}
        kill = (
            f"This hypothesis is falsified if {label(fals.get('metric', 'metric'))} is "
            f"{fals.get('comparator', '')} {number(fals.get('threshold'), units.get(fals.get('metric'), ''))} by "
            f"{period(fals.get('deadline_period'))}."
        )
    why = [
        item(t)
        for t in (
            "The detector compares the company's financial history with an explicit baseline.",
            "An unusual movement is a candidate for investigation; it does not establish its cause.",
            "Independent sources and alternative explanations determine how much weight the claim deserves.",
        )
    ]
    return {
        "discovery": item(summary),
        "why": why,
        "evidence": [],
        "timeline": [],
        "numbers": [],
        "counterargument": item("No substantive challenge has been answered with admissible evidence."),
        "falsification": {"text": kill, "rids": [claim.rid] if claim else []},
        "strength": item(verdict.payload["grade"] if verdict else "No evidence grade is available."),
    }


def render_report(run_dir: Path, *, store=None) -> str:
    """Read a saved run and return HTML without appending records or seals."""
    run_dir = Path(run_dir).resolve()
    notes = []
    meta = _read_json(run_dir / "report-meta.json")
    settings = meta.get("settings") or load_config().model_dump()
    with Ledger.open(run_dir) as ledger:
        records = list(ledger.iter_records())
        heads = ledger.heads()
        links = RecordLinks(records, store)
        groups = {t: [e for e in records if e.type.value == t] for t in {e.type.value for e in records}}
        run = groups.get("RUN", [])[0].payload
        entity = groups["ENTITY"][0].payload if groups.get("ENTITY") else {}
        panel = _load_panel(run_dir, run, entity, notes)
        claims = groups.get("CLAIM", [])
        verdicts = {v.payload["claim_rid"]: v for v in groups.get("VERDICT", [])}
        findings = {
            f.payload["claim_rid"]: f
            for f in groups.get("FINDING", [])
            if f.payload.get("elements", {}).get("discovery", {}).get("text", "").upper() != "STUB"
        }

        def score(c):
            sig = links.by_rid.get(c.payload.get("signal_rid"))
            return float((sig.payload.get("surprise") or {}).get("score") or 0) if sig else 0

        claims = sorted(
            claims,
            key=lambda c: (GRADE_ORDER.get(verdicts[c.rid].payload["grade"], 4) if c.rid in verdicts else 4, -score(c)),
        )
        claim = claims[0] if claims else None
        verdict = verdicts.get(claim.rid) if claim else None
        signal = links.by_rid.get(claim.payload["signal_rid"]) if claim else None
        if signal is None and groups.get("SIGNAL"):
            signal = max(groups["SIGNAL"], key=lambda s: (s.payload.get("surprise") or {}).get("score") or 0)
        finding = findings.get(claim.rid) if claim else None
        old_footer = next((f.payload.get("footer", {}) for f in reversed(groups.get("FINDING", []))), {})
        impl = {s: "unknown" for s in STAGE_ORDER}
        impl.update(old_footer.get("impl", {}))
        impl.update(meta.get("impl", {}))
        cp, sp = claim.payload if claim else {}, signal.payload if signal else {}
        prereg_stub = (
            not claim
            or cp.get("statement", "").strip().upper() == "STUB"
            or cp.get("falsification", {}).get("comparator") == "none"
            or impl["prereg"] == "stub"
        )
        court_records = groups.get("CHALLENGE", [])
        court_stub = impl["court"] == "stub" or any(
            c.payload.get("alt_type") == "stub" or c.payload.get("argument", "").strip().upper() == "STUB"
            for c in court_records
        )
        elements = finding.payload["elements"] if finding else _fallback_elements(signal, claim, verdict, prereg_stub)
        evidence_overview = [
            item
            for item in elements.get("evidence", [])
            if "https://" not in item["text"] and "http://" not in item["text"]
        ]
        title = (entity.get("legal_names") or [run.get("company_input", "Research note")])[0]
        metric_id = (sp.get("metrics") or [""])[0]
        metrics = {m["id"]: m for m in load_registry("metrics.yaml")}
        unit = metrics.get(metric_id, {}).get("unit", "")
        from .chart import render_chart

        chart = render_chart(panel, sp, settings, metric_label=label(metric_id), unit=unit) if signal else ""
        signal_records = {e.rid for e in ledger.closure(signal.rid)} if signal else set()
        facts = [
            e
            for e in groups.get("FACT", [])
            if e.payload.get("entity") == entity.get("symbol", run.get("symbol"))
            and e.payload.get("metric") == metric_id
            and (not signal or e.rid in signal_records)
        ]
        facts.sort(key=lambda e: e.payload.get("period_end", ""))
        evidence = {}
        relevant_doc_ids = {doc.rid for fact in facts if (doc := links.source(fact)) is not None}
        relevant_doc_ids.update(
            rid for rid in sp.get("inputs", []) if rid in links.by_rid and links.by_rid[rid].type.value == "DOC"
        )
        for ev in groups.get("EVIDENCE", []):
            doc = links.source(ev)
            if doc is not None:
                relevant_doc_ids.add(doc.rid)
        for doc in groups.get("DOC", []):
            if links.eligible(doc) and (not signal or doc.rid in relevant_doc_ids):
                evidence.setdefault(doc.payload["source_family"], []).append(links.doc_view(doc))
        court_evidence = []
        for ev in groups.get("EVIDENCE", []):
            if claim and ev.payload["claim_rid"] != claim.rid:
                continue
            span = links.by_rid.get(ev.payload.get("span_rid"))
            doc = links.source(span)
            if doc is not None and not links.eligible(doc):
                notes.append(f"Court evidence {ev.rid} was excluded because its source failed the as-of cutoff.")
                continue
            court_evidence.append(
                {
                    "rid": ev.rid,
                    "sentence": ev.payload["sentence"],
                    "quote": span.payload["text"] if span else None,
                    "span_rid": span.rid if span else None,
                    "doc": links.doc_view(doc) if doc else None,
                    "stance": ev.payload["stance"],
                    "family": label(ev.payload["source_family"]),
                }
            )
        challenges = [
            c
            for c in court_records
            if claim
            and c.payload["claim_rid"] == claim.rid
            and c.payload.get("alt_type") != "stub"
            and c.payload.get("argument") != "STUB"
        ]
        challenges.sort(key=lambda c: c.payload["severity"] != "substantive")
        challenge = challenges[0] if challenges else None
        rebuttals = [
            r
            for r in groups.get("REBUTTAL", [])
            if challenge and r.payload["challenge_rid"] == challenge.rid and r.payload.get("argument") != "STUB"
        ]
        rebuttal = next(
            (r for r in rebuttals if admissible(ledger, r.payload)[0]), rebuttals[-1] if rebuttals else None
        )
        admissibility = admissible(ledger, rebuttal.payload) if rebuttal else (False, "No rebuttal was recorded.")
        alternatives = cp.get("alt_checklist") or [a["id"] for a in load_registry("alternatives.yaml")]
        tiles = [
            {"label": "Gap from baseline", "value": number(sp.get("size"), unit)},
            {"label": "Robust deviation", "value": number(sp.get("z"), "", 2)},
            {
                "label": "Placebo rank",
                "value": (
                    f"{sp['placebo'].get('rank') or 'Unavailable'} of {sp['placebo']['n']}"
                    if sp.get("placebo")
                    else "Unavailable"
                ),
            },
            {"label": "Persistence · quarters", "value": number(sp.get("persistence_q"), "count")},
        ]
        timeline = list(elements.get("timeline", []))
        if not timeline:
            for f in facts:
                timeline.append(
                    {
                        "text": f"{f.payload['period_end']} · {label(metric_id)}: "
                        f"{number(f.payload['value'], f.payload['unit'])}",
                        "rids": [f.rid],
                    }
                )
            if claim:
                timeline.append(
                    {
                        "text": f"{day(claim.created_at)} · "
                        + (
                            "Placeholder claim recorded; no valid pre-registration."
                            if prereg_stub
                            else "Hypothesis recorded."
                        ),
                        "rids": [claim.rid],
                    }
                )
        claimed_signals = {c.payload["signal_rid"] for c in claims}
        others = [
            {
                "rid": c.rid,
                "kind": "Hypothesis",
                "text": (
                    c.payload["statement"] if c.payload["statement"] != "STUB" else "Hypothesis not pre-registered"
                ),
                "grade": verdicts[c.rid].payload["grade"] if c.rid in verdicts else "Not graded",
                "score": score(c),
                "signal_rid": c.payload["signal_rid"],
            }
            for c in claims
            if c != claim
        ]
        others += [
            {
                "rid": s.rid,
                "kind": "Unclaimed signal",
                "text": label((s.payload.get("metrics") or ["signal"])[0]),
                "grade": "Not pre-registered",
                "score": (s.payload.get("surprise") or {}).get("score"),
                "signal_rid": s.rid,
            }
            for s in groups.get("SIGNAL", [])
            if s.rid not in claimed_signals
        ]
        stage_rows = [
            {
                "name": s,
                "impl": impl[s],
                "kind": (
                    "Code"
                    if s in ("harvest", "measure", "sense", "judge")
                    else "Code template"
                    if s == "report" or (s == "prereg" and cp.get("drafter", {}).get("family") == "code")
                    else "LLM"
                ),
                "records": sum(e.type.value in STAGE_TYPES[s] for e in records),
            }
            for s in STAGE_ORDER
        ]
        # Keep the appendix to cited records and their ancestors, rather than every measured metric.
        cited = {e.rid for e in records if e.type.value not in {"FACT", "SPAN", "CALC", "DOC"}}
        cited.update(doc["rid"] for docs in evidence.values() for doc in docs)
        cited.update(rid for item in timeline for rid in item.get("rids", []))
        ancestors = {}
        for rid in cited:
            for record in ledger.closure(rid):
                ancestors[record.rid] = record
        appendix = []
        for record in sorted(ancestors.values(), key=lambda r: r.seq):
            doc = links.source(record)
            appendix.append(
                {
                    "env": record,
                    "description": links.description(record).replace("STUB", "Not implemented"),
                    "value": number(record.payload.get("value"), record.payload.get("unit", ""))
                    if record.type.value in ("FACT", "CALC") and (doc is None or links.eligible(doc))
                    else "",
                    "eligible": doc is None or links.eligible(doc),
                    "inputs": list(
                        dict.fromkeys(
                            r
                            for r in [
                                *record.payload.get("inputs", []),
                                *record.parents,
                                record.payload.get("span_rid"),
                                record.payload.get("signal_rid"),
                                record.payload.get("claim_rid"),
                                record.payload.get("challenge_rid"),
                                *record.payload.get("evidence", []),
                                *record.payload.get("calc", []),
                            ]
                            if r in links.by_rid
                        )
                    ),
                    "doc": links.doc_view(doc) if doc else None,
                }
            )
        scan = {
            "metrics": panel.metrics(panel.target)
            if panel
            else sorted({f.payload["metric"] for f in groups.get("FACT", [])}),
            "peers": len(entity.get("peers", [])),
            "baseline": sp.get("specificity_source", "not recorded"),
            "notes": (sp.get("surprise") or {}).get("coverage_status") or "Baseline details not recorded.",
        }
        if not finding and claim:
            notes.append(
                "This saved run has no completed research finding; the page shows the recorded signal and verdict."
            )
        if not meta.get("settings"):
            notes.append("Original run settings were not saved; chart reconstruction uses the current configuration.")
        if any(e.type.value == "CALC" and e.payload.get("formula_id") == "report_projection" for e in records):
            notes.append(
                "Report projection CALCs extract saved signal statistics, thresholds and source counts for citation. "
                "They do not independently recompute the detector or establish source reconciliation."
            )
        view = dict(
            title=title,
            symbol=entity.get("symbol") or run.get("symbol") or "Unresolved",
            subtype=label(entity.get("subtype") or meta.get("subtype") or "financial company"),
            run=run,
            run_id=ledger.run_id,
            as_of=day(run.get("as_of")),
            generated=datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y · %H:%M IST"),
            run_dir=str(run_dir),
            grade=verdict.payload["grade"] if verdict else "No finding",
            claim=claim,
            signal=signal,
            finding=finding,
            verdict=verdict,
            elements=elements,
            tiles=tiles,
            chart=Markup(chart),
            court_stub=court_stub,
            prereg_stub=prereg_stub,
            court_evidence=court_evidence,
            challenge=challenge,
            rebuttal=rebuttal,
            admissibility=admissibility,
            alternatives=alternatives,
            evidence=evidence,
            evidence_overview=evidence_overview,
            timeline=timeline,
            checks=verdict.payload.get("checks", {}) if verdict else {},
            others=others,
            scan=scan,
            rejections=groups.get("REJECT", []),
            killed=[v for v in groups.get("VERDICT", []) if v.payload["grade"] == "Killed"],
            stops=groups.get("STOP", []),
            stages=stage_rows,
            counts=Counter(e.type.value for e in records),
            heads=heads,
            notes=notes + meta.get("notes", []),
            appendix=appendix,
            records=len(records),
        )
    root = Path(__file__).parent
    env = Environment(loader=FileSystemLoader(str(root / "templates")), autoescape=select_autoescape(["j2", "html"]))
    env.globals.update(link=links.link, rich=links.text, label=label, number=number, period=period, day=day)
    return env.get_template("report.html.j2").render(
        **view,
        css=(root / "static" / "report.css").read_text(encoding="utf-8"),
        js=(root / "static" / "report.js").read_text(encoding="utf-8"),
    )
