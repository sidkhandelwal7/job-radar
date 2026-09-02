"""Provider adapter registry."""

from __future__ import annotations

from radar.fetch.adapters.ashby import AshbyAdapter
from radar.fetch.adapters.base import BaseAdapter
from radar.fetch.adapters.github_aggregators import GitHubAggregatorAdapter
from radar.fetch.adapters.greenhouse import GreenhouseAdapter
from radar.fetch.adapters.lever import LeverAdapter
from radar.fetch.adapters.oracle import OracleAdapter
from radar.fetch.adapters.recruitee import RecruiteeAdapter
from radar.fetch.adapters.smartrecruiters import SmartRecruitersAdapter
from radar.fetch.adapters.workable import WorkableAdapter
from radar.fetch.adapters.workday import WorkdayAdapter
from radar.fetch.http import PoliteClient

ADAPTERS: dict[str, type[BaseAdapter]] = {
    GreenhouseAdapter.provider: GreenhouseAdapter,
    WorkdayAdapter.provider: WorkdayAdapter,
    OracleAdapter.provider: OracleAdapter,
    LeverAdapter.provider: LeverAdapter,
    AshbyAdapter.provider: AshbyAdapter,
    WorkableAdapter.provider: WorkableAdapter,
    SmartRecruitersAdapter.provider: SmartRecruitersAdapter,
    RecruiteeAdapter.provider: RecruiteeAdapter,
    GitHubAggregatorAdapter.provider: GitHubAggregatorAdapter,
}

#: providers whose URLs identify a company board (used by slug discovery); aggregators excluded
DETECTABLE: tuple[type[BaseAdapter], ...] = (
    GreenhouseAdapter,
    LeverAdapter,
    AshbyAdapter,
    WorkdayAdapter,
    OracleAdapter,
    SmartRecruitersAdapter,
    WorkableAdapter,
    RecruiteeAdapter,
)


def register(cls: type[BaseAdapter]) -> type[BaseAdapter]:
    ADAPTERS[cls.provider] = cls
    return cls


def get_adapter(provider: str, client: PoliteClient) -> BaseAdapter:
    try:
        cls = ADAPTERS[provider]
    except KeyError as e:
        raise KeyError(f"no adapter for provider {provider!r}; known: {sorted(ADAPTERS)}") from e
    return cls(client)


def providers() -> list[str]:
    return sorted(ADAPTERS)
