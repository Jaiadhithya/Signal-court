from signalcourt.extract import llm_extract as L

PASSAGES = [
    L.Passage("p1", "Net interest margin was 4.31% in Q2FY27 versus 4.40% in Q1FY27", 3),
    L.Passage("p2", "Gross advances stood at 1,23,456 crore", 4),
]


def test_valid_items_become_proposals_and_bad_ones_are_dropped():
    resp = {
        "facts": [
            {
                "metric": "nim",
                "period_label": "Q2FY27",
                "value_as_written": "4.31%",
                "unit_as_written": "",
                "pid": "p1",
                "row_label": "Net interest margin",
            },
            {
                "metric": "nim",
                "period_label": "Q1FY27",
                "value_as_written": "4.55%",
                "unit_as_written": "",
                "pid": "p1",
            },  # not in passage
            {
                "metric": "advances",
                "period_label": "Q2FY27",
                "value_as_written": "1,23,456",
                "unit_as_written": "crore",
                "pid": "p2",
            },
            {
                "metric": "pcr",
                "period_label": "Q2FY27",
                "value_as_written": "80%",
                "unit_as_written": "",
                "pid": "p1",
            },  # not requested
            {
                "metric": "nim",
                "period_label": "Q3FY27",
                "value_as_written": "4.31%",
                "unit_as_written": "",
                "pid": "p9",
            },  # unknown pid
            {"metric": "nim", "period_label": "2027", "value_as_written": "4.31%", "unit_as_written": "", "pid": "p1"},
        ]
    }  # bad period
    props = L.parse_response(resp, ["nim", "advances"], PASSAGES, "A", "standalone", "sha")
    assert [(p.metric, round(p.value, 6)) for p in props] == [("nim", 0.0431), ("advances", 123456.0)]
    assert props[0].span.pid == "p1" and props[0].extractor == "llm" and props[0].span.page == 3


def test_extract_missing_calls_once_and_skips_when_nothing_missing():
    calls = []

    class Fake:
        def complete_json(self, role, prompt):
            calls.append((role, prompt))
            return {"facts": []}

    assert L.extract_missing(Fake(), [], PASSAGES, "A", "standalone", None) == []
    L.extract_missing(Fake(), ["nim", "advances"], PASSAGES, "A", "standalone", None)
    assert len(calls) == 1 and "[p1]" in calls[0][1] and "nim:" in calls[0][1]
