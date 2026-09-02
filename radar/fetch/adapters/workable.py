"""Workable public job widget API (documented: workable.com/developers — "Jobs widget" / v1 accounts).

GET https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true
    → {"name": "...", "jobs": [{title, shortcode, code, employment_type, telecommuting, department,
       url, shortlink, application_url, published_on, created_at, country, city, state, education,
       experience, function, industry, description, requirements, benefits}]}
GET https://apply.workable.com/api/v2/accounts/{slug}/jobs/{shortcode}   → 200 | 404  (verification)
"""

from __future__ import annotations

import json
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
from radar.models import RawJob, SourceSpec

API = "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
VERIFY = "https://apply.workable.com/api/v2/accounts/{slug}/jobs/{code}"


class WorkableAdapter(BaseAdapter):
    provider: ClassVar[str] = "workable"
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
        resp = await self._get(url, etag=etag, last_modified=last_modified, follow_redirects=True)
        raw.http_status = resp.status
        if resp.not_modified:
            raw.not_modified = True
            return raw
        raw.pages.append(
            FetchedPage(url=url, status=resp.status, body=resp.content, elapsed_ms=resp.elapsed_ms)
        )
        if resp.status != 200 or not resp.headers.get("content-type", "").startswith(
            "application/json"
        ):
            raw.error = f"HTTP {resp.status}" if resp.status != 200 else "non-JSON response"
            raw.complete = False
            return raw
        raw.etag = resp.headers.get("etag")
        raw.last_modified = resp.headers.get("last-modified")
        raw.jobs = self.parse_payload(spec, raw.combined_payload())
        return raw

    def parse_page(self, spec: SourceSpec, body: bytes, url: str = "") -> list[RawJob]:
        out: list[RawJob] = []
        jobs = expect_list(body, "jobs", provider="workable", url=url)
        data = json.loads(body)  # the widget also carries the account name next to the list
        for j in jobs:
            code = j.get("shortcode") or j.get("code")
            if not code:
                continue
            locs: list[str] = []
            loc = ", ".join(p for p in (j.get("city"), j.get("state"), j.get("country")) if p)
            if loc:
                locs.append(loc)
            for extra in j.get("locations") or []:
                if isinstance(extra, dict):
                    s = ", ".join(
                        p
                        for p in (extra.get("city"), extra.get("region"), extra.get("country"))
                        if p
                    )
                    if s and s not in locs:
                        locs.append(s)
            html = "\n".join(
                x
                for x in (
                    j.get("description"),
                    ("<h3>Requirements</h3>" + j["requirements"])
                    if j.get("requirements")
                    else None,
                    ("<h3>Benefits</h3>" + j["benefits"]) if j.get("benefits") else None,
                )
                if x
            )
            out.append(
                RawJob(
                    source_job_id=str(code),
                    title=j.get("title") or "",
                    apply_url=j.get("url")
                    or j.get("shortlink")
                    or f"https://apply.workable.com/{spec.slug}/j/{code}/",
                    canonical_url=j.get("shortlink")
                    or j.get("url")
                    or f"https://apply.workable.com/{spec.slug}/j/{code}/",
                    company_name=data.get("name") or spec.company_name,
                    locations=locs,
                    remote=bool(j.get("telecommuting")) or None,
                    department=j.get("department"),
                    team=j.get("function"),
                    employment_type=j.get("employment_type"),
                    posted_at=j.get("published_on") or j.get("created_at"),
                    description_html=html or None,
                    description_md=html_to_md(html),
                    raw={
                        "experience": j.get("experience"),
                        "education": j.get("education"),
                        "industry": j.get("industry"),
                        "application_url": j.get("application_url"),
                    },
                )
            )
        return out

    async def verify(self, spec: SourceSpec, source_job_id: str, url: str) -> LinkVerdict:
        try:
            resp = await self._get(
                VERIFY.format(slug=spec.slug, code=source_job_id), retries=1, follow_redirects=True
            )
        except Exception as e:
            return LinkVerdict(status="unverified", method="api", reason=str(e)[:200])
        if resp.status == 200:
            return LinkVerdict(
                status="live",
                method="api",
                http_status=200,
                final_url=url,
                reason="present in Workable jobs API",
            )
        if resp.status == 404:
            return LinkVerdict(
                status="dead",
                method="api",
                http_status=404,
                reason="Workable API 404 for this shortcode",
            )
        return LinkVerdict(
            status="unverified", method="api", http_status=resp.status, reason=f"HTTP {resp.status}"
        )

    @classmethod
    def detect(cls, url: str) -> SourceSpec | None:
        u = urlparse(url)
        if u.netloc.lower() == "apply.workable.com":
            parts = [p for p in u.path.split("/") if p]
            if parts and parts[0] not in ("api",):
                return SourceSpec(
                    provider="workable", slug=parts[0], company_slug=parts[0], company_name=parts[0]
                )
            m = re.search(r"/accounts/([^/?]+)", u.path)
            if m:
                return SourceSpec(
                    provider="workable",
                    slug=m.group(1),
                    company_slug=m.group(1),
                    company_name=m.group(1),
                )
        m = re.match(r"^([a-z0-9-]+)\.workable\.com$", u.netloc.lower())
        if m and m.group(1) not in ("www", "apply", "jobs"):
            return SourceSpec(
                provider="workable",
                slug=m.group(1),
                company_slug=m.group(1),
                company_name=m.group(1),
            )
        return None
