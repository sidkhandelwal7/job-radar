"""§8.3 comp cascade: posted → company recent → LCA prior → peer model. Intervals + named source +
confidence, never a confident point estimate from nothing. Base is the primary figure; TC secondary."""

from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from radar import db
from radar.config import CONFIG_DIR, Config

POSTED_SOURCES = {
    "posted_range": 0.90,
    "ashby_posted": 0.92,
    "pay_transparency": 0.90,
    "posted_range_text": 0.75,
}


@lru_cache(maxsize=1)
def load_priors(path: Path | None = None) -> dict[str, Any]:
    return yaml.safe_load((path or (CONFIG_DIR / "comp_priors.yaml")).read_text())


@dataclass
class CompEstimate:
    base_est: float | None
    base_est_low: float | None
    base_est_high: float | None
    comp_source: str | None
    comp_confidence: float
    signing_est: float | None
    bonus_target_pct_est: float | None
    equity_type: str | None
    equity_annual_est: float | None
    tc_year1_est: float | None
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("explanation")
        return d


class PeerModel:
    """Medians of OUR OWN posted ranges, bucketed. Built once per scoring run. Buckets (most to least
    specific): (category, tier, bucket, seniority) → (category, seniority) → (seniority) → global."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        rows = db.all_rows(
            conn,
            "SELECT target_category, company_tier, primary_metro, seniority, role_family, base_posted_min, base_posted_max, company_id, posted_at, first_seen_at "
            "FROM postings WHERE base_posted_min IS NOT NULL AND base_posted_max IS NOT NULL AND base_posted_max >= 30000 AND base_posted_max < 600000 "
            "AND role_family IN ('software_engineering','ml_ai','data_engineering','devops_sre','security') AND is_international_only = 0",
        )
        from radar.parse.locations import load_metros

        metros = load_metros().metros
        self.buckets: dict[tuple, list[tuple[float, float]]] = {}
        self.company: dict[tuple, list[tuple[float, float]]] = {}
        cols: list[float] = []
        for r in rows:
            cols.append(float(metros.get(r["primary_metro"] or "", {}).get("col_index") or 100.0))
            sen = (
                r["seniority"] or "unknown"
            )  # explicit new_grad samples are kept apart from unknown
            pb = metros.get(r["primary_metro"] or "", {}).get(
                "premium_bucket", "elsewhere" if r["primary_metro"] != "remote" else "remote"
            )
            pair = (float(r["base_posted_min"]), float(r["base_posted_max"]))
            for key in (
                ("cat_tier_bucket_sen", r["target_category"], r["company_tier"], pb, sen),
                ("cat_sen", r["target_category"], sen),
                ("sen", sen),
                ("global",),
            ):
                self.buckets.setdefault(key, []).append(pair)
            if r["company_id"]:
                self.company.setdefault((r["company_id"], sen, r["role_family"]), []).append(pair)
        self.sample_col = statistics.median(cols) if cols else 100.0

    @staticmethod
    def _summ(pairs: list[tuple[float, float]]) -> tuple[float, float, float, int]:
        los = [a for a, _ in pairs]
        his = [b for _, b in pairs]
        return (
            statistics.median(los),
            statistics.median(his),
            statistics.median([(a + b) / 2 for a, b in pairs]),
            len(pairs),
        )

    UNKNOWN_SENIORITY_DISCOUNT = 0.8  # unknown-seniority samples skew mid-level

    def company_recent(
        self, company_id: int | None, seniority: str, family: str
    ) -> tuple[float, float, float, int] | None:
        if not company_id:
            return None
        sen = "new_grad" if seniority in ("new_grad", "unknown") else seniority
        pairs = self.company.get((company_id, sen, family)) or self.company.get(
            (company_id, sen, "software_engineering")
        )
        if pairs and len(pairs) >= 2:
            return self._summ(pairs)
        if sen == "new_grad":
            unk = self.company.get((company_id, "unknown", family)) or self.company.get(
                (company_id, "unknown", "software_engineering")
            )
            if unk and len(unk) >= 2:
                lo, hi, mid, n = self._summ(unk)
                d = self.UNKNOWN_SENIORITY_DISCOUNT
                return lo * d, hi * d, mid * d, n
        return None

    def peers(
        self, category: str | None, tier: int | None, bucket: str, seniority: str
    ) -> tuple[tuple[float, float, float, int], str, float] | None:
        sen = "new_grad" if seniority in ("new_grad", "unknown") else seniority
        for key, label, conf in (
            (("cat_tier_bucket_sen", category, tier, bucket, sen), "peer_model", 0.35),
            (("cat_sen", category, sen), "peer_model_category", 0.30),
            (("sen", sen), "peer_model_seniority", 0.25),
            (("global",), "peer_model_global", 0.20),
        ):
            pairs = self.buckets.get(key)
            if pairs and len(pairs) >= 5:
                return self._summ(pairs), label, conf
        return None


def estimate_comp(
    p: dict[str, Any],
    company: dict[str, Any] | None,
    peers: PeerModel,
    cfg: Config,
    lca: Any = None,
) -> CompEstimate:
    pri = load_priors()
    cat = p.get("target_category") or (company or {}).get("target_category") or "other"
    tier = p.get("company_tier") or (company or {}).get("tier")
    sen = p.get("seniority") or "unknown"
    point = pri["stretch_point_in_range"] if p.get("is_stretch") else pri["new_grad_point_in_range"]
    lo = hi = base = None
    source: str | None = None
    conf = 0.0
    expl = ""
    # 1. posted
    if p.get("base_posted_min") is not None or p.get("base_posted_max") is not None:
        lo = float(p.get("base_posted_min") or p.get("base_posted_max"))
        hi = float(p.get("base_posted_max") or p.get("base_posted_min"))
        base = lo + point * (hi - lo)
        source = p.get("comp_source") or "posted_range"
        conf = POSTED_SOURCES.get(source, 0.75)
        expl = f"posted range ${lo:,.0f}–${hi:,.0f}; new-grad point at {point:.0%} of range"
    # 2. company recent
    if base is None:
        cr = peers.company_recent(
            p.get("company_id"), sen, p.get("role_family") or "software_engineering"
        )
        if cr:
            lo, hi, _mid, n = cr
            base = lo + point * (hi - lo)
            source, conf = "company_recent", 0.60
            expl = f"{n} recent posted ranges at this company for this level: median ${lo:,.0f}–${hi:,.0f}"
    # 3. LCA prior
    if base is None and lca is not None:
        est = lca.lookup(company, p)
        if est:
            lo, hi, base, n = est["low"], est["high"], est["point"], est["n"]
            source, conf = "lca_prior", 0.45
            expl = f"DOL LCA filings ({n}) for this employer/SOC/metro at Level I–II — a floor, not an offer"
    # 4. peer model
    if base is None:
        from radar.parse.locations import load_metros

        metros = load_metros().metros
        pb = metros.get(p.get("primary_metro") or "", {}).get(
            "premium_bucket", "remote" if p.get("primary_metro") == "remote" else "elsewhere"
        )
        pm = peers.peers(cat, tier, pb, sen)
        if pm:
            (lo, hi, _mid, n), source, conf = pm
            if source != "peer_model":
                # fallback buckets mix metros: scale sub-linearly by this metro's COL vs the SF-heavy sample
                target_col = float(
                    metros.get(p.get("primary_metro") or "", {}).get("col_index") or 100.0
                )
                scale = (target_col / peers.sample_col) ** 0.6
                lo, hi = lo * scale, hi * scale
                lo *= pri["tier_multiplier"].get(int(tier) if tier else 2, 1.0)
                hi *= pri["tier_multiplier"].get(int(tier) if tier else 2, 1.0)
            base = lo + point * (hi - lo)
            expl = (
                f"{source.replace('_', ' ')} from {n} posted ranges: median ${lo:,.0f}–${hi:,.0f}"
            )
        else:
            g = pri["global_new_grad_swe_base"]
            lo, hi, base = float(g["low"]), float(g["high"]), float(g["mid"])
            source, conf = "unknown", 0.15
            expl = "no signal — wide US new-grad SWE prior"
    # sanity: a new-grad base inferred from anything but a posted range is capped (LCA Level II at the
    # very top of the market is ~$195k; anything above that is a mid-level sample leaking in)
    if source not in POSTED_SOURCES and base is not None and sen in ("new_grad", "unknown"):
        cap = float(pri.get("inferred_new_grad_cap", 210000))
        if base > cap:
            base, hi = cap, min(hi or cap, cap * 1.1)
            lo = min(lo or cap, cap)
            expl += f"; capped at ${cap:,.0f} (inferred new-grad ceiling)"
    # signing / bonus / equity priors
    cp = pri["by_category"].get(cat, pri["by_category"]["other"])
    tags = set((company or {}).get("tags") or [])
    equity_type = cp["equity_type"]
    if equity_type != "none" and (
        tags & set(pri["public_tags"]) or (company or {}).get("is_public")
    ):
        equity_type = "rsu_public"
    hc = pri["haircuts"]
    equity_annual = (
        float(cp["equity_annual"]) * hc.get(equity_type, 0.0) if equity_type != "none" else 0.0
    )
    signing = float(cp["signing"])
    bonus_pct = float(cp["bonus_pct"])
    tc = (
        (base or 0)
        + signing * hc["signing"]
        + (base or 0) * bonus_pct * hc["bonus_target"]
        + equity_annual
    )
    return CompEstimate(
        base_est=round(base) if base is not None else None,
        base_est_low=round(lo) if lo is not None else None,
        base_est_high=round(hi) if hi is not None else None,
        comp_source=source,
        comp_confidence=round(conf, 2),
        signing_est=round(signing),
        bonus_target_pct_est=bonus_pct,
        equity_type=equity_type,
        equity_annual_est=round(equity_annual),
        tc_year1_est=round(tc) if base is not None else None,
        explanation=expl
        + (
            f"; signing/bonus/equity from {cat} priors (signing ×{hc['signing']:.0%}, {equity_type} ×{hc.get(equity_type, 0):.0%})"
            if base is not None
            else ""
        ),
    )


def comp_json(est: CompEstimate) -> str:
    return json.dumps({**est.to_dict(), "explanation": est.explanation})
