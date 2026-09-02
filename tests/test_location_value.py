"""§9b/§9e: the correction that matters most, pinned as tests.

Numbers assume the example config (baseline metro `chicago`: COL 85, IL 4.9%; New York: COL 140,
NYS+NYC 9.4%). Premiums are zero in the example config, so the tests that exercise the premium set
it explicitly on the config object.
"""

from radar.score.location_value import compute_location_value, three_state_verdict


def _primary(metro, kind="metro", tax=None):
    from radar.parse.locations import load_metros

    m = load_metros().metros.get(metro, {})
    return {
        "kind": kind,
        "metro": metro if kind == "metro" else None,
        "premium_bucket": m.get("premium_bucket", "remote" if kind == "remote" else "elsewhere"),
        "tax_jurisdiction": tax or m.get("tax"),
        "raw": metro,
    }


def test_worked_example_nyc_130k(tmp_project):
    """D56 unit-consistent form, NYC $130k with a $32k premium: after-tax first, then purchasing
    power, then premium.
    after-tax 130,000 × (1 − 0.094) = 117,780 · re-expressed in baseline pre-tax dollars ÷ (1 − 0.049)
    = 123,849 (tax step −6,151) · purchasing power × 85/140 → 75,194 (step −48,655) · + $32k
    premium = 107,194."""
    tmp_project.location_utility_premium.new_york = 32_000
    lv = compute_location_value(130_000, _primary("new_york"), tmp_project)
    assert lv.tax_delta == 6_151
    assert lv.col_adjustment == -48_655
    assert lv.base_col_adjusted == 75_194
    assert lv.location_utility_premium == 32_000
    assert lv.effective_value == 107_194
    # the steps compose (each stored step is rounded independently, so ±2 dollars)
    assert (
        abs(
            130_000
            - lv.tax_delta
            + lv.col_adjustment
            + lv.location_utility_premium
            - lv.effective_value
        )
        <= 2
    )


def test_parity_salary_in_premium_metro_is_never_worse(tmp_project):
    """The §9b correction: a posting at the baseline salary in a metro you have stated a premium
    for must never come out `worse` — the premium is the case, and the real-terms cut stays visible."""
    cfg = tmp_project
    cfg.location_utility_premium.new_york = 40_000
    base = float(cfg.baseline.base_salary)
    lv = compute_location_value(base, _primary("new_york"), cfg)
    assert lv.real_terms_vs_baseline < -25_000  # honest: it's a big real-terms cut…
    assert lv.effective_value >= cfg.comp_gates.hard_floor + 5_000  # …but well above the floor
    v = three_state_verdict(
        nominal_base=base,
        comp_confidence=0.9,
        comp_source="posted_range",
        location=lv,
        company_tier=2,
        is_dream_list=False,
        target_rank=3,
        cfg=cfg,
    )
    assert v.state in ("arguably_better", "clearly_better")
    assert "premium" in v.reason


def test_no_premium_means_money_only(tmp_project):
    """With every premium at zero (the shipped default) the same posting is judged on money alone."""
    base = float(tmp_project.baseline.base_salary)
    lv = compute_location_value(base, _primary("new_york"), tmp_project)
    assert lv.location_utility_premium == 0
    assert lv.effective_value == lv.base_col_adjusted
    assert lv.effective_value < tmp_project.comp_gates.hard_floor


def test_seattle_no_income_tax_beats_nyc_at_equal_base(tmp_project):
    sea = compute_location_value(110_000, _primary("seattle"), tmp_project)
    nyc = compute_location_value(110_000, _primary("new_york"), tmp_project)
    assert sea.tax_delta < 0 < nyc.tax_delta
    assert sea.base_after_tax_est > nyc.base_after_tax_est


def test_remote_is_neutral(tmp_project):
    base = float(tmp_project.baseline.base_salary)
    r = compute_location_value(base, _primary("remote", kind="remote", tax="remote"), tmp_project)
    assert r.location_utility_premium == 0 and r.col_adjustment == 0 and abs(r.tax_delta) < 1
    assert r.effective_value == base


def test_instant_yes_requires_posted_or_confident_comp(tmp_project):
    lv = compute_location_value(105_000, _primary("columbus"), tmp_project)
    inferred = three_state_verdict(
        nominal_base=105_000,
        comp_confidence=0.4,
        comp_source="peer_model",
        location=lv,
        company_tier=3,
        is_dream_list=False,
        target_rank=8,
        cfg=tmp_project,
    )
    assert inferred.state != "clearly_better"
    posted = three_state_verdict(
        nominal_base=105_000,
        comp_confidence=0.95,
        comp_source="posted_range",
        location=lv,
        company_tier=3,
        is_dream_list=False,
        target_rank=8,
        cfg=tmp_project,
    )
    assert posted.state == "clearly_better"


def test_below_floor_is_worse_even_in_nyc(tmp_project):
    lv = compute_location_value(70_000, _primary("new_york"), tmp_project)
    v = three_state_verdict(
        nominal_base=70_000,
        comp_confidence=0.9,
        comp_source="posted_range",
        location=lv,
        company_tier=1,
        is_dream_list=True,
        target_rank=1,
        cfg=tmp_project,
    )
    assert v.state == "worse"
