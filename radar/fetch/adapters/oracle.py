"""Oracle Recruiting Cloud (Oracle HCM "Candidate Experience") JSON API.
JPMorgan Chase lives here (jpmc.fa.oraclecloud.com), not on Workday — see PLAN.md §2.1.

slug format: "host/siteNumber"  e.g. "jpmc.fa.oraclecloud.com/CX_1001"

  GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?onlyData=true&expand=requisitionList.secondaryLocations
        &finder=findReqs;siteNumber={site},limit=200,offset=0,sortBy=POSTING_DATES_DESC
      → {"items":[{"TotalJobsCount": N, "requisitionList":[{Id, Title, PostedDate, PrimaryLocation, secondaryLocations, JobFamily, ...}]}]}
  GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails
        ?expand=all&onlyData=true&finder=ById;Id="{Id}",siteNumber={site}
      → {"items":[{ExternalDescriptionStr, ExternalQualificationsStr, ExternalResponsibilitiesStr, JobSchedule, ...}]}  (empty items → req closed)
  public: https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{Id}

Page size caps at 200. Newest-first, so incremental = first page.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, ClassVar
from urllib.parse import quote, urlparse

from radar.fetch.adapters.base import (
    BaseAdapter,
    FetchedPage,
    FetchRaw,
    LinkVerdict,
    expect_list,
    html_to_md,
)
from radar.models import RawJob, SourceSpec

PAGE = 200
_HOST = re.compile(r"^[a-z0-9-]+\.fa\.(?:[a-z0-9-]+\.)?oraclecloud\.com$", re.I)


def _parts(slug: str) -> tuple[str, str]:
    host, site = slug.split("/", 1)
    return host, site


def list_url(slug: str, limit: int, offset: int, keyword: str | None = None) -> str:
    host, site = _parts(slug)
    finder = f"findReqs;siteNumber={site},limit={limit},offset={offset},sortBy=POSTING_DATES_DESC"
    if keyword:
        finder += f",keyword={quote(keyword)}"
    return (
        f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        f"?onlyData=true&expand=requisitionList.secondaryLocations&finder={finder}"
    )


def detail_url(slug: str, req_id: str) -> str:
    host, site = _parts(slug)
    return (
        f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
        f"?expand=all&onlyData=true&finder=ById;Id=%22{req_id}%22,siteNumber={site}"
    )


def public_url(slug: str, req_id: str) -> str:
    host, site = _parts(slug)
    return f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{req_id}"


class OracleAdapter(BaseAdapter):
    provider: ClassVar[str] = "oracle"
    incremental_items: ClassVar[int] = 200
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
        keyword = spec.extra.get("keyword")
        offset, total = 0, None
        limit_items = self.incremental_items if mode == "incremental" else 10**9
        while True:
            url = list_url(spec.slug, PAGE, offset, keyword)
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
                items = resp.json().get("items") or []
            except json.JSONDecodeError:
                raw.error = "non-JSON response"
                raw.complete = False
                break
            if not items:
                break
            head = items[0]
            if total is None:
                total = int(head.get("TotalJobsCount") or 0)
            reqs = head.get("requisitionList") or []
            offset += len(reqs)
            if not reqs or offset >= total or offset >= limit_items:
                break
        if mode == "incremental" and total is not None and total > limit_items:
            raw.complete = False
        raw.jobs = self.parse_payload(spec, raw.combined_payload())
        return raw

    def parse_page(self, spec: SourceSpec, body: bytes, url: str = "") -> list[RawJob]:
        out: list[RawJob] = []
        for head in expect_list(body, "items", provider="oracle", url=url):
            for r in head.get("requisitionList") or []:
                rid = str(r.get("Id") or "")
                if not rid:
                    continue
                locs: list[str] = []
                if r.get("PrimaryLocation"):
                    locs.append(r["PrimaryLocation"])
                for s in r.get("secondaryLocations") or []:
                    name = s.get("Name") if isinstance(s, dict) else str(s)
                    if name and name not in locs:
                        locs.append(name)
                wp = (r.get("WorkplaceType") or "").lower()
                out.append(
                    RawJob(
                        source_job_id=rid,
                        title=r.get("Title") or "",
                        apply_url=public_url(spec.slug, rid),
                        canonical_url=public_url(spec.slug, rid),
                        company_name=spec.company_name,
                        locations=locs,
                        remote=True if "remote" in wp else None,
                        department=r.get("JobFamily") or r.get("JobFunction"),
                        team=r.get("BusinessUnit") or r.get("Organization"),
                        employment_type=r.get("JobSchedule") or r.get("WorkerType"),
                        posted_at=r.get("PostedDate"),
                        # PostingEndDate is a rolling posting-window end, not an application deadline (D36)
                        description_md=_short(r.get("ShortDescriptionStr")),
                        detail_needed=True,
                        detail_ref=rid,
                        raw={
                            "JobFamily": r.get("JobFamily"),
                            "JobFunction": r.get("JobFunction"),
                            "WorkplaceType": r.get("WorkplaceType"),
                            "WorkerType": r.get("WorkerType"),
                            "ContractType": r.get("ContractType"),
                            "StudyLevel": r.get("StudyLevel"),
                            "ManagerLevel": r.get("ManagerLevel"),
                            "PrimaryLocationCountry": r.get("PrimaryLocationCountry"),
                            "HotJobFlag": r.get("HotJobFlag"),
                        },
                    )
                )
        return out

    async def fetch_details(self, spec: SourceSpec, jobs: list[RawJob]) -> list[FetchedPage]:
        pages: list[FetchedPage] = []

        async def one(job: RawJob) -> None:
            url = detail_url(spec.slug, job.source_job_id)
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
        return detail_url(spec.slug, job.source_job_id)

    def apply_detail(self, job: RawJob, body: bytes) -> None:
        try:
            items = json.loads(body).get("items") or []
        except (json.JSONDecodeError, AttributeError):
            return
        if not items:
            job.raw["detail_empty"] = True
            return
        d: dict[str, Any] = items[0]
        parts = [
            d.get("ExternalDescriptionStr"),
            ("<h3>Qualifications</h3>" + d["ExternalQualificationsStr"])
            if d.get("ExternalQualificationsStr")
            else None,
            ("<h3>Responsibilities</h3>" + d["ExternalResponsibilitiesStr"])
            if d.get("ExternalResponsibilitiesStr")
            else None,
        ]
        html = "\n".join(p for p in parts if p)
        if html:
            job.description_html = html
            job.description_md = html_to_md(html)
        job.detail_needed = False
        job.employment_type = d.get("JobSchedule") or job.employment_type
        if d.get("ExternalPostedStartDate"):
            job.posted_at = d["ExternalPostedStartDate"]
        if d.get("ExternalPostedEndDate"):
            job.raw["ExternalPostedEndDate"] = d["ExternalPostedEndDate"]
        job.department = d.get("Category") or job.department
        job.team = d.get("BusinessUnit") or d.get("Organization") or job.team
        if d.get("WorkplaceType"):
            job.raw["WorkplaceType"] = d["WorkplaceType"]
            job.remote = "remote" in str(d["WorkplaceType"]).lower() or job.remote
        locs = list(job.locations)
        if d.get("PrimaryLocation") and d["PrimaryLocation"] not in locs:
            locs.insert(0, d["PrimaryLocation"])
        for s in d.get("secondaryLocations") or []:
            name = s.get("Name") if isinstance(s, dict) else str(s)
            if name and name not in locs:
                locs.append(name)
        job.locations = locs
        job.raw["RequisitionId"] = d.get("RequisitionId")
        job.raw["JobLevel"] = d.get("JobLevel")
        job.raw["StudyLevel"] = d.get("StudyLevel") or job.raw.get("StudyLevel")
        job.raw["NumberOfOpenings"] = d.get("NumberOfOpenings")
        job.raw["skills"] = [s.get("Skill") for s in (d.get("skills") or []) if isinstance(s, dict)]

    async def verify(self, spec: SourceSpec, source_job_id: str, url: str) -> LinkVerdict:
        try:
            resp = await self._get(detail_url(spec.slug, source_job_id), retries=1)
        except Exception as e:
            return LinkVerdict(status="unverified", method="api", reason=str(e)[:200])
        if resp.status != 200:
            return LinkVerdict(
                status="unverified",
                method="api",
                http_status=resp.status,
                reason=f"HTTP {resp.status}",
            )
        try:
            items = resp.json().get("items") or []
        except json.JSONDecodeError:
            return LinkVerdict(
                status="unverified", method="api", http_status=200, reason="non-JSON"
            )
        if items:
            return LinkVerdict(
                status="live",
                method="api",
                http_status=200,
                final_url=url,
                reason="Oracle HCM returns the requisition",
            )
        return LinkVerdict(
            status="dead",
            method="api",
            http_status=200,
            reason="Oracle HCM returns no requisition for this id — closed",
        )

    @classmethod
    def detect(cls, url: str) -> SourceSpec | None:
        u = urlparse(url)
        if not _HOST.match(u.netloc):
            return None
        m = re.search(r"/sites/([A-Za-z0-9_]+)", u.path)
        site = m.group(1) if m else None
        if not site:
            m = re.search(r"siteNumber=([A-Za-z0-9_]+)", u.query)
            site = m.group(1) if m else None
        if not site:
            return None
        tenant = u.netloc.split(".")[0]
        return SourceSpec(
            provider="oracle",
            slug=f"{u.netloc.lower()}/{site}",
            company_slug=tenant,
            company_name=tenant,
        )


def _short(s: str | None) -> str | None:
    if not s:
        return None
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip() or None
