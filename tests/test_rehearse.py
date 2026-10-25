"""Exercise the rehearsal shell script without providers or browser side effects."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rehearse.sh"


@pytest.fixture
def shell() -> str:
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    if os.name == "nt" and git_bash.is_file():
        return str(git_bash)
    executable = shutil.which("bash")
    if executable is None or (os.name == "nt" and "windowsapps" in executable.lower()):
        pytest.skip("Rehearsal behavior tests require Bash (Git Bash on Windows); Bash is unavailable")
    return executable


@pytest.fixture
def rehearsal(tmp_path, shell):
    boundary = tmp_path / "fake commands"
    boundary.mkdir()
    capture = tmp_path / "captured arguments"
    capture.mkdir()
    fake = boundary / "uv"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'command="$4"\n'
        'printf "%s\\0" "$@" > "$FAKE_CAPTURE/$command.argv"\n'
        'pwd -P > "$FAKE_CAPTURE/$command.cwd"\n'
        'if [[ "$command" == run ]]; then\n'
        '  printf "harvest progress\\n"\n'
        '  sleep "${FAKE_RUN_DELAY:-0}"\n'
        '  printf "%s\\r\\n" "$FAKE_REPORT"\n'
        '  exit "${FAKE_RUN_STATUS:-0}"\n'
        "fi\n"
        'sleep "${FAKE_VERIFY_DELAY:-0}"\n'
        'printf "verification progress\\n"\n'
        'exit "${FAKE_VERIFY_STATUS:-0}"\n',
        encoding="utf-8",
        newline="\n",
    )
    fake.chmod(0o755)
    elsewhere = tmp_path / "outside repository"
    elsewhere.mkdir()
    env = os.environ.copy()
    env.update(
        PATH=str(boundary) + os.pathsep + env["PATH"],
        FAKE_CAPTURE=capture.as_posix(),
        FAKE_REPORT="/tmp/actual run with spaces/report.html",
    )

    def invoke(*args, **overrides):
        return subprocess.run(
            [shell, str(SCRIPT), *args],
            cwd=elsewhere,
            env={**env, **overrides},
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    return invoke, capture


def arguments(capture, command):
    return (capture / f"{command}.argv").read_bytes().decode().rstrip("\0").split("\0")


def wall_seconds(result):
    timing = re.search(r"Total wall seconds: (\d+(?:\.\d+)?)", result.stdout)
    assert timing, result.stdout + result.stderr
    return float(timing[1])


@pytest.mark.parametrize(
    ("options", "mode"),
    [([], "warm"), (["--fallback"], "replay")],
)
def test_runs_company_as_one_argument_from_repository_root(rehearsal, options, mode):
    invoke, capture = rehearsal
    result = invoke(*options, "Example Finance & Holdings")

    assert result.returncode == 0, result.stdout + result.stderr
    assert arguments(capture, "run") == [
        "run", "--frozen", "signal", "run", "--company", "Example Finance & Holdings", "--mode", mode, "--open"
    ]
    cwd = (capture / "run.cwd").read_text().strip()
    if os.name == "nt":
        cwd = re.sub(r"^/([A-Za-z])/", r"\1:/", cwd)
    assert Path(cwd).resolve() == ROOT
    assert "harvest progress" in result.stdout
    assert "verification progress" in result.stdout
    wall_seconds(result)


def test_fallback_after_company_and_windows_report_path(rehearsal):
    invoke, capture = rehearsal
    result = invoke(
        "Example Finance", "--fallback", FAKE_REPORT=r"C:\report archive\actual run\report.html"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert arguments(capture, "run")[-3:] == ["--mode", "replay", "--open"]
    assert arguments(capture, "verify") == [
        "run", "--frozen", "signal", "verify", "C:/report archive/actual run"
    ]


def test_verifies_printed_posix_report_parent(rehearsal):
    invoke, capture = rehearsal
    result = invoke("Example Finance")

    assert result.returncode == 0, result.stdout + result.stderr
    assert arguments(capture, "verify") == [
        "run", "--frozen", "signal", "verify", "/tmp/actual run with spaces"
    ]


def test_failed_pipeline_returns_status_without_verifying_even_if_it_prints_report(rehearsal):
    invoke, capture = rehearsal
    result = invoke("Example Finance", FAKE_RUN_STATUS="23")

    assert result.returncode == 23, result.stdout + result.stderr
    assert not (capture / "verify.argv").exists()
    wall_seconds(result)


def test_failed_verification_returns_status_and_total_includes_both_processes(rehearsal):
    invoke, capture = rehearsal
    result = invoke(
        "Example Finance", FAKE_VERIFY_STATUS="17", FAKE_RUN_DELAY="1", FAKE_VERIFY_DELAY="1"
    )

    assert result.returncode == 17, result.stdout + result.stderr
    assert (capture / "verify.argv").is_file()
    assert wall_seconds(result) >= 2


def test_success_without_report_fails_instead_of_verifying_latest_run(rehearsal):
    invoke, capture = rehearsal
    result = invoke("Example Finance", FAKE_REPORT="no report was produced")

    assert result.returncode != 0
    assert not (capture / "verify.argv").exists()
    wall_seconds(result)
