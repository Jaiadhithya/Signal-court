"""Record types, payload models and the capability table.

Every payload model forbids unknown fields. A correction is a new record whose
payload sets ``supersedes`` to the rid it replaces; nothing is ever edited.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class RecordType(str, Enum):
    RUN = "RUN"
    ENTITY = "ENTITY"
    DOC = "DOC"
    SPAN = "SPAN"
    FACT = "FACT"
    CALC = "CALC"
    REGEVENT = "REGEVENT"
    EDGE = "EDGE"
    GUIDANCE = "GUIDANCE"
    DIFF = "DIFF"
    SIGNAL = "SIGNAL"
    PLAN = "PLAN"
    CLAIM = "CLAIM"
    TASK = "TASK"
    PROBE = "PROBE"
    EVIDENCE = "EVIDENCE"
    CHALLENGE = "CHALLENGE"
    REBUTTAL = "REBUTTAL"
    GATE = "GATE"
    REJECT = "REJECT"
    VERDICT = "VERDICT"
    FINDING = "FINDING"
    LLMCALL = "LLMCALL"
    STOP = "STOP"


PREFIX: dict[RecordType, str] = {
    RecordType.RUN: "RN",
    RecordType.ENTITY: "EN",
    RecordType.DOC: "DC",
    RecordType.SPAN: "SP",
    RecordType.FACT: "FA",
    RecordType.CALC: "CA",
    RecordType.REGEVENT: "RE",
    RecordType.EDGE: "ED",
    RecordType.GUIDANCE: "GD",
    RecordType.DIFF: "DF",
    RecordType.SIGNAL: "SG",
    RecordType.PLAN: "PL",
    RecordType.CLAIM: "CL",
    RecordType.TASK: "TK",
    RecordType.PROBE: "PR",
    RecordType.EVIDENCE: "EV",
    RecordType.CHALLENGE: "CH",
    RecordType.REBUTTAL: "RB",
    RecordType.GATE: "GT",
    RecordType.REJECT: "RJ",
    RecordType.VERDICT: "VD",
    RecordType.FINDING: "FD",
    RecordType.LLMCALL: "LC",
    RecordType.STOP: "ST",
}
TYPE_BY_PREFIX: dict[str, RecordType] = {v: k for k, v in PREFIX.items()}

# rid = prefix + "-" + at least 8 hex (longer only on a collision)
RID_PATTERN = r"^({})-[0-9a-f]{{8,64}}$".format("|".join(PREFIX.values()))


# --------------------------------------------------------------------------- author

AGENT_ROLES = frozenset({"investigator", "advocate", "redteam", "drafter", "writer", "extractor", "entail", "coverage"})


class Author(BaseModel):
    """{component, role, model, family, provider} for model-backed work, {component: "code"} otherwise.

    ``component`` names the code module that appends (controller, resolve, harvest, tools,
    quote_gate, extract, calculator, forensics, sensors, ranker, angle_planner, prereg,
    scheduler, gates, judge, writer, router). ``role`` is set when an LLM agent produced
    the content (investigator, advocate, redteam, ...).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    component: str
    role: str | None = None
    model: str | None = None
    family: str | None = None
    provider: str | None = None

    @classmethod
    def code(cls, component: str) -> Author:
        return cls(component=component)

    @property
    def writer(self) -> str:
        """The identity checked against the capability table: the role if an agent wrote it."""
        return self.role or self.component


# --------------------------------------------------------------------------- capability table

CAPABILITIES: dict[RecordType, frozenset[str]] = {
    RecordType.RUN: frozenset({"controller"}),
    RecordType.ENTITY: frozenset({"resolve"}),
    RecordType.DOC: frozenset({"harvest", "tools"}),
    RecordType.SPAN: frozenset({"quote_gate"}),
    RecordType.FACT: frozenset({"extract", "extractor"}),
    RecordType.CALC: frozenset({"calculator"}),
    RecordType.REGEVENT: frozenset({"extract", "extractor"}),
    RecordType.EDGE: frozenset({"extract", "extractor"}),
    RecordType.GUIDANCE: frozenset({"extract", "extractor"}),
    RecordType.DIFF: frozenset({"forensics"}),
    RecordType.SIGNAL: frozenset({"sensors", "ranker"}),
    RecordType.PLAN: frozenset({"angle_planner"}),
    RecordType.CLAIM: frozenset({"prereg"}),
    RecordType.TASK: frozenset({"scheduler"}),
    RecordType.PROBE: frozenset({"tools"}),
    RecordType.EVIDENCE: frozenset({"investigator", "advocate", "redteam"}),
    RecordType.CHALLENGE: frozenset({"redteam"}),
    RecordType.REBUTTAL: frozenset({"advocate"}),
    RecordType.GATE: frozenset({"gates"}),
    RecordType.REJECT: frozenset({"gates"}),
    RecordType.VERDICT: frozenset({"judge"}),
    RecordType.FINDING: frozenset({"writer"}),
    RecordType.LLMCALL: frozenset({"router"}),
    RecordType.STOP: frozenset({"scheduler"}),
}


def may_write(author: Author, rtype: RecordType) -> bool:
    return author.writer in CAPABILITIES[RecordType(rtype)]


# --------------------------------------------------------------------------- payloads


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supersedes: str | None = None


class RunPayload(Payload):
    company_input: str
    symbol: str | None = None
    as_of: str
    mode: Literal["live", "warm", "replay"]
    budgets: dict[str, Any] = Field(default_factory=dict)
    k: int
    code_git_sha: str
    settings_hash: str
    registry_hashes: dict[str, str] = Field(default_factory=dict)
    prompt_hashes: dict[str, str] = Field(default_factory=dict)
    model_pools_hash: str
    started_at: str


class DomainRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: str
    verified_by_doc_sha: str | None = None


class PeerRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    subtype: str
    tier2: bool = False


class SubtypeEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    taxonomy: str | None = None
    entry_point: str | None = None
    segments: list[str] = Field(default_factory=list)


class EntityPayload(Payload):
    symbol: str
    isin: str | None = None
    bse_code: str | None = None
    legal_names: list[str] = Field(default_factory=list)
    domains: list[DomainRef] = Field(default_factory=list)
    subtype: str
    subtype_evidence: SubtypeEvidence
    peers: list[PeerRef] = Field(default_factory=list)
    peer_rule: str


class DocPayload(Payload):
    sha256: str
    url: str
    final_url: str
    doc_type: str
    source_id: str
    source_family: str
    origin_id: str
    entity_symbol: str | None = None
    period_end: str | None = None
    publish_date: str | None = None
    publish_date_source: str = "none"
    n_pages: int | None = None
    ocr: bool = False
    time_gate: Literal["ok", "dropped", "undated"]


class XbrlRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    element: str
    context_id: str
    unit: str | None = None
    decimals: str | None = None


class CellRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sheet: str | None = None
    row: int
    col: int
    header: str | None = None


class SpanPayload(Payload):
    doc_sha256: str
    page: int
    kind: Literal["prose", "table_cell", "xbrl", "dataset_cell"]
    text: str
    norm_text: str
    start: int | None = None
    end: int | None = None
    pid: str | None = None
    row_label: str | None = None
    value_text: str | None = None
    xbrl: XbrlRef | None = None
    cell: CellRef | None = None
    match: Literal["exact", "fuzzy"] = "exact"
    similarity: float | None = None


class FactPayload(Payload):
    entity: str
    metric: str
    value: float
    unit: str
    period_end: str
    basis: str
    span_rid: str
    extractor: Literal["xbrl", "table", "llm", "dataset"]
    value_as_written: str
    recon_status: Literal["pass", "fail", "single_source"]


class CalcPayload(Payload):
    formula_id: str
    formula_version: int
    formula_text: str
    inputs: list[str]
    params: dict[str, Any] = Field(default_factory=dict)
    value: float | None
    unit: str
    period_end: str | None = None
    entity: str | None = None
    recon_status: Literal["pass", "fail", "single_source"]


class RegEventPayload(Payload):
    entity: str | None = None
    event_type: Literal[
        "penalty",
        "direction",
        "circular",
        "scheme",
        "merger",
        "acquisition",
        "rating_action",
        "auditor_change",
        "kpi_redefinition",
        "restatement",
        "insolvency_admission",
        "auction_notice",
        "other",
    ]
    date: str
    summary: str
    span_rid: str


class EdgePayload(Payload):
    entity: str
    counterparty_type: Literal["funder", "co_lender", "guarantor", "strategic_holder", "investor"]
    counterparty_name: str | None = None
    amount: float | None = None
    share: float | None = None
    period_end: str
    direction: Literal["inbound", "outbound"]
    span_rid: str
    self_disclosed: bool | None = None


class GuidancePayload(Payload):
    entity: str
    metric: str
    comparator: str
    value_low: float
    value_high: float | None = None
    by_period: str
    made_on: str
    made_in_doc: str
    span_rid: str
    status: Literal["pending", "met", "missed", "revised", "silently_dropped"]
    outcome_calc_rid: str | None = None
    revised_by_rid: str | None = None


class DiffPayload(Payload):
    entity: str
    doc_a: str
    doc_b: str
    section: str
    kind: Literal[
        "dropped", "added", "redefined", "resegmented", "policy_change", "auditor_language", "similarity_drop"
    ]
    a_span_rid: str | None = None
    b_span_rid: str | None = None
    similarity: float | None = None
    label: str | None = None


class Regime(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: str
    end: str | None = None
    mean_composition: dict[str, float] = Field(default_factory=dict)


class ExternalLead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: str
    lag_q: int
    lead_corr: float | None = None
    lead_p: float | None = None


class PlaceboStat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    p_raw: float | None = None
    n: int
    rank: int | None = None


class Surprise(BaseModel):
    model_config = ConfigDict(extra="forbid")
    score: float
    z: float | None = None
    persistence: float | None = None
    specificity: float | None = None
    coverage: float | None = None
    coverage_status: str | None = None


class SignalPayload(Payload):
    sensor: str
    angle_id: str | None = None
    entity: str
    metrics: list[str]
    direction: str
    break_period: str | None = None
    regimes: list[Regime] | None = None
    size: float | None = None
    z: float | None = None
    persistence_q: int | None = None
    specificity: float | None = None
    specificity_source: Literal["twin", "peer_median", "na"]
    external: ExternalLead | None = None
    placebo: PlaceboStat | None = None
    corp_action_rid: str | None = None
    inputs: list[str] = Field(default_factory=list)
    surprise: Surprise | None = None


class AngleApplied(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    signal_rids: list[str] = Field(default_factory=list)


class AngleSkipped(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    reason: str


class PlanPayload(Payload):
    angles_applied: list[AngleApplied] = Field(default_factory=list)
    angles_skipped: list[AngleSkipped] = Field(default_factory=list)


class Falsification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: str
    comparator: str
    threshold: float
    deadline_period: str
    source_rule: str


class DrafterRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    family: str


class ClaimPayload(Payload):
    hypothesis_index: int
    statement: str
    mechanism: str
    signal_rid: str
    angle_id: str | None = None
    falsification: Falsification
    alt_checklist: list[str] = Field(default_factory=list)
    drafter: DrafterRef
    entity_blind: Literal[True] = True


class TaskPayload(Payload):
    kind: str
    claim_rid: str
    target_rid: str | None = None
    role: str
    alt_type: str | None = None
    budget_calls: int
    budget_tools: int
    priority: float
    pack_hash: str


class ProbePayload(Payload):
    task_rid: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    result_count: int
    doc_shas: list[str] = Field(default_factory=list)
    time_gate_drops: int = 0


class EntailRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Literal["entails", "neutral", "contradicts"]
    checker_model: str | None = None
    checker_family: str | None = None
    batch_rid: str | None = None


class EvidencePayload(Payload):
    claim_rid: str
    span_rid: str
    stance: Literal["support", "contradict", "neutral"]
    sentence: str
    source_family: str
    origin_id: str
    entail: EntailRef | None = None


class ChallengePayload(Payload):
    claim_rid: str
    alt_type: str
    argument: str
    evidence: list[str] = Field(default_factory=list)
    calc: list[str] = Field(default_factory=list)
    severity: Literal["substantive", "minor"]
    same_family: bool = False


class RebuttalPayload(Payload):
    challenge_rid: str
    argument: str
    evidence: list[str] = Field(default_factory=list)
    calc: list[str] = Field(default_factory=list)
    admissible: bool
    admissible_reason: str


class GatePayload(Payload):
    gate: str
    subject_rid: str | None = None
    proposal_hash: str | None = None
    result: Literal["pass", "fail", "deferred"]
    reason: str
    checker_model: str | None = None


class RejectPayload(Payload):
    proposal: dict[str, Any]
    proposal_type: str
    failing_gate: str
    reason: str
    detail: dict[str, Any] = Field(default_factory=dict)


class VerdictStat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    p_raw: float | None = None
    p_holm: float | None = None
    n_placebo: int | None = None
    mode: str | None = None


class VerdictPayload(Payload):
    claim_rid: str
    grade: Literal["Strong", "Moderate", "Weak", "Killed"]
    checks: dict[str, dict[str, Any]] = Field(default_factory=dict)
    stat: VerdictStat = Field(default_factory=VerdictStat)
    reasons: list[str] = Field(default_factory=list)


class TextItem(BaseModel):
    """A piece of finding text and the ledger records that back it."""

    model_config = ConfigDict(extra="forbid")
    text: str
    rids: list[str] = Field(default_factory=list)


class FindingElements(BaseModel):
    model_config = ConfigDict(extra="forbid")
    discovery: TextItem
    why: list[TextItem]
    evidence: list[TextItem]
    timeline: list[TextItem]
    numbers: list[TextItem]
    counterargument: TextItem
    falsification: TextItem
    strength: TextItem


class FindingPayload(Payload):
    claim_rid: str
    elements: FindingElements
    footer: dict[str, Any] = Field(default_factory=dict)
    writer: Literal["llm", "template"]


class LlmCallPayload(Payload):
    role: str
    provider: str
    model: str
    family: str
    prompt_template: str
    prompt_hash: str
    response_hash: str | None = None
    cache_hit: bool
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    error: str | None = None
    key_alias: str | None = None


class StopPayload(Payload):
    reason: Literal["budget", "time_cap", "nothing_open", "error"]
    calls_used: int
    tools_used: int
    elapsed_s: float
    open_items: list[str] = Field(default_factory=list)


PAYLOAD_MODELS: dict[RecordType, type[Payload]] = {
    RecordType.RUN: RunPayload,
    RecordType.ENTITY: EntityPayload,
    RecordType.DOC: DocPayload,
    RecordType.SPAN: SpanPayload,
    RecordType.FACT: FactPayload,
    RecordType.CALC: CalcPayload,
    RecordType.REGEVENT: RegEventPayload,
    RecordType.EDGE: EdgePayload,
    RecordType.GUIDANCE: GuidancePayload,
    RecordType.DIFF: DiffPayload,
    RecordType.SIGNAL: SignalPayload,
    RecordType.PLAN: PlanPayload,
    RecordType.CLAIM: ClaimPayload,
    RecordType.TASK: TaskPayload,
    RecordType.PROBE: ProbePayload,
    RecordType.EVIDENCE: EvidencePayload,
    RecordType.CHALLENGE: ChallengePayload,
    RecordType.REBUTTAL: RebuttalPayload,
    RecordType.GATE: GatePayload,
    RecordType.REJECT: RejectPayload,
    RecordType.VERDICT: VerdictPayload,
    RecordType.FINDING: FindingPayload,
    RecordType.LLMCALL: LlmCallPayload,
    RecordType.STOP: StopPayload,
}

# Types that need pre-registration first (ordering rule, 7.1).
POST_PREREG_TYPES = frozenset({RecordType.EVIDENCE, RecordType.CHALLENGE, RecordType.REBUTTAL})


def validate_payload(rtype: RecordType | str, payload: BaseModel | dict[str, Any]) -> Payload:
    """Validate (or re-validate) a payload against its type's model."""
    model = PAYLOAD_MODELS[RecordType(rtype)]
    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="json")
    return model.model_validate(payload)


def payload_dict(model: Payload) -> dict[str, Any]:
    """JSON-ready payload; ``supersedes`` is dropped when unset so it does not clutter every record."""
    d = model.model_dump(mode="json")
    if d.get("supersedes") is None:
        d.pop("supersedes", None)
    return d


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    seq: int
    rid: str
    type: RecordType
    run_id: str
    author: Author
    created_at: str
    parents: list[str]
    payload: dict[str, Any]
    prev_hash: str
    hash: str

    def as_dict(self) -> dict[str, Any]:
        d = self.model_dump(mode="json")
        d["author"] = {k: v for k, v in d["author"].items() if v is not None}
        return d

    def typed(self) -> Payload:
        return PAYLOAD_MODELS[self.type].model_validate(self.payload)
