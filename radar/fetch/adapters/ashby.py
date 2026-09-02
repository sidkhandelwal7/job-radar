"""Ashby Job Posting API (documented: developers.ashbyhq.com/docs/public-job-posting-api).

  GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
      → {"apiVersion": "1", "jobs": [{id, title, department, team, employmentType, location,
         secondaryLocations, address, publishedAt, isListed, isRemote, workplaceType, jobUrl,
         applyUrl, descriptionHtml, compensation{compensationTiers[{components[...]}]}}]}

Best posted-comp coverage of any ATS (§6d). There is no per-job endpoint, so verification is
membership in a fresh board fetch (cached per adapter instance / sweep).
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

API = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
_EMP = {
    "FullTime": "Full-time",
    "PartTime": "Part-time",
    "Intern": "Internship",
    "Contract": "Contract",
    "Temporary": "Temporary",
}


class AshbyAdapter(BaseAdapter):
    provider: ClassVar[str] = "ashby"
    incremental_items: ClassVar[int] = 10_000

    def __init__(self, client=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(client)
        self._board_cache: dict[str, set[str]] = {}

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
        self._board_cache[spec.slug] = {j.source_job_id for j in raw.jobs}
        return raw

    def parse_page(self, spec: SourceSpec, body: bytes, url: str = "") -> list[RawJob]:
        out: list[RawJob] = []
        for j in expect_list(body, "jobs", provider="ashby", url=url):
            if j.get("isListed") is False:
                continue
            locs: list[str] = []
            addr = (j.get("address") or {}).get("postalAddress") or {}
            primary = j.get("location") or ""
            primary = re.sub(r"\s*\((?:HQ|Headquarters)\)\s*$", "", primary)
            if primary:
                if (
                    addr.get("addressRegion")
                    and addr["addressRegion"] not in primary
                    and addr.get("addressCountry", "").upper() in ("USA", "US", "UNITED STATES")
                ):
                    primary = f"{primary}, {addr['addressRegion']}"
                locs.append(primary)
            for s in j.get("secondaryLocations") or []:
                name = s.get("location") if isinstance(s, dict) else str(s)
                if name and name not in locs:
                    locs.append(name)
            wp = (j.get("workplaceType") or "").lower()
            comp = _comp(j.get("compensation"))
            out.append(
                RawJob(
                    source_job_id=str(j["id"]),
                    title=(j.get("title") or "").strip(),
                    apply_url=j.get("jobUrl")
                    or j.get("applyUrl")
                    or f"https://jobs.ashbyhq.com/{spec.slug}/{j['id']}",
                    canonical_url=j.get("jobUrl")
                    or f"https://jobs.ashbyhq.com/{spec.slug}/{j['id']}",
                    company_name=spec.company_name,
                    locations=locs,
                    remote=bool(j.get("isRemote")) or wp == "remote" or None,
                    department=j.get("department"),
                    team=j.get("team"),
                    employment_type=_EMP.get(
                        j.get("employmentType") or "", j.get("employmentType")
                    ),
                    posted_at=j.get("publishedAt"),
                    description_html=j.get("descriptionHtml"),
                    description_md=html_to_md(j.get("descriptionHtml"))
                    or (j.get("descriptionPlain") or None),
                    comp=comp,
                    raw={
                        "workplaceType": j.get("workplaceType"),
                        "compensationTierSummary": (j.get("compensation") or {}).get(
                            "compensationTierSummary"
                        ),
                        "equity": bool(
                            comp
                            and comp.source == "ashby_posted"
                            and _has_equity(j.get("compensation"))
                        ),
                    },
                )
            )
        return out

    async def verify(self, spec: SourceSpec, source_job_id: str, url: str) -> LinkVerdict:
        ids = self._board_cache.get(spec.slug)
        if ids is None:
            try:
                resp = await self._get(API.format(slug=spec.slug), retries=1)
            except Exception as e:
                return LinkVerdict(status="unverified", method="api", reason=str(e)[:200])
            if resp.status != 200:
                return LinkVerdict(
                    status="unverified",
                    method="api",
                    http_status=resp.status,
                    reason=f"board HTTP {resp.status}",
                )
            jobs = self.parse_page(spec, resp.content)
            ids = {j.source_job_id for j in jobs}
            self._board_cache[spec.slug] = ids
        if source_job_id in ids:
            return LinkVerdict(
                status="live",
                method="api",
                http_status=200,
                final_url=url,
                reason="present in Ashby job board API",
            )
        return LinkVerdict(
            status="dead",
            method="api",
            http_status=200,
            reason="absent from Ashby job board API — closed or unlisted",
        )

    @classmethod
    def detect(cls, url: str) -> SourceSpec | None:
        u = urlparse(url)
        if u.netloc.lower() == "jobs.ashbyhq.com":
            parts = [p for p in u.path.split("/") if p]
            if parts:
                return SourceSpec(
                    provider="ashby", slug=parts[0], company_slug=parts[0], company_name=parts[0]
                )
        m = re.search(r"api\.ashbyhq\.com/posting-api/job-board/([^/?]+)", url)
        if m:
            return SourceSpec(
                provider="ashby", slug=m.group(1), company_slug=m.group(1), company_name=m.group(1)
            )
        return None


def _comp(c: dict[str, Any] | None) -> CompSnapshot | None:
    if not c:
        return None
    best: CompSnapshot | None = None
    for tier in c.get("compensationTiers") or []:
        for comp in tier.get("components") or []:
            if comp.get("compensationType") != "Salary":
                continue
            lo, hi = comp.get("minValue"), comp.get("maxValue")
            if lo is None and hi is None:
                continue
            interval = (comp.get("interval") or "1 YEAR").upper()
            snap = CompSnapshot(
                min=lo,
                max=hi,
                currency=comp.get("currencyCode") or "USD",
                interval="hour"
                if "HOUR" in interval
                else ("month" if "MONTH" in interval else "year"),
                source="ashby_posted",
            )
            if best is None or (snap.max or 0) > (best.max or 0):
                best = snap
    if best is None:
        m = re.search(
            r"\$([\d.]+)K\s*[–-]\s*\$([\d.]+)K",
            c.get("scrapeableCompensationSalarySummary") or c.get("compensationTierSummary") or "",
        )
        if m:
            best = CompSnapshot(
                min=float(m.group(1)) * 1000, max=float(m.group(2)) * 1000, source="ashby_posted"
            )
    return best


def _has_equity(c: dict[str, Any] | None) -> bool:
    if not c:
        return False
    return "equity" in (c.get("compensationTierSummary") or "").lower()
