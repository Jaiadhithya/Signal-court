# Signal Court

[![CI](https://github.com/Jaiadhithya/Signal-court/actions/workflows/ci.yml/badge.svg)](https://github.com/Jaiadhithya/Signal-court/actions/workflows/ci.yml)

Signal Court is an autonomous research agent for the Moonveil entry (Track A, banking / NBFC / financial) in the
BITS Pilani Hyderabad "Finding the Next Signal" hackathon. Given a listed Indian financial company it looks for a
non-obvious, evidence-backed finding, argues against it, and reports it with eight elements: discovery, why it
matters, evidence, timeline, numbers, counterargument, falsification and evidence strength.

> Status: work in progress for the finals on 25 Oct 2026.
>
> Built and tested: the evidence ledger (append-only, hash-chained), the document store, the gates (quote, schema,
> time, unbacked numbers), and the data layer (NSE harvest, XBRL parser, table and LLM extraction, calculator and
> reconciliation).
> In progress: sensors and surprise ranking, the LLM router, pre-registration, the evidence court, the code judge,
> the writer, the report/dashboard, and the `signal run` command.

## Quick start

```
pip install uv
uv python install 3.12
uv sync
uv run signal version
uv run pytest        # offline suite; network tests are excluded by default
uv run ruff check .
```

The code stays compatible with Python 3.10 (no `tomllib`, `TaskGroup`, `Self`, `StrEnum`, `datetime.UTC`);
CI runs 3.10 and 3.12.

## Layout

- `config/` thresholds (`settings.toml`) and model chains (`models.yaml`)
- `registry/` versioned data (metrics, formulas, angles, alternatives, sources); no logic, no company names
- `signalcourt/` the package; one subpackage per module group
- `scripts/` setup scripts: `capture_fixtures.py`, `models_smoke.py`, `dump_xbrl_elements.py` (plain `requests`, no install needed)
- `tests/` offline suite; `tests/network/` live contract tests (need network access)
- `fixtures/`, `backtests/`, `holdout/` the only places company names may appear (`holdout/` holds only a sealed hash)

## Guarantees

- No company names, symbols, ISINs or BSE codes in `signalcourt/`, `prompts/`, `registry/` or `config/`
  (`tests/test_no_names.py`). The words "NSE" and "BSE" are allowed as exchange names in `signalcourt/net/`,
  `registry/sources.yaml` and `registry/datasets.yaml` only, because BSE Ltd is also a Track A company.
- Every run records the hash of each config, registry and prompt file it used.
- Zero cost: free tiers only, keys rotated across providers; no card anywhere.

## Licence

AGPL-3.0, full text in `LICENSE` (compatible with PyMuPDF's AGPL and the GPLv3 `nse` / `bse` libraries).
