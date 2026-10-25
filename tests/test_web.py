import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from signalcourt.pipeline import STAGE_ORDER
from signalcourt.resolve import universe as U
from signalcourt.web import create_app


@pytest.fixture
def home(tmp_path):
    con = U.connect(tmp_path / "universe.sqlite")
    U.ingest(
        con,
        [
            {"SYMBOL": "ALPHA", "NAME OF COMPANY": "Alpha Finance Limited"},
            {"SYMBOL": "BETA", "NAME OF COMPANY": "Alpha Insurance Limited"},
            {"SYMBOL": "GAMMA", "NAME OF COMPANY": "Gamma Limited"},
        ],
        {},
    )
    con.execute("UPDATE entities SET financial=1, subtype='nbfc'")
    con.execute("INSERT INTO entity_names VALUES ('ALPHA', 'Shared Finance', 'test')")
    con.execute("INSERT INTO entity_names VALUES ('BETA', 'Shared Insurance', 'test')")
    con.commit()
    con.close()
    (tmp_path / "prewarm_report.csv").write_text(
        "symbol,role,subtype\nALPHA,track_a,nbfc\nBETA,dev,life_insurer\nGAMMA,peer,bank\n", encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def client(home):
    with TestClient(create_app(home=home)) as client:
        deadline = time.monotonic() + 30
        while client.get("/api/status").json()["state"] == "warming":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert client.get("/api/status").json()["state"] == "ready"
        yield client


def run_stub(client):
    response = client.post("/api/runs", json={"company": "Alpha Finance", "mode": "replay", "stub": list(STAGE_ORDER)})
    assert response.status_code == 202, response.text
    return response.json()["id"]


def events(client, run_id, headers=None):
    response = client.get(f"/api/runs/{run_id}/events", headers=headers or {})
    assert response.status_code == 200
    result = []
    for block in response.text.replace("\r\n", "\n").split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines and "data" in lines:
            result.append((lines["event"], json.loads(lines["data"]), lines.get("id")))
    return result


def test_companies_are_runtime_targets_and_replay_readiness(client):
    targets = client.get("/api/companies").json()
    assert [(c["symbol"], c["name"], c["subtype"]) for c in targets] == [
        ("ALPHA", "Alpha Finance Limited", "nbfc"),
        ("BETA", "Alpha Insurance Limited", "life_insurer"),
    ]
    assert not any(c["replay_ready"] for c in targets)
    run_id = run_stub(client)
    events(client, run_id)
    assert client.get("/api/companies").json()[0]["replay_ready"] is False  # stub research is not a replay


def test_resolve_unique_ambiguous_and_unknown(client):
    unique = client.get("/api/resolve", params={"q": "ALPHA"}).json()
    assert unique["status"] == "match" and unique["match"]["symbol"] == "ALPHA"
    ambiguous = client.get("/api/resolve", params={"q": "Shared"}).json()
    assert ambiguous["status"] == "ambiguous"
    assert {c["symbol"] for c in ambiguous["candidates"]} == {"ALPHA", "BETA"}
    assert client.get("/api/resolve", params={"q": "zqxwvu"}).json()["status"] == "unknown"
    response = client.post("/api/runs", json={"company": "Shared", "mode": "replay"})
    assert response.status_code == 422 and response.json()["detail"]["candidates"]


def test_stub_run_stream_report_history_and_verify(client):
    run_id = run_stub(client)
    stream = events(client, run_id)
    assert [d["stage"] for k, d, _ in stream if k == "stage_end"] == list(STAGE_ORDER)
    assert [d["stage"] for k, d, _ in stream if k == "stage_start"] == list(STAGE_ORDER)
    assert all({"impl", "records", "seconds"} <= set(d) for k, d, _ in stream if k == "stage_end")
    assert stream[-1][0] == "done"
    assert stream[-1][1]["report"] == f"/runs/{run_id}/report"
    assert stream[-1][1]["total_seconds"] >= 0
    report = client.get(f"/runs/{run_id}/report")
    assert report.status_code == 200 and "text/html" in report.headers["content-type"]
    assert "Alpha Finance" in report.text
    history = client.get("/api/runs").json()
    assert history[0]["id"] == run_id
    assert history[0]["grade"] == "Weak" and history[0]["mode"] == "replay"
    verified = client.post(f"/api/runs/{run_id}/verify").json()
    assert verified["status"] == "OK" and verified["records"] == 7
    assert len(verified["head_hash"]) == 64
    last_id = stream[-2][2]
    assert [k for k, _, _ in events(client, run_id, {"Last-Event-ID": last_id})] == ["done"]


def test_second_run_is_refused_until_worker_finishes(client, monkeypatch):
    from signalcourt import pipeline

    entered, release = threading.Event(), threading.Event()
    original = pipeline.STAGES["harvest"]["stub"]

    def slow(ctx):
        entered.set()
        assert release.wait(10)
        original(ctx)

    monkeypatch.setitem(pipeline.STAGES["harvest"], "stub", slow)
    try:
        run_id = run_stub(client)
        assert entered.wait(5)
        other = client.post("/api/runs", json={"company": "ALPHA", "mode": "replay"})
        assert other.status_code == 409
        assert client.get("/api/status").json()["active_run"]["id"] == run_id
        assert client.post(f"/api/runs/{run_id}/verify").status_code == 409
    finally:
        release.set()
    assert events(client, run_id)[-1][0] == "done"


def test_failure_stream_contains_stage_reason_and_stop_then_unlocks(client, monkeypatch):
    from signalcourt import pipeline

    def fail(ctx):
        ctx.on_event("stage_note", {"stage": "sense", "note": "looking for signals"})
        raise RuntimeError("metric panel unavailable")

    with monkeypatch.context() as patch:
        patch.setitem(pipeline.STAGES["sense"], "stub", fail)
        run_id = run_stub(client)
        stream = events(client, run_id)
    failure = next(d for k, d, _ in stream if k == "error")
    assert failure["stage"] == "sense" and "metric panel unavailable" in failure["error"]
    assert any(k == "stop" and d["reason"] == "error" for k, d, _ in stream)
    assert stream[-1][0] == "error"
    assert events(client, run_stub(client))[-1][0] == "done"


@pytest.mark.parametrize(
    "route",
    [
        "/runs/%2e%2e/report",
        "/runs/anything/files/%2e%2e/.env",
        "/cache/%2e%2e/.env",
        "/cache/%2e%2e%5c.env",
        "/runs/anything/files/C%3A%5cWindows%5cwin.ini",
        "/cache/manifest.sqlite",
        "/cache/.env",
        "/api/runs/%2e%2e/events",
    ],
)
def test_routes_refuse_traversal_and_private_files(client, home, route):
    (home / ".env").write_text("PRIVATE-CANARY", encoding="utf-8")
    response = client.get(route)
    assert response.status_code in {400, 404}
    assert "PRIVATE-CANARY" not in response.text


def test_report_cache_links_are_rewritten_and_confined(client, home):
    run_id = run_stub(client)
    events(client, run_id)
    cached = home / "cache" / "raw" / "source.html"
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text("<p>Cached source</p>", encoding="utf-8")
    report = home / "runs" / run_id / "report.html"
    report.write_text(f'<a href="{cached.as_uri()}">Cached</a>', encoding="utf-8")
    response = client.get(f"/runs/{run_id}/report")
    assert "/cache/raw/source.html" in response.text and "file:" not in response.text
    assert client.get("/cache/raw/source.html").status_code == 200
    assert client.get("/cache/raw/source.html").headers["content-security-policy"].startswith("sandbox")


def test_pdf_unavailable_has_clear_message(client, monkeypatch):
    from signalcourt.render import pdf

    run_id = run_stub(client)
    events(client, run_id)
    monkeypatch.setattr(pdf, "find_chrome", lambda: None)
    response = client.post(f"/api/runs/{run_id}/pdf")
    assert response.status_code == 503 and "Chrome" in response.json()["detail"]


def test_validation_and_missing_runs(client):
    assert client.post("/api/runs", json={"company": "", "mode": "warm"}).status_code == 422
    assert client.post("/api/runs", json={"company": "ALPHA", "mode": "other"}).status_code == 422
    assert client.post("/api/runs", json={"company": "ALPHA", "stub": ["oops"]}).status_code == 422
    assert client.get("/api/runs/missing/events").status_code == 404
    assert client.get("/runs/missing/report").status_code == 404
    assert client.post("/api/runs/missing/verify").status_code == 404


def test_startup_is_responsive_while_warming_and_rejects_run(home, monkeypatch):
    from signalcourt.web.service import RunManager

    entered, release = threading.Event(), threading.Event()
    original = RunManager.warmup

    def slow_warmup(self):
        entered.set()
        assert release.wait(10)
        original(self)

    monkeypatch.setattr(RunManager, "warmup", slow_warmup)
    try:
        with TestClient(create_app(home=home)) as client:
            assert entered.wait(5)
            assert client.get("/").status_code == 200
            assert client.get("/api/status").json()["state"] == "warming"
            assert client.post("/api/runs", json={"company": "ALPHA"}).status_code == 503
            release.set()
    finally:
        release.set()


def test_error_and_notes_redact_configured_keys(client, monkeypatch):
    from signalcourt import pipeline

    secret = "private-provider-canary"
    client.app.state.manager._secrets.append(secret)

    def fail(ctx):
        ctx.on_event("stage_note", {"stage": "sense", "note": f"upstream rejected {secret}"})
        raise RuntimeError(f"provider failed {secret}")

    monkeypatch.setitem(pipeline.STAGES["sense"], "stub", fail)
    stream = events(client, run_stub(client))
    assert secret not in json.dumps(stream)
    assert "[redacted]" in json.dumps(stream)


def test_mutations_from_another_origin_are_refused(client):
    response = client.post("/api/runs", json={"company": "ALPHA"}, headers={"Origin": "https://other.example"})
    assert response.status_code == 403


def test_pdf_success_downloads_exported_file(client, monkeypatch, home):
    from signalcourt.render import pdf

    run_id = run_stub(client)
    events(client, run_id)

    def export(html, pdf_path=None):
        assert "fonts.googleapis.com" not in html.read_text(encoding="utf-8")
        output = pdf_path or html.with_suffix(".pdf")
        output.write_bytes(b"%PDF-1.7\nfixture")
        return output

    monkeypatch.setattr(pdf, "find_chrome", lambda: "chrome")
    monkeypatch.setattr(pdf, "export_pdf", export)
    response = client.post(f"/api/runs/{run_id}/pdf")
    assert response.content.startswith(b"%PDF-")
    assert response.headers["content-type"] == "application/pdf"
    assert "attachment" in response.headers["content-disposition"]


def test_cleanup_failure_emits_error_and_releases_worker_lock(client, monkeypatch):
    from signalcourt import runner

    original = runner.close_run

    def broken_close(ctx):
        original(ctx)
        raise OSError("could not finish closing the store")

    with monkeypatch.context() as patch:
        patch.setattr(runner, "close_run", broken_close)
        run_id = run_stub(client)
        deadline = time.monotonic() + 2
        while not client.app.state.manager.sessions[run_id].finished and time.monotonic() < deadline:
            time.sleep(0.01)
        assert client.app.state.manager.sessions[run_id].finished
        stream = events(client, run_id)
    assert stream[-1][0] == "error"
    assert "closing the store" in stream[-1][1]["error"]
    assert events(client, run_stub(client))[-1][0] == "done"


def test_relative_report_assets_resolve_inside_run(client, home):
    run_id = run_stub(client)
    events(client, run_id)
    directory = home / "runs" / run_id
    (directory / "theme.css").write_text("body{color:navy}", encoding="utf-8")
    (directory / "report.html").write_text(
        '<link rel="stylesheet" href="theme.css"><a href="#evidence">Jump</a>', encoding="utf-8"
    )
    response = client.get(f"/runs/{run_id}/report")
    assert f"/runs/{run_id}/files/theme.css" in response.text
    assert 'href="#evidence"' in response.text
    assert client.get(f"/runs/{run_id}/files/theme.css").text == "body{color:navy}"


def test_report_uses_system_fonts_without_runtime_cdn(client, home):
    run_id = run_stub(client)
    events(client, run_id)
    report = home / "runs" / run_id / "report.html"
    report.write_text(
        '<link rel="preconnect" href="https://fonts.gstatic.com"><link href="https://fonts.googleapis.com/css2?family=Example" rel="stylesheet"><a href="https://public.example/source">Source</a>',
        encoding="utf-8",
    )
    html = client.get(f"/runs/{run_id}/report").text
    assert "fonts.googleapis.com" not in html and "fonts.gstatic.com" not in html
    assert "https://public.example/source" in html
