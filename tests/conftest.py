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


def make_ctx(tmp_path, impl, on_event=None, subtype=None):
    """A RunContext on a fresh ledger in tmp_path/run, with the real settings and a fixed clock."""
    from signalcourt.pipeline import RunContext
    from signalcourt.settings import load_config

    run_dir = tmp_path / "run"
    led = Ledger.create(run_dir, run_payload(), clock=fixed_clock())
    return RunContext(
        ledger=led,
        store=None,
        settings=load_config().model_dump(),
        company="Test Co",
        symbol="TESTCO",
        as_of=date(2026, 10, 1),
        mode="replay",
        run_dir=run_dir,
        impl=dict(impl),
        subtype=subtype,
        **({"on_event": on_event} if on_event else {}),
    )
