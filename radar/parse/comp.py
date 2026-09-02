"""Deterministic posted-compensation extraction from description text.

Pay-transparency postings (CO, CA, WA, NY, IL, …) state a base range in prose. We pull it with
regexes; anything ambiguous is left for the LLM enrichment step. Never invents numbers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Alternatives, most to least specific: comma-grouped annual (150,000) · k-suffixed (45k, 62.5k) ·
# plain 5-6 digit annual (150000) · plain 2-3 digit with optional cents (32.19, 20) — the last form
# is how hourly ranges are written ("$32.19-$53.68/hour"); the sanity bands below reject it unless
# the surrounding text says hourly.
_NUM = r"\$?\s?(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d+)?\s?[kK]|\d{5,6}|\d{2,3}(?:\.\d{1,2})?)"
_SEP = r"\s*(?:-|–|—|to|and|through)\s*"
RANGE = re.compile(_NUM + _SEP + _NUM + r"(?!\s*(?:%|percent))", re.I)
HOURLY = re.compile(r"(?:per\s+hour|/\s*hour|/hr|hourly|an hour)", re.I)
YEARLY_HINT = re.compile(
    r"(?:per\s+(?:year|annum)|/\s*(?:year|yr)|annual|annually|base salary|salary range|pay range|compensation range|base pay)",
    re.I,
)
BONUS_CTX = re.compile(
    r"(?:bonus|equity|stock|rsu|sign[- ]?on|relocation|total (?:target )?comp)", re.I
)
CURRENCY_NON_USD = re.compile(r"(?:€|£|CAD|C\$|A\$|INR|₹|GBP|EUR|SGD|CHF)", re.I)


@dataclass
class PostedRange:
    min: float
    max: float
    interval: str  # year | hour
    currency: str
    context: str
    confidence: float


def _to_number(tok: str) -> float | None:
    t = tok.replace("$", "").replace(",", "").strip()
    mult = 1.0
    if t and t[-1] in "kK":
        mult = 1000.0
        t = t[:-1].strip()
    try:
        return float(t) * mult
    except ValueError:
        return None


def extract_posted_range(text: str | None) -> PostedRange | None:
    """Find the most plausible annual base range in text. Returns None if nothing credible."""
    if not text:
        return None
    best: PostedRange | None = None
    for m in RANGE.finditer(text):
        lo, hi = _to_number(m.group(1)), _to_number(m.group(2))
        if lo is None or hi is None:
            continue
        window = text[max(0, m.start() - 160) : m.end() + 80]
        if CURRENCY_NON_USD.search(window) and "$" not in m.group(0) and "USD" not in window:
            continue
        interval = "year"
        if HOURLY.search(window) and lo < 500:
            interval = "hour"
        elif lo < 1000 and hi < 1000:
            # "30 - 45" without $k → could be hourly without label; skip unless '$' present and hourly ctx
            continue
        if interval == "year" and (lo < 20_000 or hi > 1_500_000 or hi < lo):
            continue
        if interval == "hour" and (lo < 7 or hi > 400 or hi < lo):
            continue
        # Does the surrounding text talk about base pay / salary?
        conf = 0.55
        if YEARLY_HINT.search(window) or HOURLY.search(window):
            conf += 0.25
        if "$" in m.group(0):
            conf += 0.1
        if BONUS_CTX.search(text[max(0, m.start() - 60) : m.start()]) and not YEARLY_HINT.search(
            window
        ):
            conf -= 0.25  # probably a bonus/equity figure
        cand = PostedRange(
            min=lo,
            max=hi,
            interval=interval,
            currency="USD",
            context=window.strip()[:240],
            confidence=round(max(0.0, min(1.0, conf)), 2),
        )
        if (
            best is None
            or cand.confidence > best.confidence
            or (
                cand.confidence == best.confidence
                and cand.interval == "year"
                and best.interval == "hour"
            )
        ):
            best = cand
    if best and best.confidence < 0.5:
        return None
    return best


def annualize(value: float, interval: str | None) -> float:
    if interval == "hour":
        return value * 2080.0
    if interval == "month":
        return value * 12.0
    return value
