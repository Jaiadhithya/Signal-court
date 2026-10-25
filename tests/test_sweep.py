"""Sweep acceptance boundaries, incremental evidence, and replay network enforcement."""

import csv
import importlib
import json
import socket
import subprocess
from pathlib import Path

import httpx
import pytest


def sweep_module():
    return importlib.import_module("scripts.sweep")


def test_target_roles_filter_peers_and_minimal_csv_accepts_company(tmp_path):
    sweep = sweep_module()
    targets = tmp_path / "targets.csv"
    targets.write_text("symbol,role,subtype\nALPHA,track_a,bank\nBETA,peer,bank\nGAMMA,dev,nbfc\n", encoding="utf-8")
    assert [(t["symbol"], t["role"]) for t in sweep.read_targets(targets)] == [
        ("ALPHA", "track_a"), ("GAMMA", "dev")
    ]
    targets.write_text("company\nExample Finance Limited\n", encoding="utf-8")
    assert sweep.read_targets(targets) == [{"symbol": "", "company": "Example Finance Limited", "role": "", "subtype": ""}]


@pytest.mark.parametrize("mode,elapsed,verify,report,passed", [
    ("warm", 180.0, True, True, True),
    ("warm", 180.01, True, True, False),
    ("replay", 60.0, True, True, True),
    ("replay", 60.01, True, True, False),
    ("warm", 1.0, False, True, False),
    ("warm", 1.0, True, False, False),
])
def test_sweep_pass_requires_valid_report_within_end_to_end_cap(
    tmp_path, monkeypatch, mode, elapsed, verify, report, passed,
):
    sweep = sweep_module()
    targets = tmp_path / "targets.csv"
    targets.write_text("symbol\nALPHA\n", encoding="utf-8")
    report_path = tmp_path / "report.html"
    if report:
        report_path.write_text("report", encoding="utf-8")

    def worker(args, **kwargs):
        result_path = args[args.index("--result-file") + 1]
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump({"verify_ok": verify, "report_path": str(report_path), "error": "", "startup_s": 0.5}, f)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(sweep.subprocess, "run", worker)
    ticks = iter([0.0, elapsed])
    monkeypatch.setattr(sweep.time, "perf_counter", lambda: next(ticks))
    path = sweep.run_sweep(targets, mode, tmp_path / "workspace")
    with path.open(newline="", encoding="utf-8") as f:
        row, = csv.DictReader(f)
    assert row["pass"] == str(passed)
    assert float(row["total_s"]) == elapsed


def test_failed_worker_row_is_flushed_before_next_target_and_secrets_are_not_recorded(tmp_path, monkeypatch):
    sweep = sweep_module()
    targets = tmp_path / "targets.csv"
    targets.write_text("symbol,role\nBROKEN,dev\nOK,track_a\n", encoding="utf-8")
    home = tmp_path / "workspace"
    report = tmp_path / "report.html"
    report.write_text("report", encoding="utf-8")

    def worker(args, **kwargs):
        if args[args.index("--symbol") + 1] == "BROKEN":
            raise RuntimeError("secret-provider-key-must-not-escape")
        path, = home.glob("sweep_*_warm.csv")
        with path.open(newline="", encoding="utf-8") as f:
            first, = csv.DictReader(f)
        assert first["symbol"] == "BROKEN" and first["pass"] == "False"
        result = {"verify_ok": True, "report_path": str(report), "error": ""}
        with open(args[args.index("--result-file") + 1], "w", encoding="utf-8") as f:
            json.dump(result, f)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(sweep.subprocess, "run", worker)
    path = sweep.run_sweep(targets, "warm", home)
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [(r["symbol"], r["pass"]) for r in rows] == [("BROKEN", "False"), ("OK", "True")]
    assert "secret-provider-key" not in path.read_text(encoding="utf-8")
    assert rows[0]["error"] == "RuntimeError"


def test_replay_guard_blocks_httpx_and_raw_socket_and_restores_them():
    sweep = sweep_module()
    original_send = httpx.Client.send
    original_connect = socket.socket.connect
    with sweep.block_network():
        with pytest.raises(sweep.NetworkBlocked):
            httpx.Client().get("https://example.test")
        with socket.socket() as sock, pytest.raises(sweep.NetworkBlocked):
            sock.connect(("127.0.0.1", 9))
    assert httpx.Client.send is original_send
    assert socket.socket.connect is original_connect


def test_worker_timeout_retains_incremental_run_evidence(tmp_path, monkeypatch):
    sweep = sweep_module()
    targets = tmp_path / "targets.csv"
    targets.write_text("symbol\nALPHA\n", encoding="utf-8")

    def worker(args, **kwargs):
        with open(args[args.index("--result-file") + 1], "w", encoding="utf-8") as f:
            json.dump({"run_dir": "saved-run", "harvest_s": 1.5, "error": ""}, f)
        raise subprocess.TimeoutExpired(args, 240)

    monkeypatch.setattr(sweep.subprocess, "run", worker)
    path = sweep.run_sweep(targets, "warm", tmp_path / "workspace")
    with path.open(newline="", encoding="utf-8") as f:
        row, = csv.DictReader(f)
    assert row["run_dir"] == "saved-run" and row["harvest_s"] == "1.5"
    assert row["pass"] == "False" and row["error"] == "TimeoutExpired"


def test_worker_failure_saves_verifiable_run_and_safe_stage_diagnostics(tmp_path, workspace):
    from datetime import date

    sweep = sweep_module()
    home, _store = workspace
    result = tmp_path / "result.json"
    row = sweep.worker("Unknown Entity", "ABSENT", "replay", home, date(2026, 10, 1), result)
    saved = json.loads(result.read_text(encoding="utf-8"))
    progress = json.loads((Path(row["run_dir"]) / "sweep-progress.json").read_text(encoding="utf-8"))
    assert row["error"] == "UnknownCompany" and row["verify_ok"] is True
    assert saved["run_dir"] == row["run_dir"] and saved["harvest_s"] >= 0
    assert progress["stage"] == "harvest" and progress["status"] == "error"
    assert row["network_attempts"] == 0 and row["startup_s"] >= 0


@pytest.mark.parametrize("field,first,other", [
    ("symbol", "ALPHA", "BETA"),
    ("company", "Alpha Finance", "Beta Finance"),
])
def test_rerun_replaces_latest_canonical_row_and_preserves_attempt_history(
    tmp_path, monkeypatch, field, first, other,
):
    sweep = sweep_module()
    targets = tmp_path / "targets.csv"
    targets.write_text(f"{field}\n{first}\n{other}\n", encoding="utf-8")
    attempts = iter(["run-first", "run-other", "run-latest"])

    def worker(args, **kwargs):
        with open(args[args.index("--result-file") + 1], "w", encoding="utf-8") as f:
            json.dump({"run_dir": next(attempts), "error": ""}, f)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(sweep.subprocess, "run", worker)
    path = sweep.run_sweep(targets, "warm", tmp_path / "workspace")
    targets.write_text(f"{field}\n{first}\n", encoding="utf-8")
    sweep.run_sweep(targets, "warm", tmp_path / "workspace")
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [(r[field], r["run_dir"]) for r in rows] == [(first, "run-latest"), (other, "run-other")]
    with path.with_suffix(".history.csv").open(newline="", encoding="utf-8") as f:
        history = list(csv.DictReader(f))
    assert [r["run_dir"] for r in history] == ["run-first", "run-other", "run-latest"]


def test_existing_duplicate_attempts_migrate_to_history_once_before_upsert(tmp_path, monkeypatch):
    from datetime import date

    sweep = sweep_module()
    home = tmp_path / "workspace"
    home.mkdir()
    output = home / f"sweep_{date.today().isoformat()}_warm.csv"
    with output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=sweep.COLUMNS)
        writer.writeheader()
        writer.writerows([
            {"symbol": "ALPHA", "run_dir": "old-first"},
            {"symbol": "BETA", "run_dir": "other-saved"},
            {"symbol": "ALPHA", "run_dir": "old-latest"},
        ])
    targets = tmp_path / "targets.csv"
    targets.write_text("symbol\nALPHA\n", encoding="utf-8")

    def worker(args, **kwargs):
        with open(args[args.index("--result-file") + 1], "w", encoding="utf-8") as f:
            json.dump({"run_dir": "new-latest", "error": ""}, f)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(sweep.subprocess, "run", worker)
    sweep.run_sweep(targets, "warm", home)
    with output.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [(r["symbol"], r["run_dir"]) for r in rows] == [("ALPHA", "new-latest"), ("BETA", "other-saved")]
    targets.write_text("symbol\n", encoding="utf-8")
    sweep.run_sweep(targets, "warm", home)
    with output.with_suffix(".history.csv").open(newline="", encoding="utf-8") as f:
        history = list(csv.DictReader(f))
    assert [r["run_dir"] for r in history] == ["old-first", "other-saved", "old-latest", "new-latest"]
