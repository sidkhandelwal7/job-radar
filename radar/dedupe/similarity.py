"""Pairwise similarity for dedupe. Deterministic, cheap, explainable.

Description similarity uses local sentence embeddings when the `ml` extra is installed
(Phase 3), otherwise token Jaccard over the first 2k characters. No network, ever.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_STOP = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "for",
    "to",
    "in",
    "at",
    "with",
    "on",
    "by",
    "we",
    "you",
    "our",
    "your",
    "is",
    "are",
    "be",
    "will",
    "as",
    "that",
    "this",
    "it",
    "from",
    "their",
    "they",
    "us",
    "new",
    "grad",
    "graduate",
    "2026",
    "2027",
    "early",
    "career",
    "university",
    "campus",
    "full",
    "time",
    "fulltime",
    "program",
    "i",
    "ii",
    "1",
    "2",
}
_TOKEN = re.compile(r"[a-z0-9+#.]+")
_TRACKING = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm",
    "ref",
    "source",
    "src",
    "gh_src",
    "lever-source",
    "lever-source[]",
    "gh_jid_src",
    "oga",
    "spread",
    "_gl",
    "fbclid",
    "gclid",
    "trk",
    "trackingid",
    "refid",
}


def url_key(url: str | None) -> str | None:
    """Stable identity for a posting URL: scheme/host lowercased, tracking params removed, trailing slash dropped,
    and well-known ATS job ids extracted so `jobs.dropbox.com/listing/123?gh_jid=123` ≡ `job-boards.greenhouse.io/dropbox/jobs/123`."""
    if not url:
        return None
    try:
        u = urlparse(url.strip())
    except ValueError:
        return None
    host = u.netloc.lower().removeprefix("www.")
    path = re.sub(r"/+$", "", u.path)
    q = [
        (k, v)
        for k, v in parse_qsl(u.query, keep_blank_values=True)
        if k.lower() not in _TRACKING and not k.lower().startswith("utm_")
    ]
    # ATS-specific ids
    m = re.search(r"(?:^|&)gh_jid=(\d+)", u.query)
    if m:
        return f"greenhouse:{m.group(1)}"
    if host.endswith("greenhouse.io"):
        m = re.search(r"/jobs/(\d+)", path)
        if m:
            return f"greenhouse:{m.group(1)}"
    if host.endswith("lever.co"):
        m = re.search(r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", path)
        if m:
            return f"lever:{m.group(1)}"
    if host.endswith("ashbyhq.com"):
        m = re.search(r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", path)
        if m:
            return f"ashby:{m.group(1)}"
    if host.endswith("myworkdayjobs.com"):
        m = re.search(r"_([A-Za-z]{0,4}-?\d[\w-]*)$", path)
        if m:
            return f"workday:{host.split('.')[0]}:{m.group(1)}"
    if host.endswith("workable.com"):
        m = re.search(r"/j/([A-Z0-9]{8,12})", path, re.I)
        if m:
            return f"workable:{m.group(1).upper()}"
    if host.endswith("smartrecruiters.com"):
        m = re.search(r"/(\d{9,})", path)
        if m:
            return f"smartrecruiters:{m.group(1)}"
    if host.endswith("oraclecloud.com"):
        m = re.search(r"/job/(\d+)", path)
        if m:
            return f"oracle:{host.split('.')[0]}:{m.group(1)}"
    if host.endswith("recruitee.com") or "/o/" in path:
        m = re.search(r"/o/([a-z0-9-]+)", path)
        if m:
            return f"recruitee:{host.split('.')[0]}:{m.group(1)}"
    if host.endswith("icims.com"):
        m = re.search(r"/jobs/(\d+)", path)
        if m:
            return f"icims:{host.split('.')[0]}:{m.group(1)}"
    # Generic employer sites: the last path segment (or query value) carrying ≥5 digits is the req id
    # (lifeattiktok.com/search/7669908…, tesla.com/careers/search/job/259427, careers.adobe.com/…/ADOBUSR159163…).
    segs = [seg for seg in path.split("/") if seg]
    ident = next((seg for seg in reversed(segs) if re.search(r"\d{5,}", seg)), None)
    if ident is None:
        for _, v in q:
            if re.fullmatch(r"\d{5,}", v):
                ident = v
                break
    if ident:
        return f"site:{host}:{ident}"
    return urlunparse((u.scheme.lower() or "https", host, path, "", urlencode(sorted(q)), ""))


def title_tokens(title_norm: str | None) -> set[str]:
    return {t for t in _TOKEN.findall((title_norm or "").lower()) if t not in _STOP}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def title_similarity(a: str | None, b: str | None) -> float:
    ta, tb = title_tokens(a), title_tokens(b)
    j = jaccard(ta, tb)
    seq = SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()
    return 0.6 * j + 0.4 * seq


def desc_tokens(desc: str | None) -> set[str]:
    return {t for t in _TOKEN.findall((desc or "")[:2000].lower()) if t not in _STOP and len(t) > 2}


def description_similarity(a: str | None, b: str | None, *, embed=None) -> float | None:
    """None when either side lacks a description (no evidence either way)."""
    if not a or not b:
        return None
    if embed is not None:
        try:
            return float(embed(a, b))
        except Exception:
            pass
    return jaccard(desc_tokens(a), desc_tokens(b))


def comp_overlap(a_min, a_max, b_min, b_max) -> float | None:
    if (a_min is None and a_max is None) or (b_min is None and b_max is None):
        return None
    a_lo, a_hi = a_min or a_max, a_max or a_min
    b_lo, b_hi = b_min or b_max, b_max or b_min
    if a_hi < b_lo or b_hi < a_lo:
        return 0.0
    inter = min(a_hi, b_hi) - max(a_lo, b_lo)
    union = max(a_hi, b_hi) - min(a_lo, b_lo) or 1.0
    return max(0.0, min(1.0, inter / union)) if union else 1.0


@dataclass
class PairScore:
    score: float
    title: float
    desc: float | None
    comp: float | None
    days_apart: float | None
    reason: str


def pair_score(a: dict, b: dict, *, embed=None) -> PairScore:
    """Combine evidence. Same URL key is decisive (handled before calling this)."""
    t = title_similarity(
        a.get("title_normalized") or a.get("title"), b.get("title_normalized") or b.get("title")
    )
    d = description_similarity(a.get("description_md"), b.get("description_md"), embed=embed)
    c = comp_overlap(
        a.get("base_posted_min"),
        a.get("base_posted_max"),
        b.get("base_posted_min"),
        b.get("base_posted_max"),
    )
    days = None
    if a.get("posted_ts") is not None and b.get("posted_ts") is not None:
        days = abs(a["posted_ts"] - b["posted_ts"]) / 86400.0
    score = t
    if d is not None:
        score = score * 0.6 + d * 0.4
    if c is not None:
        score = score * 0.85 + c * 0.15
    if days is not None:
        if days > 300:
            score -= 0.4
        elif days > 90:
            score -= 0.25
        elif days > 45:
            score -= 0.12
        elif days <= 14:
            score += 0.03
    sa, sb = a.get("seniority"), b.get("seniority")
    if sa and sb and sa != "unknown" and sb != "unknown" and sa != sb:
        score -= 0.3  # "Senior Software Engineer" is not "Software Engineer"
    # different explicit req ids from the same provider → not the same req
    if (
        a.get("source_provider") == b.get("source_provider")
        and a.get("source_provider") != "github"
        and a.get("source_job_id") != b.get("source_job_id")
        and a.get("source_slug") == b.get("source_slug")
    ):
        score -= 0.5
    score = max(0.0, min(1.0, score))
    reason = (
        f"title {t:.2f}"
        + (f", desc {d:.2f}" if d is not None else "")
        + (f", comp {c:.2f}" if c is not None else "")
        + (f", {days:.0f}d apart" if days is not None else "")
    )
    return PairScore(score=score, title=t, desc=d, comp=c, days_apart=days, reason=reason)
