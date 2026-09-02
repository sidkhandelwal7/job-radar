"""Deterministic qualification extraction (D63) — free, runs on every in-scope row.

Two jobs:
  extract_min_years(title, desc) — years-of-experience requirement from title + description,
      with the negative cases that must NOT gate a new grad out ("0–2 years", "including
      internship experience", "no experience required", "new grad welcome").
  title_hard_seniority(title) — tokens that make a role senior REGARDLESS of the body text:
      Senior, Staff, Principal, Lead, Sr., Manager, Director, Architect, II/III/IV, numeric
      levels above I (Engineer 2, L4, Level 3). Title seniority is more reliable than description
      parsing; it wins over anything the description says.
"""

from __future__ import annotations

import re

# "X+ years", "X-Y years", "X to Y years" ... of experience (existing form, kept)
_RANGE = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:-|–|to)?\s*(\d{1,2})?\s*\+?\s*(?:years|yrs)(?:'|’)?\s+(?:of\s+)?"
    r"(?:relevant |professional |industry |related |hands[- ]on |work |software |full[- ]time "
    r"|practical |prior |demonstrated |proven |overall )*(?:experience|exp\b)",
    re.I,
)
# "minimum (of) X years", "at least X years", "X or more years", "min. X yrs"
_MINIMUM = re.compile(
    r"(?:minimum(?:\s+of)?|at\s+least|min\.?)\s+(\d{1,2})\s*\+?\s*(?:years|yrs)"
    r"|(\d{1,2})\s+or\s+more\s+(?:years|yrs)",
    re.I,
)
# phrases that mean a new grad qualifies even when a number appears
_NEUTRAL = re.compile(
    r"including\s+internship|internship(?:s)?\s+(?:experience\s+)?(?:count|counts|qualify|qualifies|included)"
    r"|no\s+(?:prior\s+|previous\s+)?(?:work\s+|professional\s+)?experience\s+(?:required|necessary|needed)"
    r"|new\s?[- ]?grad(?:uate)?s?\s+(?:are\s+)?(?:welcome|encouraged)"
    r"|recent\s+graduates?\s+(?:are\s+)?(?:welcome|encouraged|eligible)",
    re.I,
)

MAX_PLAUSIBLE = 15


def extract_min_years(
    title: str | None, desc: str | None
) -> tuple[float | None, float | None, str | None]:
    """(min_years, max_years, evidence). The smallest stated minimum wins ("0–2 years" beats
    "5+ years preferred" elsewhere in the page); a neutralizing phrase caps a small minimum to 0."""
    text = " ".join(x for x in (title, desc) if x)
    if not text:
        return None, None, None
    mins: list[tuple[float, str]] = []
    maxs: list[float] = []
    for m in _RANGE.finditer(text):
        lo = float(m.group(1))
        if lo > MAX_PLAUSIBLE:
            continue
        mins.append((lo, m.group(0).strip()))
        if m.group(2):
            maxs.append(float(m.group(2)))
    for m in _MINIMUM.finditer(text):
        lo = float(m.group(1) or m.group(2))
        if lo <= MAX_PLAUSIBLE:
            mins.append((lo, m.group(0).strip()))
    if not mins:
        neutral = _NEUTRAL.search(text)
        if neutral:
            return 0.0, None, neutral.group(0).strip()
        return None, None, None
    lo, evidence = min(mins, key=lambda x: x[0])
    neutral = _NEUTRAL.search(text)
    if neutral and lo <= 3:
        return 0.0, (max(maxs) if maxs else None), f"{evidence} — but: {neutral.group(0).strip()}"
    return lo, (max(maxs) if maxs else None), evidence


# --- title seniority: hard, description can never override -------------------------------------

_WORDS = re.compile(
    r"\b(senior|staff|principal|lead|sr\.?|manager|director|architect|vp|vice president|head of|distinguished|fellow)\b",
    re.I,
)
# Roman numerals II+ (uppercase only, so 'ii' inside words never matches), trailing/level digits ≥ 2
_ROMAN = re.compile(r"\b(II|III|IV|V|VI)\b")
_LEVEL = re.compile(
    r"\b(?:L|IC|E|P|T)([2-9])\b"  # L4, IC3, E5 …  (L1/E1 pass; most ladders start new grads at 3 — over-filtering is the instruction)
    r"|\b(?:level|lvl)\s*([2-9])\b"
    r"|(?:engineer|developer|swe|sde|analyst|scientist|consultant)\s+([2-9])\b",
    re.I,
)
# words that would false-positive: 'Lead' as a verb is rare in titles; 'Staff' in 'Staffing' is
# excluded by \b; 'Sr' inside words likewise. No allowlist on purpose: over-filter, don't under.


def title_hard_seniority(title: str | None) -> str | None:
    """The matched token when the TITLE itself says this is not an entry-level role, else None."""
    if not title:
        return None
    m = _WORDS.search(title)
    if m:
        return m.group(1)
    m = _ROMAN.search(title)
    if m:
        return m.group(1)
    m = _LEVEL.search(title)
    if m:
        return m.group(0).strip()
    return None


_NEWGRAD_TITLE = re.compile(
    r"new\s?[- ]?grad|entry[- ]level|university|campus|early\s+career|graduate\b|recent\s+graduate|junior\b|\bjr\.?\b",
    re.I,
)


def title_newgrad_signal(title: str | None) -> bool:
    return bool(title and _NEWGRAD_TITLE.search(title))


# --- start-date compatibility (D64): separate from years, the body beats the title ---------------

_START_BAD = re.compile(
    r"not\s+a\s+new\s?[- ]?grad(?:uate)?\s+(?:role|position|job)"
    r"|start(?:ing)?\s+(?:full[- ]time\s+)?(?:immediately|right\s+away)"
    r"|can\s+start\s+full\s?[- ]?time\s+right\s+away"
    r"|immediate\s+start"
    r"|immediately\s+available"
    r"|available\s+(?:to\s+start\s+)?immediately"
    r"|currently\s+enrolled\s+students\s+are\s+not\s+eligible",
    re.I,
)
_START_BY = re.compile(
    r"(?:must\s+(?:be\s+able\s+to\s+)?start|start(?:ing)?\s+(?:date\s+)?(?:by|no\s+later\s+than|on\s+or\s+before))\s+"
    r"(?:\w+\s+)?((?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{4}|\d{1,2}/\d{4}|\d{4})",
    re.I,
)
_START_GOOD = re.compile(
    r"class\s+of\s+2027"
    r"|2027\s+graduate|graduating\s+in\s+(?:\w+\s+)?2027"
    r"|(?:summer|spring|fall|winter)\s+2027(?:\s+start)?"
    r"|december\s+2026\s+or\s+spring\s+2027"
    r"|university\s+graduate[^.]{0,80}2027|2027[^.]{0,80}university\s+graduate",
    re.I,
)

_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ]
    )
}


def _parse_month_year(text: str) -> tuple[int, int] | None:
    text = text.strip().lower()
    parts = text.split()
    if len(parts) == 2 and parts[0] in _MONTHS and parts[1].isdigit():
        return int(parts[1]), _MONTHS[parts[0]]
    if "/" in text:
        m, y = text.split("/", 1)
        if m.isdigit() and y.isdigit():
            return int(y), int(m)
    if text.isdigit() and len(text) == 4:
        return int(text), 12  # a bare year deadline means "within that year" — generous
    return None


def start_date_signal(
    desc: str | None, *, earliest_start: tuple[int, int]
) -> tuple[str | None, str | None]:
    """('incompatible'|'compatible'|None, evidence). earliest_start = (year, month) you can begin.
    Only the DESCRIPTION is consulted: a title's "Early Career" means nothing against an explicit
    immediate-start requirement in the body (the Notion case, 2026-08-30)."""
    if not desc:
        return None, None
    m = _START_BAD.search(desc)
    if m:
        return "incompatible", m.group(0).strip()
    m = _START_BY.search(desc)
    if m:
        wanted = _parse_month_year(m.group(1))
        if wanted and wanted < earliest_start:
            return "incompatible", m.group(0).strip()
    m = _START_GOOD.search(desc)
    if m:
        return "compatible", m.group(0).strip()
    return None, None
