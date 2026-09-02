"""Typed configuration: loads config/config.yaml (or the shipped example), validates, exposes a singleton.

Also hosts the §18 model-pinning assertion: any model that resolves to a premium tier for a
scheduled task is a hard startup failure.
"""

from __future__ import annotations

import os
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

PROJECT_ROOT = Path(os.environ.get("RADAR_ROOT", Path(__file__).resolve().parent.parent))
CONFIG_DIR = PROJECT_ROOT / "config"

FORBIDDEN_SCHEDULED_MODELS = ("fable", "opus", "mythos")
ALLOWED_SCHEDULED_MODELS = ("sonnet", "haiku")


class ModelPinningError(RuntimeError):
    pass


class OperatorCfg(BaseModel):
    name: str
    email: str
    school: str = ""
    graduation: date
    gpa: float
    citizenship: str = "US"
    needs_sponsorship: bool = False
    resume_path: str = "./resume.pdf"
    earliest_start: date | None = None
    preferred_start: date | None = None
    treat_as_quant_candidate: bool = False
    leetcode_level: str = "medium"
    referral_availability: str = "high"


class BaselineCfg(BaseModel):
    employer: str
    program: str = ""
    metro: str
    state: str = ""
    base_salary: float
    signing_bonus: float = 0
    equity_annual: float = 0
    decision_deadline: date
    start_date: date
    signed: bool = False


class CompGatesCfg(BaseModel):
    instant_yes: float
    parity: float
    hard_floor: float
    instant_yes_requires_confidence: float = 0.7
    international_high_pay_threshold: float = 150000


class LocationPremiumCfg(BaseModel):
    """Annual $ a year in each bucket is worth to you, added AFTER tax and purchasing power (§9b).
    All zero by default: the tool ranks on money alone until you state a preference."""

    new_york: float = 0
    san_francisco: float = 0
    seattle: float = 0
    washington_dc: float = 0
    other_major_tech_hub: float = 0
    remote: float = 0
    elsewhere: float = 0
    family_proximity_weight: float = 0
    col_uplift_cap: float = 0.25

    def for_bucket(self, bucket: str) -> float:
        return float(getattr(self, bucket, self.elsewhere))


class WeightsCfg(BaseModel):
    comp_score: float
    career_capital_score: float
    fit_score: float
    winnability_score: float
    location_score: float
    culture_score: float

    @model_validator(mode="after")
    def _sum_to_one(self) -> WeightsCfg:
        total = sum(self.model_dump().values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"weights must sum to 1.0, got {total:.4f}")
        return self

    def as_dict(self) -> dict[str, float]:
        return self.model_dump()


class ModifiersCfg(BaseModel):
    low_comp_confidence_threshold: float = 0.3
    low_comp_confidence_multiplier: float = 0.9
    secured_referral_multiplier: float = 1.05
    stretch_req_winnability_multiplier: float = 0.85
    dream_list_career_capital_multiplier: float = 1.10
    quant_trading_firm_winnability_cap: float = 0.25
    leetcode_max_share_of_winnability: float = 0.15


class HardExcludeCfg(BaseModel):
    clearance_required: bool = True
    advanced_degree_required: bool = True
    internship: bool = True
    contract: bool = True
    part_time: bool = True
    unpaid: bool = True


class ScopeCfg(BaseModel):
    include_stretch_1_2_yoe: bool = True
    max_years_experience_in_scope: float = 2
    hard_exclude: HardExcludeCfg = HardExcludeCfg()
    international_default: str = "exclude"


class EVCfg(BaseModel):
    horizon_years: float = 3
    hourly_opportunity_cost: float = 40
    career_capital_premium_per_point: float = 4000


class SwitchingFrictionCfg(BaseModel):
    """Itemized cost of walking away from the baseline offer after accepting it. Every term is
    zero by default and zero-able; the curve rises convexly from decision_deadline to start_date."""

    signing_bonus_clawback: float = 0
    goodwill_cost_at_signing: float = 0
    goodwill_cost_at_start: float = 0
    curve_exponent: float = 2.0
    university_channel_cost: float = 0
    same_market_penalty: float = 0
    cheap_zone_ends: date = date(2026, 12, 31)


class ThroughputCfg(BaseModel):
    applications_per_week: int = 8
    prep_hours_per_week: float = 10
    today_bucket_max: int = 10


class LLMCfg(BaseModel):
    enabled: bool = True
    classifier_model: str = "haiku"
    enrichment_model: str = "sonnet"
    drafting_model: str = "sonnet"
    bare: bool = False
    max_calls_per_run: int = 200
    calls_per_cycle: int = 12
    cache_dir: str = "data/cache/llm"

    @field_validator("classifier_model", "enrichment_model", "drafting_model")
    @classmethod
    def _pinned(cls, v: str) -> str:
        assert_model_allowed_for_scheduled_work(v)
        return v

    def models(self) -> dict[str, str]:
        return {
            "classifier": self.classifier_model,
            "enrichment": self.enrichment_model,
            "drafting": self.drafting_model,
        }


class EmbeddingsCfg(BaseModel):
    model: str = "sentence-transformers/all-MiniLM-L6-v2"
    cache_dir: str = "data/cache/embeddings"


class CadencesCfg(BaseModel):
    dream_list: int = 15
    tier1: int = 15
    tier2: int = 60
    long_tail: int = 360
    aggregators: int = 15


class FetchCfg(BaseModel):
    user_agent: str
    concurrency: int = 8
    per_host_concurrency: int = 3
    timeout_seconds: float = 30
    detail_fetch: str = "all_new"
    source_timeout_seconds: float = 600  # one source may not hold a cycle hostage
    cycle_budget_seconds: float = 720  # stop starting new sources after this; the rest stay due
    cadences: CadencesCfg = CadencesCfg()
    reprobe_empty_after_days: int = 14


class LinksCfg(BaseModel):
    verify_after_hours: int = 48
    sweep_every_hours: int = 6
    per_host_rps: float = 1.0


class ActionsCfg(BaseModel):
    # The cloud job is a backstop, not the primary path (D52). Raw invocation count is the only
    # number we compute ourselves; real minutes come from GitHub's billing page/API.
    invocation_target_per_month: int = 800
    github_user: str = ""  # for `radar actions-usage --billing` (needs GITHUB_BILLING_TOKEN)


class NotifyCfg(BaseModel):
    quiet_hours_et: list[int] = [23, 8]
    p0_max_per_week_target: int = 3
    digest_time_et: str = "08:00"
    weekly_digest_day: str = "sunday"
    weekly_digest_hour_et: int = 18
    p0_triggers: dict[str, bool] = Field(
        default_factory=lambda: {
            "dream_list_new_grad": True,
            "clearly_better_closing": True,
            "shortlist_deadline_72h": True,
            "saved_filter": True,
        }
    )


class PathsCfg(BaseModel):
    data_dir: str = "data"
    db: str = "data/radar.db"
    raw_dir: str = "data/raw"
    backups_dir: str = "data/backups"


class SkillCfg(BaseModel):
    """One skill the operator has (or explicitly lacks). proficiency 0..1; terms = lowercase
    substrings that locate the evidence bullet in the resume text."""

    proficiency: float
    label: str = ""
    terms: list[str] = Field(default_factory=list)


class ProfileCfg(BaseModel):
    """Who the operator is, for the fit scorer and the LLM prompts. Everything personal lives here
    or in `operator`; the code carries no candidate-specific assumptions."""

    summary: str = "a graduating computer-science student"
    skills: dict[str, SkillCfg] = Field(default_factory=dict)
    edge_tags: list[str] = Field(default_factory=list)  # required tags that are a real advantage
    gap_tags: list[str] = Field(default_factory=list)  # required tags that are a real weakness
    edges: list[str] = Field(default_factory=list)  # prose, fed to the LLM prompts
    gaps: list[str] = Field(default_factory=list)
    domain_tag: str = ""  # e.g. finance_domain — a bonus for postings in domain_categories
    domain_categories: list[str] = Field(default_factory=list)
    big_tech_stack_note: str = ""  # appended as a gap on big_tech_swe reqs that miss every edge tag


class Config(BaseModel):
    operator: OperatorCfg
    profile: ProfileCfg = ProfileCfg()
    baseline: BaselineCfg
    comp_gates: CompGatesCfg
    location_utility_premium: LocationPremiumCfg = LocationPremiumCfg()
    col_index_version: str = "2026-08"
    tax_tables_version: str = "2026-08"
    weights: WeightsCfg
    modifiers: ModifiersCfg = ModifiersCfg()
    target_ranking: dict[str, int]
    dream_list: list[str] = Field(default_factory=list)
    floor_exempt_companies: list[str] = Field(default_factory=list)
    blocked_companies: list[str] = Field(default_factory=list)
    blocked_metros: list[str] = Field(default_factory=list)
    scope: ScopeCfg = ScopeCfg()
    ev: EVCfg = EVCfg()
    switching_friction: SwitchingFrictionCfg = SwitchingFrictionCfg()
    throughput: ThroughputCfg = ThroughputCfg()
    llm: LLMCfg = LLMCfg()
    embeddings: EmbeddingsCfg = EmbeddingsCfg()
    fetch: FetchCfg
    links: LinksCfg = LinksCfg()
    actions: ActionsCfg = ActionsCfg()
    notify: NotifyCfg = NotifyCfg()
    paths: PathsCfg = PathsCfg()

    # --- resolved paths -------------------------------------------------------------------------
    @property
    def root(self) -> Path:
        return PROJECT_ROOT

    @property
    def db_path(self) -> Path:
        return self._abs(self.paths.db)

    @property
    def raw_dir(self) -> Path:
        return self._abs(self.paths.raw_dir)

    @property
    def data_dir(self) -> Path:
        return self._abs(self.paths.data_dir)

    @property
    def backups_dir(self) -> Path:
        return self._abs(self.paths.backups_dir)

    @property
    def resume_path(self) -> Path:
        return self._abs(self.operator.resume_path)

    def _abs(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else PROJECT_ROOT / path

    def is_floor_exempt(self, company_slug: str | None) -> bool:
        return bool(company_slug) and (
            company_slug in self.dream_list or company_slug in self.floor_exempt_companies
        )


def assert_model_allowed_for_scheduled_work(model: str) -> None:
    """§18: nothing recurring may run on a premium-tier model. Fail loudly."""
    m = (model or "").lower()
    if not m:
        raise ModelPinningError("LLM model must be set explicitly (sonnet or haiku); got empty")
    if any(f in m for f in FORBIDDEN_SCHEDULED_MODELS):
        raise ModelPinningError(
            f"Model {model!r} is forbidden for scheduled/recurring work (§18). "
            f"Use one of {ALLOWED_SCHEDULED_MODELS}."
        )
    if not any(a in m for a in ALLOWED_SCHEDULED_MODELS):
        raise ModelPinningError(
            f"Model {model!r} is not an allowed scheduled model; use one of {ALLOWED_SCHEDULED_MODELS}."
        )


def config_path() -> Path:
    """config/config.yaml if the operator has one, else the shipped example (a fresh clone runs)."""
    real = CONFIG_DIR / "config.yaml"
    return real if real.exists() else CONFIG_DIR / "example.config.yaml"


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text()) or {}
    # the one-flag kill switch for all LLM work (§18), also honoured from the environment so a
    # container or a cron line can disable enrichment without editing config
    if os.environ.get("RADAR_LLM_ENABLED", "").lower() in ("0", "false", "no"):
        raw.setdefault("llm", {})
        raw["llm"] = {**raw["llm"], "enabled": False}
    return Config.model_validate(raw)


@lru_cache(maxsize=1)
def get_config() -> Config:
    override = os.environ.get("RADAR_CONFIG")
    return load_config(Path(override) if override else None)


def reset_config_cache() -> None:
    get_config.cache_clear()
