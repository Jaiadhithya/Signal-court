"""Read-only report regeneration through the console command."""

from datetime import date
from pathlib import Path

from conftest import fixed_clock, run_payload
from typer.testing import CliRunner

from signalcourt.cli import app
from signalcourt.ledger import Author, Ledger, RecordType, verify_run
from signalcourt.store import DocStore


def _run_dir(tmp_path):
    path = tmp_path / "saved run"
    with Ledger.create(path, run_payload(), clock=fixed_clock()) as ledger:
        ledger.seal_head("run_head")
    return path


def test_cli_report_writes_html(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    missing_home = tmp_path / "no_workspace"
    monkeypatch.setenv("SIGNAL_HOME", str(missing_home))
    before = (run_dir / "ledger.jsonl").read_bytes()
    result = CliRunner().invoke(app, ["report", str(run_dir)])
    assert result.exit_code == 0, result.output
    report_path = run_dir / "report.html"
    assert str(report_path) in result.output
    assert "No signal cleared the bar" in report_path.read_text(encoding="utf-8")
    assert (run_dir / "finding.html").read_bytes() == report_path.read_bytes()
    assert (run_dir / "ledger.jsonl").read_bytes() == before
    assert not missing_home.exists()
    assert verify_run(run_dir).ok


def test_cli_report_refuses_a_directory_without_ledger(tmp_path):
    result = CliRunner().invoke(app, ["report", str(tmp_path)])
    assert result.exit_code == 1
    assert "no ledger" in result.output.lower()
    assert not (tmp_path / "ledger.sqlite").exists()


def test_cli_report_open_uses_the_rendered_file_url(tmp_path, monkeypatch):
    import webbrowser

    run_dir = _run_dir(tmp_path)
    monkeypatch.setenv("SIGNAL_HOME", str(tmp_path / "no_workspace"))
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    result = CliRunner().invoke(app, ["report", str(run_dir), "--open"])
    assert result.exit_code == 0, result.output
    assert opened == [(run_dir / "report.html").resolve().as_uri()]


def test_cli_report_uses_the_configured_document_store(tmp_path, monkeypatch):
    configured_home = tmp_path / "configured"
    nearby_home = tmp_path / "other_workspace"
    run_dir = nearby_home / "runs" / "old_run"
    with_store = DocStore(configured_home / "cache")
    try:
        meta = with_store.put_bytes(
            b"Public filing text.",
            url="https://example.test/filing.txt",
            final_url="https://example.test/filing.txt",
            mime="text/plain",
            source_id="exchange_results",
            source_family="exchange",
            doc_type="results",
            period_end=date(2026, 6, 30),
            publish_date=date(2026, 8, 1),
        )
        local_uri = with_store.blobs.find(meta.sha256).resolve().as_uri()
    finally:
        with_store.close()
    nearby_store = DocStore(nearby_home / "cache")
    nearby_store.close()
    with Ledger.create(run_dir, run_payload(), clock=fixed_clock()) as ledger:
        ledger.append(
            RecordType.DOC,
            {
                "sha256": meta.sha256,
                "url": meta.first_url,
                "final_url": meta.final_url,
                "doc_type": meta.doc_type,
                "source_id": meta.source_id,
                "source_family": meta.source_family,
                "origin_id": meta.sha256,
                "period_end": meta.period_end,
                "publish_date": meta.publish_date,
                "time_gate": "ok",
            },
            Author.code("harvest"),
        )
        ledger.seal_head("run_head")
    monkeypatch.setenv("SIGNAL_HOME", str(configured_home))
    result = CliRunner().invoke(app, ["report", str(run_dir)])
    assert result.exit_code == 0, result.output
    assert local_uri in (run_dir / "report.html").read_text(encoding="utf-8")


def test_cli_report_pdf_skips_cleanly_without_chrome(tmp_path, monkeypatch):
    import signalcourt.render.pdf as pdf

    run_dir = _run_dir(tmp_path)
    monkeypatch.setenv("SIGNAL_HOME", str(tmp_path / "no_workspace"))
    monkeypatch.setattr(pdf, "find_chrome", lambda: None)
    result = CliRunner().invoke(app, ["report", str(run_dir), "--pdf"])
    assert result.exit_code == 0, result.output
    assert str(run_dir / "report.html") in result.output
    assert "chrome" in result.output.lower()
    assert not (run_dir / "report.pdf").exists()


def test_cli_report_pdf_prints_exported_path(tmp_path, monkeypatch):
    import signalcourt.render.pdf as pdf

    run_dir = _run_dir(tmp_path)
    monkeypatch.setenv("SIGNAL_HOME", str(tmp_path / "no_workspace"))

    def export(html_path: Path, pdf_path=None):
        assert html_path == run_dir / "report.html"
        path = html_path.with_suffix(".pdf")
        path.write_bytes(b"%PDF-1.4\n%%EOF\n")
        return path

    monkeypatch.setattr(pdf, "export_pdf", export)
    result = CliRunner().invoke(app, ["report", str(run_dir), "--pdf"])
    assert result.exit_code == 0, result.output
    assert str(run_dir / "report.pdf") in result.output
