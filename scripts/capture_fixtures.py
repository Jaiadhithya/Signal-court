"""Moonveil day-1 fixture capture.

Downloads a small, real fixture pack for the development companies from NSE and
checks the endpoint details the build depends on (exact issuer names, row counts,
quarters of history, field names). Public filings only; no API keys needed.

Run on your laptop, on your home network:
    python scripts/capture_fixtures.py   (from the repo root)
Needs only `requests` (already installed). Expect 10-20 minutes, mostly annual
reports. It prints a SUMMARY block at the end.

Output, in the repo root:
    fixtures_v0/<SYMBOL>/...        XBRL, decks, transcripts, rating letters, listings (.json.gz)
    fixtures_v0_large/<SYMBOL>/...  annual reports (too big for git; they go into a release asset)
    fixtures_v0/manifest.csv        one row per file: symbol, kind, date, url, sha256, bytes
    fixtures_v0/report.md           the summary
Re-running skips files already downloaded.
"""
import csv
import datetime
import gzip
import hashlib
import json
import pathlib
import re
import time

import requests

# Development set. The sub-type is only for the report.
DEV_SET = {
    "ICICIBANK": "bank",
    "CHOLAFIN": "nbfc",
    "ICICIPRULI": "insurer_life",
    "BAJAJFINSV": "holding",
    "JIOFIN": "holding",
    "BSE": "generic",
}
N_DECKS, N_TRANSCRIPTS, N_RATINGS, N_ANNUAL_REPORTS, N_XBRL_EACH = 2, 2, 1, 2, 2

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0 Safari/537.36")
API = "https://www.nseindia.com/api/"
HERE = pathlib.Path(__file__).resolve().parent.parent  # repo root (script lives in scripts/)
SMALL = HERE / "fixtures_v0"
LARGE = HERE / "fixtures_v0_large"
DATE_KEYS = ("toDate", "to_date", "period_ended", "periodEnded", "qe_Date", "qeDate", "period", "an_dt", "sort_date")
DATE_FORMATS = ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d %b %Y")

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.5"})
_last_call = [0.0]


def throttle(gap: float = 1.0) -> None:
    wait = _last_call[0] + gap - time.time()
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()


def fetch(url: str, params: dict | None = None, referer_symbol: str = "ICICIBANK") -> requests.Response:
    """GET with a 1 req/s throttle, retries on 429/5xx/network errors and one cookie re-prime on 401/403."""
    headers = {"Referer": f"https://www.nseindia.com/get-quotes/equity?symbol={referer_symbol}"}
    primed = False
    for attempt in range(4):
        throttle()
        try:
            r = session.get(url, params=params, headers=headers, timeout=60)
        except requests.RequestException:
            time.sleep(2 ** (attempt + 1))
            continue
        if r.status_code in (401, 403) and not primed:
            primed = True
            throttle()
            try:
                session.get("https://www.nseindia.com/", timeout=30)
            except requests.RequestException:
                pass
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 ** (attempt + 1))
            continue
        r.raise_for_status()
        return r
    raise RuntimeError(f"gave up on {url}")


def rows_of(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "Table", "rows"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def parse_date(value):
    if not isinstance(value, str):
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.datetime.strptime(value.strip(), fmt)
        except ValueError:
            pass
    return None


def row_date(row: dict):
    for key in DATE_KEYS:
        d = parse_date(row.get(key))
        if d:
            return d
    return None


def newest_first(rows: list) -> list:
    dated = [(row_date(r), i, r) for i, r in enumerate(rows)]
    if all(d is None for d, _, _ in dated):
        return rows
    return [r for d, i, r in sorted(dated, key=lambda t: (t[0] or datetime.datetime.min, -t[1]), reverse=True)]


def urls_in(row: dict, suffixes=(".xml", ".pdf", ".zip")) -> list:
    return [v for v in row.values() if isinstance(v, str) and v.startswith("http") and v.lower().endswith(suffixes)]


def save_listing(symbol: str, name: str, data) -> None:
    path = SMALL / symbol / f"{name}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(json.dumps(data, ensure_ascii=False).encode("utf-8")))


MANIFEST: list = []


def download(symbol: str, kind: str, url: str, date, large: bool = False):
    base = LARGE if large else SMALL
    name = re.sub(r"[^A-Za-z0-9._-]", "_", url.rsplit("/", 1)[-1])[:150]
    path = base / symbol / kind / name
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(fetch(url, referer_symbol=symbol).content)
    data = path.read_bytes()
    MANIFEST.append({"symbol": symbol, "kind": kind, "date": date.strftime("%Y-%m-%d") if date else "",
                     "url": url, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                     "path": str(path.relative_to(HERE))})
    return len(data)


def issuer_candidates(symbol: str, legacy_rows: list) -> list:
    names = []
    for row in legacy_rows[:5]:
        for key, value in row.items():
            if "name" in key.lower() and isinstance(value, str) and re.search(r"[A-Za-z]{3}", value):
                names.append(value.strip())
    try:
        info = fetch(API + "quote-equity", {"symbol": symbol}, symbol).json().get("info", {})
        if info.get("companyName"):
            names.append(info["companyName"].strip())
    except Exception:
        pass
    out = []
    for n in names:
        for variant in (n, re.sub(r"\bLtd\.?$", "Limited", n), re.sub(r"\bLimited$", "Ltd", n)):
            if variant not in out:
                out.append(variant)
    return out


def integrated_rows(symbol: str, issuer: str):
    params = {"index": "equities", "symbol": symbol, "issuer": issuer, "period_ended": "all",
              "type": "Integrated Filing- Financials", "page": 1, "size": 50}
    first = fetch(API + "integrated-filing-results", params, symbol).json()
    rows = rows_of(first)
    total = first.get("totalCount", len(rows)) if isinstance(first, dict) else len(rows)
    page = 1
    while len(rows) < (total or 0) and page < 6:
        page += 1
        params["page"] = page
        more = rows_of(fetch(API + "integrated-filing-results", params, symbol).json())
        if not more:
            break
        rows += more
    return rows, total


def classify(row: dict):
    text = f"{row.get('desc', '')} {row.get('attchmntText', '')}".lower()
    if "transcript" in text:
        return "transcript"
    if re.search(r"investor presentation|analyst presentation|earnings presentation|investors? (meet|call) presentation", text):
        return "deck"
    if "credit rating" in text or re.search(r"\brating\b", str(row.get("desc", "")).lower()):
        return "rating_letter"
    return None


def quarters(rows: list) -> int:
    ends = set()
    for r in rows:
        for key in ("toDate", "to_date", "period_ended", "periodEnded", "qe_Date", "qeDate", "period"):
            d = parse_date(r.get(key))
            if d:
                ends.add(d.date())
                break
    return len(ends)


def capture(symbol: str, subtype: str) -> dict:
    rep = {"symbol": symbol, "expected_subtype": subtype, "errors": [], "keys": {}}

    # Legacy results (Reg 33 XBRL, to Dec 2024). Insurers may need another index value.
    legacy = []
    for index in ("equities", "insurance"):
        try:
            legacy = rows_of(fetch(API + "corporates-financial-results",
                                   {"index": index, "period": "Quarterly", "symbol": symbol}, symbol).json())
        except Exception as e:
            rep["errors"].append(f"legacy index={index}: {e}")
            legacy = []
        if legacy:
            rep["legacy_index"] = index
            break
    rep["legacy_rows"] = len(legacy)
    rep["legacy_quarters"] = quarters(legacy)
    rep["keys"]["legacy"] = sorted(legacy[0].keys()) if legacy else []
    save_listing(symbol, "results_legacy", legacy)

    # Integrated filing (from Mar 2025): the issuer must be the exact legal name.
    integrated, total = [], 0
    for issuer in issuer_candidates(symbol, legacy):
        try:
            integrated, total = integrated_rows(symbol, issuer)
        except Exception as e:
            rep["errors"].append(f"integrated issuer={issuer!r}: {e}")
            continue
        if integrated:
            rep["issuer"] = issuer
            break
    rep["integrated_rows"] = len(integrated)
    rep["integrated_total"] = total
    rep["integrated_quarters"] = quarters(integrated)
    rep["keys"]["integrated"] = sorted(integrated[0].keys()) if integrated else []
    save_listing(symbol, "results_integrated", integrated)

    for label, rows in (("xbrl_legacy", legacy), ("xbrl_integrated", integrated)):
        got = 0
        for row in newest_first(rows):
            for url in urls_in(row, (".xml",)):
                if got < N_XBRL_EACH:
                    try:
                        download(symbol, label, url, row_date(row))
                        got += 1
                    except Exception as e:
                        rep["errors"].append(f"{label} {url}: {e}")
        rep[label] = got

    # Announcements: decks, transcripts, rating letters.
    try:
        ann = rows_of(fetch(API + "corporate-announcements", {"index": "equities", "symbol": symbol}, symbol).json())
    except Exception as e:
        rep["errors"].append(f"announcements: {e}")
        ann = []
    rep["announcements"] = len(ann)
    rep["keys"]["announcements"] = sorted(ann[0].keys()) if ann else []
    save_listing(symbol, "announcements", ann)
    wanted = {"deck": N_DECKS, "transcript": N_TRANSCRIPTS, "rating_letter": N_RATINGS}
    found = {k: 0 for k in wanted}
    got = {k: 0 for k in wanted}
    for row in newest_first(ann):
        kind = classify(row)
        if not kind:
            continue
        found[kind] += 1
        url = row.get("attchmntFile") or ""
        if got[kind] < wanted[kind] and url.lower().endswith(".pdf"):
            try:
                download(symbol, kind, url, row_date(row))
                got[kind] += 1
            except Exception as e:
                rep["errors"].append(f"{kind} {url}: {e}")
    rep["found"], rep["downloaded"] = found, got

    # Annual reports (large files).
    try:
        ars = rows_of(fetch(API + "annual-reports", {"index": "equities", "symbol": symbol}, symbol).json())
    except Exception as e:
        rep["errors"].append(f"annual reports: {e}")
        ars = []
    rep["keys"]["annual_reports"] = sorted(ars[0].keys()) if ars else []
    save_listing(symbol, "annual_reports", ars)
    n = 0
    for row in newest_first(ars):
        for url in urls_in(row, (".pdf", ".zip")):
            if n < N_ANNUAL_REPORTS:
                try:
                    download(symbol, "annual_report", url, row_date(row), large=True)
                    n += 1
                except Exception as e:
                    rep["errors"].append(f"annual report {url}: {e}")
    rep["annual_reports"] = n

    # Credit-rating pointer list (listing only).
    try:
        save_listing(symbol, "credit_ratings",
                     fetch(API + "corporate-credit-rating", {"index": "equities", "symbol": symbol}, symbol).json())
    except Exception as e:
        rep["errors"].append(f"credit ratings: {e}")
    return rep


def main() -> None:
    SMALL.mkdir(exist_ok=True)
    LARGE.mkdir(exist_ok=True)
    try:
        fetch("https://www.nseindia.com/")
    except Exception:
        pass  # the APIs answered without cookies on 10 Oct
    reports = []
    for symbol, subtype in DEV_SET.items():
        print(f"... {symbol}", flush=True)
        try:
            reports.append(capture(symbol, subtype))
        except Exception as e:
            reports.append({"symbol": symbol, "expected_subtype": subtype, "errors": [f"fatal: {e}"], "keys": {}})

    with open(SMALL / "manifest.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["symbol", "kind", "date", "url", "sha256", "bytes", "path"])
        w.writeheader()
        w.writerows(MANIFEST)

    lines = ["==== SUMMARY (paste this into the thread) ====",
             f"captured {datetime.datetime.now().isoformat(timespec='seconds')}, {len(MANIFEST)} files, "
             f"{sum(m['bytes'] for m in MANIFEST) / 1e6:.1f} MB"]
    for r in reports:
        lines.append(f"\n{r['symbol']} (expected {r['expected_subtype']})")
        lines.append(f"  issuer that worked: {r.get('issuer', 'NONE FOUND')}")
        lines.append(f"  legacy rows {r.get('legacy_rows', 0)} (index={r.get('legacy_index', '-')}), "
                     f"quarters {r.get('legacy_quarters', 0)}; integrated rows {r.get('integrated_rows', 0)} "
                     f"(totalCount {r.get('integrated_total', 0)}), quarters {r.get('integrated_quarters', 0)}")
        lines.append(f"  xbrl saved: legacy {r.get('xbrl_legacy', 0)}, integrated {r.get('xbrl_integrated', 0)}")
        lines.append(f"  announcements {r.get('announcements', 0)}; found {r.get('found', {})}; "
                     f"saved {r.get('downloaded', {})}; annual reports saved {r.get('annual_reports', 0)}")
        for name, keys in r.get("keys", {}).items():
            lines.append(f"  fields[{name}]: {', '.join(keys)[:400]}")
        for err in r["errors"][:6]:
            lines.append(f"  ERROR {err[:200]}")
    text = "\n".join(lines)
    (SMALL / "report.md").write_text(text + "\n", encoding="utf-8")
    print("\n" + text)


if __name__ == "__main__":
    main()
