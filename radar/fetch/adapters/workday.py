"""Workday CXS JSON (the API behind every *.myworkdayjobs.com career site).

slug format: "tenant/wdN/site"  e.g. "capitalone/wd12/Capital_One"

  POST https://{tenant}.{wdN}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
       {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
       → {"total": N, "jobPostings": [{title, externalPath, locationsText, postedOn, bulletFields}], "facets": [...]}
  GET  https://{tenant}.{wdN}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{externalPath}
       → {"jobPostingInfo": {title, jobDescription(html), location, additionalLocations?, startDate, timeType, jobReqId, externalUrl, ...}}

The list is newest-first, so an incremental scan reads the first few pages only.
Not officially documented; public, unauthenticated, robots-permitted. See PLAN.md §2.3.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import timedelta
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
from radar.util import to_iso, utcnow

PAGE = 20
WORKDAY_RESULT_CAP = 2000
_REQ_ID = re.compile(r"_([A-Za-z]{0,4}-?\d[\w-]*)$")
_HOST = re.compile(r"^([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com$", re.I)


def _parts(slug: str) -> tuple[str, str, str]:
    tenant, wd, site = slug.split("/", 2)
    return tenant, wd, site


def base_url(slug: str) -> str:
    tenant, wd, site = _parts(slug)
    return f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"


def public_url(slug: str, external_path: str) -> str:
    tenant, wd, site = _parts(slug)
    return f"https://{tenant}.{wd}.myworkdayjobs.com/en-US/{site}{external_path}"


def posted_on_to_date(text: str | None) -> str | None:
    """'Posted Today' / 'Posted Yesterday' / 'Posted 18 Days Ago' / 'Posted 30+ Days Ago' → ISO (approx)."""
    if not text:
        return None
    t = text.lower()
    now = utcnow()
    if "today" in t:
        return to_iso(now)
    if "yesterday" in t:
        return to_iso(now - timedelta(days=1))
    m = re.search(r"(\d+)\+?\s*day", t)
    if m:
        return to_iso(now - timedelta(days=int(m.group(1))))
    return None


class WorkdayAdapter(BaseAdapter):
    provider: ClassVar[str] = "workday"
    incremental_items: ClassVar[int] = 60
    min_interval: ClassVar[float] = 0.25

    async def fetch(
        self,
        spec: SourceSpec,
        *,
        mode: str = "full",
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchRaw:
        raw = FetchRaw(spec=spec, mode=mode)
        url = base_url(spec.slug) + "/jobs"
        search_text = spec.extra.get("search_text", "")
        facets = spec.extra.get("applied_facets", {})
        offset, total = 0, None
        limit_items = self.incremental_items if mode == "incremental" else 10**9
        while True:
            body = {
                "appliedFacets": facets,
                "limit": PAGE,
                "offset": offset,
                "searchText": search_text,
            }
            resp = await self._post(url, json=body, headers={"Content-Type": "application/json"})
            raw.http_status = resp.status
            raw.pages.append(
                FetchedPage(
                    url=f"{url}?offset={offset}",
                    status=resp.status,
                    body=resp.content,
                    elapsed_ms=resp.elapsed_ms,
                )
            )
            if resp.status != 200:
                raw.error = f"HTTP {resp.status} at offset {offset}"
                raw.complete = False
                break
            try:
                data = resp.json()
            except json.JSONDecodeError:
                raw.error = "non-JSON response"
                raw.complete = False
                break
            if total is None:
                total = int(data.get("total") or 0)
            postings = data.get("jobPostings") or []
            offset += PAGE
            if not postings or offset >= total or offset >= limit_items:
                break
        if mode == "incremental" and total is not None and total > limit_items:
            raw.complete = False
        if total is not None and total >= WORKDAY_RESULT_CAP:
            # Workday silently caps search results at 2,000: the list is not the whole board, so it
            # must not drive delist detection (D15). Facet partitioning can lift this later.
            raw.complete = False
            raw.error = (
                raw.error
                or f"result cap hit ({total} ≥ {WORKDAY_RESULT_CAP}); delist detection suppressed"
            )
        raw.jobs = self.parse_payload(spec, raw.combined_payload())
        return raw

    def parse_page(self, spec: SourceSpec, body: bytes, url: str = "") -> list[RawJob]:
        out: list[RawJob] = []
        for p in expect_list(body, "jobPostings", provider="workday", url=url):
            path = p.get("externalPath") or ""
            if not path:
                continue
            jid = _job_id(path, p.get("bulletFields"))
            out.append(
                RawJob(
                    source_job_id=jid,
                    title=p.get("title") or "",
                    apply_url=public_url(spec.slug, path),
                    canonical_url=public_url(spec.slug, path),
                    company_name=spec.company_name,
                    locations=[p["locationsText"]]
                    if p.get("locationsText")
                    and not re.match(r"^\d+ locations?$", p["locationsText"], re.I)
                    else [],
                    posted_at=posted_on_to_date(p.get("postedOn")),
                    detail_needed=True,
                    detail_ref=path,
                    raw={
                        "externalPath": path,
                        "postedOn": p.get("postedOn"),
                        "bulletFields": p.get("bulletFields"),
                        "locationsText": p.get("locationsText"),
                    },
                )
            )
        return out

    async def fetch_details(self, spec: SourceSpec, jobs: list[RawJob]) -> list[FetchedPage]:
        pages: list[FetchedPage] = []
        base = base_url(spec.slug)

        async def one(job: RawJob) -> None:
            if not job.detail_ref:
                return
            url = base + job.detail_ref
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
            else:
                job.raw["detail_error"] = f"HTTP {resp.status}"

        await asyncio.gather(*(one(j) for j in jobs))
        return pages

    def detail_request_url(self, spec: SourceSpec, job: RawJob) -> str | None:
        return base_url(spec.slug) + job.detail_ref if job.detail_ref else None

    def apply_detail(self, job: RawJob, body: bytes) -> None:
        try:
            info = json.loads(body).get("jobPostingInfo") or {}
        except (json.JSONDecodeError, AttributeError):
            return
        if not info:
            return
        desc = info.get("jobDescription")
        job.description_html = desc
        job.description_md = html_to_md(desc)
        job.detail_needed = False
        locs: list[str] = []
        if info.get("location"):
            locs.append(info["location"])
        for extra in info.get("additionalLocations") or []:
            if isinstance(extra, str) and extra not in locs:
                locs.append(extra)
        jrl = info.get("jobRequisitionLocation") or {}
        if not locs and jrl.get("descriptor"):
            locs.append(jrl["descriptor"])
        country = (info.get("country") or {}).get("descriptor")
        if country and locs and country.lower() not in " ".join(locs).lower():
            locs = [f"{loc}, {country}" for loc in locs]
        if locs:
            job.locations = locs
        if info.get("remoteType"):
            job.remote = "remote" in str(info["remoteType"]).lower()
            job.raw["remoteType"] = info["remoteType"]
        job.employment_type = info.get("timeType") or job.employment_type
        if info.get("startDate"):
            job.posted_at = info["startDate"]
        if info.get("externalUrl"):
            job.apply_url = info["externalUrl"]
        if info.get("jobReqId"):
            job.raw["jobReqId"] = info["jobReqId"]
        if info.get("timeLeftToApply"):
            job.raw["timeLeftToApply"] = info["timeLeftToApply"]
        if info.get("endDate"):
            job.raw["endDate"] = info[
                "endDate"
            ]  # rolling posting-window end, NOT an application deadline (D36)
        job.raw["canApply"] = info.get("canApply")
        job.raw["posted"] = info.get("posted")
        job.raw["jobPostingId"] = info.get("jobPostingId")

    async def verify(self, spec: SourceSpec, source_job_id: str, url: str) -> LinkVerdict:
        # Derive the CXS detail path from the public URL: /en-US/{site}/job/... → /job/...
        u = urlparse(url)
        m = re.search(r"/job/.+$", u.path)
        if not m:
            return LinkVerdict(
                status="unverified", method="api", reason="cannot derive detail path"
            )
        api_url = base_url(spec.slug) + m.group(0)
        try:
            resp = await self._get(api_url, retries=1)
        except Exception as e:
            return LinkVerdict(status="unverified", method="api", reason=str(e)[:200])
        if resp.status == 200:
            try:
                info = resp.json().get("jobPostingInfo") or {}
            except json.JSONDecodeError:
                info = {}
            if info and info.get("posted", True) is not False:
                return LinkVerdict(
                    status="live",
                    method="api",
                    http_status=200,
                    final_url=url,
                    reason="Workday CXS returns the posting",
                )
            return LinkVerdict(
                status="dead",
                method="api",
                http_status=200,
                reason="Workday reports posting no longer posted",
            )
        if resp.status in (404, 410):
            return LinkVerdict(
                status="dead",
                method="api",
                http_status=resp.status,
                reason="Workday CXS 404 — req closed",
            )
        return LinkVerdict(
            status="unverified", method="api", http_status=resp.status, reason=f"HTTP {resp.status}"
        )

    @classmethod
    def detect(cls, url: str) -> SourceSpec | None:
        u = urlparse(url)
        m = _HOST.match(u.netloc)
        if not m:
            return None
        tenant, wd = m.group(1).lower(), m.group(2).lower()
        parts = [p for p in u.path.split("/") if p]
        # /en-US/{site}/... or /{site}/...
        site = None
        for i, p in enumerate(parts):
            if re.match(r"^[a-z]{2}-[A-Z]{2}$", p):
                site = parts[i + 1] if i + 1 < len(parts) else None
                break
        if site is None and parts:
            site = parts[0] if parts[0] not in ("wday",) else None
        if not site or site in ("job", "jobs", "details"):
            return None
        slug = f"{tenant}/{wd}/{site}"
        return SourceSpec(provider="workday", slug=slug, company_slug=tenant, company_name=tenant)


def _job_id(path: str, bullet_fields: Any) -> str:
    if (
        isinstance(bullet_fields, list)
        and bullet_fields
        and isinstance(bullet_fields[0], str)
        and re.match(r"^[A-Za-z]{0,4}-?\d", bullet_fields[0])
    ):
        return bullet_fields[0]
    m = _REQ_ID.search(path.split("?")[0])
    if m:
        return m.group(1)
    return path.rsplit("/", 1)[-1]
