from datetime import date, datetime

import pytest
from conftest import FIX

from signalcourt.extract import events as EV
from signalcourt.harvest import announcements as A
from signalcourt.harvest import filings as Fl
from signalcourt.harvest import ratings as R
from signalcourt.harvest import regulators as RG
from signalcourt.harvest import tiers as T
from signalcourt.net.http import Blocked

BANK_XML = (FIX / "xbrl" / "legacy_banking_sample.xml").read_bytes()


class FakeStore:
    def __init__(self):
        self.files, self.by_url, self.meta = {}, {}, []

    def put_bytes(self, data, **kw):
        import hashlib

        sha = hashlib.sha256(data).hexdigest()
        self.files[sha] = data
        self.by_url[kw["url"]] = sha
        self.meta.append(kw)
        return type("M", (), {"sha256": sha})()

    def known_url(self, url):
        return self.by_url.get(url)

    def read_bytes(self, sha):
        return self.files[sha]


class Resp:
    def __init__(self, content, url):
        self.content, self.url = content, url


class FakeNSE:
    def __init__(self, legacy=None, integrated=None, anns=None, ars=None, feed=None):
        self.legacy, self.integrated, self.anns = legacy or [], integrated or [], anns or []
        self.ars, self.feed = ars or [], feed or []
        self.downloads = []

    def results_legacy(self, symbol, period="Quarterly", index="equities"):
        return self.legacy

    def results_integrated(self, symbol, issuer, **kw):
        return self.integrated

    def announcements(self, symbol, frm, to):
        return [a for a in self.anns if frm <= datetime.strptime(a["an_dt"][:11], "%d-%b-%Y").date() <= to]

    def annual_reports(self, symbol):
        return self.ars

    def credit_rating_feed(self, frm, to):
        return self.feed

    def download(self, url):
        self.downloads.append(url)
        if "blocked" in url:
            raise Blocked("wall")
        return Resp(BANK_XML if url.endswith(".xml") else b"%PDF-1.4 " + url.encode(), url)


LEG = {
    "toDate": "30-Sep-2023",
    "consolidated": "Non-Consolidated",
    "audited": "Unaudited",
    "broadCastDate": "21-Oct-2023 16:30:00",
    "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/BANKING_1.xml",
}
INT = {
    "qe_Date": "30-Jun-2026",
    "consolidated": "Standalone",
    "broadcast_Date": "19-Jul-2026 17:00:00",
    "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/INTEGRATED_FILING_BANKING_2_WEB.xml",
    "pdf_attach": "https://nsearchives.nseindia.com/corporate/res.pdf",
}
ENT = T.EntityRef("SYM", "Sym Bank Limited", "bank", "INE1", ["Sym Bank Limited"])


def test_filing_rows_both_formats_and_consolidation_flags():
    a = Fl.filing_from_row("S", LEG, "legacy")
    b = Fl.filing_from_row("S", INT, "integrated")
    assert a.period_end == date(2023, 9, 30) and a.consolidated is False and a.audited is False
    assert b.period_end == date(2026, 6, 30) and b.consolidated is False
    assert Fl.filing_from_row("S", {**INT, "consolidated": "Consolidated"}, "integrated").consolidated is True


def test_issuer_mismatch_when_recent_legacy_but_no_integrated():
    recent = {**LEG, "toDate": "31-Dec-2024"}
    with pytest.raises(Fl.IssuerMismatch):
        Fl.list_filings(FakeNSE(legacy=[recent], integrated=[]), "S", "Wrong Name")
    old = Fl.list_filings(FakeNSE(legacy=[LEG], integrated=[]), "S", "Old Co")  # delisted/old filer: legit
    assert len(old) == 1


def test_t1_harvest_stores_parses_gates_by_as_of_and_is_idempotent():
    nse, st = FakeNSE(legacy=[LEG], integrated=[INT]), FakeStore()
    rep = T.harvest(ENT, date(2026, 7, 1), {"T1"}, 600, "live", exch=nse, store=st, quarters=40)
    assert rep.by_doc_type == {"results_xbrl": 1} and rep.time_gate_dropped == 1  # Jul-19 broadcast > Jul-1 as-of
    assert rep.parsed[0].filing.period_end == date(2023, 9, 30)
    pat = [p for p in rep.parsed[0].quarter if p.metric == "pat"][0]
    assert pat.span.doc_sha256 == rep.parsed[0].sha256 and pat.value == pytest.approx(10261.0)
    n = len(nse.downloads)
    rep2 = T.harvest(ENT, date(2026, 7, 1), {"T1"}, 600, "live", exch=nse, store=st, quarters=40)
    assert len(nse.downloads) == n and rep2.new_files == 0 and rep2.known_files == 1


def test_t1_records_publish_date_from_exchange_broadcast():
    nse, st = FakeNSE(legacy=[LEG]), FakeStore()
    T.harvest(ENT, date(2026, 7, 1), {"T1"}, 600, "live", exch=nse, store=st, quarters=40)
    m = st.meta[0]
    assert m["publish_date"] == date(2023, 10, 21) and m["publish_date_source"] == "exchange_broadcast"
    assert m["source_family"] == "exchange_xbrl" and m["doc_type"] == "results_xbrl"


def test_replay_mode_touches_no_network_and_time_cap_skips_tiers():
    nse, st = FakeNSE(legacy=[LEG]), FakeStore()
    rep = T.harvest(ENT, date(2026, 7, 1), {"T1", "T2"}, 600, "replay", exch=nse, store=st)
    assert nse.downloads == [] and rep.skipped_tiers == ["T1", "T2"]
    ticks = iter([0, 0, 999, 999, 999, 999])
    rep2 = T.harvest(
        ENT, date(2026, 7, 1), {"T1", "T2"}, 100, "live", exch=nse, store=st, clock=lambda: next(ticks, 999)
    )
    assert "T2" in rep2.skipped_tiers


def test_blocked_download_is_logged_not_fatal():
    bad = {**LEG, "xbrl": "https://x/blocked.xml"}
    rep = T.harvest(
        ENT, date(2026, 7, 1), {"T1"}, 600, "live", exch=FakeNSE(legacy=[bad]), store=FakeStore(), quarters=40
    )
    assert rep.parsed == [] and any("blocked" in e for e in rep.errors)


ANNS = [
    {
        "desc": "Investor Presentation",
        "an_dt": "21-Oct-2023 17:00:00",
        "attchmntFile": "https://n/deck.pdf",
        "attchmntText": "Q2 investor presentation",
    },
    {
        "desc": "Analysts/Institutional Investor Meet/Con. Call Updates",
        "an_dt": "25-Oct-2023 10:00:00",
        "attchmntFile": "https://n/tr.pdf",
        "attchmntText": "Transcript of earnings call",
    },
    {
        "desc": "Analysts/Institutional Investor Meet/Con. Call Updates",
        "an_dt": "20-Oct-2023 10:00:00",
        "attchmntFile": "https://n/a.mp3",
        "attchmntText": "Audio recording of the call",
    },
    {
        "desc": "Credit Rating",
        "an_dt": "02-Nov-2023 09:00:00",
        "attchmntFile": "https://n/rating.pdf",
        "attchmntText": "rating action",
    },
    {
        "desc": "Scheme of Arrangement - Amalgamation",
        "an_dt": "05-Nov-2023 09:00:00",
        "attchmntFile": "https://n/scheme.pdf",
        "attchmntText": "Amalgamation of X with Y",
    },
    {"desc": "Trading Window", "an_dt": "06-Nov-2023 09:00:00", "attchmntFile": "https://n/tw.pdf", "attchmntText": ""},
    {
        "desc": "Press Release",
        "an_dt": "01-Jan-2030 09:00:00",
        "attchmntFile": "https://n/future.pdf",
        "attchmntText": "",
    },
]


def test_announcement_classification():
    kinds = {i.url.split("/")[-1]: i.kind for i in A.classify_all(ANNS, "S")}
    assert kinds == {
        "deck.pdf": "deck",
        "tr.pdf": "transcript",
        "a.mp3": "audio",
        "rating.pdf": "rating_letter",
        "scheme.pdf": "scheme",
        "tw.pdf": "other",
        "future.pdf": "press_release",
    }


def test_t2_downloads_only_fetchable_kinds_and_respects_as_of():
    nse = FakeNSE(
        anns=ANNS,
        ars=[
            {
                "fileName": "https://n/ar22.pdf",
                "broadcast_dttm": "01-Jul-2022 10:00:00",
                "fromYr": "2021",
                "toYr": "2022",
            },
            {"fileName": "https://n/ar_future.pdf", "broadcast_dttm": "01-Jul-2031 10:00:00"},
        ],
    )
    st = FakeStore()
    rep = T.harvest(ENT, date(2023, 12, 31), {"T2"}, 600, "live", exch=nse, store=st, years=1)
    got = {u.split("/")[-1] for u in nse.downloads}
    assert got == {
        "deck.pdf",
        "tr.pdf",
        "rating.pdf",
        "scheme.pdf",
        "ar22.pdf",
    }  # no audio, no 'other', no future items
    assert rep.by_doc_type["deck"] == 1 and rep.by_doc_type["transcript"] == 1 and rep.by_doc_type["annual_report"] == 1
    assert rep.time_gate_dropped >= 1 or "future.pdf" not in got


def test_events_from_announcements():
    items = A.classify_all(ANNS, "S")
    evs = {e.event_type for e in EV.all_events(items)}
    assert evs == {"rating_action", "merger"}
    merger = [e for e in EV.all_events(items) if e.event_type == "merger"][0]
    assert merger.date == date(2023, 11, 5) and "Amalgamation" in merger.span.text


def test_rating_feed_filtered_client_side():
    feed = [
        {"ISIN": "INE1", "NameOfTheCompany": "Other Co", "PdfFileName": "https://n/r1.pdf"},
        {"ISIN": "INE9", "NameOfTheCompany": "SYM BANK LIMITED", "XbrlFileName": "https://n/r2.xml"},
        {"ISIN": "INE7", "NameOfTheCompany": "Unrelated Ltd", "PdfFileName": "https://n/r3.pdf"},
    ]
    mine = R.filter_feed(feed, "INE1", ["Sym Bank Limited"])
    assert [r["PdfFileName" if "PdfFileName" in r else "XbrlFileName"].split("/")[-1] for r in mine] == [
        "r1.pdf",
        "r2.xml",
    ]
    assert R.rating_urls(mine) == ["https://n/r1.pdf", "https://n/r2.xml"]


def test_regulator_release_matching():
    html_text = (
        "<html><body>"
        '<a href="/Scripts/BS_PressReleaseDisplay.aspx?prid=1">RBI imposes monetary penalty on Sym Bank Limited</a>'
        '<a href="/Scripts/BS_PressReleaseDisplay.aspx?prid=2">RBI imposes monetary penalty on Other Bank Ltd</a>'
        '<a href="/Scripts/BS_PressReleaseDisplay.aspx?prid=3">Directions under Section 35A on Sym Bank</a>'
        '<a href="/about">About</a></body></html>'
    )
    links = RG.parse_listing(html_text)
    assert len(links) == 3 and links[0].url.startswith("https://www.rbi.org.in/Scripts")
    ev = RG.events_for_entity(links, ["Sym Bank Limited"])
    assert [e.kind for e in ev] == ["penalty", "direction"]
    p = EV.from_regulator(ev[0], "SYM")
    assert p.event_type == "penalty"


def test_ytd_derivation_for_filings_without_quarter_context():
    """A Q2 filing used with its Q1 predecessor: Q2 quarter = YTD(Q2) - YTD(Q1)."""
    import re

    # Build a synthetic Q4-style filing: drop the 3-month context so only YTD remains.
    xml = BANK_XML.decode()
    xml = re.sub(r'<xbrli:context id="OneD">.*?</xbrli:context>', "", xml, flags=re.S)
    xml = re.sub(r'(<[^>]+ contextRef=)"OneD"', r'\1"Gone"', xml)
    f = Fl.ResultFiling("S", date(2023, 9, 30), False, False, None, None, None, "legacy")
    q2 = Fl.parse_filing(f, xml.encode(), "bank", "sha-q2")
    assert "interest_earned" not in {p.metric for p in q2.quarter} and "interest_earned" in q2.ytd
    q1_ytd = {"interest_earned": (33327.61, "333276100000.00", "InterestEarned", "FourD")}
    q1 = Fl.ParsedFiling(
        Fl.ResultFiling("S", date(2023, 6, 30), False, False, None, None, None, "legacy"),
        "sha-q1",
        "bank",
        [],
        q1_ytd,
        "e",
        None,
        date(2023, 6, 30),
    )
    out = {p.metric: p for p in Fl.derive_missing_quarters([q1, q2])}
    d = out["interest_earned"]
    assert d.period_end == date(2023, 9, 30) and d.derived_from == "ytd_to_quarter"
    assert d.value == pytest.approx(68248.0 - 33327.61)
    # Q2 without its Q1 predecessor cannot be derived, so nothing is invented:
    assert "interest_earned" not in {p.metric for p in Fl.derive_missing_quarters([q2])}
