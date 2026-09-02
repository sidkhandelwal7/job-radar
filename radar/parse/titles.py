"""Title normalization: raw title → (role_family, subfamily, seniority, program_type, …).

Rules live in config/title_rules.yaml so they can be tuned without code changes and re-applied
with `radar rescore`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from radar.config import CONFIG_DIR

_PUNCT = re.compile(r"[–—\-_/|(),:;\[\]{}\"'`’]+")
_WS = re.compile(r"\s+")
_PAREN_YEAR = re.compile(r"\b(20\d\d)\s*[-–]\s*(20\d\d)\b")


@dataclass
class TitleInfo:
    raw: str
    normalized: str
    role_family: str
    role_subfamily: str | None
    seniority: str  # internship | new_grad | mid | senior | staff | principal | manager | executive | unknown
    program_type: str  # rotational | analyst_program | apprenticeship | new_grad_program | standard
    employment_type_hint: str | None  # internship | contract | part_time | None
    step_down: bool
    tech_tags: list[str] = field(default_factory=list)
    is_new_grad_signal: bool = False
    matched_rules: dict[str, str] = field(default_factory=dict)


class _Rule:
    __slots__ = ("attrs", "patterns")

    def __init__(self, patterns: list[str], **attrs: object) -> None:
        self.patterns = [re.compile(p, re.IGNORECASE) for p in patterns]
        self.attrs = attrs

    def match(self, text: str) -> str | None:
        for p in self.patterns:
            if p.search(text):
                return p.pattern
        return None


class TitleRules:
    def __init__(self, data: dict) -> None:
        self.version = data.get("version")
        self.seniority = [_Rule(r["patterns"], level=r["level"]) for r in data["seniority"]]
        self.role_family = [
            _Rule(
                r["patterns"],
                family=r["family"],
                subfamily=r.get("subfamily"),
                step_down=bool(r.get("step_down", False)),
            )
            for r in data["role_family"]
        ]
        self.subfamily = [_Rule(r["patterns"], name=r["name"]) for r in data["subfamily"]]
        self.program_type = [_Rule(r["patterns"], name=r["name"]) for r in data["program_type"]]
        self.employment_type = [
            _Rule(r["patterns"], type=r["type"]) for r in data["employment_type"]
        ]
        self.tech_tags = {k: _Rule(v) for k, v in data["tech_tags"].items()}


@lru_cache(maxsize=1)
def load_rules(path: Path | None = None) -> TitleRules:
    p = path or (CONFIG_DIR / "title_rules.yaml")
    return TitleRules(yaml.safe_load(p.read_text()))


def normalize_title_text(title: str) -> str:
    """Lowercase, strip punctuation to spaces, collapse whitespace. Keeps '+', '#', '.' (C++, C#, .NET)."""
    t = title or ""
    t = t.replace("&", " and ")
    t = _PUNCT.sub(" ", t)
    t = _WS.sub(" ", t).strip().lower()
    return t


def normalize_title(title: str, *, rules: TitleRules | None = None) -> TitleInfo:
    rules = rules or load_rules()
    norm = normalize_title_text(title)
    text = f" {norm} "
    matched: dict[str, str] = {}

    # seniority
    seniority = "unknown"
    for r in rules.seniority:
        if (m := r.match(text)) is not None:
            seniority = str(r.attrs["level"])
            matched["seniority"] = m
            break

    # role family
    family, subfamily, step_down = "unknown", None, False
    for r in rules.role_family:
        if (m := r.match(text)) is not None:
            family = str(r.attrs["family"])
            subfamily = r.attrs.get("subfamily")  # type: ignore[assignment]
            step_down = bool(r.attrs.get("step_down", False))
            matched["role_family"] = m
            break

    # A generic "engineer" title with a quant/trading context already resolved above.
    if family == "software_engineering" and subfamily is None:
        for r in rules.subfamily:
            if (m := r.match(text)) is not None:
                subfamily = str(r.attrs["name"])
                matched["subfamily"] = m
                break
        subfamily = subfamily or "generalist"

    program_type = "standard"
    for r in rules.program_type:
        if (m := r.match(text)) is not None:
            program_type = str(r.attrs["name"])
            matched["program_type"] = m
            break

    emp_hint = None
    for r in rules.employment_type:
        if (m := r.match(text)) is not None:
            emp_hint = str(r.attrs["type"])
            matched["employment_type"] = m
            break

    tags = [tag for tag, rule in rules.tech_tags.items() if rule.match(text) is not None]

    return TitleInfo(
        raw=title,
        normalized=norm,
        role_family=family,
        role_subfamily=subfamily,  # type: ignore[arg-type]
        seniority=seniority,
        program_type=program_type,
        employment_type_hint=emp_hint,
        step_down=step_down,
        tech_tags=tags,
        is_new_grad_signal=(seniority == "new_grad" or program_type != "standard"),
        matched_rules=matched,
    )


def extract_tech_tags(text: str, *, rules: TitleRules | None = None) -> list[str]:
    """Tech tags from a longer text (description). Same rule table as titles."""
    rules = rules or load_rules()
    t = f" {(text or '').lower()} "
    return [tag for tag, rule in rules.tech_tags.items() if rule.match(t) is not None]


# Families that are plausibly "software-engineering-adjacent" enough to belong in scope.
ENGINEERING_FAMILIES = {
    "software_engineering",
    "ml_ai",
    "data_engineering",
    "devops_sre",
    "security",
}
