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
            rows.append(dict(entity=entity, metric=metric, pidx=first + i, period=P.from_index(first + i),
                             value=value * scale, rid=f"FA-{j:02d}{i:06d}", basis="standalone", tier=1,
                             published=date(2026, 8, 1), recon_status="pass"))
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

    rendered = render_chart(chart_panel(), chart_signal(), load_config().model_dump(), metric_label="Profit", unit="inr_cr")
    svg_text = rendered[rendered.index("<svg"):rendered.index("</svg>") + 6]
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
    svg = ET.fromstring(rendered[rendered.index("<svg"):rendered.index("</svg>") + 6])
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
