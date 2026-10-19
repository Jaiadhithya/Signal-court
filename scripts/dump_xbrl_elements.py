"""Laptop helper for the Mon 12 Oct tag dump: list every element in an XBRL file.

    python scripts/dump_xbrl_elements.py path\to\file.xml [more.xml ...] > elements.txt

Prints entry point, result type, period, then one line per numeric fact in the quarter and
instant contexts (name, context, unit, value).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signalcourt.extract import xbrl as X  # noqa: E402


def main(paths):
    for p in paths:
        doc = X.parse(Path(p).read_bytes())
        end = X.period_end(doc)
        print("=" * 70)
        print(p)
        print(
            "entry_point:",
            doc.entry_point,
            "| result_type:",
            doc.result_type,
            "| nature:",
            doc.nature,
            "| period_end:",
            end,
        )
        for name, v in sorted(X.quarter_facts(doc, end).items()):
            if v.fact.value is not None:
                print(f"{name:<90} {v.context.id:<8} {v.fact.unit or '':<6} {v.fact.text}")
        print("-- text facts --")
        for name, v in sorted(X.quarter_facts(doc, end).items()):
            if v.fact.value is None and len(v.fact.text) < 80:
                print(f"{name:<60} {v.fact.text}")


if __name__ == "__main__":
    main(sys.argv[1:])
