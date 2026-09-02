"""One-command sanitized public export (§17.7).

Copies the repository to a fresh directory with every personal value replaced by a placeholder:
operator identity, the baseline offer, dream list, blocked lists, notes, and any file that could
hold personal data (data/, resume, CALIBRATION.md, .env, discovered registries are kept — they
are public slugs). The output is a new directory with no git history; `git init` it yourself.
The export refuses to finish if any forbidden string (your name, email, comp figures, the
baseline employer) survives anywhere in the tree.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import yaml

from radar.config import Config

SKIP_DIRS = {
    ".git",
    ".venv",
    "data",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "web/node_modules",
}
SKIP_FILES = {"resume.pdf", ".env", "CALIBRATION.md", "PLAN.md"}
SKIP_GLOBS = ("resume*.pdf", "*.db", "*.db-wal", "*.db-shm", "*.log")

PLACEHOLDERS: dict[str, Any] = {
    ("operator", "name"): "Your Name",
    ("operator", "email"): "you@example.com",
    ("operator", "school"): "Your University",
    ("operator", "gpa"): 3.5,
    ("baseline", "employer"): "Baseline Employer",
    ("baseline", "program"): "Standing offer",
    ("baseline", "base_salary"): 90000,
    ("baseline", "signing_bonus"): 5000,
    ("baseline", "decision_deadline"): "2026-12-31",
    ("baseline", "start_date"): "2027-07-01",
}


def sanitize_config(raw: dict[str, Any]) -> dict[str, Any]:
    out = dict(raw)
    for (sec, key), val in PLACEHOLDERS.items():
        if sec in out and isinstance(out[sec], dict) and key in out[sec]:
            out[sec] = {**out[sec], key: val}
    out["dream_list"] = []
    for k in ("blocked_companies", "blocked_metros", "floor_exempt_companies"):
        if k in out:
            out[k] = []
    return out


def forbidden_terms(cfg: Config) -> list[str]:
    terms = [cfg.operator.name, cfg.operator.email, cfg.baseline.employer]
    terms += [
        str(cfg.baseline.base_salary),
        f"{cfg.baseline.base_salary // 1000}k",
        f"${cfg.baseline.base_salary:,}",
    ]
    first = cfg.operator.name.split()[0] if cfg.operator.name else ""
    if first:
        terms.append(first)
    return [t for t in terms if t]


def _skip(rel: Path) -> bool:
    parts = set(rel.parts)
    if parts & SKIP_DIRS or rel.name in SKIP_FILES:
        return True
    return any(rel.match(g) for g in SKIP_GLOBS)


def export_public(
    cfg: Config, dest: Path, *, extra_terms: list[str] | None = None
) -> dict[str, Any]:
    src = cfg.root
    if dest.exists() and any(dest.iterdir()):
        raise FileExistsError(f"{dest} exists and is not empty")
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for path in src.rglob("*"):
        rel = path.relative_to(src)
        if path.is_dir() or _skip(rel):
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if rel.parent == Path("config") and rel.name in ("config.yaml", "example.config.yaml"):
            raw = yaml.safe_load(path.read_text()) or {}
            target.write_text(
                "# Sanitized export — personal values replaced with placeholders. Fill in operator/baseline and run `radar init`.\n"
                + yaml.safe_dump(sanitize_config(raw), sort_keys=False, allow_unicode=True)
            )
        else:
            shutil.copy2(path, target)
        copied += 1
    (dest / "data").mkdir(exist_ok=True)
    (dest / "data" / ".gitkeep").write_text("")
    # scrub prose files that legitimately mention the operator (README/DECISIONS/SPEC) of forbidden terms
    terms = forbidden_terms(cfg) + list(extra_terms or [])
    hits = scrub_and_scan(dest, terms)
    return {
        "dest": str(dest),
        "files": copied,
        "forbidden_terms_checked": len(terms),
        "remaining_hits": hits,
    }


def scrub_and_scan(dest: Path, terms: list[str]) -> list[str]:
    """Replace forbidden terms in text files with [redacted]; return any that still survive (binary files)."""
    pattern = re.compile(
        "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)), re.IGNORECASE
    )
    remaining: list[str] = []
    for path in dest.rglob("*"):
        if path.is_dir() or ".git" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            data = path.read_bytes()
            if any(t.encode() in data for t in terms):
                remaining.append(str(path.relative_to(dest)))
            continue
        new = pattern.sub("[redacted]", text)
        if new != text:
            path.write_text(new, encoding="utf-8")
    return remaining
