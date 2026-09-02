"""Monthly calibration loop (§10): fit a tiny local model on revealed preference and *propose*
weight changes in CALIBRATION.md. Proposals only — nothing here writes config.yaml.

Labels (revealed preference):
  +1  shortlisted, starred, applied, or an application that reached screen/onsite/offer
  −1  dismissed (any reason), or an application marked rejected-at-resume-screen (a fit miss)
Features: the six sub-scores at the time of the decision (from score_explanation history when
available, else current). Model: L2-regularized logistic regression, plain Python (six weights
and a few hundred rows need no numpy). Output: standardized coefficients → a suggested weight
vector blended 70/30 with the current one, plus dismissal-reason tallies, notification precision,
and the calibration of P(offer). Written to CALIBRATION.md (git-ignored: it carries dismissal reasons).
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from radar import db
from radar.config import Config
from radar.util import utcnow, utcnow_iso

FEATURES = (
    "comp_score",
    "career_capital_score",
    "fit_score",
    "winnability_score",
    "location_score",
    "culture_score",
)
WEIGHT_KEYS = {f: f for f in FEATURES}  # config.weights uses the same *_score names
MIN_LABELED = 20
MIN_POSITIVES = 5


def labeled_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cols = ", ".join(f"p.{f}" for f in FEATURES)
    rows = db.all_rows(
        conn,
        f"SELECT p.id, p.company_name, p.title, p.status, p.starred, p.dismiss_reason, p.beats_baseline, p.winnability_score AS w, {cols}, "
        "a.stage AS app_stage FROM postings p LEFT JOIN applications a ON a.posting_id = p.id "
        "WHERE p.status IN ('shortlisted','applied','dismissed') OR p.starred = 1 OR a.id IS NOT NULL",
    )
    out = []
    for r in rows:
        d = dict(r)
        if d["status"] == "dismissed":
            y = 0
        elif d["status"] in ("shortlisted", "applied") or d.get("starred") or d.get("app_stage"):
            y = 1
        else:
            continue
        if any(d.get(f) is None for f in FEATURES):
            continue
        d["y"] = y
        out.append(d)
    return out


def fit_logistic(
    X: list[list[float]], y: list[int], *, l2: float = 0.5, iters: int = 800, lr: float = 0.1
) -> list[float]:
    """Standardize, then gradient descent on the L2 logistic loss. Returns standardized coefficients."""
    n, k = len(X), len(X[0])
    mu = [sum(r[j] for r in X) / n for j in range(k)]
    sd = [math.sqrt(sum((r[j] - mu[j]) ** 2 for r in X) / n) or 1.0 for j in range(k)]
    Z = [[(r[j] - mu[j]) / sd[j] for j in range(k)] for r in X]
    w = [0.0] * k
    b = 0.0
    for _ in range(iters):
        gw = [0.0] * k
        gb = 0.0
        for z, t in zip(Z, y, strict=True):
            p = 1 / (
                1
                + math.exp(
                    -max(-30, min(30, sum(wi * zi for wi, zi in zip(w, z, strict=True)) + b))
                )
            )
            e = p - t
            gb += e
            for j in range(k):
                gw[j] += e * z[j]
        w = [wi - lr * (gw[j] / n + l2 * wi / n) for j, wi in enumerate(w)]
        b -= lr * gb / n
    return w


def propose_weights(cfg: Config, coefs: list[float]) -> dict[str, float]:
    cur = cfg.weights.model_dump()
    pos = [max(0.0, c) for c in coefs]
    if sum(pos) <= 0:
        return cur
    implied = {WEIGHT_KEYS[f]: pos[i] / sum(pos) for i, f in enumerate(FEATURES)}
    blended = {k: 0.7 * cur[k] + 0.3 * implied.get(k, cur[k]) for k in cur}
    s = sum(blended.values())
    return {k: round(v / s, 3) for k, v in blended.items()}


def offer_calibration(conn: sqlite3.Connection) -> dict[str, Any]:
    """Did P(offer) = 2% + 18% × winnability predict responses? Bucket applied rows by winnability."""
    rows = db.all_rows(
        conn,
        "SELECT p.winnability_score AS w, a.stage FROM applications a JOIN postings p ON p.id = a.posting_id WHERE p.winnability_score IS NOT NULL",
    )
    buckets: dict[str, list[int]] = {"low <0.4": [], "mid 0.4–0.6": [], "high ≥0.6": []}
    for r in rows:
        w = float(r["w"])
        key = "low <0.4" if w < 0.4 else ("mid 0.4–0.6" if w < 0.6 else "high ≥0.6")
        buckets[key].append(
            1 if r["stage"] in ("screen", "onsite", "offer", "oa_done", "oa_pending") else 0
        )
    return {
        k: {"n": len(v), "response_rate": round(sum(v) / len(v), 2) if v else None}
        for k, v in buckets.items()
    }


def run_calibration(
    conn: sqlite3.Connection, cfg: Config, *, out_path: Path | None = None
) -> dict[str, Any]:
    from radar.notify.engine import precision_report

    month = utcnow().strftime("%Y-%m")
    rows = labeled_rows(conn)
    positives = sum(r["y"] for r in rows)
    reasons = Counter((r.get("dismiss_reason") or "no reason given") for r in rows if r["y"] == 0)
    verdict_mix = Counter((r["beats_baseline"], "kept" if r["y"] else "dismissed") for r in rows)
    prec = precision_report(conn, days=30)
    result: dict[str, Any] = {
        "month": month,
        "labeled": len(rows),
        "positives": positives,
        "negatives": len(rows) - positives,
        "dismiss_reasons": reasons.most_common(),
        "verdict_mix": {f"{k[0]}/{k[1]}": v for k, v in verdict_mix.items()},
        "notification_precision_30d": prec,
        "offer_calibration": offer_calibration(conn),
        "current_weights": cfg.weights.model_dump(),
        "proposed_weights": None,
        "coefficients": None,
        "enough_signal": len(rows) >= MIN_LABELED
        and positives >= MIN_POSITIVES
        and positives < len(rows),
    }
    if result["enough_signal"]:
        X = [[float(r[f]) for f in FEATURES] for r in rows]
        y = [r["y"] for r in rows]
        coefs = fit_logistic(X, y)
        result["coefficients"] = {
            WEIGHT_KEYS[f]: round(c, 3) for f, c in zip(FEATURES, coefs, strict=True)
        }
        result["proposed_weights"] = propose_weights(cfg, coefs)
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO calibration_runs (month, ran_at, labeled, positives, proposal_json, applied) VALUES (?,?,?,?,?,0)",
            (month, utcnow_iso(), len(rows), positives, json.dumps(result, default=str)),
        )
    db.kv_set(conn, "last_calibration_at", utcnow_iso())
    path = out_path or (cfg.root / "CALIBRATION.md")
    path.write_text(render_markdown(result, cfg))
    result["path"] = str(path)
    return result


def render_markdown(r: dict[str, Any], cfg: Config) -> str:
    L = [
        f"# Calibration — {r['month']}",
        "",
        "_Proposals only. Nothing here was applied; edit `config/config.yaml` and run `radar rescore` if you agree._",
        "",
    ]
    L += [
        f"**Revealed preference so far:** {r['labeled']} labeled postings ({r['positives']} kept: shortlisted/starred/applied/advanced; {r['negatives']} dismissed).",
        "",
    ]
    if not r["enough_signal"]:
        L += [
            f"Not enough signal yet — need ≥{MIN_LABELED} labeled rows with ≥{MIN_POSITIVES} positives and at least one negative. Keep dismissing *with a reason* (`radar dismiss <id> --reason ...`); that is what teaches this loop.",
            "",
        ]
    else:
        L += [
            "## Proposed weights",
            "",
            "| sub-score | current | proposed | model coefficient (standardized) |",
            "|---|---|---|---|",
        ]
        for k, v in r["current_weights"].items():
            L.append(
                f"| {k} | {v:.2f} | {r['proposed_weights'][k]:.2f} | {r['coefficients'].get(k, 0):+.2f} |"
            )
        L += [
            "",
            "Proposed = 70% current + 30% the direction your keeps/dismisses point to (negative coefficients are clamped to zero — a sub-score you *dislike* should shrink, not flip sign).",
            "",
        ]
    if r["dismiss_reasons"]:
        L += ["## Why you dismissed", ""]
        L += [f"- {n} × {reason}" for reason, n in r["dismiss_reasons"][:12]]
        L += [""]
    if r["verdict_mix"]:
        L += ["## Verdict vs. your decision", "", "| verdict/decision | n |", "|---|---|"]
        L += [f"| {k} | {v} |" for k, v in sorted(r["verdict_mix"].items())]
        L += [
            "",
            "If you keep dismissing `clearly_better` rows, the gates are too generous; if you keep applying to `worse` rows, the premiums or the baseline are wrong.",
            "",
        ]
    p = r["notification_precision_30d"]
    L += ["## Notification precision (30 days)", ""]
    L += [
        f"- sent: {p.get('sent', 0)} · engaged: {p.get('engaged', 0)} · precision: {p.get('precision')} · P0s/week: {p.get('p0_per_week')} (target < {cfg.notify.p0_max_per_week_target})"
    ]
    if p.get("demotions"):
        L += [f"- auto-demoted to digest: {', '.join(p['demotions'])}"]
    L += [
        "",
        "## P(offer) calibration",
        "",
        "| winnability bucket | applications | response rate |",
        "|---|---|---|",
    ]
    for k, v in r["offer_calibration"].items():
        L.append(
            f"| {k} | {v['n']} | {v['response_rate'] if v['response_rate'] is not None else '—'} |"
        )
    L += [
        "",
        "P(offer) = 2% + 18% × winnability (D34). If the high bucket's response rate isn't clearly above the low bucket's after ~30 applications, winnability is not measuring what it claims.",
        "",
    ]
    return "\n".join(L)
