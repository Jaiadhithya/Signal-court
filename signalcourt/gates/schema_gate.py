"""Gate 1: schema + capability + unbacked numbers."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from pydantic import BaseModel, ValidationError

from ..ledger.schema import Author, Payload, RecordType, may_write, validate_payload
from .common import Rejection
from .unbacked import CitedValue, cited_values_from_ledger, find_unbacked

# Payload fields that carry free text an agent wrote, per record type.
FREE_TEXT_FIELDS: dict[RecordType, tuple[str, ...]] = {
    RecordType.CLAIM: ("statement", "mechanism"),
    RecordType.EVIDENCE: ("sentence",),
    RecordType.CHALLENGE: ("argument",),
    RecordType.REBUTTAL: ("argument",),
}
# Fields listing the rids whose values may back the text.
CITE_FIELDS: dict[RecordType, tuple[str, ...]] = {
    RecordType.EVIDENCE: ("span_rid",),
    RecordType.CHALLENGE: ("evidence", "calc"),
    RecordType.REBUTTAL: ("evidence", "calc"),
}


def check_schema(
    rtype: RecordType | str, payload: BaseModel | dict[str, Any], author: Author
) -> tuple[Payload | None, Rejection | None]:
    """Pydantic validation, then the capability table. Returns (model, None) or (None, Rejection)."""
    rtype = RecordType(rtype)
    if not may_write(author, rtype):
        return None, Rejection("schema", "capability", {"author": author.writer, "type": rtype.value})
    try:
        model = validate_payload(rtype, payload)
    except ValidationError as e:
        return None, Rejection(
            "schema",
            "schema_error",
            {"errors": [{"loc": ".".join(str(x) for x in err["loc"]), "msg": err["msg"]} for err in e.errors()][:10]},
        )
    return model, None


def _texts(item: Any) -> Iterable[str]:
    if isinstance(item, str):
        yield item
    elif isinstance(item, dict):
        for k, v in item.items():
            if k == "text" or k in ("statement", "mechanism", "argument", "sentence"):
                yield from _texts(v)
            elif isinstance(v, (dict, list)):
                yield from _texts(v)
    elif isinstance(item, list):
        for v in item:
            yield from _texts(v)


def check_unbacked_numbers(
    texts: Sequence[str], cited: Iterable[CitedValue], *, exempt_small_counts: bool = False
) -> Rejection | None:
    cited = list(cited)
    bad = []
    for t in texts:
        bad.extend(find_unbacked(t, cited, exempt_small_counts=exempt_small_counts))
    if bad:
        return Rejection(
            "schema",
            "unbacked_number",
            {"tokens": [{"token": b.token, "shown": b.shown, "unit": b.unit} for b in bad][:10]},
        )
    return None


def check_proposal(
    ledger,
    rtype: RecordType | str,
    payload: BaseModel | dict[str, Any],
    author: Author,
    *,
    extra_cited: Sequence[str] = (),
    exempt_small_counts: bool = False,
) -> tuple[Payload | None, Rejection | None]:
    """Gate 1 in full: schema, capability, then unbacked numbers in the type's free-text fields."""
    rtype = RecordType(rtype)
    model, rej = check_schema(rtype, payload, author)
    if rej:
        return None, rej
    fields = FREE_TEXT_FIELDS.get(rtype)
    if fields:
        d = model.model_dump(mode="json")
        texts = [d[f] for f in fields if d.get(f)]
        cite_rids: list[str] = list(extra_cited)
        for f in CITE_FIELDS.get(rtype, ()):
            v = d.get(f)
            cite_rids.extend(v if isinstance(v, list) else [v] if v else [])
        rej = check_unbacked_numbers(
            texts, cited_values_from_ledger(ledger, cite_rids), exempt_small_counts=exempt_small_counts
        )
        if rej:
            return None, rej
    return model, None


def check_finding_text(
    ledger, finding_payload: dict[str, Any], *, exempt_small_counts: bool = False
) -> Rejection | None:
    """Each TextItem in a FINDING is checked against the values of the rids it carries."""
    bad = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            if "text" in o and "rids" in o:
                vals = cited_values_from_ledger(ledger, o["rids"])
                bad.extend(find_unbacked(o["text"], vals, exempt_small_counts=exempt_small_counts))
            else:
                for v in o.values():
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(finding_payload.get("elements", {}))
    if bad:
        return Rejection(
            "schema",
            "unbacked_number",
            {"tokens": [{"token": b.token, "shown": b.shown, "unit": b.unit} for b in bad][:10]},
        )
    return None
