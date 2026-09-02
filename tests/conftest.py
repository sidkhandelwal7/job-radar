"""Shared fixtures: temp config/DB, recorded payloads."""

from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures" / "recorded"


@pytest.fixture(scope="session", autouse=True)
def _project_root_env() -> None:
    os.environ.setdefault("RADAR_ROOT", str(ROOT))


@pytest.fixture()
def tmp_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A throwaway data dir with the real config files; DB + raw store land in tmp."""
    from radar import config as cfgmod

    cfg_dir = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg_dir)
    if not (cfg_dir / "config.yaml").exists():  # fresh clone: run on the shipped example
        shutil.copy(cfg_dir / "example.config.yaml", cfg_dir / "config.yaml")
    text = (cfg_dir / "config.yaml").read_text()
    text = text.replace("data_dir: data", f"data_dir: {tmp_path / 'data'}")
    text = text.replace("db: data/radar.db", f"db: {tmp_path / 'data' / 'radar.db'}")
    text = text.replace("raw_dir: data/raw", f"raw_dir: {tmp_path / 'data' / 'raw'}")
    text = text.replace(
        "backups_dir: data/backups", f"backups_dir: {tmp_path / 'data' / 'backups'}"
    )
    (cfg_dir / "config.yaml").write_text(text)
    monkeypatch.setenv("RADAR_CONFIG", str(cfg_dir / "config.yaml"))
    cfgmod.reset_config_cache()
    cfg = cfgmod.get_config()
    yield cfg
    cfgmod.reset_config_cache()


@pytest.fixture()
def conn(tmp_project) -> sqlite3.Connection:
    from radar import db
    from radar.fetch.registry import sync_registry

    c = db.connect(tmp_project.db_path)
    db.migrate(c)
    sync_registry(c, tmp_project)
    return c


def load_fixture(rel: str) -> bytes:
    p = FIXTURES / rel
    with gzip.open(p, "rb") as f:
        return f.read()


@pytest.fixture()
def gh_brex_jobs() -> bytes:
    return load_fixture("greenhouse/brex_jobs.json.gz")


@pytest.fixture()
def gh_brex_departments() -> bytes:
    return load_fixture("greenhouse/brex_departments.json.gz")


@pytest.fixture()
def wd_mastercard_page() -> bytes:
    return load_fixture("workday/mastercard_jobs_p0.json.gz")


@pytest.fixture()
def wd_mastercard_detail() -> bytes:
    return load_fixture("workday/mastercard_detail.json.gz")


@pytest.fixture()
def ora_jpmc_page() -> bytes:
    return load_fixture("oracle/jpmc_reqs_p0.json.gz")


@pytest.fixture()
def ora_jpmc_detail() -> bytes:
    return load_fixture("oracle/jpmc_detail.json.gz")
