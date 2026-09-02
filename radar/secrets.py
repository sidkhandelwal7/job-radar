"""Secrets live in ONE place: data/secrets.env (git-ignored, mode 0600), KEY=VALUE per line.

Loaded into the process environment at startup (CLI callback, API startup) so every channel keeps
reading `os.environ` — nothing else in the codebase knows the file exists. Values are never logged,
never echoed, never copied into launchd plists.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from radar.config import get_config

SECRET_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_WEBHOOK_URL",
    "TELEGRAM_WEBHOOK_SECRET",
    "TELEGRAM_DRAIN_TOKEN",
    "IOS_SHORTCUT_WEBHOOK_URL",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASS",
    "NOTIFY_EMAIL_TO",
    "GITHUB_BILLING_TOKEN",
)


def secrets_path() -> Path:
    return get_config().data_dir / "secrets.env"


def _parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_secrets(path: Path | None = None) -> list[str]:
    """Put the file's keys into os.environ (existing environment wins). Returns the keys loaded."""
    path = path or secrets_path()
    if not path.exists():
        return []
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        # refuse to use a world/group-readable secrets file; say so without printing anything else
        raise PermissionError(f"{path} is mode {oct(mode)}; it must be 0600 (chmod 600 {path})")
    loaded = []
    for k, v in _parse(path.read_text()).items():
        if k not in os.environ:
            os.environ[k] = v
        loaded.append(k)
    return loaded


def set_secret(key: str, value: str, path: Path | None = None) -> Path:
    """Write/replace one key. Creates the file 0600 inside a 0700 data dir; never returns the value."""
    path = path or secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _parse(path.read_text()) if path.exists() else {}
    existing[key] = value
    body = (
        "# Job Radar secrets — git-ignored, mode 0600. Managed by `radar secret set KEY`.\n"
        + "".join(f"{k}={v}\n" for k, v in existing.items())
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(body)
    os.chmod(path, 0o600)
    os.environ[key] = value
    return path


def unset_secret(key: str, path: Path | None = None) -> bool:
    path = path or secrets_path()
    if not path.exists():
        return False
    existing = _parse(path.read_text())
    if key not in existing:
        return False
    del existing[key]
    body = (
        "# Job Radar secrets — git-ignored, mode 0600. Managed by `radar secret set KEY`.\n"
        + "".join(f"{k}={v}\n" for k, v in existing.items())
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(body)
    os.environ.pop(key, None)
    return True


def list_keys(path: Path | None = None) -> list[str]:
    path = path or secrets_path()
    return sorted(_parse(path.read_text())) if path.exists() else []
