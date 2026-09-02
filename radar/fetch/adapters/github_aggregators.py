"""Community GitHub new-grad lists (§6b): Simplify's structured listings.json and README tables.

provider = "github"; slug = "owner/repo". Rows carry their own company names; the pipeline resolves
them against the registry so dream-list / tier flags apply, and queues unknown employers for slug
discovery. Fetched from raw.githubusercontent.com with ETags (a 304 costs nothing).
"""

from __future__ import annotations

import hashlib
import html as htmllib
import json
import re
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar

import yaml

from radar.config import CONFIG_DIR
from radar.fetch.adapters.base import BaseAdapter, FetchedPage, FetchRaw, LinkVerdict
from radar.models import RawJob, SourceSpec
from radar.util import parse_dt, to_iso, utcnow

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
        start=1,
    )
}
_HREF = re.compile(r'href="([^"]+)"', re.I)
_MDLINK = re.compile(r"\]\((https?://[^)\s]+)\)")
_BARE_URL = re.compile(r"https?://[^\s|<>\")\]]+")
_TAGS = re.compile(r"<[^>]+>")
_BOLD = re.compile(r"\*\*|__")
_CLOSED = re.compile(r"🔒|closed|no longer|expired", re.I)
_AGE = re.compile(r"^\s*(\d+)\s*([mhdw])\s*$", re.I)  # zapply "10m", "3h", "2d"


@lru_cache(maxsize=1)
def load_aggregators(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or (CONFIG_DIR / "aggregators.yaml")
    return list((yaml.safe_load(p.read_text()) or {}).get("aggregators", []))


def aggregator_by_slug(slug: str) -> dict[str, Any] | None:
    for a in load_aggregators():
        if a["slug"] == slug:
            return a
    return None


def _stable_id(url: str) -> str:
    return hashlib.sha1(_norm_url(url).encode()).hexdigest()[:16]


def _norm_url(u: str) -> str:
    u = htmllib.unescape(u.strip())
    u = re.sub(
        r"[?&](utm_[a-z]+|ref|source|src|gh_src|lever-source(?:%5B%5D|\[\])?|utm)=[^&#]*", "", u
    )
    return u.rstrip("?&")


def _clean_cell(s: str) -> str:
    s = _TAGS.sub(" ", s)
    s = _BOLD.sub("", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)  # [text](url) → text
    s = htmllib.unescape(s)
    return re.sub(r"\s+", " ", s).strip(" *_")


def _links(cell: str) -> list[str]:
    out = _HREF.findall(cell) + _MDLINK.findall(cell)
    if not out:
        out = _BARE_URL.findall(cell)
    return [htmllib.unescape(u) for u in out]


def _parse_date(cell: str) -> str | None:
    s = _clean_cell(cell)
    if not s:
        return None
    now = utcnow()
    m = _AGE.match(s)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {
            "m": timedelta(minutes=n),
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
            "w": timedelta(weeks=n),
        }[unit]
        return to_iso(now - delta)
    m = re.match(r"^([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:,?\s*(\d{4}))?$", s)
    if m and m.group(1).lower() in _MONTHS:
        month, day = _MONTHS[m.group(1).lower()], int(m.group(2))
        year = int(m.group(3)) if m.group(3) else now.year
        try:
            dt = now.replace(
                year=year, month=month, day=day, hour=12, minute=0, second=0, microsecond=0
            )
        except ValueError:
            return None
        if not m.group(3) and dt > now + timedelta(days=2):
            dt = dt.replace(year=year - 1)
        return to_iso(dt)
    dt = parse_dt(s)
    return to_iso(dt) if dt else None


class GitHubAggregatorAdapter(BaseAdapter):
    provider: ClassVar[str] = "github"
    incremental_items: ClassVar[int] = 10_000

    async def fetch(
        self,
        spec: SourceSpec,
        *,
        mode: str = "full",
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchRaw:
        agg = aggregator_by_slug(spec.slug) or spec.extra.get("aggregator") or {}
        url = agg.get("url") or spec.careers_url
        raw = FetchRaw(spec=spec, mode="full")
        if not url:
            raw.error = "aggregator has no url"
            raw.complete = False
            return raw
        headers = {}
        import os

        if (tok := os.environ.get("GITHUB_TOKEN")) and "raw.githubusercontent.com" in url:
            headers["Authorization"] = f"Bearer {tok}"
        resp = await self._get(
            url, etag=etag, last_modified=last_modified, headers=headers, follow_redirects=True
        )
        raw.http_status = resp.status
        if resp.not_modified:
            raw.not_modified = True
            return raw
        raw.pages.append(
            FetchedPage(url=url, status=resp.status, body=resp.content, elapsed_ms=resp.elapsed_ms)
        )
        if resp.status != 200:
            raw.error = f"HTTP {resp.status}"
            raw.complete = False
            return raw
        raw.etag = resp.headers.get("etag")
        raw.last_modified = resp.headers.get("last-modified")
        raw.jobs = self.parse_payload(spec, raw.combined_payload())
        return raw

    def parse_page(self, spec: SourceSpec, body: bytes, url: str = "") -> list[RawJob]:
        agg = aggregator_by_slug(spec.slug) or spec.extra.get("aggregator") or {}
        kind = agg.get("kind") or ("simplify_json" if url.endswith(".json") else "markdown_table")
        third_party = agg.get("link_quality") == "third_party"
        if kind == "simplify_json":
            return self._parse_simplify(spec, body)
        return self._parse_markdown(spec, body, third_party=third_party)

    # ---- Simplify structured JSON ---------------------------------------------------------------
    def _parse_simplify(self, spec: SourceSpec, body: bytes) -> list[RawJob]:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return []
        out: list[RawJob] = []
        for x in data:
            if not x.get("active", True) or not x.get("is_visible", True):
                continue
            url = x.get("url")
            if not url:
                continue
            degrees = [d for d in (x.get("degrees") or []) if d]
            adv_only = bool(degrees) and all(d in ("Master's", "PhD", "Doctorate") for d in degrees)
            spons = x.get("sponsorship") or ""
            out.append(
                RawJob(
                    source_job_id=str(x.get("id") or _stable_id(url)),
                    title=(x.get("title") or "").strip(),
                    apply_url=_norm_url(url),
                    canonical_url=_norm_url(url),
                    company_name=(x.get("company_name") or "").strip() or None,
                    locations=[loc for loc in (x.get("locations") or []) if loc],
                    posted_at=x.get("date_posted"),
                    updated_at=x.get("date_updated"),
                    tags=["simplify", (x.get("category") or "").lower().replace(" ", "_")],
                    raw={
                        "category": x.get("category"),
                        "sponsorship": spons,
                        "degrees": degrees,
                        "advanced_degree_only": adv_only,
                        "citizenship_required": "citizenship" in spons.lower(),
                        "company_url": x.get("company_url"),
                        "source": x.get("source"),
                    },
                )
            )
        return out

    # ---- README markdown tables -----------------------------------------------------------------
    def _parse_markdown(self, spec: SourceSpec, body: bytes, *, third_party: bool) -> list[RawJob]:
        text = body.decode("utf-8", errors="replace")
        out: list[RawJob] = []
        cols: dict[str, int] | None = None
        last_company: str | None = None
        for line in text.splitlines():
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 3:
                continue
            low = [c.lower() for c in cells]
            if any("company" in c for c in low) and any(
                ("role" in c) or ("title" in c) or ("position" in c) for c in low
            ):
                cols = {}
                for i, c in enumerate(low):
                    c = _clean_cell(c)
                    if "company" in c:
                        cols["company"] = i
                    elif "role" in c or "title" in c or "position" in c:
                        cols["role"] = i
                    elif "location" in c:
                        cols["location"] = i
                    elif "apply" in c or "link" in c or "application" in c:
                        cols["link"] = i
                    elif "date" in c or "posted" in c or "age" in c:
                        cols["date"] = i
                    elif "work" in c and "model" in c:
                        cols["work_model"] = i
                    elif "visa" in c or "sponsor" in c:
                        cols["visa"] = i
                continue
            if cols is None or set(cells[0]) <= {"-", ":", " "}:
                continue
            try:
                company_cell = cells[cols["company"]]
                role_cell = cells[cols["role"]]
            except (KeyError, IndexError):
                continue
            company = _clean_cell(company_cell)
            if company in ("↳", "", "—"):
                company = last_company or ""
            else:
                last_company = company
            title = _clean_cell(role_cell)
            link_cell = cells[cols["link"]] if "link" in cols and cols["link"] < len(cells) else ""
            if _CLOSED.search(link_cell) or _CLOSED.search(role_cell):
                continue
            links = (
                _links(link_cell)
                or _links(role_cell)
                or _links(company_cell if "link" not in cols else "")
            )
            # prefer the employer link over simplify.jobs / aggregator trackers
            direct = [u for u in links if "simplify.jobs" not in u and "/utm" not in u]
            url = (direct or links or [None])[0]
            if not url or not title or not company:
                continue
            loc_cell = (
                cells[cols["location"]]
                if "location" in cols and cols["location"] < len(cells)
                else ""
            )
            locs = [
                x
                for x in (
                    _clean_cell(p)
                    for p in re.split(r"</?br\s*/?>|\n|;|, (?=[A-Z][a-z]+, [A-Z]{2})", loc_cell)
                )
                if x
            ]
            date_cell = cells[cols["date"]] if "date" in cols and cols["date"] < len(cells) else ""
            work_model = (
                _clean_cell(cells[cols["work_model"]])
                if "work_model" in cols and cols["work_model"] < len(cells)
                else None
            )
            visa = (
                _clean_cell(cells[cols["visa"]])
                if "visa" in cols and cols["visa"] < len(cells)
                else None
            )
            out.append(
                RawJob(
                    source_job_id=_stable_id(url),
                    title=title,
                    apply_url=_norm_url(url),
                    canonical_url=_norm_url(url),
                    company_name=company,
                    locations=locs,
                    remote=True if (work_model and "remote" in work_model.lower()) else None,
                    posted_at=_parse_date(date_cell),
                    tags=[spec.slug.split("/")[0].lower()],
                    raw={"work_model": work_model, "visa": visa, "third_party_link": third_party},
                )
            )
        return self._dedupe(out)

    async def verify(self, spec: SourceSpec, source_job_id: str, url: str) -> LinkVerdict:
        return LinkVerdict(
            status="unverified", method="api", reason="no api verifier"
        )  # → HTTP path

    @classmethod
    def detect(cls, url: str) -> SourceSpec | None:
        return None
