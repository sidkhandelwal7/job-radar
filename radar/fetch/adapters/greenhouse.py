"""Greenhouse Job Board API (documented, public).
https://developers.greenhouse.io/job-board.html

  GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
  GET https://boards-api.greenhouse.io/v1/boards/{slug}/departments
  GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{id}      ← used for link verification
"""

from __future__ import annotations

import html as htmllib
import json
import re
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse

from radar.fetch.adapters.base import (
    BaseAdapter,
    FetchedPage,
    FetchRaw,
    LinkVerdict,
    expect_list,
    html_to_md,
)
from radar.models import CompSnapshot, RawJob, SourceSpec

API = "https://boards-api.greenhouse.io/v1/boards/{slug}"
_BOARD_HOSTS = (
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "boards.eu.greenhouse.io",
    "job-boards.eu.greenhouse.io",
)
_EMBED = re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?(?:.*&)?for=([a-z0-9_-]+)", re.I)


class GreenhouseAdapter(BaseAdapter):
    provider: ClassVar[str] = "greenhouse"
    incremental_items: ClassVar[int] = (
        10_000  # single request returns everything; no incremental mode
    )

    async def fetch(
        self,
        spec: SourceSpec,
        *,
        mode: str = "full",
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchRaw:
        url = API.format(slug=spec.slug) + "/jobs?content=true"
        raw = FetchRaw(spec=spec, mode="full")
        resp = await self._get(url, etag=etag, last_modified=last_modified)
        raw.http_status = resp.status
        if resp.not_modified:
            raw.not_modified = True
            return raw
        raw.pages.append(
            FetchedPage(url=url, status=resp.status, body=resp.content, elapsed_ms=resp.elapsed_ms)
        )
        if resp.status == 404:
            raw.error = "board not found (404)"
            raw.complete = False
            return raw
        if resp.status != 200:
            raw.error = f"HTTP {resp.status}"
            raw.complete = False
            return raw
        raw.etag = resp.headers.get("etag")
        raw.last_modified = resp.headers.get("last-modified")
        # Departments: cheap, optional — used only to enrich `department` when jobs lack it.
        try:
            dresp = await self._get(API.format(slug=spec.slug) + "/departments")
            if dresp.status == 200:
                raw.pages.append(
                    FetchedPage(
                        url=dresp.url, status=200, body=dresp.content, elapsed_ms=dresp.elapsed_ms
                    )
                )
        except Exception as e:
            raw.error = f"departments fetch failed: {e}"
        raw.jobs = self.parse_payload(spec, raw.combined_payload())
        return raw

    def parse_payload(self, spec: SourceSpec, payload: bytes) -> list[RawJob]:
        doc = json.loads(payload)
        jobs: list[RawJob] = []
        dept_map: dict[int, str] = {}
        for page in doc.get("pages", []):
            if "/departments" in page.get("url", ""):
                dept_map = self._dept_map(page["body"])
        for page in doc.get("pages", []):
            if "/departments" in page.get("url", ""):
                continue
            jobs.extend(self.parse_page(spec, page["body"].encode("utf-8"), page.get("url", "")))
        if dept_map:
            for j in jobs:
                if not j.department:
                    dept_id = j.raw.get("_dept_id")
                    if dept_id in dept_map:
                        j.department = dept_map[dept_id]
        return self._dedupe(jobs)

    @staticmethod
    def _dept_map(body: str) -> dict[int, str]:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return {}
        out: dict[int, str] = {}
        for d in data.get("departments", []):
            for j in d.get("jobs", []) or []:
                out[j["id"]] = d["name"]
        return out

    def parse_page(self, spec: SourceSpec, body: bytes, url: str = "") -> list[RawJob]:
        jobs: list[RawJob] = []
        for j in expect_list(body, "jobs", provider="greenhouse", url=url):
            jobs.append(self._to_rawjob(spec, j))
        return jobs

    @staticmethod
    def _to_rawjob(spec: SourceSpec, j: dict[str, Any]) -> RawJob:
        jid = str(j["id"])
        loc_names: list[str] = []
        if j.get("location", {}).get("name"):
            loc_names.append(j["location"]["name"])
        for o in j.get("offices", []) or []:
            name = o.get("location") or o.get("name")
            if name and name not in loc_names:
                loc_names.append(name)
        depts = j.get("departments") or []
        department = depts[0]["name"] if depts else None
        content = j.get("content")
        if content:
            content = htmllib.unescape(content)
        meta = {m.get("name"): m.get("value") for m in (j.get("metadata") or []) if m.get("name")}
        emp = None
        for k, v in meta.items():
            if isinstance(v, str) and re.search(r"employment|job type|worker type", k or "", re.I):
                emp = v
        comp = _comp_from_metadata(meta)
        canonical = f"https://job-boards.greenhouse.io/{spec.slug}/jobs/{jid}"
        return RawJob(
            source_job_id=jid,
            title=j.get("title") or "",
            apply_url=j.get("absolute_url") or canonical,
            canonical_url=canonical,
            company_name=j.get("company_name") or spec.company_name,
            locations=loc_names,
            department=department,
            employment_type=emp,
            posted_at=j.get("first_published") or j.get("updated_at"),
            updated_at=j.get("updated_at"),
            application_deadline=j.get("application_deadline"),
            description_html=content,
            description_md=html_to_md(content),
            comp=comp,
            raw={
                "requisition_id": j.get("requisition_id"),
                "internal_job_id": j.get("internal_job_id"),
                "metadata": meta,
                "_dept_id": (depts[0]["id"] if depts else None),
                "offices": [o.get("name") for o in (j.get("offices") or [])],
            },
        )

    async def verify(self, spec: SourceSpec, source_job_id: str, url: str) -> LinkVerdict:
        api_url = API.format(slug=spec.slug) + f"/jobs/{source_job_id}"
        try:
            resp = await self._get(api_url, retries=1)
        except Exception as e:
            return LinkVerdict(status="unverified", method="api", reason=str(e)[:200])
        if resp.status == 200:
            return LinkVerdict(
                status="live",
                method="api",
                http_status=200,
                final_url=url,
                reason="present in Greenhouse board API",
            )
        if resp.status == 404:
            return LinkVerdict(
                status="dead",
                method="api",
                http_status=404,
                reason="Greenhouse API returns 404 for this job id",
            )
        return LinkVerdict(
            status="unverified", method="api", http_status=resp.status, reason=f"HTTP {resp.status}"
        )

    @classmethod
    def detect(cls, url: str) -> SourceSpec | None:
        u = urlparse(url)
        host = u.netloc.lower()
        if host in _BOARD_HOSTS:
            parts = [p for p in u.path.split("/") if p]
            if parts:
                slug = parts[0]
                return SourceSpec(
                    provider="greenhouse", slug=slug, company_slug=slug, company_name=slug
                )
        if host == "boards-api.greenhouse.io":
            m = re.search(r"/boards/([^/]+)", u.path)
            if m:
                return SourceSpec(
                    provider="greenhouse",
                    slug=m.group(1),
                    company_slug=m.group(1),
                    company_name=m.group(1),
                )
        m = _EMBED.search(url)
        if m:
            return SourceSpec(
                provider="greenhouse",
                slug=m.group(1),
                company_slug=m.group(1),
                company_name=m.group(1),
            )
        qs = parse_qs(u.query)
        if "gh_jid" in qs:
            return None  # company-hosted page; slug unknown from URL alone
        return None


def _comp_from_metadata(meta: dict[str, Any]) -> CompSnapshot | None:
    lo = hi = None
    for k, v in meta.items():
        if v is None:
            continue
        kl = (k or "").lower()
        if "salary" in kl or "compensation" in kl or "pay" in kl:
            if isinstance(v, dict):
                lo = lo or _num(v.get("min_value") or v.get("min"))
                hi = hi or _num(v.get("max_value") or v.get("max"))
            elif isinstance(v, int | float):
                if "min" in kl:
                    lo = float(v)
                elif "max" in kl:
                    hi = float(v)
    if lo or hi:
        return CompSnapshot(min=lo, max=hi, source="posted_range")
    return None


def _num(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
