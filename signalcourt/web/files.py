"""Confine browser file access and translate local report links to HTTP routes."""

from __future__ import annotations

import re
from html import escape, unescape
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from urllib.request import url2pathname

from fastapi import HTTPException

PUBLIC_SUFFIXES = {
    ".html",
    ".htm",
    ".pdf",
    ".css",
    ".js",
    ".png",
    ".jpg",
    ".jpeg",
    ".svg",
    ".webp",
    ".gif",
    ".txt",
    ".xml",
    ".xbrl",
    ".bin",
    ".parquet",
}


def offline_fonts(html: str) -> str:
    """Keep the renderer's system-font fallbacks without contacting a font CDN."""

    def replace(match):
        href = re.search(r"""\bhref\s*=\s*(["'])(.*?)\1""", match.group(0), flags=re.I)
        if href and urlsplit(unescape(href.group(2))).hostname in {"fonts.googleapis.com", "fonts.gstatic.com"}:
            return ""
        return match.group(0)

    return re.sub(r"<link\b[^>]*>", replace, html, flags=re.I)


def confined(root: Path, relative: str, *, public: bool = False) -> Path:
    decoded = unquote(relative)
    if not decoded or "\\" in decoded or ":" in decoded or decoded.startswith("/"):
        raise HTTPException(404, "File not found")
    parts = decoded.split("/")
    if any(p in {"", ".", ".."} or p.startswith(".") for p in parts):
        raise HTTPException(404, "File not found")
    path = (root / decoded).resolve()
    if not path.is_relative_to(root.resolve()) or (public and path.suffix.lower() not in PUBLIC_SUFFIXES):
        raise HTTPException(404, "File not found")
    return path


def run_directory(home: Path, run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise HTTPException(404, "Run not found")
    path = confined(home / "runs", run_id)
    if not (path / "ledger.sqlite").is_file():
        raise HTTPException(404, "Run not found")
    return path


def browser_report(html: str, run_dir: Path, home: Path) -> str:
    def replace(match):
        attribute, delimiter, raw = match.groups()
        url = urlsplit(unescape(raw))
        if not url.scheme and not url.netloc and url.path and not url.path.startswith("/"):
            try:
                confined(run_dir, url.path, public=True)
            except HTTPException:
                return f"{attribute}={delimiter}#{delimiter}"
            target = f"/runs/{quote(run_dir.name)}/files/{quote(unquote(url.path), safe='/')}"
            if url.fragment:
                target += "#" + quote(url.fragment, safe="=")
            return f"{attribute}={delimiter}{escape(target, quote=True)}{delimiter}"
        if url.scheme.lower() != "file":
            return match.group(0)
        if url.netloc and url.netloc != "localhost":
            return f"{attribute}={delimiter}#{delimiter}"
        path = Path(url2pathname(unquote(url.path))).resolve()
        for root, prefix in ((run_dir, f"/runs/{quote(run_dir.name)}/files/"), (home / "cache", "/cache/")):
            if path.is_relative_to(root.resolve()):
                rel = path.relative_to(root.resolve()).as_posix()
                try:
                    confined(root, rel, public=True)
                except HTTPException:
                    break
                target = prefix + quote(rel, safe="/")
                if url.fragment:
                    target += "#" + quote(url.fragment, safe="=")
                return f"{attribute}={delimiter}{escape(target, quote=True)}{delimiter}"
        return f"{attribute}={delimiter}#{delimiter}"

    return re.sub(r"""\b(href|src)\s*=\s*(["'])(.*?)\2""", replace, offline_fonts(html), flags=re.I)
