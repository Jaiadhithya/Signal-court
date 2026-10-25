"""Report charts, export, and page rendering from persisted run data."""

from datetime import date
from pathlib import Path
from subprocess import CompletedProcess
from xml.etree import ElementTree as ET

import numpy as np
import pandas as pd
import pytest

from signalcourt.calc import periods as P
from signalcourt.calc.panel import Panel
from signalcourt.settings import load_config


def chart_panel(metric="pat", n_peers=4):
    values = [100.0, 110, 90, 120, 105, 115, 95, 125, 160, 175, 155, 180]
    first = P.index("Q1FY24")
    rows = []
    for j, entity in enumerate(["TARGET"] + [f"PEER{i}" for i in range(n_peers)]):
        scale = 1 if j == 0 else (j + 1) * 3
        series = values if j == 0 else values[:8] + [110, 125, 105, 130]
        for i, value in enumerate(series):
            rows.append(
                dict(
                    entity=entity,
                    metric=metric,
                    pidx=first + i,
                    period=P.from_index(first + i),
                    value=value * scale,
                    rid=f"FA-{j:02d}{i:06d}",
                    basis="standalone",
                    tier=1,
                    published=date(2026, 8, 1),
                    recon_status="pass",
                )
            )
    return Panel(pd.DataFrame(rows), "TARGET", [f"PEER{i}" for i in range(n_peers)], date(2026, 10, 1))


def chart_signal(metric="pat", **overrides):
    payload = dict(entity="TARGET", metrics=[metric], break_period="Q1FY26", specificity_source="peer_median")
    payload.update(overrides)
    return payload


@pytest.mark.parametrize("n_peers,source", [(0, "own_history"), (4, "peer_median"), (15, "twin")])
def test_chart_series_matches_the_sensor_baseline(n_peers, source):
    from signalcourt.render.chart import chart_series
    from signalcourt.sensors.baseline import fit_baseline
    from signalcourt.sensors.gap import normalise

    panel, settings = chart_panel(n_peers=n_peers), load_config().model_dump()
    target, baseline, actual_source = chart_series(panel, chart_signal(), settings)
    pre_end = P.index("Q4FY25")
    normalized, scale = normalise(panel.series("TARGET", "pat"), pre_end, "pat")
    donors = {}
    for peer in panel.peers:
        donors[peer], _ = normalise(panel.series(peer, "pat"), pre_end, "pat")
    expected = fit_baseline(normalized, pd.DataFrame(donors), pre_end, settings)
    pd.testing.assert_series_equal(target, panel.series("TARGET", "pat"))
    pd.testing.assert_series_equal(baseline, expected.series * scale)
    assert actual_source == source
    assert baseline.iloc[0] == pytest.approx(107.5 if not n_peers else 100.0)
    if n_peers:
        assert baseline.iloc[-1] == pytest.approx(130.0)


def test_chart_series_restates_per_share_values_before_normalising():
    from signalcourt.render.chart import chart_series

    panel = chart_panel(metric="pat", n_peers=4)
    eps = panel.df.copy()
    eps["metric"] = "eps_basic"
    eps["value"] = eps.value / np.where(eps.pidx < P.index("Q2FY25"), 10.0, 20.0)
    eps["rid"] = eps.rid.str.replace("FA-", "FA-9", regex=False)
    panel.df = pd.concat([panel.df, eps], ignore_index=True)
    target, baseline, source = chart_series(panel, chart_signal("eps_basic"), load_config().model_dump())
    np.testing.assert_allclose(target.to_numpy(), [5, 5.5, 4.5, 6, 5.25, 5.75, 4.75, 6.25, 8, 8.75, 7.75, 9])
    assert baseline.iloc[0] == pytest.approx(5.0)
    assert baseline.iloc[-1] == pytest.approx(6.5)
    assert source == "peer_median"
    assert panel.series("TARGET", "eps_basic").iloc[0] == 10.0  # persisted panel is not mutated


def test_chart_missing_break_draws_only_the_recorded_target():
    from signalcourt.render.chart import chart_series, render_chart

    panel = chart_panel()
    signal = chart_signal(break_period=None)
    target, baseline, source = chart_series(panel, signal, {})
    assert len(target) == 12 and baseline.empty and source == "unavailable"
    rendered = render_chart(panel, signal, {}, metric_label="Profit", unit="inr_cr")
    assert "Baseline unavailable" in rendered
    assert "chart-target-line" in rendered
    assert 'class="chart-baseline-line"' not in rendered


def test_chart_svg_marks_the_break_quarter():
    from signalcourt.render.chart import render_chart

    rendered = render_chart(
        chart_panel(), chart_signal(), load_config().model_dump(), metric_label="Profit", unit="inr_cr"
    )
    svg_text = rendered[rendered.index("<svg") : rendered.index("</svg>") + 6]
    svg = ET.fromstring(svg_text)
    marker = next(el for el in svg.iter() if el.attrib.get("class") == "chart-break")
    assert marker.attrib["data-break-period"] == "Q1FY26"
    assert float(marker.attrib["x1"]) == float(marker.attrib["x2"])
    assert "Q1 FY26" in rendered and "cr" in rendered
    assert "peer median of 4 peers" in rendered
    assert svg.attrib["viewBox"] == "0 0 1000 360"


def test_chart_points_link_to_facts_and_have_tooltips_without_javascript():
    from signalcourt.render.chart import render_chart

    panel = chart_panel()
    rendered = render_chart(panel, chart_signal(), load_config().model_dump(), metric_label="Profit <recorded>")
    svg = ET.fromstring(rendered[rendered.index("<svg") : rendered.index("</svg>") + 6])
    links = [el for el in svg.iter() if el.tag.endswith("}a")]
    assert {el.attrib["href"] for el in links} == {f"#{rid}" for rid in panel.df[panel.df.entity == "TARGET"].rid}
    assert all(any(child.tag.endswith("}title") for child in link.iter()) for link in links)
    assert "Profit &lt;recorded&gt;" in rendered


def test_chart_missing_optional_panel_has_an_explicit_note():
    from signalcourt.render.chart import render_chart

    rendered = render_chart(None, chart_signal(), {})
    assert "Chart unavailable" in rendered
    assert "<polyline" not in rendered


def test_chart_without_fact_rids_keeps_the_series_and_explains_missing_links():
    from signalcourt.render.chart import render_chart

    panel = chart_panel()
    panel.df = panel.df.drop(columns="rid")
    rendered = render_chart(panel, chart_signal(), load_config().model_dump())
    assert 'class="chart-target-line"' in rendered
    assert "Source links unavailable" in rendered


def test_find_chrome_prefers_an_existing_environment_path(tmp_path, monkeypatch):
    from signalcourt.render import pdf

    chrome = tmp_path / "custom chrome.exe"
    chrome.touch()
    monkeypatch.setenv("CHROME_PATH", str(chrome))
    assert Path(pdf.find_chrome()) == chrome


def test_find_chrome_falls_back_to_path(tmp_path, monkeypatch):
    from signalcourt.render import pdf

    chrome = tmp_path / "google-chrome"
    chrome.touch()
    monkeypatch.delenv("CHROME_PATH", raising=False)
    monkeypatch.setattr(pdf, "CHROME_LOCATIONS", ())
    monkeypatch.setattr(pdf.shutil, "which", lambda name: str(chrome) if name == "google-chrome" else None)
    assert Path(pdf.find_chrome()) == chrome


def test_pdf_export_skips_cleanly_without_chrome(tmp_path, monkeypatch):
    from signalcourt.render import pdf

    monkeypatch.setattr(pdf, "find_chrome", lambda: None)
    html = tmp_path / "report.html"
    html.write_text("<html>Report</html>", encoding="utf-8")
    assert pdf.export_pdf(html) is None
    assert not html.with_suffix(".pdf").exists()


def test_pdf_export_uses_a_file_url_and_validates_the_created_output(tmp_path, monkeypatch):
    from signalcourt.render import pdf

    html = tmp_path / "space in report.html"
    html.write_text("<html>Report</html>", encoding="utf-8")
    output = tmp_path / "report.pdf"
    monkeypatch.setattr(pdf, "find_chrome", lambda: "chrome-test")

    def run_chrome(args, **kwargs):
        assert args[0] == "chrome-test"
        assert {"--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--virtual-time-budget=8000"} <= set(args)
        assert f"--print-to-pdf={output.resolve()}" in args
        assert args[-1] == html.resolve().as_uri()
        assert kwargs.get("timeout", 0) > 0
        if pdf.os.name == "nt":
            assert kwargs["creationflags"] & pdf.subprocess.CREATE_NO_WINDOW
        output.write_bytes(b"%PDF-1.7\nfixture")
        return CompletedProcess(args, 0)

    monkeypatch.setattr(pdf.subprocess, "run", run_chrome)
    assert pdf.export_pdf(html, output) == output
    assert output.read_bytes().startswith(b"%PDF-")


def test_pdf_export_does_not_report_a_missing_or_stale_file_as_success(tmp_path, monkeypatch):
    from signalcourt.render import pdf

    html = tmp_path / "report.html"
    html.write_text("<html>Report</html>", encoding="utf-8")
    output = html.with_suffix(".pdf")
    output.write_bytes(b"%PDF-1.7\nprevious export")
    monkeypatch.setattr(pdf, "find_chrome", lambda: "chrome-test")
    monkeypatch.setattr(pdf.subprocess, "run", lambda args, **kwargs: CompletedProcess(args, 0))
    assert pdf.export_pdf(html) is None
    assert output.read_bytes() == b"%PDF-1.7\nprevious export"


"""Renderer integration tests, appended to the permitted report test module."""


def _research_run(tmp_path, *, with_signal=True, prereg_stub=True, stop=False):
    import json

    from conftest import fixed_clock, run_payload

    from signalcourt.ledger import Author, Ledger, RecordType

    run_dir = tmp_path / "research"
    led = Ledger.create(run_dir, run_payload(), clock=fixed_clock())
    led.append(
        RecordType.ENTITY,
        dict(
            symbol="TESTCO",
            legal_names=["Example Financial Limited"],
            subtype="bank",
            subtype_evidence={},
            peer_rule="same sub-type",
        ),
        Author.code("resolve"),
    )
    doc = led.append(
        RecordType.DOC,
        dict(
            sha256="a" * 64,
            url="https://example.test/filing",
            final_url="https://example.test/filing",
            doc_type="results_xbrl",
            source_id="exchange",
            source_family="exchange_xbrl",
            origin_id="one",
            time_gate="ok",
            publish_date="2026-08-01",
            period_end="2026-06-30",
        ),
        Author.code("harvest"),
    )
    span = led.append(
        RecordType.SPAN,
        dict(
            doc_sha256="a" * 64,
            page=1,
            kind="xbrl",
            text="Financial income increased",
            norm_text="Financial income increased",
        ),
        Author.code("quote_gate"),
    )
    fact = led.append(
        RecordType.FACT,
        dict(
            entity="TESTCO",
            metric="total_income",
            value=1234.5,
            unit="inr_cr",
            period_end="2026-06-30",
            basis="standalone",
            span_rid=span.rid,
            extractor="xbrl",
            value_as_written="1234.5",
            recon_status="single_source",
        ),
        Author.code("extract"),
    )
    if with_signal:
        sig = led.append(
            RecordType.SIGNAL,
            dict(
                sensor="gap",
                entity="TESTCO",
                metrics=["total_income"],
                direction="up",
                break_period="Q1FY26",
                size=120.5,
                z=2.6,
                persistence_q=3,
                specificity=0.8,
                specificity_source="peer_median",
                placebo={"p_raw": 0.02, "n": 49, "rank": 1},
                inputs=[fact.rid, doc.rid],
                surprise={"score": 2.08, "coverage_status": "peer_median: 5 donors"},
            ),
            Author.code("sensors"),
        )
        claim = led.append(
            RecordType.CLAIM,
            dict(
                hypothesis_index=0,
                statement="STUB" if prereg_stub else "Income is unusually high relative to comparable lenders.",
                mechanism="STUB" if prereg_stub else "The funding mix may have changed.",
                signal_rid=sig.rid,
                falsification=dict(
                    metric="total_income",
                    comparator="none" if prereg_stub else "<",
                    threshold=1100.0,
                    deadline_period="Q4FY27",
                    source_rule="results filings",
                ),
                drafter=dict(model="stub" if prereg_stub else "template", family="code"),
            ),
            Author.code("prereg"),
        )
        led.seal_head("prereg_head")
        led.append(
            RecordType.CHALLENGE,
            dict(claim_rid=claim.rid, alt_type="stub", argument="STUB", severity="minor"),
            Author(component="court", role="redteam"),
        )
        led.append(
            RecordType.VERDICT,
            dict(
                claim_rid=claim.rid,
                grade="Weak",
                checks={"support_families_strong": {"value": 0, "need": 3, "pass": False}},
                reasons=["No independent supporting evidence was admitted."],
            ),
            Author.code("judge"),
        )
    if stop:
        led.append(
            RecordType.STOP,
            dict(reason="time_cap", calls_used=0, tools_used=0, elapsed_s=15.2, open_items=["court"]),
            Author.code("scheduler"),
        )
    led.seal_head("run_head")
    count = len(led)
    led.close()
    impl = {
        "harvest": "real",
        "measure": "real",
        "sense": "real",
        "prereg": "stub" if prereg_stub else "real",
        "court": "stub",
        "judge": "real",
        "report": "real",
    }
    (run_dir / "report-meta.json").write_text(json.dumps({"impl": impl}), encoding="utf-8")
    return run_dir, count


def _renderer():
    import signalcourt.render as render

    assert callable(getattr(render, "render_report", None)), "Saved-ledger report renderer is not implemented"
    return render.render_report


def test_no_signal_report_renders(tmp_path):
    run_dir, _ = _research_run(tmp_path, with_signal=False)
    page = _renderer()(run_dir)
    assert "No signal cleared the bar" in page
    assert "Example Financial Limited" in page
    assert "Evidence ledger" in page


def test_court_stub_is_reported_honestly(tmp_path):
    run_dir, _ = _research_run(tmp_path)
    page = _renderer()(run_dir)
    assert "The court ran as a stub in this run: no evidence was argued" in page
    assert ">STUB<" not in page and '"STUB"' not in page


def test_prereg_stub_is_reported_honestly(tmp_path):
    run_dir, _ = _research_run(tmp_path)
    page = _renderer()(run_dir)
    assert "not pre-registered in this run" in page
    assert "sealed before any evidence was searched" not in page


def test_stop_record_is_shown(tmp_path):
    run_dir, _ = _research_run(tmp_path, stop=True)
    page = _renderer()(run_dir)
    assert "Time cap" in page and "court" in page


def test_report_lists_every_stage_with_its_impl(tmp_path):
    from signalcourt.pipeline import STAGE_ORDER

    run_dir, _ = _research_run(tmp_path)
    page = _renderer()(run_dir)
    for stage in STAGE_ORDER:
        assert f'data-stage="{stage}"' in page
    assert 'data-impl="stub"' in page and 'data-impl="real"' in page


def test_numbers_link_to_appendix_rows(tmp_path):
    import re

    run_dir, _ = _research_run(tmp_path)
    page = _renderer()(run_dir)
    targets = re.findall(r'href="#([A-Z]{2}-[a-f0-9]+)"', page)
    assert targets
    for target in targets:
        assert f'id="{target}"' in page
    assert "data-record=" in page


def test_report_rerenders_old_run_without_writing_records(tmp_path):
    from signalcourt.ledger import Ledger

    run_dir, count = _research_run(tmp_path)
    before = (run_dir / "ledger.jsonl").read_bytes()
    _renderer()(run_dir)
    with Ledger.open(run_dir) as ledger:
        assert len(ledger) == count
    assert (run_dir / "ledger.jsonl").read_bytes() == before


def test_report_escapes_untrusted_content_and_rejects_unsafe_links(tmp_path):
    from signalcourt.ledger import Author, Ledger, RecordType

    run_dir, _ = _research_run(tmp_path)
    with Ledger.open(run_dir) as led:
        led.append(
            RecordType.DOC,
            dict(
                sha256="b" * 64,
                url="javascript:alert(1)",
                final_url="javascript:alert(1)",
                doc_type="<script>alert(1)</script>",
                source_id="test",
                source_family="test",
                origin_id="bad",
                time_gate="ok",
            ),
            Author.code("harvest"),
        )
    page = _renderer()(run_dir)
    assert 'href="javascript:' not in page
    assert "<script>alert(1)</script>" not in page


def test_other_hypotheses_include_unclaimed_signals(tmp_path):
    from signalcourt.ledger import Author, Ledger, RecordType

    run_dir, _ = _research_run(tmp_path)
    with Ledger.open(run_dir) as led:
        sig = led.append(
            RecordType.SIGNAL,
            dict(
                sensor="gap",
                entity="TESTCO",
                metrics=["opex"],
                direction="down",
                specificity_source="na",
                surprise={"score": 1.9},
            ),
            Author.code("sensors"),
        )
    page = _renderer()(run_dir)
    assert sig.rid in page and "Unclaimed signal" in page


def test_signal_and_verdict_appendix_expose_the_clicked_numbers(tmp_path):
    from lxml import html

    run_dir, _ = _research_run(tmp_path)
    page = _renderer()(run_dir)
    document = html.fromstring(page)
    signal = document.xpath('//tr[.//span[@class="record-type" and text()="SIGNAL"]]')[0]
    verdict = document.xpath('//tr[.//span[@class="record-type" and text()="VERDICT"]]')[0]
    assert "120.5" in signal.text_content()
    assert "2.60" in signal.text_content()
    assert "49" in signal.text_content()
    assert "support_families_strong" in verdict.text_content()
    assert "3" in verdict.text_content()


def test_signal_without_claim_is_shown_as_an_unclaimed_candidate(tmp_path):
    from conftest import run_payload

    from signalcourt.ledger import Author, Ledger, RecordType

    run_dir = tmp_path / "unclaimed"
    with Ledger.create(run_dir, run_payload()) as led:
        led.append(
            RecordType.SIGNAL,
            dict(
                sensor="gap",
                entity="TESTCO",
                metrics=["total_income"],
                direction="up",
                break_period="Q1FY26",
                specificity_source="na",
            ),
            Author.code("sensors"),
        )
    page = _renderer()(run_dir)
    assert "No signal cleared the bar" in page
    assert "Strongest unclaimed candidate" in page


def test_old_real_prereg_threshold_keeps_its_metric_unit_and_provenance(tmp_path):
    import json

    from conftest import run_payload
    from lxml import html

    from signalcourt.ledger import Author, Ledger, RecordType

    run_dir = tmp_path / "ratio"
    with Ledger.create(run_dir, run_payload()) as led:
        signal = led.append(
            RecordType.SIGNAL,
            dict(
                sensor="gap",
                entity="TESTCO",
                metrics=["gnpa_pct"],
                direction="up",
                break_period="Q1FY26",
                specificity_source="na",
            ),
            Author.code("sensors"),
        )
        claim = led.append(
            RecordType.CLAIM,
            dict(
                hypothesis_index=0,
                statement="Credit quality worsened.",
                mechanism="Persistent deterioration.",
                signal_rid=signal.rid,
                falsification=dict(
                    metric="gnpa_pct",
                    comparator="<",
                    threshold=0.04,
                    deadline_period="Q4FY27",
                    source_rule="results filings",
                ),
                drafter=dict(model="template", family="code"),
            ),
            Author.code("prereg"),
        )
        led.seal_head("prereg_head")
    (run_dir / "report-meta.json").write_text(
        json.dumps({"impl": {"prereg": "real", "court": "real"}}), encoding="utf-8"
    )
    document = html.fromstring(_renderer()(run_dir))
    box = document.get_element_by_id("falsification")
    assert "4.0%" in box.text_content()
    assert box.xpath('.//a[text()="4.0%"]')
    row = document.get_element_by_id(claim.rid)
    assert "4.0%" in row.text_content()
    assert "Q4 FY27" in row.text_content()


def test_full_court_renders_source_quote_challenge_and_admissible_rebuttal(tmp_path):
    import json

    from conftest import run_payload

    from signalcourt.ledger import Author, Ledger, RecordType

    run_dir = tmp_path / "full_court"
    with Ledger.create(run_dir, run_payload()) as led:
        sig = led.append(
            RecordType.SIGNAL,
            dict(
                sensor="gap",
                entity="TESTCO",
                metrics=["total_income"],
                direction="up",
                break_period="Q1FY26",
                specificity_source="na",
            ),
            Author.code("sensors"),
        )
        claim = led.append(
            RecordType.CLAIM,
            dict(
                hypothesis_index=0,
                statement="Income rose persistently.",
                mechanism="Business mix may have changed.",
                signal_rid=sig.rid,
                falsification=dict(
                    metric="total_income",
                    comparator="<",
                    threshold=1100,
                    deadline_period="Q4FY27",
                    source_rule="results filings",
                ),
                drafter=dict(model="template", family="code"),
            ),
            Author.code("prereg"),
        )
        led.seal_head("prereg_head")
        span = led.append(
            RecordType.SPAN,
            dict(
                doc_sha256="c" * 64,
                page=1,
                kind="prose",
                text="Public evidence describes a changed business mix.",
                norm_text="Public evidence describes a changed business mix.",
            ),
            Author.code("quote_gate"),
        )
        ev = led.append(
            RecordType.EVIDENCE,
            dict(
                claim_rid=claim.rid,
                span_rid=span.rid,
                stance="support",
                sentence="The public source describes a mix shift.",
                source_family="exchange",
                origin_id="example",
            ),
            Author(component="court", role="investigator"),
        )
        challenge = led.append(
            RecordType.CHALLENGE,
            dict(
                claim_rid=claim.rid,
                alt_type="one_off",
                argument="An exceptional item could explain the shift.",
                severity="substantive",
            ),
            Author(component="court", role="redteam"),
        )
        led.append(
            RecordType.REBUTTAL,
            dict(
                challenge_rid=challenge.rid,
                argument="The cited source describes a recurring change in business mix.",
                evidence=[ev.rid],
                admissible=True,
                admissible_reason="Cites existing evidence.",
            ),
            Author(component="court", role="advocate"),
        )
    (run_dir / "report-meta.json").write_text(
        json.dumps({"impl": {"prereg": "real", "court": "real"}}), encoding="utf-8"
    )
    page = _renderer()(run_dir)
    assert "Public evidence describes a changed business mix." in page
    assert "An exceptional item could explain the shift." in page
    assert "Admissible" in page
    assert "no evidence was argued" not in page


@pytest.mark.parametrize(
    "metric,split,missing_profit,adjusted",
    [("interest_expended", False, True, False), ("eps_basic", False, False, False), ("eps_basic", True, False, True)],
)
def test_chart_share_base_notes_describe_only_the_plotted_metric(metric, split, missing_profit, adjusted):
    from signalcourt.render.chart import chart_series, render_chart

    panel = chart_panel(metric="pat")
    eps = panel.df.copy()
    eps["metric"] = "eps_basic"
    divisor = np.where(eps.pidx < P.index("Q2FY25"), 10.0, 20.0) if split else 20.0
    eps["value"] = eps.value / divisor
    eps["rid"] = eps.rid.str.replace("FA-", "FA-9", regex=False)
    if missing_profit:
        panel.df["metric"] = "interest_expended"
    panel.df = pd.concat([panel.df, eps], ignore_index=True)
    settings, signal = load_config().model_dump(), chart_signal(metric)
    target, _, _ = chart_series(panel, signal, settings)
    notes = target.attrs.get("notes", [])
    assert all(note.startswith(f"{metric}:") for note in notes)
    assert bool(notes) is adjusted
    rendered = render_chart(panel, signal, settings)
    assert ("adjusted onto a consistent share base" in rendered) is adjusted
    if metric != "eps_basic":
        assert "eps_basic:" not in rendered


def test_future_court_evidence_is_excluded_from_the_research_note(tmp_path):
    from signalcourt.ledger import Author, Ledger, RecordType

    run_dir, _ = _research_run(tmp_path)
    with Ledger.open(run_dir) as led:
        claim = led.query("CLAIM")[0]
        doc = led.append(
            RecordType.DOC,
            dict(
                sha256="e" * 64,
                url="https://example.test/future-only",
                final_url="https://example.test/future-only",
                doc_type="results",
                source_id="exchange",
                source_family="exchange",
                origin_id="future",
                time_gate="dropped",
                publish_date="2026-10-20",
            ),
            Author.code("harvest"),
        )
        span = led.append(
            RecordType.SPAN,
            dict(
                doc_sha256="e" * 64,
                page=1,
                kind="prose",
                text="Future-only secret quotation.",
                norm_text="Future-only secret quotation.",
            ),
            Author.code("quote_gate"),
            [doc.rid],
        )
        led.append(
            RecordType.EVIDENCE,
            dict(
                claim_rid=claim.rid,
                span_rid=span.rid,
                stance="support",
                sentence="Future-only claim must be excluded.",
                source_family="exchange",
                origin_id="future",
            ),
            Author(component="court", role="investigator"),
            [span.rid],
        )
    page = _renderer()(run_dir)
    assert "Future-only secret quotation." not in page
    assert "Future-only claim must be excluded." not in page
    assert 'href="https://example.test/future-only"' not in page
    assert "excluded" in page.lower()


def test_report_period_formats_iso_dates_as_financial_quarters():
    from signalcourt.render.records import period

    assert period("2026-06-30") == "Q1 FY27"
    assert period("2025-12-31") == "Q3 FY26"
    assert period(None) == "Date unavailable"


def test_chart_terminal_quarter_labels_do_not_collide():
    import re

    from signalcourt.render.chart import render_chart

    panel = chart_panel()
    first = panel.df.pidx.min()
    extra = panel.df[panel.df.pidx < first + 8].copy()
    extra["pidx"] += 12
    extra["period"] = extra.pidx.map(P.from_index)
    panel.df = pd.concat([panel.df, extra], ignore_index=True)
    svg = render_chart(panel, chart_signal(), load_config().model_dump())
    positions = [float(x) for x in re.findall(r'<text class="chart-muted" x="([\d.]+)" y="304"', svg)]
    assert len(positions) > 2
    assert min(np.diff(positions)) >= 60
