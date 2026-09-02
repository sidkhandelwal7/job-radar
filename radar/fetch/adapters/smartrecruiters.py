"""SmartRecruiters Posting API (documented: developers.smartrecruiters.com/reference/postings).

  GET https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset=N
      → {"totalFound", "content": [{id, name, refNumber, releasedDate, location{city,region,country,remote,hybrid},
         department{label}, function{label}, typeOfEmployment{label}, experienceLevel{label}}]}
  GET https://api.smartrecruiters.com/v1/companies/{slug}/postings/{id}
      → {jobAd{sections{companyDescription,jobDescription,qualifications,additionalInformation}}, applyUrl, postingUrl, active}
  public: https://jobs.smartrecruiters.com/{slug}/{id}

List is newest-first (releasedDate desc). Incremental = first page.
"""

from __future__ import annotations

import asyncio
import json
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
from radar.models import RawJob, SourceSpec

API = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
PAGE = 100
_COUNTRY_NAMES = {
    "us": "United States",
    "gb": "United Kingdom",
    "ca": "Canada",
    "in": "India",
    "de": "Germany",
    "sg": "Singapore",
    "ie": "Ireland",
    "pl": "Poland",
    "au": "Australia",
    "fr": "France",
    "nl": "Netherlands",
    "es": "Spain",
    "br": "Brazil",
    "mx": "Mexico",
    "jp": "Japan",
    "il": "Israel",
    "ae": "United Arab Emirates",
    "ch": "Switzerland",
    "se": "Sweden",
    "it": "Italy",
    "pt": "Portugal",
    "cz": "Czech Republic",
    "ro": "Romania",
    "hu": "Hungary",
    "be": "Belgium",
    "dk": "Denmark",
    "no": "Norway",
    "fi": "Finland",
    "at": "Austria",
    "kr": "Korea",
    "cn": "China",
    "tw": "Taiwan",
    "hk": "Hong Kong",
    "ph": "Philippines",
    "ar": "Argentina",
    "co": "Colombia",
    "cl": "Chile",
    "pe": "Peru",
    "za": "South Africa",
    "ua": "Ukraine",
    "lt": "Lithuania",
    "ee": "Estonia",
    "bg": "Bulgaria",
    "rs": "Serbia",
    "gr": "Greece",
    "tr": "Turkey",
    "eg": "Egypt",
    "ke": "Kenya",
    "ng": "Nigeria",
    "vn": "Vietnam",
    "th": "Thailand",
    "id": "Indonesia",
    "my": "Malaysia",
    "nz": "New Zealand",
    "cr": "Costa Rica",
}


def _loc_string(loc: dict[str, Any] | None) -> str | None:
    if not loc:
        return None
    parts = [loc.get("city"), loc.get("region")]
    cc = (loc.get("country") or "").lower()
    if cc:
        parts.append(_COUNTRY_NAMES.get(cc, cc.upper()))
    s = ", ".join(p for p in parts if p)
    if loc.get("remote"):
        s = f"Remote - {s}" if s else "Remote"
    return s or None


class SmartRecruitersAdapter(BaseAdapter):
    provider: ClassVar[str] = "smartrecruiters"
    incremental_items: ClassVar[int] = 100
    min_interval: ClassVar[float] = 0.2

    async def fetch(
        self,
        spec: SourceSpec,
        *,
        mode: str = "full",
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchRaw:
        raw = FetchRaw(spec=spec, mode=mode)
        offset, total = 0, None
        limit_items = self.incremental_items if mode == "incremental" else 10**9
        while True:
            url = API.format(slug=spec.slug) + f"?limit={PAGE}&offset={offset}"
            resp = await self._get(url)
            raw.http_status = resp.status
            raw.pages.append(
                FetchedPage(
                    url=url, status=resp.status, body=resp.content, elapsed_ms=resp.elapsed_ms
                )
            )
            if resp.status != 200:
                raw.error = f"HTTP {resp.status} at offset {offset}"
                raw.complete = False
                break
            try:
                data = resp.json()
            except json.JSONDecodeError:
                raw.error = "non-JSON"
                raw.complete = False
                break
            if total is None:
                total = int(data.get("totalFound") or 0)
            content = data.get("content") or []
            offset += len(content)
            if not content or offset >= total or offset >= limit_items:
                break
        if mode == "incremental" and total is not None and total > limit_items:
            raw.complete = False
        raw.jobs = self.parse_payload(spec, raw.combined_payload())
        return raw

    def parse_page(self, spec: SourceSpec, body: bytes, url: str = "") -> list[RawJob]:
        out: list[RawJob] = []
        for p in expect_list(body, "content", provider="smartrecruiters", url=url):
            pid = str(p.get("id") or "")
            if not pid:
                continue
            loc = p.get("location") or {}
            loc_s = _loc_string(loc)
            out.append(
                RawJob(
                    source_job_id=pid,
                    title=p.get("name") or "",
                    apply_url=f"https://jobs.smartrecruiters.com/{spec.slug}/{pid}",
                    canonical_url=f"https://jobs.smartrecruiters.com/{spec.slug}/{pid}",
                    company_name=(p.get("company") or {}).get("name") or spec.company_name,
                    locations=[loc_s] if loc_s else [],
                    remote=True if loc.get("remote") else None,
                    department=(p.get("department") or {}).get("label"),
                    team=(p.get("function") or {}).get("label"),
                    employment_type=(p.get("typeOfEmployment") or {}).get("label"),
                    posted_at=p.get("releasedDate"),
                    detail_needed=True,
                    detail_ref=pid,
                    raw={
                        "refNumber": p.get("refNumber"),
                        "experienceLevel": (p.get("experienceLevel") or {}).get("label"),
                        "hybrid": loc.get("hybrid"),
                        "industry": (p.get("industry") or {}).get("label"),
                    },
                )
            )
        return out

    def detail_request_url(self, spec: SourceSpec, job: RawJob) -> str | None:
        return API.format(slug=spec.slug) + f"/{job.source_job_id}"

    async def fetch_details(self, spec: SourceSpec, jobs: list[RawJob]) -> list[FetchedPage]:
        pages: list[FetchedPage] = []

        async def one(job: RawJob) -> None:
            url = self.detail_request_url(spec, job)
            try:
                resp = await self._get(url, retries=1)
            except Exception as e:
                job.raw["detail_error"] = str(e)[:200]
                return
            pages.append(
                FetchedPage(
                    url=url, status=resp.status, body=resp.content, elapsed_ms=resp.elapsed_ms
                )
            )
            if resp.status == 200:
                self.apply_detail(job, resp.content)

        await asyncio.gather(*(one(j) for j in jobs))
        return pages

    def apply_detail(self, job: RawJob, body: bytes) -> None:
        try:
            d = json.loads(body)
        except json.JSONDecodeError:
            return
        sections = (d.get("jobAd") or {}).get("sections") or {}
        parts = []
        for key in (
            "jobDescription",
            "qualifications",
            "additionalInformation",
            "companyDescription",
        ):
            sec = sections.get(key) or {}
            if sec.get("text"):
                parts.append(f"<h3>{sec.get('title') or key}</h3>{sec['text']}")
        html = "\n".join(parts)
        if html:
            job.description_html = html
            job.description_md = html_to_md(html)
        job.detail_needed = False
        if d.get("postingUrl"):
            job.canonical_url = d["postingUrl"]
            job.apply_url = d["postingUrl"]
        job.raw["active"] = d.get("active")
        job.raw["experienceLevel"] = (d.get("experienceLevel") or {}).get("label") or job.raw.get(
            "experienceLevel"
        )

    async def verify(self, spec: SourceSpec, source_job_id: str, url: str) -> LinkVerdict:
        try:
            resp = await self._get(API.format(slug=spec.slug) + f"/{source_job_id}", retries=1)
        except Exception as e:
            return LinkVerdict(status="unverified", method="api", reason=str(e)[:200])
        if resp.status == 200:
            try:
                active = resp.json().get("active", True)
            except json.JSONDecodeError:
                active = True
            if active:
                return LinkVerdict(
                    status="live",
                    method="api",
                    http_status=200,
                    final_url=url,
                    reason="present and active in SmartRecruiters API",
                )
            return LinkVerdict(
                status="dead",
                method="api",
                http_status=200,
                reason="SmartRecruiters reports posting inactive",
            )
        if resp.status == 404:
            return LinkVerdict(
                status="dead", method="api", http_status=404, reason="SmartRecruiters API 404"
            )
        return LinkVerdict(
            status="unverified", method="api", http_status=resp.status, reason=f"HTTP {resp.status}"
        )

    @classmethod
    def detect(cls, url: str) -> SourceSpec | None:
        u = urlparse(url)
        if u.netloc.lower() in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com"):
            parts = [p for p in u.path.split("/") if p]
            if parts:
                return SourceSpec(
                    provider="smartrecruiters",
                    slug=parts[0],
                    company_slug=parts[0].lower(),
                    company_name=parts[0],
                )
        m = re.search(r"api\.smartrecruiters\.com/v1/companies/([^/?]+)", url)
        if m:
            return SourceSpec(
                provider="smartrecruiters",
                slug=m.group(1),
                company_slug=m.group(1).lower(),
                company_name=m.group(1),
            )
        return None
