"""RawJob → canonical posting column values (normalized, hashable, ready to upsert).

Everything here is deterministic and re-runnable from stored raw payloads (`radar rescore`).
Workflow columns (status, notes, …) are never produced here.
"""

from __future__ import annotations

import json
import re
from typing import Any

from radar.models import RawJob, SourceSpec
from radar.parse.comp import annualize, extract_posted_range
from radar.parse.locations import parse_locations, summarize_locations
from radar.parse.titles import extract_tech_tags, normalize_title
from radar.util import content_hash, iso_or_none, squash_ws

AGGREGATOR_PROVIDERS = {"github"}

_EMP_MAP = [
    (re.compile(r"intern|co-?op", re.I), "internship"),
    (re.compile(r"contract|contingent|temporary|temp\b|freelance|fixed[- ]term", re.I), "contract"),
    (re.compile(r"part[- ]?time", re.I), "part_time"),
    (re.compile(r"full[- ]?time|regular|permanent|employee", re.I), "full_time"),
]

_CLEARANCE = re.compile(
    r"\b(active|current)\s+(ts/?sci|top[- ]secret|secret|security clearance|dod clearance|clearance)\b"
    r"|\bmust (?:currently )?(?:hold|possess|have)\b[^.]{0,60}\b(clearance|ts/?sci|top secret|secret)\b"
    r"|\b(ts/?sci|top secret)\b[^.]{0,40}\b(required|is required|clearance)\b"
    r"|\bclearance (?:is )?required\b|\brequires? (?:an? )?(?:active )?(?:security )?clearance\b"
    r"|\bpolygraph\b",
    re.I,
)
_CLEARANCE_SOFT = re.compile(
    r"\b(ability|eligib\w+|able) to obtain\b[^.]{0,40}\bclearance\b|\bmay require\b[^.]{0,40}\bclearance\b",
    re.I,
)
_ADV_DEGREE = re.compile(
    r"\b(ph\.?d|doctorate|doctoral)\b[^.]{0,40}\b(required|is required|in)\b"
    r"|\b(master'?s|ms|m\.s\.|msc|graduate degree)\b[^.]{0,30}\b(required|is required|minimum)\b"
    r"|\b(requires?|required:?|minimum (?:of )?)\s*(?:an? )?(master'?s|ms|m\.s\.|ph\.?d)\b",
    re.I,
)
_ADV_DEGREE_SOFT = re.compile(
    r"\b(master'?s|ms|m\.s\.|ph\.?d)\b[^.]{0,25}\b(preferred|a plus|bonus|or equivalent|or bachelor|bs/ms|bs or ms)\b|\b(bachelor'?s|bs|b\.s\.)\b[^.]{0,12}\b(or|/)\s*(master'?s|ms|m\.s\.)\b",
    re.I,
)
_YEARS = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:-|–|to)?\s*(\d{1,2})?\s*\+?\s*(?:years|yrs)(?:'|’)?\s+(?:of\s+)?(?:relevant |professional |industry |related |hands[- ]on |work |software |full[- ]time |practical |prior |demonstrated |proven |overall )*(?:experience|exp\b)",
    re.I,
)
_SPONSOR_NO = re.compile(
    r"\b(?:no|not|unable to|cannot|will not|won't|does not|do not)\b[^.]{0,60}\b(sponsor|sponsorship)\b|\bwithout (?:the need for )?(?:visa )?sponsorship\b|\bsponsorship (?:is )?not (?:available|offered|provided)\b",
    re.I,
)
_SPONSOR_YES = re.compile(r"\b(?:will|can|does|able to|open to)\b[^.]{0,30}\bsponsor", re.I)
_TITLE_ADV = re.compile(
    r"\b(ph\.?d|doctora(l|te)|ms/phd|phd/ms|master'?s (required|only|degree)|m\.?s\.? (required|only))\b",
    re.I,
)
_GRAD_WINDOW = re.compile(
    r"(?:graduat\w+|degree)[^.]{0,80}?\b(?:between|by|before|in|from|on or before|no later than)\s+([A-Z][a-z]+\s+)?(20\d\d)(?:\s*(?:-|–|and|to|through)\s*([A-Z][a-z]+\s+)?(20\d\d))?",
    re.I,
)


def _employment_type(hint: str | None, raw_type: str | None, title_hint: str | None) -> str:
    for src in (title_hint, raw_type, hint):
        if not src:
            continue
        for pat, val in _EMP_MAP:
            if pat.search(src):
                return val
    return "unknown"


def _years(desc: str | None) -> tuple[float | None, float | None]:
    if not desc:
        return None, None
    mins: list[float] = []
    maxs: list[float] = []
    for m in _YEARS.finditer(desc):
        lo = float(m.group(1))
        hi = float(m.group(2)) if m.group(2) else None
        if lo > 15:
            continue
        mins.append(lo)
        if hi is not None:
            maxs.append(hi)
    if not mins:
        return None, None
    # A posting that says "0-2 years" and "5+ years preferred" → min is the smallest stated minimum.
    return min(mins), (max(maxs) if maxs else None)


def _grad_window(desc: str | None) -> str | None:
    if not desc:
        return None
    m = _GRAD_WINDOW.search(desc)
    if not m:
        return None
    a, b = m.group(2), m.group(4)
    return f"{a}-{b}" if b else a


_TITLE_YEARS = re.compile(r"\b(202[5-8])\b")


def _title_window(title: str, new_grad_signal: bool) -> str | None:
    """'New Grad 2026: Software Engineer' (posted in 2026) is a 2026-start cohort → window '2026'.
    '2027 Software Engineer Program' → '2027'. Only applied to new-grad-signalled titles."""
    if not new_grad_signal:
        return None
    years = sorted({int(y) for y in _TITLE_YEARS.findall(title)})
    if not years:
        return None
    return f"{years[0]}-{years[-1]}" if len(years) > 1 else str(years[0])


# target categories that recruit from the same pool as the baseline employer; edit for yours
SAME_MARKET_CATEGORIES = {"bank_and_exchange_tech", "fintech_infrastructure"}


def _is_quant_name(name: str | None) -> bool:
    from radar.fetch.registry import is_quant_firm_name

    return is_quant_firm_name(name)


def derive_from_description(desc: str | None, title: str = "") -> dict[str, Any]:
    """Fields that come from the description text alone (used at ingest and when a description
    arrives later for an aggregator row)."""
    out: dict[str, Any] = {}
    if not desc:
        return out
    low = desc
    out["requires_clearance"] = 1 if _CLEARANCE.search(low) else 0
    out["requires_advanced_degree"] = (
        1
        if (_ADV_DEGREE.search(low) and not _ADV_DEGREE_SOFT.search(low))
        or _TITLE_ADV.search(title or "")
        else 0
    )
    from radar.parse.quals import extract_min_years, start_date_signal

    y_min, y_max, _ev = extract_min_years(title, desc)
    out["min_years_experience"], out["max_years_experience"] = y_min, y_max
    from radar.config import get_config

    es = get_config().operator.earliest_start
    flag, evidence = start_date_signal(
        desc, earliest_start=(es.year, es.month) if es else (2027, 5)
    )
    out["start_flag"], out["start_evidence"] = flag, evidence
    if _SPONSOR_NO.search(low):
        out["sponsorship"] = "does_not_offer"
    elif _SPONSOR_YES.search(low):
        out["sponsorship"] = "offers"
    else:
        out["sponsorship"] = "unknown"
    out["graduation_window"] = _grad_window(desc)
    pr = extract_posted_range(desc)
    if pr:
        lo, hi = pr.min, pr.max
        if pr.interval != "year":
            lo, hi = annualize(lo, pr.interval), annualize(hi, pr.interval)
        out.update(
            {
                "base_posted_min": lo,
                "base_posted_max": hi,
                "base_posted_currency": "USD",
                "base_posted_interval": pr.interval,
                "comp_source": "posted_range_text",
            }
        )
    out["tech_tags"] = extract_tech_tags(desc)
    return out


def build_posting_values(
    job: RawJob, spec: SourceSpec, *, company: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return the dict of posting columns derived purely from the raw job + registry info.

    `company` = {id, name, tier, is_dream_list, target_category, is_quant_trading_firm}; for ATS
    sources it comes from the registry row behind the spec, for aggregator rows from CompanyMatcher.
    """
    company = company or {
        "id": spec.extra.get("company_id"),
        "name": spec.company_name,
        "tier": spec.extra.get("company_tier"),
        "is_dream_list": spec.extra.get("is_dream_list"),
        "target_category": spec.extra.get("target_category"),
        "is_quant_trading_firm": spec.extra.get("is_quant_trading_firm"),
    }
    company_id = company.get("id")
    company_name = company.get("name")
    title = squash_ws(job.title) or "(untitled)"
    ti = normalize_title(title)
    desc: str | None = None
    if job.description_md:
        desc = job.description_md
    elif job.description_html:
        from radar.fetch.adapters.base import html_to_md

        desc = html_to_md(job.description_html)
    loc_summary = summarize_locations(parse_locations(job.locations))
    primary = loc_summary["primary"]
    remote_eligible = bool(job.remote) or loc_summary["remote_eligible"]
    work_mode = loc_summary["work_mode"]
    if job.remote and work_mode in {"unknown", "onsite"}:
        work_mode = "remote" if not loc_summary["metros"] else "hybrid"
    emp = _employment_type(None, job.employment_type, ti.employment_type_hint)
    if ti.seniority == "internship":
        emp = "internship"

    # comp: adapter-provided snapshot first, then regex over the description
    base_min = base_max = None
    base_interval = None
    comp_source = None
    if job.comp and (job.comp.min or job.comp.max):
        base_min, base_max = job.comp.min, job.comp.max
        base_interval = job.comp.interval or "year"
        comp_source = job.comp.source
    else:
        pr = extract_posted_range(desc)
        if pr:
            base_min, base_max, base_interval, comp_source = (
                pr.min,
                pr.max,
                pr.interval,
                "posted_range_text",
            )
    if base_min is not None and base_interval and base_interval != "year":
        base_min, base_max = (
            annualize(base_min, base_interval),
            annualize(base_max or base_min, base_interval),
        )

    desc_lower = desc or ""
    clearance: int | None = None
    if desc:
        if _CLEARANCE.search(desc_lower):
            clearance = 1
        elif _CLEARANCE_SOFT.search(desc_lower):
            clearance = 0
        else:
            clearance = 0
    adv: int | None = None
    if desc:
        adv = (
            1 if (_ADV_DEGREE.search(desc_lower) and not _ADV_DEGREE_SOFT.search(desc_lower)) else 0
        )
    if job.raw.get("advanced_degree_only") or _TITLE_ADV.search(title):
        adv = 1
    if job.raw.get("citizenship_required") and clearance is None:
        clearance = 0  # citizenship ≠ clearance; note only
    y_min, y_max = _years(desc)
    sponsorship = None
    if desc:
        if _SPONSOR_NO.search(desc_lower):
            sponsorship = "does_not_offer"
        elif _SPONSOR_YES.search(desc_lower):
            sponsorship = "offers"
        else:
            sponsorship = "unknown"
    agg_spons = (job.raw.get("sponsorship") or "").lower()
    if "does not" in agg_spons:
        sponsorship = "does_not_offer"
    elif agg_spons.startswith("offers"):
        sponsorship = "offers"
    tech_tags = sorted(set(ti.tech_tags) | set(extract_tech_tags(desc) if desc else []))
    if spec.provider in AGGREGATOR_PROVIDERS:
        source_kind = "third_party" if job.raw.get("third_party_link") else "aggregator"
    else:
        source_kind = "company_direct"
    is_new_grad = int(
        ti.is_new_grad_signal
        or (y_min is not None and y_min <= 1 and ti.seniority in {"unknown", "new_grad"})
    )

    values: dict[str, Any] = {
        "source": source_kind,
        "source_provider": spec.provider,
        "source_slug": spec.slug,
        "source_job_id": job.source_job_id,
        "apply_url": job.apply_url,
        "canonical_url": job.canonical_url or job.apply_url,
        "company_name": company_name or job.company_name or spec.company_name,
        "company_id": company_id,
        "title": title,
        "title_normalized": ti.normalized,
        "description_md": desc,
        "description_fetched": int(bool(desc)),
        "department": squash_ws(job.department) or None,
        "team": squash_ws(job.team) or None,
        "employment_type": emp,
        "posted_at": iso_or_none(job.posted_at),
        "updated_at_source": iso_or_none(job.updated_at),
        "application_deadline": iso_or_none(job.application_deadline),
        "start_date": iso_or_none(job.start_date),
        "locations_json": json.dumps(loc_summary["locations"]),
        "metros_json": json.dumps(loc_summary["metros"]),
        "primary_metro": loc_summary["primary_metro"],
        "country_codes_json": json.dumps(loc_summary["country_codes"]),
        "is_international_only": int(loc_summary["is_international_only"]),
        "is_multiple_locations": int(loc_summary["is_multiple_locations"]),
        "work_mode": work_mode,
        "remote_eligible": int(remote_eligible),
        "requires_clearance": clearance,
        "requires_advanced_degree": adv,
        "min_years_experience": y_min,
        "max_years_experience": y_max,
        "sponsorship": sponsorship,
        "graduation_window": _grad_window(desc) or _title_window(title, ti.is_new_grad_signal),
        "base_posted_min": base_min,
        "base_posted_max": base_max,
        "base_posted_currency": "USD" if base_min is not None else None,
        "base_posted_interval": base_interval,
        "comp_source": comp_source,
        "role_family": ti.role_family,
        "role_subfamily": ti.role_subfamily,
        "seniority": ti.seniority,
        "is_new_grad": is_new_grad,
        "program_type": ti.program_type,
        "tech_tags_json": json.dumps(tech_tags),
        # registry-derived (cheap; scoring may refine target_category for quant roles)
        "company_tier": company.get("tier"),
        "is_dream_list": int(bool(company.get("is_dream_list"))),
        "target_category": (
            "quant_dev_research_trading"
            if (
                ti.role_family == "quant"
                or company.get("is_quant_trading_firm")
                or _is_quant_name(company_name or job.company_name)
            )
            else company.get("target_category")
        ),
        # a posting in a metro flagged `baseline_market` (metros.yaml) at a company in one of the
        # categories that compete with the baseline employer for the same candidates
        "same_market_as_baseline_offer": int(
            bool(primary and primary.baseline_market)
            and (company.get("target_category") in SAME_MARKET_CATEGORIES)
        ),
        "parse_confidence": round(
            min(
                1.0,
                0.5
                + (0.2 if desc else 0)
                + (0.2 if loc_summary["metros"] or loc_summary["is_international_only"] else 0)
                + (0.1 if ti.role_family != "unknown" else 0),
            ),
            2,
        ),
    }
    values["content_hash"] = content_hash(
        {
            "title": title,
            "locations": loc_summary["metros"] or [loc["raw"] for loc in loc_summary["locations"]],
            "desc": (desc or "")[:20000],
            "comp": [base_min, base_max],
            "emp": emp,
            "deadline": values["application_deadline"],
            "apply_url": job.apply_url,
        }
    )
    values["_primary_location"] = primary.to_dict() if primary else None
    return values


POSTING_DERIVED_COLUMNS = [
    "apply_url",
    "canonical_url",
    "company_name",
    "company_id",
    "title",
    "title_normalized",
    "description_md",
    "description_fetched",
    "department",
    "team",
    "employment_type",
    "posted_at",
    "updated_at_source",
    "application_deadline",
    "start_date",
    "locations_json",
    "metros_json",
    "primary_metro",
    "country_codes_json",
    "is_international_only",
    "is_multiple_locations",
    "work_mode",
    "remote_eligible",
    "requires_clearance",
    "requires_advanced_degree",
    "min_years_experience",
    "max_years_experience",
    "sponsorship",
    "graduation_window",
    "base_posted_min",
    "base_posted_max",
    "base_posted_currency",
    "base_posted_interval",
    "comp_source",
    "role_family",
    "role_subfamily",
    "seniority",
    "is_new_grad",
    "program_type",
    "tech_tags_json",
    "parse_confidence",
    "content_hash",
    "company_tier",
    "is_dream_list",
    "target_category",
    "same_market_as_baseline_offer",
]
