"""Location parsing: raw strings → normalized metros with COL / tax / premium bucket.

Semantics preserved distinctly (§7): multi-office postings explode into one LocationInfo per
office; "Multiple locations" and remote-eligibility are flagged, not collapsed.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from radar.config import CONFIG_DIR

# Aliases that are ambiguous on their own (state names / abbreviations that are also metro names).
WEAK_ALIASES = {
    "washington",
    "new york",
    "ny",
    "dc",
    "sf",
    "la, ca",
    "columbia md",
    "new york, new york",
}

# Metros spanning more than one state; a parsed state in this set does not contradict the metro.
EXTRA_STATES = {
    "new_york": {"NY", "NJ", "CT"},
    "washington_dc": {"VA", "MD", "DC"},
    "kansas_city": {"MO", "KS"},
    "philadelphia": {"PA", "NJ", "DE"},
    "portland": {"OR", "WA"},
    "charlotte": {"NC", "SC"},
    "chicago": {"IL", "IN", "WI"},
    "cincinnati": {"OH", "KY"},
    "st_louis": {"MO", "IL"},
    "boston": {"MA", "NH"},
    "wilmington_de": {"DE", "PA", "NJ"},
    "omaha": {"NE", "IA"},
}

_REMOTE = re.compile(
    r"\bremote\b|\banywhere\b|\bwork from home\b|\bwfh\b|\bdistributed\b|\bvirtual\b|\btelecommute\b",
    re.I,
)
_HYBRID = re.compile(r"\bhybrid\b", re.I)
_MULTIPLE = re.compile(
    r"\bmultiple (locations|cities|offices)\b|\bvarious( locations)?\b|\bother locations\b|\bmultiple\b|\bnationwide\b|\bopen to (any|all) (us )?locations?\b|\bany (us )?location\b|\bflexible location\b",
    re.I,
)
_US_WORDS = re.compile(
    r"\bunited states( of america)?\b|\busa\b|\bu\.s\.a?\.?\b|\bus\b|\bamerica\b", re.I
)
_STATE_ABBR = re.compile(r"(?:^|[,\s\-/(])([A-Z]{2})(?=$|[,\s)\-/.])")
_SPLIT = re.compile(r"\s*(?:;|\||•|\n| or )\s*", re.I)
_WS = re.compile(r"\s+")

US_NON_HUB_COL = 85.0  # prior COL index for a US location that matched no metro


@dataclass
class LocationInfo:
    raw: str
    kind: str  # metro | us_unknown | remote | multiple | international | unknown
    metro: str | None = None
    metro_name: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    is_remote: bool = False
    is_hybrid: bool = False
    premium_bucket: str = "elsewhere"
    col_index: float | None = None
    tax_jurisdiction: str | None = None
    major_tech_hub: bool = False
    baseline_market: bool = False
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


class MetroTable:
    def __init__(self, data: dict) -> None:
        self.version = data.get("version")
        self.metros: dict[str, dict] = data["metros"]
        self.us_states: dict[str, str] = data["us_states"]
        self.state_names = {v.lower(): k for k, v in self.us_states.items()}
        self.countries: dict[str, list[str]] = data["countries"]
        entries: list[tuple[int, str, str, re.Pattern[str]]] = []
        for metro_id, m in self.metros.items():
            for alias in m.get("aliases", []):
                a = alias.lower().strip()
                pat = re.compile(r"(?<![a-z])" + re.escape(a) + r"(?![a-z])", re.I)
                entries.append((len(a), a, metro_id, pat))
        entries.sort(key=lambda e: -e[0])  # longest alias first
        self._aliases = entries
        self._country_pats: list[tuple[str, re.Pattern[str]]] = []
        for cc, words in self.countries.items():
            for w in words:
                self._country_pats.append(
                    (cc, re.compile(r"(?<![a-z])" + re.escape(w.lower()) + r"(?![a-z])", re.I))
                )

    def match_metro(self, text: str) -> tuple[str | None, str | None, bool]:
        """Return (metro_id, alias, weak). Strong aliases win over weak ones regardless of length."""
        weak_hit: tuple[str, str] | None = None
        for _, alias, metro_id, pat in self._aliases:
            if pat.search(text):
                if alias in WEAK_ALIASES:
                    if weak_hit is None:
                        weak_hit = (metro_id, alias)
                    continue
                return metro_id, alias, False
        if weak_hit:
            return weak_hit[0], weak_hit[1], True
        return None, None, False

    def match_country(self, text: str) -> str | None:
        """Non-US country code if a country/city word matches."""
        for cc, pat in self._country_pats:
            if pat.search(text):
                return cc
        return None


@lru_cache(maxsize=1)
def load_metros(path: Path | None = None) -> MetroTable:
    p = path or (CONFIG_DIR / "metros.yaml")
    return MetroTable(yaml.safe_load(p.read_text()))


@lru_cache(maxsize=1)
def load_tax_tables(path: Path | None = None) -> dict:
    p = path or (CONFIG_DIR / "tax_tables.yaml")
    return yaml.safe_load(p.read_text())


def _clean(s: str) -> str:
    s = (s or "").replace("–", "-").replace("—", "-")
    s = re.sub(r"\s+-\s+", ", ", s)  # "US - NY - New York" → "US, NY, New York"
    s = re.sub(r"[()]", " ", s)
    return _WS.sub(" ", s).strip(" ,")


def _parse_state(text: str, table: MetroTable, country_words: str | None) -> str | None:
    """US state abbreviation or full name. Returns None for e.g. 'Bangalore, IN' (IN == India)."""
    for m in _STATE_ABBR.finditer(text):
        abbr = m.group(1)
        if abbr not in table.us_states:
            continue
        if country_words and abbr == country_words:
            continue  # 'IN' after an Indian city is India, 'DE' after Berlin is Germany, etc.
        return abbr
    low = text.lower()
    for name, abbr in table.state_names.items():
        if name in {"washington", "new york"}:
            continue  # ambiguous with the metros; handled by WEAK_ALIASES
        if re.search(r"(?<![a-z])" + re.escape(name) + r"(?![a-z])", low):
            return abbr
    if re.search(r"\b(d\.c\.|dc)\b", low) and "washington" in low:
        return "DC"
    return None


def parse_location(
    raw: str, *, table: MetroTable | None = None, tax: dict | None = None
) -> LocationInfo:
    """Parse one location string."""
    table = table or load_metros()
    tax = tax or load_tax_tables()
    text = _clean(raw)
    low = text.lower()
    info = LocationInfo(raw=raw, kind="unknown")
    if not text:
        return info

    info.is_remote = bool(_REMOTE.search(text))
    info.is_hybrid = bool(_HYBRID.search(text))
    is_multiple = bool(_MULTIPLE.search(text))
    country_words = table.match_country(low)
    state = _parse_state(text, table, country_words)
    us_words = bool(_US_WORDS.search(text))

    metro_id, alias, weak = table.match_metro(low)
    if metro_id:
        allowed = EXTRA_STATES.get(metro_id, {table.metros[metro_id]["state"]})
        if state and state not in allowed:
            metro_id = None  # "Columbus, GA" is not Columbus, OH
        elif weak and country_words and not state:
            metro_id = None  # "New York" as a weak alias next to a foreign country word

    if metro_id:
        m = table.metros[metro_id]
        info.kind = "metro"
        info.metro = metro_id
        info.metro_name = m["name"]
        info.state = state or m["state"]
        info.country = "US"
        info.premium_bucket = m["premium_bucket"]
        info.col_index = float(m["col_index"])
        # Multi-state metros: Jersey City is NJ tax, Bethesda is MD tax, even though the metro is NYC/DC.
        if state and state != m["state"]:
            info.tax_jurisdiction = tax["state_to_jurisdiction"].get(state, m["tax"])
        else:
            info.tax_jurisdiction = m["tax"]
        info.major_tech_hub = bool(m.get("major_tech_hub", False))
        info.baseline_market = bool(m.get("baseline_market", False))
        info.city = (alias or "").title() or None
        info.confidence = 0.6 if weak else 0.9
        if info.is_remote and not info.is_hybrid:
            info.premium_bucket = "remote"  # "Remote - New York": remote job, NY-based for tax
        return info

    is_international = bool(country_words) and not (state or us_words)
    if is_international:
        info.kind = "international"
        info.country = country_words
        info.confidence = 0.8
        return info

    if info.is_remote:
        info.kind = "remote"
        info.country = "US"
        info.state = state
        info.premium_bucket = "remote"
        info.col_index = US_NON_HUB_COL
        info.tax_jurisdiction = (
            tax["state_to_jurisdiction"].get(state, "remote") if state else "remote"
        )
        info.confidence = 0.8
        return info

    if is_multiple:
        info.kind = "multiple"
        info.country = "US"
        info.col_index = US_NON_HUB_COL
        info.tax_jurisdiction = "us_unknown"
        info.confidence = 0.7
        return info

    if state:
        info.kind = "us_unknown"
        info.country = "US"
        info.state = state
        info.city = text.split(",")[0].strip().title() if "," in text else None
        info.col_index = US_NON_HUB_COL
        info.tax_jurisdiction = tax["state_to_jurisdiction"].get(state, "us_unknown")
        info.confidence = 0.7
        return info

    if us_words:
        info.kind = "us_unknown"
        info.country = "US"
        info.col_index = US_NON_HUB_COL
        info.tax_jurisdiction = "us_unknown"
        info.confidence = 0.5
        return info

    info.confidence = 0.2
    return info


def parse_locations(raw_values: list[str] | str | None) -> list[LocationInfo]:
    """Parse one or many raw location strings, splitting combined strings on ; | 'or'."""
    if raw_values is None:
        return []
    if isinstance(raw_values, str):
        raw_values = [raw_values]
    table = load_metros()
    tax = load_tax_tables()
    out: list[LocationInfo] = []
    seen: set[tuple] = set()
    for raw in raw_values:
        if not raw or not str(raw).strip():
            continue
        parts = [p for p in _SPLIT.split(str(raw)) if p and p.strip()] or [str(raw)]
        for part in parts:
            info = parse_location(part, table=table, tax=tax)
            key = (info.kind, info.metro, info.state, info.country, info.is_remote)
            if key in seen:
                continue
            seen.add(key)
            out.append(info)
    return out


_BUCKET_ORDER = {
    "new_york": 0,
    "san_francisco": 1,
    "seattle": 2,
    "other_major_tech_hub": 3,
    "washington_dc": 4,
    "elsewhere": 6,
    "remote": 7,
}


def summarize_locations(locs: list[LocationInfo]) -> dict:
    """Posting-level rollup used by the posting builder."""
    metros = sorted({loc.metro for loc in locs if loc.metro})
    countries = sorted({loc.country for loc in locs if loc.country})
    known = [loc for loc in locs if loc.kind != "unknown"]
    intl_only = bool(known) and all(loc.kind == "international" for loc in known)
    remote = any(loc.is_remote for loc in locs)
    hybrid = any(loc.is_hybrid for loc in locs)
    multiple = any(loc.kind == "multiple" for loc in locs) or len(metros) > 1
    # Primary metro = the best office for the operator (NYC > SF > SEA > hubs > …), since a
    # multi-office req lets the candidate pick.
    candidates = [loc for loc in locs if loc.kind in {"metro", "us_unknown", "remote"}]
    primary = min(
        candidates,
        key=lambda loc: (_BUCKET_ORDER.get(loc.premium_bucket, 9), -loc.confidence),
        default=None,
    )
    if locs and all(loc.is_remote or loc.kind == "remote" for loc in locs) and not hybrid:
        work_mode = "remote"
    elif hybrid:
        work_mode = "hybrid"
    elif any(loc.kind == "metro" for loc in locs):
        work_mode = "hybrid" if remote else "onsite"
    else:
        work_mode = "unknown"
    return {
        "locations": [loc.to_dict() for loc in locs],
        "metros": metros,
        "primary_metro": (primary.metro or primary.kind) if primary else None,
        "primary": primary,
        "country_codes": countries,
        "is_international_only": intl_only,
        "is_multiple_locations": multiple,
        "work_mode": work_mode,
        "remote_eligible": remote,
    }
