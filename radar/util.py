"""Small shared helpers with no project dependencies."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from dateutil import parser as dateparser


def utcnow() -> datetime:
    return datetime.now(UTC)


def utcnow_iso() -> str:
    return utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_dt(value: Any) -> datetime | None:
    """Best-effort datetime parse for the many formats ATS APIs emit. Returns aware UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, int | float):
        # epoch seconds or ms
        v = float(value)
        if v > 1e12:
            v /= 1000.0
        try:
            return datetime.fromtimestamp(v, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(value).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{10,13}", s):
        return parse_dt(int(s))
    try:
        dt = dateparser.parse(s)
    except (ValueError, OverflowError, TypeError):
        return None
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def iso_or_none(value: Any) -> str | None:
    return to_iso(parse_dt(value))


def days_ago(iso: str | None) -> float | None:
    dt = parse_dt(iso)
    if dt is None:
        return None
    return (utcnow() - dt).total_seconds() / 86400.0


def ago_human(iso: str | None) -> str:
    dt = parse_dt(iso)
    if dt is None:
        return "never"
    delta = utcnow() - dt
    s = int(delta.total_seconds())
    if s < 0:
        return "just now"
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def content_hash(obj: Any) -> str:
    return sha256_text(stable_json(obj))


def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return re.sub(r"-{2,}", "-", s)


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def business_days_after(start: datetime, n: int) -> datetime:
    d = start
    added = 0
    while added < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d


_WS = re.compile(r"\s+")


def squash_ws(s: str | None) -> str:
    return _WS.sub(" ", s or "").strip()
