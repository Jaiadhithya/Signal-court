import json
import re
from pathlib import Path

from typer.testing import CliRunner

from signalcourt import __version__
from signalcourt.cli import app
from signalcourt.pipeline import STAGE_ORDER
from signalcourt.settings import config_digest


def test_version():
    r = CliRunner().invoke(app, ["version"])
    assert r.exit_code == 0 and r.stdout.strip() == __version__


def test_config_digest_lists_settings():
    r = CliRunner().invoke(app, ["config-digest"])
    assert r.exit_code == 0 and "config/settings.toml" in r.stdout


def _run(tmp_path, monkeypatch, *extra):
    monkeypatch.setenv("SIGNAL_HOME", str(tmp_path))
    r = CliRunner().invoke(app, ["run", "--company", "Test Co", "--mode", "replay", *extra])
    assert r.exit_code == 0, r.output
    card = Path(r.output.strip().splitlines()[-1])
    return r, card


def test_run_replay_on_stubs_prints_card_path(tmp_path, monkeypatch):
    r, card = _run(tmp_path, monkeypatch)
    assert "finding.html" in r.output and card.exists()
    assert card.parent.parent == tmp_path / "runs"
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", card.parent.name)
    lines = r.output.strip().splitlines()
    assert [ln.split()[0] for ln in lines[:7]] == list(STAGE_ORDER)
    assert all(re.fullmatch(r"\S+\s+(stub|real)\s+\d+\s+\d+\.\d+s", ln) for ln in lines[:7])


def test_run_records_config_digest_and_git_sha(tmp_path, monkeypatch):
    _, card = _run(tmp_path, monkeypatch, "--as-of", "2026-09-30", "--stub", "harvest")
    run = json.loads((card.parent / "ledger.jsonl").read_text(encoding="utf-8").splitlines()[0])
    p = run["payload"]
    digest = config_digest()
    assert p["company_input"] == "Test Co" and p["mode"] == "replay" and p["as_of"] == "2026-09-30"
    assert p["settings_hash"] == digest["config/settings.toml"]
    assert p["model_pools_hash"] == digest["config/models.yaml"]
    assert p["registry_hashes"] and all(k.startswith("registry/") for k in p["registry_hashes"])
    assert re.fullmatch(r"[0-9a-f]{40}|unknown", p["code_git_sha"])


def test_unknown_stub_stage_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNAL_HOME", str(tmp_path))
    r = CliRunner().invoke(app, ["run", "--company", "Test Co", "--mode", "replay", "--stub", "nope"])
    assert r.exit_code != 0


def test_two_runs_get_two_run_dirs(tmp_path, monkeypatch):
    _, a = _run(tmp_path, monkeypatch)
    _, b = _run(tmp_path, monkeypatch)
    assert a.parent != b.parent and a.exists() and b.exists()


def test_verify_accepts_a_clean_run(tmp_path, monkeypatch):
    _, card = _run(tmp_path, monkeypatch)
    r = CliRunner().invoke(app, ["verify", str(card.parent)])
    assert r.exit_code == 0 and re.fullmatch(r"OK \d+ records, head [0-9a-f]{16}", r.output.strip())


def test_verify_flags_a_tampered_jsonl(tmp_path, monkeypatch):
    _, card = _run(tmp_path, monkeypatch)
    jl = card.parent / "ledger.jsonl"
    data = bytearray(jl.read_bytes())
    i = data.index(b'"STUB"') + 1  # inside a payload, not the hash field
    data[i] ^= 0x01
    jl.write_bytes(bytes(data))
    r = CliRunner().invoke(app, ["verify", str(card.parent)])
    assert r.exit_code == 1 and r.output.startswith("BROKEN at seq ")


def test_verify_refuses_a_dir_without_ledger(tmp_path):
    r = CliRunner().invoke(app, ["verify", str(tmp_path)])
    assert r.exit_code == 1 and "BROKEN" in r.output
    assert not (tmp_path / "ledger.sqlite").exists()
