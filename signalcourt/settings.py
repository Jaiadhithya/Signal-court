"""Configuration: config/settings.toml + config/models.yaml + registry/ + environment (.env).

Everything here is read-only data. `config_digest()` gives the per-file hashes that go into every
RUN record, so a run can be tied to the exact thresholds, registries and prompts it used.
Python 3.10-compatible: TOML is read with `tomli`, never `tomllib`.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import tomli
import yaml
from pydantic import BaseModel, ConfigDict
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
REGISTRY_DIR = REPO_ROOT / "registry"
PROMPTS_DIR = REPO_ROOT / "prompts"


class _Section(BaseModel):
    # A typo in settings.toml must fail loudly, not silently fall back to a default.
    model_config = ConfigDict(extra="forbid", frozen=True)


class Panel(_Section):
    quarters: int
    min_quarters_to_run: int
    tier2_peers: int
    max_peers: int
    basis_default: Literal["standalone", "consolidated"]


class Sensors(_Section):
    robust_z_threshold: float
    persistence_min_quarters: int
    pelt_min_size: int
    pelt_penalty_rule: Literal["bic"]
    open_screen_min_corr: float
    corp_action_window_q: int
    corp_action_factor: float


class Placebo(_Section):
    in_space: Literal["all_same_subtype"]
    in_time_shifts: list[int]
    min_for_strong: int
    holm_alpha: float


class Twin(_Section):
    enabled: bool
    min_donors: int
    pre_period_min_q: int
    max_pre_rmspe_sd: float
    loo: bool


class Mirror(_Section):
    enabled: bool
    min_lenders_with_edges: int


class External(_Section):
    enabled_groups: list[Literal["asset_quality", "funding", "nowcast"]]
    min_points_q: int
    min_peers: int
    lead_lags_q: list[int]
    normalise_by: Literal["advances", "premium"]


class SayDo(_Section):
    enabled: bool
    min_scored: int
    silent_deletion_min_mentions: int
    met_tolerance: Literal["display_rounding"]


class Forensics(_Section):
    enabled: bool
    similarity: Literal["tfidf_cosine"]
    min_section_words: int
    peer_z_threshold: float


class RbiDivergence(_Section):
    enabled: bool


class Snapshot(_Section):
    enabled: bool


class Rank(_Section):
    k: int
    k_max: int
    coverage_unknown: float
    surprise_floor: float


class Budgets(_Section):
    reasoning_calls: int
    reasoning_calls_cap: int
    extraction_calls: int
    extraction_calls_cap: int
    tool_calls: int
    tool_calls_cap: int
    calls_per_claim_floor: int
    calls_per_claim_cap: int
    harvest_time_cap_s: int
    court_time_cap_s: int
    concurrency: int
    tavily_queries_per_run: int


class Packs(_Section):
    max_tokens_default: int
    max_tokens_redteam: int
    max_tokens_entail_batch: int


class QuoteGate(_Section):
    min_words: int
    max_words: int
    table_window_chars: int
    fuzzy_for_ocr_only: float


class Judge(_Section):
    strong_families: int
    moderate_families: int
    strong_challenges_answered: int
    stat_holm_max: float
    moderate_p_raw_max: float
    specificity_kill: float
    specificity_rebut: float
    lead_series_min_points: int


class Writer(_Section):
    discovery_max_words: int
    why_bullets: list[int]
    timeline_min_events: int


StageImpl = Literal["stub", "real"]


class Pipeline(_Section):
    """Which implementation each stage runs; a missing "real" falls back to "stub" (signalcourt.pipeline)."""

    harvest: StageImpl
    measure: StageImpl
    sense: StageImpl
    prereg: StageImpl
    court: StageImpl
    judge: StageImpl
    report: StageImpl


class Demo(_Section):
    """Time caps for a live finals run, and the donor floor for the peer-median baseline."""

    harvest_cap_s: float
    measure_cap_s: float
    sense_cap_s: float
    prereg_cap_s: float
    court_cap_s: float
    report_cap_s: float
    peer_median_min_donors: int


class Config(_Section):
    """Mirror of config/settings.toml."""

    panel: Panel
    sensors: Sensors
    placebo: Placebo
    twin: Twin
    mirror: Mirror
    external: External
    sayado: SayDo
    forensics: Forensics
    rbi_divergence: RbiDivergence
    snapshot: Snapshot
    rank: Rank
    budgets: Budgets
    packs: Packs
    quote_gate: QuoteGate
    judge: Judge
    writer: Writer
    pipeline: Pipeline
    demo: Demo


class Env(BaseSettings):
    """Secrets and identity strings from the environment or .env.

    Key lists stay as comma-separated strings; use `keys()` to split them.
    """

    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    mistral_api_keys: str = ""
    nvidia_api_keys: str = ""
    groq_api_keys: str = ""
    gemini_api_keys: str = ""
    openrouter_api_keys: str = ""
    zai_api_keys: str = ""
    github_models_tokens: str = ""
    tavily_api_keys: str = ""
    jina_api_key: str = ""
    http_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0 Safari/537.36"
    )
    signal_home: Path = Path("./workspace")

    def keys(self, env_name: str) -> list[str]:
        """Keys for one variable name as written in models.yaml `keys_env`, e.g. "GROQ_API_KEYS"."""
        raw = getattr(self, env_name.lower(), "") or ""
        return [k.strip() for k in raw.split(",") if k.strip()]


class Workspace(BaseModel):
    """Paths under SIGNAL_HOME. Nothing is created until `ensure()`."""

    model_config = ConfigDict(frozen=True)

    home: Path

    @property
    def cache(self) -> Path:
        return self.home / "cache"

    @property
    def raw(self) -> Path:
        return self.cache / "raw"

    @property
    def text(self) -> Path:
        return self.cache / "text"

    @property
    def manifest_db(self) -> Path:
        return self.cache / "manifest.sqlite"

    @property
    def universe_db(self) -> Path:
        return self.home / "universe.sqlite"

    @property
    def llm_db(self) -> Path:
        return self.cache / "llm.sqlite"

    @property
    def index(self) -> Path:
        return self.cache / "index"

    @property
    def panels(self) -> Path:
        return self.home / "panels"

    @property
    def runs(self) -> Path:
        return self.home / "runs"

    @property
    def manual(self) -> Path:
        return self.home / "manual"

    def run_dir(self, run_id: str) -> Path:
        return self.runs / run_id

    def ensure(self) -> Workspace:
        for p in (self.raw, self.text, self.index, self.panels, self.runs, self.manual):
            p.mkdir(parents=True, exist_ok=True)
        return self


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    config: Config
    env: Env
    workspace: Workspace


class SettingsError(ValueError):
    pass


def load_config(path: Path | None = None) -> Config:
    path = path or CONFIG_DIR / "settings.toml"
    with open(path, "rb") as f:
        cfg = Config.model_validate(tomli.load(f))
    if cfg.rbi_divergence.enabled:
        rule = load_registry("rbi_divergence.yaml")
        if not (rule.get("verified") is True and rule.get("span_rid")):
            raise SettingsError(
                "rbi_divergence.enabled is true but registry/rbi_divergence.yaml is not verified "
                "with a span_rid"
            )
    return cfg


def load_settings(config_path: Path | None = None, env: Env | None = None) -> Settings:
    env = env or Env()
    home = env.signal_home if env.signal_home.is_absolute() else (Path.cwd() / env.signal_home)
    return Settings(config=load_config(config_path), env=env, workspace=Workspace(home=home.resolve()))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


def load_models(path: Path | None = None) -> dict[str, Any]:
    """Raw config/models.yaml. The router (llm/) validates and interprets it."""
    with open(path or CONFIG_DIR / "models.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_registry(name: str) -> Any:
    """A registry file by name: YAML is parsed; .txt gives non-empty, non-comment lines."""
    path = REGISTRY_DIR / name
    if path.suffix == ".txt":
        lines = path.read_text(encoding="utf-8").splitlines()
        return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def config_digest() -> dict[str, str]:
    """sha256 of every file under config/, registry/ and prompts/, keyed by repo-relative path.

    Goes into every RUN record. Bytes are hashed as stored, with CRLF normalised to LF so a
    Windows checkout and CI agree.
    """
    out: dict[str, str] = {}
    for d in (CONFIG_DIR, REGISTRY_DIR, PROMPTS_DIR):
        if not d.exists():
            continue
        for p in sorted(d.rglob("*")):
            if p.is_file() and p.name != ".gitkeep":
                data = p.read_bytes().replace(b"\r\n", b"\n")
                out[p.relative_to(REPO_ROOT).as_posix()] = hashlib.sha256(data).hexdigest()
    return out


__all__ = [
    "CONFIG_DIR",
    "PROMPTS_DIR",
    "REGISTRY_DIR",
    "REPO_ROOT",
    "Config",
    "Env",
    "Settings",
    "SettingsError",
    "Workspace",
    "config_digest",
    "get_settings",
    "load_config",
    "load_models",
    "load_registry",
    "load_settings",
]
