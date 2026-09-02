"""launchd (macOS) / systemd (Linux) installation with catch-up-on-wake (§4 "the laptop problem").

Why launchd and not cron: cron silently skips windows the machine slept through. A launchd
`StartInterval` job that missed its interval fires once as soon as the machine wakes, and
`StartCalendarInterval` jobs that were missed also run on wake — so the nightly backup and the
digest never go missing, they arrive late with one consolidated summary (`wake_summary`).

Four agents:
  com.jobradar.cycle   every 15 min        radar cycle --quiet      (fetch due → score → notify)
  com.jobradar.serve   KeepAlive           radar serve              (dashboard on 127.0.0.1)
  com.jobradar.nightly 03:30 daily         radar nightly            (backup → snapshot → calibration → health)
  com.jobradar.telegram KeepAlive          radar telegram-listen    (long-poll button taps, applied within a second)

An fcntl lock in data/ stops a long cycle and a wake-triggered cycle from running concurrently.
"""

from __future__ import annotations

import fcntl
import os
import plistlib
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from radar.config import Config

LABELS = (
    "com.jobradar.cycle",
    "com.jobradar.serve",
    "com.jobradar.nightly",
    "com.jobradar.telegram",
)


def agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _radar_bin() -> str:
    # the console script next to the interpreter that is running us (venv-safe)
    cand = Path(sys.executable).with_name("radar")
    return str(cand if cand.exists() else "radar")


def _env(cfg: Config) -> dict[str, str]:
    # launchd agents get a minimal PATH; the LLM CLI (enrichment) and `uv` live in ~/.local/bin
    home = str(Path.home())
    path = ":".join(
        [
            str(Path(sys.executable).parent),
            f"{home}/.local/bin",
            f"{home}/.local/node/bin",
            "/usr/local/bin",
            "/opt/homebrew/bin",
            "/usr/bin",
            "/bin",
        ]
    )
    # secrets are NOT copied here: every radar process loads data/secrets.env itself (0600, git-ignored)
    env = {"PATH": path, "HOME": home, "PYTHONUNBUFFERED": "1"}
    return env


def plists(cfg: Config, *, port: int = 8787, interval_s: int = 900) -> dict[str, dict[str, Any]]:
    logs = cfg.data_dir / "logs"
    radar = _radar_bin()
    env = _env(cfg)
    # ProcessType matters on macOS: "Background" is CPU/IO-throttled whenever anything else is
    # busy — the dashboard answered in 20 s instead of 0.3 s while a cycle ran. The dashboard is
    # Interactive, the cycle Standard (niced), only the nightly disk job is Background.
    base = {"WorkingDirectory": str(cfg.root), "EnvironmentVariables": env}
    return {
        "com.jobradar.cycle": {
            **base,
            "Label": "com.jobradar.cycle",
            "ProgramArguments": [radar, "cycle", "--quiet"],
            "ProcessType": "Standard",
            "StartInterval": interval_s,  # missed intervals fire once on wake → catch-up
            "RunAtLoad": True,
            "StandardOutPath": str(logs / "cycle.log"),
            "StandardErrorPath": str(logs / "cycle.err.log"),
            "LowPriorityIO": True,
            "Nice": 5,
        },
        "com.jobradar.serve": {
            **base,
            "Label": "com.jobradar.serve",
            "ProgramArguments": [radar, "serve", "--port", str(port)],
            "ProcessType": "Interactive",
            "KeepAlive": True,
            "RunAtLoad": True,
            "StandardOutPath": str(logs / "serve.log"),
            "StandardErrorPath": str(logs / "serve.err.log"),
        },
        "com.jobradar.telegram": {
            **base,
            "Label": "com.jobradar.telegram",
            "ProgramArguments": [radar, "telegram-listen"],
            "ProcessType": "Standard",
            "KeepAlive": True,  # long-poll loop; relaunched if it exits, resumes after sleep
            "RunAtLoad": True,
            "StandardOutPath": str(logs / "telegram.log"),
            "StandardErrorPath": str(logs / "telegram.err.log"),
        },
        "com.jobradar.nightly": {
            **base,
            "Label": "com.jobradar.nightly",
            "ProgramArguments": [radar, "nightly"],
            "ProcessType": "Background",
            "StartCalendarInterval": {"Hour": 3, "Minute": 30},  # missed → runs on next wake
            "StandardOutPath": str(logs / "nightly.log"),
            "StandardErrorPath": str(logs / "nightly.err.log"),
            "LowPriorityIO": True,
            "Nice": 10,
        },
    }


def install(
    cfg: Config, *, port: int = 8787, interval_s: int = 900, load: bool = True
) -> list[str]:
    (cfg.data_dir / "logs").mkdir(parents=True, exist_ok=True)
    agents_dir().mkdir(parents=True, exist_ok=True)
    written = []
    for label, pl in plists(cfg, port=port, interval_s=interval_s).items():
        path = agents_dir() / f"{label}.plist"
        if load and path.exists():
            subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
        with open(path, "wb") as f:
            plistlib.dump(pl, f)
        os.chmod(path, 0o600)  # env block may hold the Telegram token
        if load:
            r = subprocess.run(
                ["launchctl", "load", "-w", str(path)], capture_output=True, text=True
            )
            if r.returncode != 0:
                raise RuntimeError(f"launchctl load failed for {label}: {r.stderr.strip()}")
        written.append(str(path))
    return written


def uninstall() -> list[str]:
    removed = []
    for label in LABELS:
        path = agents_dir() / f"{label}.plist"
        if path.exists():
            subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
            path.unlink()
            removed.append(str(path))
    return removed


def status() -> dict[str, str]:
    out: dict[str, str] = {}
    r = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    for label in LABELS:
        line = next((ln for ln in r.stdout.splitlines() if ln.endswith(label)), None)
        if not line:
            out[label] = "not loaded"
        else:
            pid, code, _ = line.split("\t", 2)
            out[label] = f"running pid {pid}" if pid != "-" else f"idle (last exit {code})"
    return out


def systemd_units(cfg: Config, *, port: int = 8787) -> dict[str, str]:
    """Equivalent systemd --user units (Persistent=true gives the same catch-up-on-wake semantics)."""
    radar = _radar_bin()
    wd = str(cfg.root)
    return {
        "jobradar-cycle.service": f"[Unit]\nDescription=Job Radar cycle\n\n[Service]\nType=oneshot\nWorkingDirectory={wd}\nExecStart={radar} cycle --quiet\nNice=5\n",
        "jobradar-cycle.timer": "[Unit]\nDescription=Job Radar every 15 minutes (catch-up on wake)\n\n[Timer]\nOnBootSec=2min\nOnUnitActiveSec=15min\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n",
        "jobradar-serve.service": f"[Unit]\nDescription=Job Radar dashboard\n\n[Service]\nWorkingDirectory={wd}\nExecStart={radar} serve --port {port}\nRestart=always\n\n[Install]\nWantedBy=default.target\n",
        "jobradar-nightly.service": f"[Unit]\nDescription=Job Radar nightly (backup, snapshot, calibration, health)\n\n[Service]\nType=oneshot\nWorkingDirectory={wd}\nExecStart={radar} nightly\n",
        "jobradar-nightly.timer": "[Unit]\nDescription=Job Radar nightly 03:30\n\n[Timer]\nOnCalendar=*-*-* 03:30:00\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n",
    }


@contextmanager
def single_instance(cfg: Config, name: str) -> Iterator[bool]:
    """Yield True if we hold the lock, False if another `name` is already running (no blocking)."""
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = cfg.data_dir / f"{name}.lock"
    with open(lock_path, "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()))
            fh.flush()
            yield True
        finally:
            with suppress(OSError):
                fcntl.flock(fh, fcntl.LOCK_UN)
