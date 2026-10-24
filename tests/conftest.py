import itertools
from datetime import date
from pathlib import Path

import pytest

from signalcourt.ledger import Author, Ledger, RunPayload
from signalcourt.store import DocStore

REPO_ROOT = Path(__file__).resolve().parent.parent
FIX = REPO_ROOT / "tests" / "fixtures"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT

def run_payload(**kw):
    d = dict(
        company_input="Test Co",
        symbol="TESTCO",
        as_of="2026-10-01",
        mode="replay",
        budgets={},
        k=5,
        code_git_sha="abc",
        settings_hash="s" * 8,
        registry_hashes={},
        prompt_hashes={},
        model_pools_hash="m" * 8,
        started_at="2026-10-11T00:00:00.000Z",
    )
    d.update(kw)
    return RunPayload(**d)


def fixed_clock():
    c = itertools.count()
    return lambda: f"2026-10-11T00:00:{next(c) // 1000 % 60:02d}.{next(c) % 1000:03d}Z"


@pytest.fixture
def ledger(tmp_path):
    led = Ledger.create(tmp_path / "run1", run_payload())
    yield led
    led.close()


@pytest.fixture
def store(tmp_path):
    s = DocStore(tmp_path / "cache")
    yield s
    s.close()


def make_pdf(pages):
    import fitz

    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_textbox(fitz.Rect(40, 40, 560, 800), text, fontsize=10)
    data = doc.tobytes()
    doc.close()
    return data


def put_pdf(store, pages, *, publish=date(2026, 8, 1), **kw):
    meta = dict(
        url="https://example.test/a.pdf",
        final_url="https://example.test/a.pdf",
        mime="application/pdf",
        source_id="exchange",
        source_family="exchange",
        doc_type="deck",
        publish_date=publish,
        publish_date_source="exchange_broadcast" if publish else "none",
    )
    meta.update(kw)
    return store.put_bytes(make_pdf(pages), **meta)


CODE = Author.code


def make_ctx(
    tmp_path,
    impl,
    on_event=None,
    subtype=None,
    *,
    store=None,
    home=None,
    company="Test Co",
    symbol="TESTCO",
    as_of=date(2026, 10, 1),
    mode="replay",
    settings=None,
):
    """A RunContext on a fresh ledger in tmp_path/run, with the real settings and a fixed clock."""
    from signalcourt.pipeline import RunContext
    from signalcourt.settings import load_config

    run_dir = tmp_path / "run"
    led = Ledger.create(run_dir, run_payload(), clock=fixed_clock())
    return RunContext(
        ledger=led,
        store=store,
        settings=settings or load_config().model_dump(),
        company=company,
        symbol=symbol,
        as_of=as_of,
        mode=mode,
        run_dir=run_dir,
        impl=dict(impl),
        subtype=subtype,
        home=home,
        **({"on_event": on_event} if on_event else {}),
    )


# -- synthetic results XBRL in a DocStore (stage tests) ---------------------------------------------------
# The integrated fixtures describe the quarter 2026-04-01..2026-06-30; xbrl_variant moves them to any
# quarter end and scales every rupee value, so one file stands in for many quarters and many peers.
XBRL_FIXTURE = {
    "bank": "integrated_bank_synthetic.xml",
    "nbfc": "integrated_nbfc_sample.xml",
    "insurer_life": "integrated_life_sample.xml",
    "generic": "integrated_nbfc_sample.xml",
}
XBRL_FILE_PREFIX = {
    "bank": "INTEGRATED_FILING_BANKING_",
    "nbfc": "INTEGRATED_FILING_NBFC_INDAS_",
    "insurer_life": "INTEGRATED_FILING_LI_",
    "generic": "INTEGRATED_FILING_INDAS_",
}


def quarter_ends(last: date, n: int) -> list[date]:
    """n consecutive quarter ends, oldest first, ending at ``last``."""
    from signalcourt.calc import periods as P

    i = P.index(last)
    return [P.end(P.from_index(k)) for k in range(i - n + 1, i + 1)]


def xbrl_variant(subtype: str, end: date, scale: float = 1.0) -> bytes:
    import re

    xml = (FIX / "xbrl" / XBRL_FIXTURE[subtype]).read_text(encoding="utf-8")
    m0 = end.month - 2
    start = date(end.year, m0, 1) if m0 > 0 else date(end.year - 1, m0 + 12, 1)
    xml = xml.replace("2026-04-01", start.isoformat()).replace("2026-06-30", end.isoformat())
    xml = re.sub(
        r'(unitRef="INR"[^>]*>)(-?\d+(?:\.\d+)?)<',
        lambda m: f"{m.group(1)}{int(round(float(m.group(2)) * scale))}<",
        xml,
    )
    return xml.encode("utf-8")


def put_xbrl(store, symbol: str, subtype: str, end: date, *, scale: float = 1.0, publish: date | None = None):
    """One synthetic results XBRL for ``symbol``'s quarter ``end``, published 30 days later by default."""
    from datetime import timedelta

    url = f"https://example.test/xbrl/{XBRL_FILE_PREFIX[subtype]}{symbol}_{end:%Y%m%d}.xml"
    publish = publish or end + timedelta(days=30)
    return store.put_bytes(
        xbrl_variant(subtype, end, scale),
        url=url,
        final_url=url,
        mime="application/xml",
        source_id="exchange_results_xbrl",
        source_family="exchange_xbrl",
        doc_type="results_xbrl",
        entity_symbol=symbol,
        period_end=end,
        publish_date=publish,
        publish_date_source="exchange_broadcast",
    )


def build_workspace(home: Path, spec, last=date(2026, 6, 30)):
    """Universe + DocStore under ``home`` from spec rows (symbol, name, subtype, quarters, scale).

    Every row is a financial entity marked with its sub-type; ``quarters`` synthetic results filings end at
    ``last``. Returns the open DocStore (caller closes it).
    """
    from signalcourt.resolve import universe as U
    from signalcourt.settings import Workspace

    ws = Workspace(home=home)
    con = U.connect(ws.universe_db)
    U.ingest(
        con,
        [{"SYMBOL": s, "NAME OF COMPANY": n, "ISIN NUMBER": f"INE{i:06d}01"} for i, (s, n, *_rest) in enumerate(spec)],
        {"fin": [{"Symbol": s, "Industry": "Financial Services", "Company Name": n} for s, n, *_rest in spec]},
    )
    for s, _n, st, *_rest in spec:
        U.mark_financial(con, s, st)
    con.close()
    store = DocStore(ws.cache)
    for s, _n, st, quarters, scale in spec:
        for end in quarter_ends(last, quarters):
            put_xbrl(store, s, st, end, scale=scale)
    return store


@pytest.fixture
def workspace(tmp_path):
    """A small workspace: three banks, two NBFCs (one shares a word with a bank) and a life insurer."""
    spec = [
        ("ACMEBANK", "Acme Bank Limited", "bank", 8, 1.0),
        ("BETABANK", "Beta Bank Limited", "bank", 8, 0.5),
        ("GAMMABANK", "Gamma Bank Limited", "bank", 8, 2.0),
        ("ACMEFIN", "Acme Finance Limited", "nbfc", 8, 1.0),
        ("ACMEFSV", "Acme Finserv Limited", "nbfc", 8, 1.5),
        ("ZETALIFE", "Zeta Life Insurance Company Limited", "insurer_life", 8, 1.0),
    ]
    home = tmp_path / "home"
    store = build_workspace(home, spec)
    yield home, store
    store.close()
