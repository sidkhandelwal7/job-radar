"""Recruitee public careers API (documented: docs.recruitee.com — Careers site API).

GET https://{slug}.recruitee.com/api/offers/          → {"offers": [{id, slug, title, careers_url, careers_apply_url,
                                                        location, city, state_name, country, locations[], remote, hybrid,
                                                        on_site, employment_type_code, experience_code, education_code,
                                                        department, description, requirements, published_at, close_at,
                                                        salary{min,max,period,currency}, status}]}
GET https://{slug}.recruitee.com/api/offers/{id}      → 200 | 404  (verification)
"""

from __future__ import annotations

import re
from typing import ClassVar
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

API = "https://{slug}.recruitee.com/api/offers/"
_EMP = {
    "fulltime": "Full-time",
    "fulltime_permanent": "Full-time",
    "parttime": "Part-time",
    "internship": "Internship",
    "contract": "Contract",
    "freelance": "Contract",
    "temporary": "Temporary",
    "traineeship": "Internship",
}


class RecruiteeAdapter(BaseAdapter):
    provider: ClassVar[str] = "recruitee"
    incremental_items: ClassVar[int] = 10_000

    async def fetch(
        self,
        spec: SourceSpec,
        *,
        mode: str = "full",
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchRaw:
        url = API.format(slug=spec.slug)
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
        for o in expect_list(body, "offers", provider="recruitee", url=url):
            if o.get("status") not in (None, "published"):
                continue
            locs: list[str] = []
            for loc in o.get("locations") or []:
                if isinstance(loc, dict):
                    s = ", ".join(
                        p for p in (loc.get("city"), loc.get("state"), loc.get("country")) if p
                    )
                    if s and s not in locs:
                        locs.append(s)
            if not locs and o.get("location"):
                locs.append(o["location"])
            sal = o.get("salary") or {}
            comp = None
            if sal.get("min") or sal.get("max"):
                period = (sal.get("period") or "year").lower()
                comp = CompSnapshot(
                    min=sal.get("min"),
                    max=sal.get("max"),
                    currency=sal.get("currency") or "USD",
                    interval="hour"
                    if "hour" in period
                    else ("month" if "month" in period else "year"),
                    source="posted_range",
                )
            html = "\n".join(
                x
                for x in (
                    o.get("description"),
                    ("<h3>Requirements</h3>" + o["requirements"])
                    if o.get("requirements")
                    else None,
                )
                if x
            )
            out.append(
                RawJob(
                    source_job_id=str(o["id"]),
                    title=o.get("title") or "",
                    apply_url=o.get("careers_url")
                    or f"https://{spec.slug}.recruitee.com/o/{o.get('slug')}",
                    canonical_url=o.get("careers_url")
                    or f"https://{spec.slug}.recruitee.com/o/{o.get('slug')}",
                    company_name=o.get("company_name") or spec.company_name,
                    locations=locs,
                    remote=True if o.get("remote") else None,
                    department=o.get("department"),
                    employment_type=_EMP.get(
                        o.get("employment_type_code") or "", o.get("employment_type_code")
                    ),
                    posted_at=o.get("published_at") or o.get("created_at"),
                    application_deadline=o.get("close_at"),
                    description_html=html or None,
                    description_md=html_to_md(html),
                    comp=comp,
                    raw={
                        "experience_code": o.get("experience_code"),
                        "education_code": o.get("education_code"),
                        "hybrid": o.get("hybrid"),
                        "tags": o.get("tags"),
                    },
                )
            )
        return out

    async def verify(self, spec: SourceSpec, source_job_id: str, url: str) -> LinkVerdict:
        try:
            resp = await self._get(API.format(slug=spec.slug) + source_job_id, retries=1)
        except Exception as e:
            return LinkVerdict(status="unverified", method="api", reason=str(e)[:200])
        if resp.status == 200:
            return LinkVerdict(
                status="live",
                method="api",
                http_status=200,
                final_url=url,
                reason="present in Recruitee offers API",
            )
        if resp.status == 404:
            return LinkVerdict(
                status="dead",
                method="api",
                http_status=404,
                reason="Recruitee API 404 for this offer",
            )
        return LinkVerdict(
            status="unverified", method="api", http_status=resp.status, reason=f"HTTP {resp.status}"
        )

    @classmethod
    def detect(cls, url: str) -> SourceSpec | None:
        u = urlparse(url)
        m = re.match(r"^([a-z0-9-]+)\.recruitee\.com$", u.netloc.lower())
        if m and m.group(1) not in ("www", "app", "api", "docs"):
            return SourceSpec(
                provider="recruitee",
                slug=m.group(1),
                company_slug=m.group(1),
                company_name=m.group(1),
            )
        return None
