"""Loopback-only FastAPI routes for the stage operator."""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sse_starlette.sse import EventSourceResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .data import companies, past_runs, resolve_target
from .files import browser_report, confined, offline_fonts, run_directory
from .service import BusyError, NotReadyError, RunManager

ROOT = Path(__file__).resolve().parent
STAGES = ("harvest", "measure", "sense", "prereg", "court", "judge", "report")


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    company: str = Field(min_length=1, max_length=200)
    symbol: str | None = Field(default=None, max_length=50)
    mode: Literal["warm", "replay", "live"] = "warm"
    stub: list[str] = Field(default_factory=list, max_length=7)

    @field_validator("stub")
    @classmethod
    def valid_stages(cls, value):
        if set(value) - set(STAGES):
            raise ValueError("Unknown stage in stub override")
        return value


def create_app(*, home: Path | None = None) -> FastAPI:
    if home is None:
        from signalcourt.settings import load_settings

        home = load_settings().workspace.home
    home = Path(home).resolve()
    manager = RunManager(home)

    @asynccontextmanager
    async def lifespan(app):
        threading.Thread(target=manager.warmup, name="console-warmup", daemon=True).start()
        yield

    app = FastAPI(title="Signal Court", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.manager = manager
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "Open the console on its local address to use this action."}, 403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index():
        env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape())
        return env.get_template("index.html").render(stages=STAGES)

    @app.get("/api/status")
    def status():
        return manager.status()

    @app.get("/api/companies")
    def targets():
        return companies(home)

    @app.get("/api/resolve")
    def resolve(q: str = Query(min_length=1, max_length=200)):
        if not manager.ready.is_set():
            raise HTTPException(503, "Warming up. Please wait for Ready.")
        return resolve_target(home, q)

    @app.post("/api/runs", status_code=202)
    def start(payload: RunRequest):
        if manager.active:
            raise HTTPException(409, "A run is in progress. Wait for it to finish.")
        if not manager.ready.is_set():
            raise HTTPException(503, "Warming up. Please wait for Ready.")
        resolved = resolve_target(home, payload.company, payload.symbol)
        if resolved["status"] != "match":
            raise HTTPException(422, resolved)
        request = payload.model_dump()
        request["symbol"] = resolved["match"]["symbol"]
        try:
            session = manager.start(request)
        except BusyError as exc:
            raise HTTPException(409, str(exc)) from exc
        except NotReadyError as exc:
            raise HTTPException(503, str(exc)) from exc
        return {"id": session.id, "events": f"/api/runs/{session.id}/events"}

    @app.get("/api/runs")
    def history():
        return past_runs(home)

    @app.get("/api/runs/{run_id}/events")
    async def stream(run_id: str, request: Request):
        run_directory(home, run_id)
        session = manager.sessions.get(run_id)
        if session is None:
            raise HTTPException(404, "Live events are unavailable after a server restart. Open this run in Past runs.")
        try:
            after = max(0, int(request.headers.get("last-event-id", request.query_params.get("after", "0"))))
        except ValueError as exc:
            raise HTTPException(400, "Invalid event id") from exc

        async def generate():
            cursor = min(after, len(session.events))
            while True:
                batch, finished = await asyncio.to_thread(session.next_events, cursor)
                for event in batch:
                    cursor = event["id"]
                    yield {"id": str(event["id"]), "event": event["event"], "data": json.dumps(event["data"])}
                if finished or await request.is_disconnected():
                    return

        return EventSourceResponse(generate(), ping=10)

    def saved_run(run_id):
        directory = run_directory(home, run_id)
        if manager.active and manager.active.id == run_id:
            raise HTTPException(409, "This run is still in progress. Wait for it to finish.")
        return directory

    def html_response(path, directory):
        return HTMLResponse(browser_report(path.read_text(encoding="utf-8"), directory, home))

    @app.get("/runs/{run_id}/report")
    def report(run_id: str):
        directory = saved_run(run_id)
        path = confined(directory, "report.html", public=True)
        if not path.is_file():
            raise HTTPException(404, "This run has no finished report. Choose another past run or re-run in replay.")
        return html_response(path, directory)

    @app.get("/runs/{run_id}/files/{relative:path}")
    def run_file(run_id: str, relative: str):
        directory = saved_run(run_id)
        path = confined(directory, relative, public=True)
        if not path.is_file():
            raise HTTPException(404, "File not found")
        if path.suffix.lower() in {".html", ".htm"}:
            return html_response(path, directory)
        return FileResponse(path)

    @app.get("/cache/{relative:path}")
    def cache_file(relative: str):
        path = confined(home / "cache", relative, public=True)
        if not path.is_file():
            raise HTTPException(404, "Cached source not found")
        headers = {"Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'"}
        return FileResponse(path, headers=headers)

    @app.post("/api/runs/{run_id}/verify")
    def verify(run_id: str):
        from signalcourt.ledger import verify_run

        result = verify_run(saved_run(run_id))
        return {
            "status": "OK" if result.ok else "FAIL",
            "ok": result.ok,
            "records": result.records,
            "head_hash": result.head_hash,
            "problems": [{"seq": p.seq, "kind": p.kind} for p in result.problems],
        }

    @app.post("/api/runs/{run_id}/pdf")
    def pdf(run_id: str):
        from signalcourt.render.pdf import export_pdf, find_chrome

        directory = saved_run(run_id)
        if not (directory / "report.html").is_file():
            raise HTTPException(404, "This run has no finished report.")
        if find_chrome() is None:
            raise HTTPException(503, "Chrome not found. Install Chrome or set CHROME_PATH; the HTML report is ready.")
        report_path = confined(directory, "report.html", public=True)
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".html", prefix=".console-export-", dir=directory, delete=False
        ) as source:
            source.write(offline_fonts(report_path.read_text(encoding="utf-8")))
            export_source = Path(source.name)
        try:
            output = export_pdf(export_source, directory / "report.pdf")
        finally:
            export_source.unlink(missing_ok=True)
        if output is None:
            raise HTTPException(503, "Chrome could not export the PDF. Open the report and use Print to save a PDF.")
        return FileResponse(output, media_type="application/pdf", filename=f"signal-court-{run_id}.pdf")

    return app
