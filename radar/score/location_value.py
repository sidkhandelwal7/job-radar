"""§9 — the baseline, location value, COL/tax math, and the three-state verdict.

Two numbers, always both shown (§9b):
  real_terms_vs_baseline  = base_col_adjusted − tax_delta − baseline_base      (informational only)
  effective_value         = base_col_adjusted + location_utility_premium − tax_delta
where
  base_col_adjusted = nominal_base × (baseline_col / metro_col)
  tax_delta         = nominal_base × (metro_rate − baseline_rate)      (state+local effective rates)

Worked examples are pinned in tests/test_location_value.py and generated from this code.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from radar.config import Config
from radar.parse.locations import load_metros, load_tax_tables

US_NON_HUB_COL = 85.0


@dataclass
class LocationValue:
    nominal_base: float
    metro: str | None
    metro_name: str
    col_index: float
    baseline_col: float
    base_col_adjusted: (
        float  # after tax AND purchasing power, in baseline-metro pre-tax-equivalent dollars
    )
    col_adjustment: (
        float  # purchasing-power step on after-tax money; negative when the metro is pricier
    )
    premium_bucket: str
    location_utility_premium: float
    tax_jurisdiction: str
    tax_rate: float
    baseline_tax_rate: float
    tax_delta: (
        float  # positive number = you pay this much MORE tax than in the baseline jurisdiction
    )
    base_after_tax_est: float
    effective_value: float
    baseline_base: float
    real_terms_vs_baseline: float
    effective_delta_vs_baseline: float
    lines: list[tuple[str, float]]  # decomposition rows, in display order

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["lines"] = [{"label": label, "amount": amt} for label, amt in self.lines]
        return d


def _metro_facts(primary: dict | None, cfg: Config) -> tuple[str | None, str, float, str, str]:
    """(metro_id, name, col_index, premium_bucket, tax_jurisdiction) for a posting's primary location."""
    metros = load_metros().metros
    if not primary:
        return None, "Location unknown", US_NON_HUB_COL, "elsewhere", "us_unknown"
    kind = primary.get("kind")
    metro = primary.get("metro")
    if kind == "metro" and metro in metros:
        m = metros[metro]
        bucket = primary.get("premium_bucket") or m["premium_bucket"]
        return (
            metro,
            m["name"],
            float(m["col_index"]),
            bucket,
            primary.get("tax_jurisdiction") or m["tax"],
        )
    if kind == "remote":
        return (
            "remote",
            "Remote (US)",
            float(metros[cfg.baseline.metro]["col_index"]),
            "remote",
            primary.get("tax_jurisdiction") or "remote",
        )
    if kind in ("us_unknown", "multiple"):
        return (
            None,
            primary.get("raw") or "US (metro unknown)",
            US_NON_HUB_COL,
            "elsewhere",
            primary.get("tax_jurisdiction") or "us_unknown",
        )
    return None, primary.get("raw") or "Unknown", US_NON_HUB_COL, "elsewhere", "us_unknown"


def compute_location_value(nominal_base: float, primary: dict | None, cfg: Config) -> LocationValue:
    metros = load_metros().metros
    tax = load_tax_tables()
    baseline_metro = metros[cfg.baseline.metro]
    baseline_col = float(baseline_metro["col_index"])
    baseline_jur = baseline_metro["tax"]  # the baseline metro's own jurisdiction (metros.yaml)
    baseline_rate = float(tax["jurisdictions"][baseline_jur]["effective_rate_at_100k"])
    metro, name, col, bucket, jur = _metro_facts(primary, cfg)
    if jur == "remote":  # taxed where you live: the baseline's own jurisdiction
        jur = baseline_jur
    rate = float(
        tax["jurisdictions"].get(jur, tax["jurisdictions"]["us_unknown"])["effective_rate_at_100k"]
    )
    premium = cfg.location_utility_premium.for_bucket(bucket)
    # remote: genuinely neutral on COL too (you live where you live) → use baseline COL
    if bucket == "remote":
        col = baseline_col
    baseline_base = float(cfg.baseline.base_salary)

    # Unit-consistent form (D56). Every term is expressed in "baseline-metro pre-tax-equivalent dollars":
    #   1. after-tax in the metro:        nominal × (1 − rate)
    #   2. purchasing power vs baseline:  × (baseline_col / col), uplift capped (D28)
    #   3. back to the baseline's pre-tax scale so the parity rule and the gates still apply:
    #                                     ÷ (1 − baseline_rate)
    #   4. + location premium (stated in the same units: what a year in that metro is worth to you)
    after_tax = nominal_base * (1 - rate)
    pp_ratio = baseline_col / col
    cap = cfg.location_utility_premium.col_uplift_cap
    if pp_ratio > 1 + cap:
        pp_ratio = (
            1 + cap
        )  # a cheap metro can raise value by at most +cap; deflation is never capped
    tax_equiv = nominal_base * (1 - rate) / (1 - baseline_rate)  # after step 1, re-expressed
    pretax_equiv = after_tax * pp_ratio / (1 - baseline_rate)  # after steps 1–3
    tax_delta = (
        nominal_base - tax_equiv
    )  # positive = you keep less than in the baseline jurisdiction
    col_adj = pretax_equiv - tax_equiv  # purchasing-power step, applied to after-tax money
    base_col_adjusted = pretax_equiv
    effective = pretax_equiv + premium
    real_terms = pretax_equiv - baseline_base
    lines = [
        (f"${nominal_base:,.0f} nominal base", nominal_base),
        (
            f"tax vs {tax['jurisdictions'][baseline_jur]['name']} ({tax['jurisdictions'].get(jur, {}).get('name', jur)} {rate:.1%} vs {baseline_rate:.1%})",
            -round(tax_delta),
        ),
        (
            f"purchasing power ({name.split(',')[0]} {col:.0f} vs {baseline_metro['name'].split(',')[0]} {baseline_col:.0f})",
            round(col_adj),
        ),
    ]
    if premium:
        lines.append((f"{_bucket_label(bucket)} location premium (your stated value)", premium))
    lines.append((f"effective value vs ${baseline_base:,.0f} baseline", round(effective)))
    return LocationValue(
        nominal_base=nominal_base,
        metro=metro,
        metro_name=name,
        col_index=col,
        baseline_col=baseline_col,
        base_col_adjusted=round(base_col_adjusted),
        col_adjustment=round(col_adj),
        premium_bucket=bucket,
        location_utility_premium=premium,
        tax_jurisdiction=jur,
        tax_rate=rate,
        baseline_tax_rate=baseline_rate,
        tax_delta=round(tax_delta),
        base_after_tax_est=round(after_tax),
        effective_value=round(effective),
        baseline_base=baseline_base,
        real_terms_vs_baseline=round(real_terms),
        effective_delta_vs_baseline=round(effective - baseline_base),
        lines=lines,
    )


def _bucket_label(bucket: str) -> str:
    return {
        "new_york": "NYC",
        "san_francisco": "SF",
        "seattle": "Seattle",
        "other_major_tech_hub": "tech-hub",
        "washington_dc": "DC",
        "remote": "remote",
        "elsewhere": "",
    }.get(bucket, bucket)


@dataclass
class Verdict:
    state: str  # clearly_better | arguably_better | worse
    reason: str
    rule: str
    confidence: str  # high | medium | low

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def three_state_verdict(
    *,
    nominal_base: float | None,
    comp_confidence: float,
    comp_source: str | None,
    location: LocationValue | None,
    company_tier: int | None,
    is_dream_list: bool,
    target_rank: int | None,
    cfg: Config,
) -> Verdict:
    """§9c. `clearly_better` — base ≥ instant_yes (posted/confident comp only, PLAN §2.7), or effective
    value ≥ parity at a materially better tier. `arguably_better` — effective value between floor and
    parity with a stated reason (location premium, tier, trajectory). `worse` — below floor or no case."""
    gates = cfg.comp_gates
    posted = comp_source in (
        "posted_range",
        "ashby_posted",
        "posted_range_text",
        "pay_transparency",
    )
    conf_label = (
        "high"
        if (posted or comp_confidence >= 0.8)
        else ("medium" if comp_confidence >= 0.5 else "low")
    )
    if nominal_base is None or location is None:
        return Verdict(
            "arguably_better" if (is_dream_list or (company_tier == 1)) else "worse",
            "no comp signal"
            + (
                "; top-tier employer keeps it in play"
                if (is_dream_list or company_tier == 1)
                else " and no tier case"
            ),
            "no_comp",
            "low",
        )
    # "materially better" than the baseline employer: dream list, Tier-1, or the #1 target category.
    # A mid-ranked category alone doesn't qualify — the baseline employer may itself sit there.
    better_tier = bool(is_dream_list) or (company_tier == 1) or (target_rank == 1)
    if nominal_base >= gates.instant_yes and (
        posted or comp_confidence >= gates.instant_yes_requires_confidence
    ):
        return Verdict(
            "clearly_better",
            f"base ${nominal_base:,.0f} ≥ ${gates.instant_yes:,.0f} instant-yes gate ({'posted range' if posted else f'confidence {comp_confidence:.0%}'})",
            "instant_yes",
            conf_label,
        )
    ev = location.effective_value
    if ev >= gates.parity and better_tier:
        why = (
            "dream-list company"
            if is_dream_list
            else ("Tier-1 employer" if company_tier == 1 else f"target category #{target_rank}")
        )
        return Verdict(
            "clearly_better",
            f"effective value ${ev:,.0f} ≥ parity at a materially better employer ({why})",
            "parity_plus_tier",
            conf_label,
        )
    if ev >= gates.hard_floor:
        reasons = []
        if location.location_utility_premium > 0:
            reasons.append(
                f"{location.metro_name.split(',')[0]} location premium (+${location.location_utility_premium:,.0f})"
            )
        if ev >= gates.parity and (posted or comp_confidence >= 0.5):
            reasons.append(f"effective value ${ev:,.0f} at or above parity")
        if better_tier:
            reasons.append("stronger employer tier / target category")
        elif company_tier == 2 and (target_rank is not None and target_rank <= 4):
            reasons.append("defensible trajectory / brand case (Tier-2, top-4 category)")
        if nominal_base >= gates.instant_yes and not posted:
            reasons.append(
                f"estimated base ${nominal_base:,.0f} clears the instant-yes gate but comp is inferred (confidence {comp_confidence:.0%})"
            )
        if reasons:
            return Verdict(
                "arguably_better", "; ".join(reasons), "floor_to_parity_with_case", conf_label
            )
        why = "no location premium, no tier case" + (
            ""
            if posted or comp_confidence >= 0.5
            else f", and comp is only inferred (confidence {comp_confidence:.0%})"
        )
        return Verdict("worse", f"effective value ${ev:,.0f}: {why}", "no_case", conf_label)
    return Verdict(
        "worse",
        f"effective value ${ev:,.0f} below the ${gates.hard_floor:,.0f} floor",
        "below_floor",
        conf_label,
    )
