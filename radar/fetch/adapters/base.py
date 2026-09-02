"""Adapter interface. One adapter per provider; all return RawJob lists and verbatim payloads.

Contract:
  fetch(spec, mode)      → FetchRaw: the verbatim bodies fetched (stored by the pipeline) + parsed RawJobs
  fetch_details(spec, jobs) → fills description/extra fields for jobs with detail_needed=True
  verify(spec, job_id, url) → LinkVerdict using the provider's own API (cheaper and more reliable
                              than HEAD-ing an HTML page)
  detect(url)            → SourceSpec if the URL looks like this provider's board (Phase 2)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, ClassVar

from markdownify import markdownify

from radar.fetch.http import PoliteClient, Response
from radar.models import RawJob, SourceSpec

log = logging.getLogger("radar.fetch")


@dataclass
class FetchedPage:
    url: str
    status: int
    body: bytes
    elapsed_ms: int = 0


@dataclass
class FetchRaw:
    """Everything an adapter fetched for one source in one pass."""

    spec: SourceSpec
    pages: list[FetchedPage] = field(default_factory=list)
    jobs: list[RawJob] = field(default_factory=list)
    mode: str = "full"
    complete: bool = True  # False for incremental scans (not safe for delist detection)
    not_modified: bool = False
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None
    http_status: int | None = None

    def combined_payload(self) -> bytes:
        """Single verbatim document for the raw store: each page body kept byte-for-byte (as text)."""
        doc = {
            "provider": self.spec.provider,
            "slug": self.spec.slug,
            "mode": self.mode,
            "pages": [
                {"url": p.url, "status": p.status, "body": p.body.decode("utf-8", errors="replace")}
                for p in self.pages
            ],
        }
        return json.dumps(doc, ensure_ascii=False).encode("utf-8")

    @property
    def requests_made(self) -> int:
        return len(self.pages)

    @property
    def bytes_downloaded(self) -> int:
        return sum(len(p.body) for p in self.pages)


@dataclass
class LinkVerdict:
    status: str  # live | redirected | dead | unverified
    method: str  # api | http
    http_status: int | None = None
    final_url: str | None = None
    reason: str | None = None


class AdapterError(RuntimeError):
    pass


def expect_list(body: bytes | str, key: str | None, *, provider: str, url: str = "") -> list[Any]:
    """Decode a 200 payload and return the expected list, or raise AdapterError.

    A board that answered 200 with HTML, an error object, or a JSON shape missing the list key is a
    broken adapter or a changed API — never "no jobs". Returning [] there would look like a quiet
    market and, worse, delist every posting on the next full scan.
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        head = (
            body[:80] if isinstance(body, str) else body[:80].decode("utf-8", "replace")
        ).strip()
        raise AdapterError(
            f"{provider}: response is not JSON ({e.__class__.__name__}) at {url or '?'}: {head!r}"
        ) from None
    if key is None:
        if not isinstance(data, list):
            raise AdapterError(
                f"{provider}: expected a JSON list at {url or '?'}, got {type(data).__name__}"
            )
        return data
    if not isinstance(data, dict) or key not in data:
        keys = sorted(data)[:8] if isinstance(data, dict) else type(data).__name__
        raise AdapterError(
            f"{provider}: expected key {key!r} in response at {url or '?'}; got {keys}"
        )
    val = data[key]
    if val is None:
        return []
    if not isinstance(val, list):
        raise AdapterError(
            f"{provider}: {key!r} is a {type(val).__name__}, not a list, at {url or '?'}"
        )
    return val


class BaseAdapter:
    provider: ClassVar[str] = "base"
    #: how many newest items an incremental scan covers (pages × page_size), provider-specific
    incremental_items: ClassVar[int] = 60
    #: seconds between requests to this provider's hosts (politeness)
    min_interval: ClassVar[float] = 0.0

    def __init__(self, client: PoliteClient | None = None) -> None:
        # client may be None for offline replay (parse_payload / apply_detail only)
        self.client = client

    # ---- to implement -------------------------------------------------------------------------
    async def fetch(
        self,
        spec: SourceSpec,
        *,
        mode: str = "full",
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchRaw:
        raise NotImplementedError

    def parse_payload(self, spec: SourceSpec, payload: bytes) -> list[RawJob]:
        """Re-parse a stored combined payload (for `radar rescore` / replays). Default: parse each page."""
        doc = json.loads(payload)
        jobs: list[RawJob] = []
        for page in doc.get("pages", []):
            jobs.extend(self.parse_page(spec, page["body"].encode("utf-8"), page.get("url", "")))
        return self._dedupe(jobs)

    def parse_page(self, spec: SourceSpec, body: bytes, url: str = "") -> list[RawJob]:
        raise NotImplementedError

    async def fetch_details(self, spec: SourceSpec, jobs: list[RawJob]) -> list[FetchedPage]:
        """Fill description for jobs flagged detail_needed. Returns pages fetched (for raw store)."""
        return []

    def apply_detail(self, job: RawJob, body: bytes) -> None:
        """Merge a stored detail body into a RawJob (replay path)."""
        return None

    def detail_request_url(self, spec: SourceSpec, job: RawJob) -> str | None:
        """The exact URL fetch_details() would request for this job (keys stored detail bodies on replay)."""
        return None

    async def verify(self, spec: SourceSpec, source_job_id: str, url: str) -> LinkVerdict:
        return LinkVerdict(status="unverified", method="api", reason="no api verifier")

    @classmethod
    def detect(cls, url: str) -> SourceSpec | None:
        return None

    # ---- helpers ------------------------------------------------------------------------------
    async def _get(self, url: str, **kw: Any) -> Response:
        assert self.client is not None, "adapter constructed without an HTTP client (offline mode)"
        return await self.client.get(url, **kw)

    async def _post(self, url: str, **kw: Any) -> Response:
        assert self.client is not None, "adapter constructed without an HTTP client (offline mode)"
        return await self.client.post(url, **kw)

    @staticmethod
    def _dedupe(jobs: list[RawJob]) -> list[RawJob]:
        seen: set[str] = set()
        out: list[RawJob] = []
        for j in jobs:
            if j.source_job_id in seen:
                continue
            seen.add(j.source_job_id)
            out.append(j)
        return out


_WS_LINES = re.compile(r"\n{3,}")


def html_to_md(html: str | None) -> str | None:
    if not html:
        return None
    try:
        md = markdownify(html, heading_style="ATX", strip=["img", "script", "style"])
    except Exception:
        md = re.sub(r"<[^>]+>", " ", html)
    md = md.replace("\xa0", " ")
    md = _WS_LINES.sub("\n\n", md).strip()
    return md or None


def strip_html(html: str | None) -> str:
    if not html:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()
