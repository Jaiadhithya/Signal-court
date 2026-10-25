"""Writer contracts exercised with an actual append-only ledger and independent figures."""

from datetime import date

import pandas as pd
import pytest
from conftest import fixed_clock, run_payload

from signalcourt import writer
from signalcourt.calc import periods as P
from signalcourt.calc.panel import Panel
from signalcourt.gates.schema_gate import check_finding_text
from signalcourt.gates.unbacked import cited_values_from_ledger, find_unbacked
from signalcourt.ledger import Author, Ledger


class WriterRun:
    def __init__(self, tmp_path, *, stub=False, unit="inr_cr", metric="gnpa_amt", corp=None):
        self.ledger = Ledger.create(tmp_path / "writer-run", run_payload(), clock=fixed_clock())
        self.entity = self.ledger.append(
            "ENTITY",
            {
                "symbol": "TESTCO",
                "legal_names": ["Test Finance Limited"],
                "subtype": "bank",
                "subtype_evidence": {},
                "peers": [{"symbol": "PEER", "subtype": "bank"}],
                "peer_rule": "same subtype",
            },
            Author.code("resolve"),
        )
        self.docs, self.facts, rows = [], [], []
        for i, (period, value) in enumerate((("Q1FY26", 100.0), ("Q2FY26", 120.0), ("Q3FY26", 140.0))):
            end = P.end(period).isoformat()
            doc = self.ledger.append(
                "DOC",
                {
                    "sha256": str(i) * 64,
                    "url": f"https://example.test/{i}",
                    "final_url": f"https://example.test/{i}",
                    "doc_type": "results_xbrl",
                    "source_id": "exchange",
                    "source_family": "exchange_filing",
                    "origin_id": str(i),
                    "entity_symbol": "TESTCO",
                    "period_end": end,
                    "publish_date": end,
                    "time_gate": "ok",
                },
                Author.code("harvest"),
            )
            span = self.ledger.append(
                "SPAN",
                {
                    "doc_sha256": doc.payload["sha256"],
                    "page": 0,
                    "kind": "xbrl",
                    "text": f"Value {value:g}",
                    "norm_text": f"Value {value:g}",
                },
                Author.code("quote_gate"),
                [doc.rid],
            )
            fact = self.ledger.append(
                "FACT",
                {
                    "entity": "TESTCO",
                    "metric": metric,
                    "value": value,
                    "unit": unit,
                    "period_end": end,
                    "basis": "standalone",
                    "span_rid": span.rid,
                    "extractor": "xbrl",
                    "value_as_written": str(value),
                    "recon_status": "single_source",
                },
                Author.code("extract"),
                [span.rid],
            )
            self.docs.append(doc)
            self.facts.append(fact)
            rows.append(
                {
                    "entity": "TESTCO",
                    "metric": metric,
                    "period": period,
                    "pidx": P.index(period),
                    "value": value,
                    "rid": fact.rid,
                    "basis": "standalone",
                    "published": date.fromisoformat(end),
                    "tier": 1,
                    "recon_status": "single_source",
                }
            )
        self.peer_doc = self.ledger.append(
            "DOC",
            {
                "sha256": "f" * 64,
                "url": "https://example.test/peers",
                "final_url": "https://example.test/peers",
                "doc_type": "peer_panel",
                "source_id": "measure",
                "source_family": "peer_filing",
                "origin_id": "peer-panel",
                "time_gate": "ok",
            },
            Author.code("harvest"),
        )
        rows.append({**rows[0], "entity": "PEER", "rid": self.peer_doc.rid})
        self.panel = Panel(pd.DataFrame(rows), "TESTCO", ["PEER"], date(2026, 10, 1))
        self.signal = self.ledger.append(
            "SIGNAL",
            {
                "sensor": "gap",
                "entity": "TESTCO",
                "metrics": [metric],
                "direction": "up",
                "break_period": "Q2FY26",
                "size": 20.0,
                "z": 2.6,
                "persistence_q": 2,
                "specificity": 0.8,
                "specificity_source": "peer_median",
                "inputs": [f.rid for f in self.facts],
                "placebo": {"p_raw": 0.04, "n": 40, "rank": 2},
                "surprise": {"score": 4.0, "coverage_status": corp or "coverage not searched"},
            },
            Author.code("sensors"),
            [f.rid for f in self.facts],
        )
        self.claim = self.ledger.append(
            "CLAIM",
            {
                "hypothesis_index": 0,
                "statement": "STUB" if stub else "Asset quality diverged.",
                "mechanism": "STUB" if stub else "A sustained asset-quality shift.",
                "signal_rid": self.signal.rid,
                "falsification": {
                    "metric": metric,
                    "comparator": "none" if stub else "<=",
                    "threshold": 105.0,
                    "deadline_period": "Q1FY27",
                    "source_rule": "last pre-break level plus band",
                },
                "alt_checklist": ["sector_shock", "base_effect"],
                "drafter": {"model": "m", "family": "f"},
            },
            Author.code("prereg"),
            [self.signal.rid],
        )
        self.ledger.seal_head("prereg_head")
        if stub:
            self.ledger.append(
                "CHALLENGE",
                {"claim_rid": self.claim.rid, "alt_type": "stub", "argument": "STUB", "severity": "minor"},
                Author(component="court", role="redteam"),
            )
        else:
            self.evidence = self.ledger.append(
                "EVIDENCE",
                {
                    "claim_rid": self.claim.rid,
                    "span_rid": self.facts[-1].payload["span_rid"],
                    "sentence": "The reported value reached 140 cr.",
                    "source_family": "exchange_filing",
                    "origin_id": "target-filing",
                    "stance": "support",
                },
                Author(component="court", role="investigator"),
                [self.facts[-1].rid],
            )
            self.challenge = self.ledger.append(
                "CHALLENGE",
                {
                    "claim_rid": self.claim.rid,
                    "alt_type": "one_off",
                    "argument": "A temporary item may explain it.",
                    "severity": "substantive",
                },
                Author(component="court", role="redteam"),
            )
            self.rebuttal = self.ledger.append(
                "REBUTTAL",
                {
                    "challenge_rid": self.challenge.rid,
                    "argument": "The later filing retains the shift.",
                    "evidence": [self.evidence.rid],
                    "admissible": False,
                    "admissible_reason": "self flag",
                },
                Author(component="court", role="advocate"),
            )
        self.verdict = self.ledger.append(
            "VERDICT",
            {
                "claim_rid": self.claim.rid,
                "grade": "Weak",
                "checks": {
                    "support_families": {"value": 1, "need": 3, "pass": False},
                    "p_raw": {"value": 0.04, "need": 0.1, "pass": True},
                },
                "stat": {"p_raw": 0.04, "n_placebo": 40},
                "reasons": ["Only 1 source family supports the claim; 3 are needed."],
            },
            Author.code("judge"),
            [self.claim.rid, self.signal.rid],
        )
        self.settings = {"writer": {"discovery_max_words": 35, "why_bullets": [3, 5], "timeline_min_events": 3}}
        self.impl = {"prereg": "stub" if stub else "real", "court": "stub" if stub else "real", "judge": "real"}

    def finding(self):
        return writer.write_finding(
            self.ledger, self.claim.rid, panel=self.panel, settings=self.settings, impl=self.impl
        )


@pytest.fixture
def full_run(tmp_path):
    run = WriterRun(tmp_path)
    yield run
    run.ledger.close()


def text_items(finding):
    for value in finding.elements.model_dump().values():
        yield from value if isinstance(value, list) else [value]


def test_finding_passes_check_finding_text_for_a_full_run(full_run):
    finding = full_run.finding()
    assert finding.writer == "template"
    assert check_finding_text(full_run.ledger, finding.model_dump()) is None
    assert "Test Finance" in finding.elements.discovery.text
    assert "gross NPA" in finding.elements.discovery.text
    assert "20.0 cr" in finding.elements.discovery.text
    assert "peer median" in finding.elements.discovery.text


def test_every_number_in_every_text_item_is_backed_by_its_rids(full_run):
    for item in text_items(full_run.finding()):
        assert not find_unbacked(item["text"], cited_values_from_ledger(full_run.ledger, item["rids"]))


def test_discovery_respects_word_limit(full_run):
    full_run.settings["writer"]["discovery_max_words"] = 18
    assert len(full_run.finding().elements.discovery.text.split()) <= 18


def test_why_has_three_to_five_bullets(full_run):
    why = full_run.finding().elements.why
    assert 3 <= len(why) <= 5
    assert any("2 of 40" in item.text and "0.04" in item.text for item in why)
    assert any("2 quarters" in item.text for item in why)
    assert any("peer median" in item.text for item in why)


def test_timeline_has_at_least_three_dated_events(full_run):
    timeline = full_run.finding().elements.timeline
    assert len(timeline) >= 3
    assert all(item.text[:10].count("-") == 2 for item in timeline)
    assert [item.text[:10] for item in timeline] == sorted(item.text[:10] for item in timeline)
    assert any("last pre-break" in item.text.lower() for item in timeline)
    assert any("break quarter" in item.text.lower() for item in timeline)
    assert any("latest quarter" in item.text.lower() for item in timeline)


def test_evidence_has_a_separate_entry_per_filing_and_hashed_peers(full_run):
    items = full_run.finding().elements.evidence
    for doc in full_run.docs:
        assert any(doc.rid in item.rids and doc.payload["final_url"] in item.text for item in items)
    assert any("3 quarterly XBRL filings" in item.text for item in items)
    assert any("1 peer companies" in item.text and "hashed" in item.text for item in items)
    assert any(full_run.evidence.rid in item.rids for item in items)


def test_strength_includes_checks_reasons_and_code_confidence(full_run):
    text = full_run.finding().elements.strength.text
    assert "Weak" in text and "support_families" in text and "0.04" in text
    assert "Only 1 source family" in text
    assert "No model was asked how confident it is" in text


def test_rebuttal_admissibility_uses_judge_rules_instead_of_self_flag(full_run):
    text = full_run.finding().elements.counterargument.text.lower()
    assert "temporary item" in text and "later filing" in text
    assert "judge" in text and "admissible" in text
    assert "inadmissible" not in text


def test_court_and_prereg_stub_are_reported_honestly(tmp_path):
    run = WriterRun(tmp_path, stub=True)
    finding = run.finding()
    assert "court ran as a stub" in finding.elements.counterargument.text
    assert "no evidence was argued" in finding.elements.counterargument.text
    assert "not pre-registered" in finding.elements.falsification.text
    assert "STUB" not in " ".join(item["text"] for item in text_items(finding))
    assert finding.footer["impl"] == run.impl
    run.ledger.close()


def test_projection_reuses_sources_without_claiming_reconciliation(full_run):
    first = full_run.finding()
    n = len(full_run.ledger)
    assert full_run.finding() == first
    assert len(full_run.ledger) == n
    projections = full_run.ledger.query("CALC", formula_id="report_projection")
    assert projections
    assert all(p.author.component == "calculator" for p in projections)
    assert all(p.payload["recon_status"] == "single_source" for p in projections)
    assert any(
        p.payload["inputs"] == [full_run.signal.rid] and p.payload["params"]["field"] == "z" for p in projections
    )
    assert any(p.payload["inputs"] == [full_run.claim.rid] and p.payload["value"] == 105.0 for p in projections)
    assert "projection" in first.footer["numeric_provenance"].lower()


def test_fitted_donor_count_comes_from_signal_instead_of_all_peer_rows(tmp_path):
    run = WriterRun(tmp_path, corp="peer_median: 7 donors; twin fit unavailable")
    finding = run.finding()
    assert "peer median of 7 peers" in finding.elements.discovery.text
    assert check_finding_text(run.ledger, finding.model_dump()) is None
    run.ledger.close()


def test_falsification_shows_sealed_hash_and_threshold(full_run):
    finding = full_run.finding()
    assert "105.0 cr" in finding.elements.falsification.text
    assert full_run.ledger.heads()["prereg_head"][1][:12] in finding.elements.falsification.text
    assert "sealed before any evidence was searched" in finding.elements.falsification.text


def test_small_probability_is_never_rendered_as_broken_exponent_tokens():
    assert writer.format_value(0.00001234) == "0.0000123"


def test_failed_second_gate_never_appends_finding(full_run, monkeypatch):
    import signalcourt.writer.template as template
    from signalcourt.gates.common import Rejection

    monkeypatch.setattr(template, "check_finding_text", lambda *args, **kwargs: Rejection("schema", "forced_failure"))
    rid = template.append_finding(
        full_run.ledger, full_run.claim.rid, panel=full_run.panel, settings=full_run.settings, impl=full_run.impl
    )
    assert rid is None
    assert not full_run.ledger.query("FINDING")
    assert len(full_run.ledger.query("REJECT")) == 2


def test_corporate_action_is_the_leading_alternative_and_a_timeline_event(tmp_path):
    run = WriterRun(tmp_path, corp="eps_basic: restated for a x2 share-count step at Q2FY26 (bonus issue or split)")
    finding = run.finding()
    assert "leading alternative" in finding.elements.counterargument.text.lower()
    assert "bonus issue or split" in finding.elements.counterargument.text
    assert any("share-count" in item.text and "Q2 FY26" in item.text for item in finding.elements.timeline)
    assert check_finding_text(run.ledger, finding.model_dump()) is None
    run.ledger.close()


@pytest.mark.parametrize(
    "value, unit, expected",
    [(1234567.8, "inr_cr", "12,34,567.8 cr"), (0.034, "fraction", "3.4%"), (12.5, "inr_per_share", "Rs 12.50")],
)
def test_metric_number_formatting(value, unit, expected):
    assert writer.format_value(value, unit) == expected


def test_failing_gate_writes_reject_and_retries_once(full_run, monkeypatch):
    import signalcourt.writer.template as template

    original = template.write_finding

    def with_bad_number(*args, **kwargs):
        finding = original(*args, **kwargs)
        finding.elements.discovery.text += " Unsupported 99999999."
        return finding

    monkeypatch.setattr(template, "write_finding", with_bad_number)
    rid = template.append_finding(
        full_run.ledger, full_run.claim.rid, panel=full_run.panel, settings=full_run.settings, impl=full_run.impl
    )
    assert rid
    finding = full_run.ledger.get(rid)
    assert check_finding_text(full_run.ledger, finding.payload) is None
    assert "99999999" not in finding.payload["elements"]["discovery"]["text"]
    assert len(full_run.ledger.query("REJECT")) == 1
    assert set(finding.parents) == {full_run.claim.rid, full_run.signal.rid, full_run.verdict.rid}
    assert finding.author == Author.code("writer")


def test_missing_optional_records_degrades_without_losing_provenance(tmp_path):
    run = WriterRun(tmp_path)
    finding = writer.write_finding(run.ledger, run.claim.rid, panel=None, settings={}, impl={})
    assert finding.footer["impl"] == {}
    assert check_finding_text(run.ledger, finding.model_dump()) is None
    assert finding.elements.evidence
    run.ledger.close()


def test_minimal_signal_still_has_three_real_dated_timeline_events(tmp_path):
    with Ledger.create(tmp_path / "minimal", run_payload(), clock=fixed_clock()) as ledger:
        signal = ledger.append(
            "SIGNAL",
            {
                "sensor": "stub",
                "entity": "TESTCO",
                "metrics": ["stub"],
                "direction": "none",
                "specificity_source": "na",
            },
            Author.code("sensors"),
        )
        claim = ledger.append(
            "CLAIM",
            {
                "hypothesis_index": 0,
                "statement": "STUB",
                "mechanism": "STUB",
                "signal_rid": signal.rid,
                "falsification": {
                    "metric": "stub",
                    "comparator": "none",
                    "threshold": 0.0,
                    "deadline_period": "2026-10-01",
                    "source_rule": "stub",
                },
                "drafter": {"model": "stub", "family": "code"},
            },
            Author.code("prereg"),
        )
        finding = writer.write_finding(ledger, claim.rid, panel=None, settings={}, impl={})
        assert len(finding.elements.timeline) >= 3
        assert any("Signal recorded" in item.text for item in finding.elements.timeline)
        assert check_finding_text(ledger, finding.model_dump()) is None


def test_court_evidence_never_alters_a_quote_with_unbacked_numbers(full_run):
    evidence = full_run.ledger.append(
        "EVIDENCE",
        {
            "claim_rid": full_run.claim.rid,
            "span_rid": full_run.facts[0].payload["span_rid"],
            "sentence": "The claimed effect is 987654321 cr.",
            "source_family": "news",
            "origin_id": "new",
            "stance": "neutral",
        },
        Author(component="court", role="investigator"),
    )
    finding = full_run.finding()
    text = next(item.text for item in finding.elements.evidence if evidence.rid in item.rids)
    assert "987654321" not in text
    assert "“Value 100”" in text
    assert check_finding_text(full_run.ledger, finding.model_dump()) is None


def test_append_finding_keeps_the_hash_chain_verifiable(full_run):
    from signalcourt.ledger.verify import verify_run

    rid = writer.append_finding(
        full_run.ledger, full_run.claim.rid, panel=full_run.panel, settings=full_run.settings, impl=full_run.impl
    )
    assert rid
    full_run.ledger.seal_head("run_head")
    result = verify_run(full_run.ledger.run_dir)
    assert result.ok, result.problems


@pytest.mark.parametrize("derived_quarter", [False, True])
def test_unrelated_calculation_cannot_replace_the_metric_level_or_its_unit(full_run, derived_quarter):
    extra = full_run.ledger.append(
        "CALC",
        {
            "formula_id": "growth",
            "formula_version": 1,
            "formula_text": "latest / previous - 1",
            "inputs": [fact.rid for fact in full_run.facts],
            "params": {"metric": "gnpa_amt"},
            "value": 0.25,
            "unit": "fraction",
            "period_end": "2025-12-31",
            "entity": "TESTCO",
            "recon_status": "single_source",
        },
        Author.code("calculator"),
    )
    inputs = [*full_run.signal.payload["inputs"], extra.rid]
    latest = "140.0 cr"
    if derived_quarter:
        derived = full_run.ledger.append(
            "CALC",
            {
                "formula_id": "ytd_to_quarter",
                "formula_version": 1,
                "formula_text": "current YTD - previous YTD",
                "inputs": [fact.rid for fact in full_run.facts],
                "params": {"metric": "gnpa_amt"},
                "value": 150.0,
                "unit": "inr_cr",
                "period_end": "2026-03-31",
                "entity": "TESTCO",
                "recon_status": "single_source",
            },
            Author.code("calculator"),
        )
        inputs.append(derived.rid)
        latest = "150.0 cr"
    signal = full_run.ledger.append("SIGNAL", {**full_run.signal.payload, "inputs": inputs}, Author.code("sensors"))
    claim = full_run.ledger.append("CLAIM", {**full_run.claim.payload, "signal_rid": signal.rid}, Author.code("prereg"))
    finding = writer.write_finding(
        full_run.ledger, claim.rid, panel=full_run.panel, settings=full_run.settings, impl=full_run.impl
    )
    assert "20.0 cr" in finding.elements.discovery.text
    assert any(latest in item.text for item in finding.elements.numbers if "latest level" in item.text)
    assert not any(extra.rid in item.rids for item in finding.elements.numbers if "level" in item.text)
    assert check_finding_text(full_run.ledger, finding.model_dump()) is None


@pytest.mark.parametrize("seal_type", ["PLAN", "CALC"])
def test_imported_prereg_head_can_reference_any_existing_envelope(full_run, monkeypatch, seal_type):
    if seal_type == "PLAN":
        sealed = full_run.ledger.append("PLAN", {}, Author.code("angle_planner"))
    else:
        sealed = full_run.ledger.append(
            "CALC",
            {
                "formula_id": "seal_metadata",
                "formula_version": 1,
                "formula_text": "metadata",
                "inputs": [full_run.claim.rid],
                "value": None,
                "unit": "metadata",
                "recon_status": "single_source",
            },
            Author.code("calculator"),
        )
    heads = full_run.ledger.heads()
    heads["prereg_head"] = (sealed.seq, sealed.hash)
    monkeypatch.setattr(full_run.ledger, "heads", lambda: heads)
    finding = full_run.finding()
    assert sealed.hash[:12] in finding.elements.falsification.text
    assert check_finding_text(full_run.ledger, finding.model_dump()) is None
