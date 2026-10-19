"""Name-grep: no company names, symbols, ISINs or BSE codes in code or data.

The blocklist is built at test time from tests/data/nifty50_constituents.csv plus tests/data/extra_names.txt
(non-NIFTY development companies). Scanned: signalcourt/, prompts/, registry/, config/. Names are allowed in
tests/, fixtures/, backtests/, holdout/. "NSE" and "BSE" are allowed as exchange names in signalcourt/net/,
registry/sources.yaml and registry/datasets.yaml only (BSE Ltd is also a Track A company).
"""
import csv
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = Path(__file__).resolve().parent / "data"
SCAN_DIRS = ["signalcourt", "prompts", "registry", "config"]
SCAN_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".toml", ".txt", ".json", ".html", ".css", ".js", ".csv", ""}
EXCHANGE_NAME_OK = ("signalcourt/net/", "registry/sources.yaml", "registry/datasets.yaml")
EXCHANGE_NAMES = {"BSE", "NSE"}
LEGAL_SUFFIX = re.compile(r"\s+(ltd\.?|limited)\s*$", re.I)


def _blocklist():
    """Returns list of (label, compiled regex, is_exchange_name)."""
    terms = {}  # term -> case_sensitive

    def add(term, case_sensitive):
        term = term.strip()
        if len(term) < 3:
            return  # "LT", "M&M" etc. are too ambiguous to grep; ISIN and full names still catch them
        terms[term] = terms.get(term, False) or case_sensitive

    with open(DATA / "nifty50_constituents.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            full = row["company_name"].strip()
            short = LEGAL_SUFFIX.sub("", full)
            for name in (full, short):
                # One-word names ("Titan", "Trent", "Eternal") only match as written, to avoid prose hits.
                add(name, case_sensitive=" " not in name)
            add(row["symbol"], True)
            add(row["isin"], True)
            code = row["bse_scrip_code"].strip()
            if code:
                add(code, True)
    with open(DATA / "extra_names.txt", encoding="utf-8") as f:
        for line in f:
            if line.strip() and not line.startswith("#"):
                for name in line.strip().split("|"):
                    add(name, case_sensitive=" " not in name)
    out = []
    for term, cs in terms.items():
        pat = re.compile(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", 0 if cs else re.I)
        out.append((term, pat, term in EXCHANGE_NAMES))
    return out


def _files():
    for d in SCAN_DIRS:
        base = REPO_ROOT / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file() and p.suffix in SCAN_SUFFIXES and "__pycache__" not in p.parts:
                yield p


def find_hits(files, blocklist):
    hits = []
    for p in files:
        rel = p.relative_to(REPO_ROOT).as_posix()
        text = p.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            for term, pat, is_exchange in blocklist:
                if pat.search(line):
                    if is_exchange and rel.startswith(EXCHANGE_NAME_OK):
                        continue
                    hits.append(f"{rel}:{lineno}: '{term}' in: {line.strip()[:100]}")
    return hits


def test_no_company_names_in_code_or_data():
    hits = find_hits(_files(), _blocklist())
    assert not hits, "company identifiers found outside tests/fixtures/backtests/holdout:\n" + "\n".join(hits)


def test_blocklist_catches_planted_names(tmp_path):
    # Mutation check: the grep must fire on planted names, and respect the exchange exemption.
    planted = tmp_path / "planted.txt"
    planted.write_text("HDFC Bank\nINE090A01021\nCHOLAFIN\nBSE\nthe eternal question\n", encoding="utf-8")
    bl = _blocklist()
    # find_hits needs repo-relative paths, so check the matcher directly.
    text = planted.read_text()
    found = {t for t, pat, _ in bl if pat.search(text)}
    assert {"HDFC Bank", "INE090A01021", "CHOLAFIN", "BSE"} <= found
    assert "Eternal" not in found  # one-word names are case-sensitive; prose "eternal" is not a hit


def test_exchange_names_exempt_only_in_listed_paths():
    bl = _blocklist()
    bse = [b for b in bl if b[0] == "BSE"]
    assert bse and bse[0][2] is True


def test_holdout_dir_holds_only_a_hash():
    d = REPO_ROOT / "holdout"
    allowed = {"names.sha256", ".gitkeep", "README.md"}
    extra = [p.name for p in d.iterdir() if p.name not in allowed]
    assert not extra, f"holdout/ may only contain the sealed hash; found {extra}"
