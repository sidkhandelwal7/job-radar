"""Stage 4/§2/§9d — deterministic scope classification, hard blockers, and the comp floor.

Everything here is rules (free). The embedding gate (`radar/enrich/embed.py`) only decides which
in-scope postings deserve LLM enrichment; it never changes scope.

Outputs per posting:
  in_scope (0/1), scope_reason, hard_blockers[], floor_result (pass|fail|exempt), floor_fail_reasons[],
  is_stretch (1–2 YoE), target_category (quant override), referral_likelihood
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from radar.config import Config
from radar.parse.titles import ENGINEERING_FAMILIES

STEP_DOWN_FAMILIES = {"it_support", "qa_manual", "finance_nontech", "other_nontech"}
NON_ENGINEERING = {"product", "design", "hardware", "data_science", "solutions", "quant"}
OUT_OF_SCOPE_SENIORITY = {"senior", "staff", "principal", "manager", "executive"}


@dataclass
class ScopeResult:
    in_scope: bool
    scope_reason: str
    hard_blockers: list[str] = field(default_factory=list)
    floor_result: str = "pass"  # pass | fail | exempt
    floor_fail_reasons: list[str] = field(default_factory=list)
    is_stretch: bool = False
    is_new_grad: bool = False
    referral_likelihood: str = "unknown"
    quant_capped: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "in_scope": int(self.in_scope),
            "scope_reason": self.scope_reason,
            "hard_blockers_json": json.dumps(self.hard_blockers),
            "floor_result": self.floor_result,
            "floor_fail_reasons_json": json.dumps(self.floor_fail_reasons),
            "is_stretch": int(self.is_stretch),
            "is_new_grad": int(self.is_new_grad),
            "referral_likelihood": self.referral_likelihood,
        }


def _grad_window_excludes(window: str | None, cfg: Config) -> bool:
    """'2026' or '2025-2026' excludes a May-2027 graduate; '2026-2027' or '2027' does not."""
    if not window:
        return False
    years = [int(y) for y in window.split("-") if y.isdigit()]
    if not years:
        return False
    grad_year = cfg.operator.graduation.year
    lo, hi = min(years), max(years)
    return hi < grad_year or lo > grad_year


def referral_likelihood(company: dict[str, Any] | None, cfg: Config) -> str:
    """§1d heuristic: big employer + alumni presence + target category → likely; obscure → unknown."""
    if not company:
        return "unknown"
    tier = company.get("tier")
    alumni = (company.get("alumni_presence") or "").lower()
    cat = company.get("target_category")
    score = 0
    if tier == 1:
        score += 2
    elif tier == 2:
        score += 1
    if alumni == "strong":
        score += 2
    elif alumni == "some":
        score += 1
    if cat in ("big_tech_swe", "bank_and_exchange_tech", "fintech_infrastructure"):
        score += 1
    if company.get("is_dream_list"):
        score += 1
    if cfg.operator.referral_availability == "high":
        score += 1
    if score >= 4:
        return "likely"
    if score >= 2:
        return "possible"
    return "unknown"


def classify_scope(p: dict[str, Any], company: dict[str, Any] | None, cfg: Config) -> ScopeResult:
    """p = posting row as dict (JSON columns decoded or raw). Deterministic; no network."""
    res = ScopeResult(in_scope=True, scope_reason="in scope")
    hx = cfg.scope.hard_exclude
    fam = p.get("role_family") or "unknown"
    sen = p.get("seniority") or "unknown"
    emp = p.get("employment_type") or "unknown"
    years = p.get("min_years_experience")
    title_ng = bool(p.get("is_new_grad"))
    res.is_new_grad = title_ng or (
        years is not None and years <= 1 and sen in ("unknown", "new_grad")
    )
    res.referral_likelihood = referral_likelihood(company, cfg)

    # --- hard exclusions (§2): suppressed with a reason, never deleted
    if emp == "internship" and hx.internship:
        res.hard_blockers.append("internship")
    if emp == "contract" and hx.contract:
        res.hard_blockers.append("contract")
    if emp == "part_time" and hx.part_time:
        res.hard_blockers.append("part_time")
    if p.get("requires_clearance") == 1 and hx.clearance_required:
        res.hard_blockers.append("active clearance required")
    if p.get("requires_advanced_degree") == 1 and hx.advanced_degree_required:
        res.hard_blockers.append("MS/PhD required")
    if _grad_window_excludes(p.get("graduation_window"), cfg):
        res.hard_blockers.append(
            f"graduation window {p.get('graduation_window')} excludes May {cfg.operator.graduation.year}"
        )
    if years is not None and years >= 3:
        res.hard_blockers.append(f"{years:g}+ years required")
    if p.get("start_flag") == "incompatible":
        # D64: the description says the start date rules you out; "Early Career" in the title
        # means nothing against it (the body wins)
        res.hard_blockers.append(
            f"start date incompatible: “{(p.get('start_evidence') or '')[:80]}”"
        )
    if p.get("sponsorship") == "does_not_offer" and cfg.operator.needs_sponsorship:
        res.hard_blockers.append("no sponsorship")
    if p.get("is_international_only"):
        res.hard_blockers.append("international only")
    if company and company.get("slug") in cfg.blocked_companies:
        res.hard_blockers.append("blocked company")
    if p.get("primary_metro") in cfg.blocked_metros:
        res.hard_blockers.append("blocked metro")

    # --- title seniority is HARD (D63): Senior/Staff/Lead/Sr./II/III/L4… in the title beats
    # anything the description says, including "Early Career" qualifiers
    from radar.parse.quals import title_hard_seniority, title_newgrad_signal

    tblock = title_hard_seniority(p.get("title"))
    if tblock:
        res.in_scope = False
        res.is_stretch = False
        res.scope_reason = f"title seniority: {tblock!r}"
        return res

    # --- role / seniority scope
    if sen in OUT_OF_SCOPE_SENIORITY:
        res.in_scope = False
        res.scope_reason = f"seniority {sen}"
    elif sen == "internship":
        res.in_scope = False
        res.scope_reason = "internship"
    elif fam == "not_a_role":
        res.in_scope = False
        res.scope_reason = "talent pool / event, not a req"
    elif fam in STEP_DOWN_FAMILIES:
        res.in_scope = False
        res.scope_reason = f"step down in technical depth ({fam})"
        res.floor_fail_reasons.append(f"step down in technical depth ({fam})")
    elif fam in NON_ENGINEERING:
        res.in_scope = False
        res.scope_reason = f"not a software role ({fam})"
    elif fam not in ENGINEERING_FAMILIES and fam != "unknown":
        res.in_scope = False
        res.scope_reason = f"role family {fam}"
    elif fam == "unknown":
        # recall at ingest, precision at notify: an unclassifiable title stays in scope only if the
        # description carries an engineering signal (tech tags) — "Facilitator" does not.
        tags = p.get("tech_tags")
        if tags is None:
            tags = json.loads(p.get("tech_tags_json") or "[]")
        tech = [t for t in tags if t not in ("finance_domain", "tableau")]
        if not tech:
            res.in_scope = False
            res.scope_reason = "no engineering signal in title or description"
    # --- qualification gate (D63): a gate, not a multiplier. Missing data never defaults to allowed.
    if res.in_scope:
        newgrad_signal = (
            title_ng
            or sen == "new_grad"
            or title_newgrad_signal(p.get("title"))
            or p.get("start_flag")
            == "compatible"  # "Class of 2027" in the body is a stronger signal than any title
        )
        if years is None:
            if not newgrad_signal:
                res.in_scope = False
                res.is_stretch = False
                res.scope_reason = "qualification unknown: no stated years requirement and no new-grad signal in the title"
        elif years <= 1:
            pass  # eligible
        elif years <= cfg.scope.max_years_experience_in_scope:
            # stretch (2 YoE): out of the queue and all notifications; opt-in Stretch view only
            res.is_stretch = True
            res.notes.append(f"{years:g} years required — stretch band")
            if not cfg.scope.include_stretch_1_2_yoe:
                res.in_scope = False
                res.scope_reason = f"{years:g} years required (stretch disabled)"
        # years >= 3 is already a hard blocker above
    if res.in_scope and res.hard_blockers:
        res.in_scope = False
        res.scope_reason = "hard blocker: " + "; ".join(res.hard_blockers)

    # --- §1c quant cap
    if fam == "quant" or (company and company.get("is_quant_trading_firm")):
        res.quant_capped = True

    # --- comp floor (§9d): upper bound of the posted/estimated range < hard_floor → fail
    base_hi = (
        p.get("base_posted_max")
        or p.get("base_posted_min")
        or p.get("base_est_high")
        or p.get("base_est")
    )
    exempt = bool(
        company and (company.get("is_dream_list") or cfg.is_floor_exempt(company.get("slug")))
    ) or bool(p.get("override_floor"))
    if base_hi is not None and base_hi < cfg.comp_gates.hard_floor:
        res.floor_fail_reasons.append(
            f"base upper bound ${base_hi:,.0f} < ${cfg.comp_gates.hard_floor:,.0f} floor"
        )
    if res.hard_blockers:
        res.floor_fail_reasons.extend(res.hard_blockers)
    if res.floor_fail_reasons:
        res.floor_result = "exempt" if exempt and not res.hard_blockers else "fail"
        if exempt and not res.hard_blockers:
            res.notes.append("floor-exempt company (dream list / config)")
    return res
