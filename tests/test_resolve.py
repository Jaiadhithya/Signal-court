import pytest

from signalcourt.resolve import entity as E
from signalcourt.resolve import peers as PE
from signalcourt.resolve import subtype as S
from signalcourt.resolve import universe as U


@pytest.fixture()
def con(tmp_path):
    c = U.connect(tmp_path / "u.sqlite")
    master = [
        {"SYMBOL": "ALPHABK", "NAME OF COMPANY": "Alpha Bank Limited", "SERIES": "EQ", " ISIN NUMBER": "INE000A01011"},
        {"SYMBOL": "BETABK", "NAME OF COMPANY": "Beta Bank Ltd", "SERIES": "EQ", " ISIN NUMBER": "INE000B01012"},
        {
            "SYMBOL": "GAMMAFIN",
            "NAME OF COMPANY": "Gamma Finance Limited",
            "SERIES": "EQ",
            " ISIN NUMBER": "INE000C01013",
        },
        {
            "SYMBOL": "GAMMAHFL",
            "NAME OF COMPANY": "Gamma Housing Finance Limited",
            "SERIES": "EQ",
            " ISIN NUMBER": "INE000D01014",
        },
        {"SYMBOL": "STEELCO", "NAME OF COMPANY": "Steel Co Limited", "SERIES": "EQ", " ISIN NUMBER": "INE000E01015"},
        {
            "SYMBOL": "DELTALIFE",
            "NAME OF COMPANY": "Delta Life Insurance Company Limited",
            "SERIES": "EQ",
            " ISIN NUMBER": "INE000F01016",
        },
    ]
    idx = {
        "ind_niftybanklist": [
            {
                "Company Name": "Alpha Bank Limited",
                "Industry": "Financial Services",
                "Symbol": "ALPHABK",
                "Series": "EQ",
                "ISIN Code": "INE000A01011",
            }
        ],
        "ind_niftyfinancelist": [
            {
                "Company Name": "Gamma Finance Limited",
                "Industry": "Financial Services",
                "Symbol": "GAMMAFIN",
                "Series": "EQ",
                "ISIN Code": "INE000C01013",
            }
        ],
    }
    U.ingest(c, master, idx)
    return c


def test_universe_flags_financials_from_industry_only(con):
    assert U.financial_symbols(con) == ["ALPHABK", "GAMMAFIN"]
    U.mark_financial(con, "DELTALIFE", "insurer_life")
    assert U.financial_symbols(con, "insurer_life") == ["DELTALIFE"]


def test_resolver_symbol_name_and_rename(con):
    assert E.resolve(con, "alphabk").symbol == "ALPHABK"
    assert E.resolve(con, "Alpha Bank").symbol == "ALPHABK"
    assert E.resolve(con, "Delta Life Insurance").symbol == "DELTALIFE"
    U.add_name(con, "DELTALIFE", "Omega Life Insurance Company Limited", "name_change")  # pending rename
    m = E.resolve(con, "Omega Life Insurance")
    assert m.symbol == "DELTALIFE" and "Omega Life Insurance Company Limited" in m.legal_names
    assert E.resolve(con, "Zzz Unknown Holdings") is None


def test_resolver_ambiguity_returns_top_candidates(con):
    r = E.resolve(con, "Gamma Finance")
    assert (
        not isinstance(r, E.Ambiguous) and r.symbol == "GAMMAFIN"
    )  # "Gamma Housing Finance" is a longer, different name
    amb = E.resolve(con, "Bank")
    assert isinstance(amb, E.Ambiguous) and {c.symbol for c in amb.candidates} >= {"ALPHABK", "BETABK"}


def test_name_normaliser():
    assert E.normalise_name("The Alpha Bank Ltd.") == "alpha bank"
    assert E.normalise_name("Alpha & Beta Limited") == "alpha beta"


@pytest.mark.parametrize(
    "ev,expected",
    [
        (S.SubtypeEvidence(entry_point="banking_entry_point_2019-09-30.xsd", result_type="Banking Format"), "bank"),
        (S.SubtypeEvidence(xbrl_url="https://x/corporate/xbrl/INTEGRATED_FILING_BANKING_158270_1_WEB.xml"), "bank"),
        (S.SubtypeEvidence(entry_point="Ind-AS_entry_point_2020-03-31.xsd"), "generic"),
        (S.SubtypeEvidence(entry_point="nbfc_entry_point.xsd"), "nbfc"),
        (S.SubtypeEvidence(entry_point="life_insurance_entry_point.xsd"), "insurer_life"),
        (S.SubtypeEvidence(entry_point="general_insurance_entry_point.xsd"), "insurer_general"),
        (
            S.SubtypeEvidence(entry_point="Ind-AS_entry_point.xsd", segments=["Housing Finance", "Life Insurance"]),
            "holding",
        ),
        (
            S.SubtypeEvidence(entry_point="nbfc_entry_point.xsd", segments=["Retail Lending", "General Insurance"]),
            "holding",
        ),
        (
            S.SubtypeEvidence(
                entry_point="Ind-AS_entry_point.xsd", text_snippets=["registered as a Core Investment Company"]
            ),
            "holding",
        ),
        (S.SubtypeEvidence(entry_point="Ind-AS_entry_point.xsd", segments=["Paints", "Home Decor"]), "generic"),
        (S.SubtypeEvidence(), "generic"),
        (S.SubtypeEvidence(announcement_texts=["Public Disclosures L-Form for the quarter"]), "insurer_life"),
    ],
)
def test_subtype_rules(ev, expected):
    assert S.classify_subtype(ev).subtype == expected


def test_a_bank_with_lending_and_insurance_segments_stays_a_bank():
    d = S.classify_subtype(S.SubtypeEvidence(entry_point="banking_entry_point.xsd", segments=["Treasury", "Insurance"]))
    assert d.subtype == "bank" and d.rule == "format"


def test_peer_selection_nearest_with_decks():
    st = {"T": "bank", "A": "bank", "B": "bank", "C": "bank", "D": "bank", "E": "bank", "N": "nbfc"}
    ti = {"T": 1000.0, "A": 900.0, "B": 2000.0, "C": 1100.0, "D": 10.0, "E": 1020.0, "N": 1000.0}
    ps = PE.select_peers("T", "bank", st, ti, has_deck=lambda s: s != "E")
    assert ps.tier2 == ["C", "A", "B"] and ps.placebo == ["A", "B", "C", "D", "E"]
    assert "N" not in ps.placebo and "T" not in ps.placebo


def test_verified_domains_require_appearance_in_own_filings():
    assert PE.verified_domains("X", ["alpha.bank.in", "scam.example"], ["see www.alpha.bank.in for details"]) == [
        "alpha.bank.in"
    ]
