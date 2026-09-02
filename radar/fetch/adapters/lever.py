"""Lever Postings API (documented: github.com/lever/postings-api).

  GET https://api.lever.co/v0/postings/{slug}?mode=json          → [posting, …]  (all postings)
  GET https://api.lever.co/v0/postings/{slug}/{id}?mode=json     → posting | 404   (verification)

Returns [] for both unknown slugs and zero openings; the pipeline's consecutive_empty counter and
the Phase 2 re-probe handle the ambiguity.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar
from urllib.parse import urlparse

from radar.fetch.adapters.base import (
    BaseAdapter,
    FetchedPage,
    FetchRaw,
    LinkVerdict,
    expect_list,
    html_to_md,
)
from radar.models import CompSnapshot, RawJob, SourceSpec

API = "https://api.lever.co/v0/postings/{slug}"
_HOSTS = ("jobs.lever.co", "jobs.eu.lever.co")


class LeverAdapter(BaseAdapter):
    provider: ClassVar[str] = "lever"
    incremental_items: ClassVar[int] = 10_000

    async def fetch(
        self,
        spec: SourceSpec,
        *,
        mode: str = "full",
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchRaw:
        url = API.format(slug=spec.slug) + "?mode=json"
        raw = FetchRaw(spec=spec, mode="full")
        resp = await self._get(url, etag=etag, last_modified=last_modified)
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
        out: list[RawJob] = []
        for p in expect_list(body, None, provider="lever", url=url):
            cats = p.get("categories") or {}
            locs = list(cats.get("allLocations") or [])
            if cats.get("location") and cats["location"] not in locs:
                locs.insert(0, cats["location"])
            country = p.get("country")
            if country and locs and all(len(loc) < 40 and "," not in loc for loc in locs):
                locs = [f"{loc}, {country}" for loc in locs]
            html_parts = [p.get("descriptionBody") or p.get("description") or ""]
            for lst in p.get("lists") or []:
                html_parts.append(f"<h3>{lst.get('text', '')}</h3>{lst.get('content', '')}")
            if p.get("additional"):
                html_parts.append(p["additional"])
            html = "\n".join(x for x in html_parts if x)
            sal = p.get("salaryRange") or {}
            comp = None
            if sal.get("min") or sal.get("max"):
                interval = (sal.get("interval") or "per-year-salary").lower()
                comp = CompSnapshot(
                    min=sal.get("min"),
                    max=sal.get("max"),
                    currency=sal.get("currency") or "USD",
                    interval="hour"
                    if "hour" in interval
                    else ("month" if "month" in interval else "year"),
                    source="posted_range",
                )
            wp = (p.get("workplaceType") or "").lower()
            out.append(
                RawJob(
                    source_job_id=str(p["id"]),
                    title=p.get("text") or "",
                    apply_url=p.get("hostedUrl")
                    or p.get("applyUrl")
                    or f"https://jobs.lever.co/{spec.slug}/{p['id']}",
                    canonical_url=p.get("hostedUrl")
                    or f"https://jobs.lever.co/{spec.slug}/{p['id']}",
                    company_name=spec.company_name,
                    locations=locs,
                    remote=True
                    if wp == "remote"
                    else (False if wp in ("onsite", "on-site") else None),
                    department=cats.get("department"),
                    team=cats.get("team"),
                    employment_type=cats.get("commitment"),
                    posted_at=p.get("createdAt"),
                    description_html=html or None,
                    description_md=html_to_md(html),
                    comp=comp,
                    raw={
                        "workplaceType": p.get("workplaceType"),
                        "country": country,
                        "level": cats.get("level"),
                        "opening": (p.get("openingPlain") or "")[:300],
                    },
                )
            )
        return out

    async def verify(self, spec: SourceSpec, source_job_id: str, url: str) -> LinkVerdict:
        try:
            resp = await self._get(
                API.format(slug=spec.slug) + f"/{source_job_id}?mode=json", retries=1
            )
        except Exception as e:
            return LinkVerdict(status="unverified", method="api", reason=str(e)[:200])
        if resp.status == 200:
            return LinkVerdict(
                status="live",
                method="api",
                http_status=200,
                final_url=url,
                reason="present in Lever postings API",
            )
        if resp.status == 404:
            return LinkVerdict(
                status="dead",
                method="api",
                http_status=404,
                reason="Lever API returns 404 for this posting id",
            )
        return LinkVerdict(
            status="unverified", method="api", http_status=resp.status, reason=f"HTTP {resp.status}"
        )

    @classmethod
    def detect(cls, url: str) -> SourceSpec | None:
        u = urlparse(url)
        if u.netloc.lower() in _HOSTS:
            parts = [p for p in u.path.split("/") if p]
            if parts:
                return SourceSpec(
                    provider="lever", slug=parts[0], company_slug=parts[0], company_name=parts[0]
                )
        m = re.search(r"api\.lever\.co/v0/postings/([^/?]+)", url)
        if m:
            return SourceSpec(
                provider="lever", slug=m.group(1), company_slug=m.group(1), company_name=m.group(1)
            )
        return None


def _first(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return None
