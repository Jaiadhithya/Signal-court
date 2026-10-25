"""Optional local PDF export with a discovered headless Chrome executable."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

CHROME_LOCATIONS = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
)


def find_chrome() -> Path | str | None:
    """Prefer CHROME_PATH, then standard Windows installs, then executables on PATH."""
    configured = os.environ.get("CHROME_PATH", "").strip().strip('"')
    if configured:
        path = Path(os.path.expandvars(configured)).expanduser()
        if path.is_file():
            return path
    for path in CHROME_LOCATIONS:
        if path.is_file():
            return path
    for name in ("chrome", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return None


def export_pdf(html_path: Path, pdf_path: Path | None = None) -> Path | None:
    """Return the newly written PDF, or None when the optional browser export is unavailable."""
    html_path = Path(html_path)
    output = Path(pdf_path) if pdf_path is not None else html_path.with_suffix(".pdf")
    chrome = find_chrome()
    if chrome is None or not html_path.is_file():
        return None
    before = _signature(output)
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="signalcourt-pdf-") as profile:
            args = [
                str(chrome),
                "--headless=new",
                "--disable-gpu",
                "--no-pdf-header-footer",
                "--virtual-time-budget=8000",
                f"--user-data-dir={profile}",
                f"--print-to-pdf={output.resolve()}",
                html_path.resolve().as_uri(),
            ]
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            result = subprocess.run(args, capture_output=True, timeout=45, check=False, **options)
        if result.returncode != 0 or _signature(output) in (None, before):
            return None
        with output.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                return None
        return output
    except (OSError, subprocess.SubprocessError):
        return None
