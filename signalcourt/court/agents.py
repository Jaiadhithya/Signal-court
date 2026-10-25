"""Court agents: the JSON each role must return, and the prompt it is sent.

Schemas are short and forgiving about extra keys (a model that adds a field still parses); everything that
matters is checked again by the gates before a record is written. Prompts live in prompts/<role>.md and are
filled by plain placeholder substitution, so the text sent for the same pack is the same on every run.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..settings import PROMPTS_DIR, load_registry


class _Out(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EvidenceItem(_Out):
    pid: str
    quote: str
    stance: Literal["support", "contradict", "neutral"]
    sentence: str


class InvestigatorOut(_Out):
    evidence: list[EvidenceItem] = Field(default_factory=list)


class ChallengeItem(_Out):
    alt_type: str
    argument: str
    evidence_pids: list[str] = Field(default_factory=list)
    calc_rids: list[str] = Field(default_factory=list)
    severity: Literal["substantive", "minor"]


class RedteamOut(_Out):
    challenges: list[ChallengeItem] = Field(default_factory=list)


class RebuttalItem(_Out):
    challenge_index: int
    argument: str
    evidence_rids: list[str] = Field(default_factory=list)
    calc_rids: list[str] = Field(default_factory=list)


class AdvocateOut(_Out):
    rebuttals: list[RebuttalItem] = Field(default_factory=list)


class EntailLabel(_Out):
    index: int
    label: Literal["entails", "neutral", "contradicts"]


class EntailOut(_Out):
    labels: list[EntailLabel] = Field(default_factory=list)


@lru_cache(maxsize=8)
def template(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def fill(name: str, **values: str) -> list[dict]:
    text = template(name)
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return [{"role": "user", "content": text}]


def alternatives_text() -> str:
    return "\n".join(f"- {a['id']}: answered only by {a['rebuttal']}" for a in load_registry("alternatives.yaml"))


def alternative_ids() -> frozenset[str]:
    return frozenset(a["id"] for a in load_registry("alternatives.yaml"))


def evidence_text(items: list[dict]) -> str:
    if not items:
        return "(none admitted)"
    return json.dumps(items, indent=1, ensure_ascii=False)
