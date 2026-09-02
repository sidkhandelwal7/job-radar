"""Shared Pydantic models crossing module boundaries."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SourceSpec(BaseModel):
    """One ATS board (or aggregator feed) to poll."""

    provider: str
    slug: str  # provider-specific locator. workday: "tenant/wdN/site"; oracle: "host/siteNumber"
    company_slug: str  # registry key
    company_name: str
    careers_url: str | None = None
    cadence: str = "hourly"
    extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.slug}"


class CompSnapshot(BaseModel):
    min: float | None = None
    max: float | None = None
    currency: str | None = "USD"
    interval: str | None = "year"  # year | month | hour
    source: str = "posted_range"


class RawJob(BaseModel):
    """Provider-agnostic job record produced by an adapter's parse(). Still un-normalized:
    locations are raw strings, description is HTML or markdown, dates are whatever the API gave."""

    source_job_id: str
    title: str
    apply_url: str
    canonical_url: str | None = None
    company_name: str | None = None  # aggregator feeds carry their own company names
    locations: list[str] = Field(default_factory=list)
    remote: bool | None = None
    department: str | None = None
    team: str | None = None
    employment_type: str | None = None  # raw hint: "Full time", "Intern", "Contract", ...
    posted_at: Any = None
    updated_at: Any = None
    start_date: Any = None
    application_deadline: Any = None
    description_html: str | None = None
    description_md: str | None = None
    comp: CompSnapshot | None = None
    tags: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)
    detail_needed: bool = False  # provider needs a second request for description
    detail_ref: str | None = None  # opaque pointer the adapter uses to fetch detail

    def raw_hash(self) -> str:
        """Hash of the list-level fields (description only when the provider ships it inline),
        used to skip re-parsing unchanged jobs on full scans."""
        from radar.util import content_hash

        return content_hash(
            {
                "t": self.title,
                "u": self.apply_url,
                "c": self.canonical_url,
                "n": self.company_name,
                "l": self.locations,
                "r": self.remote,
                "d": self.department,
                "e": self.employment_type,
                "p": str(self.posted_at),
                "up": str(self.updated_at),
                "dl": str(self.application_deadline),
                "desc": None
                if self.detail_needed
                else (self.description_md or self.description_html or "")[:20000],
                "comp": self.comp.model_dump() if self.comp else None,
                "tags": self.tags,
            }
        )


class FetchResult(BaseModel):
    """Outcome of one adapter fetch of one source."""

    spec: SourceSpec
    ok: bool
    http_status: int | None = None
    not_modified: bool = False
    payload_sha256: str | None = None
    payload_ref: int | None = None
    jobs: list[RawJob] = Field(default_factory=list)
    error: str | None = None
    requests_made: int = 0
    bytes_downloaded: int = 0
    etag: str | None = None
    last_modified: str | None = None
    elapsed_ms: int = 0
