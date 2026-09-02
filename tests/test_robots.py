"""robots.txt fails closed (review finding 8): unreachable or refused robots.txt denies; only an
explicit 404/410 (no robots file) or a permitting 200 allows."""

import asyncio

import httpx
import pytest
import respx
from httpx import Response

from radar.fetch.html_detail import Robots
from radar.fetch.http import PoliteClient


def _allowed(url: str) -> bool:
    async def go():
        async with PoliteClient("radar-test (test@example.com)") as c:
            return await Robots(c).allowed(url)

    return asyncio.run(go())


@respx.mock
@pytest.mark.parametrize("status", [403, 401, 429, 500, 503])
def test_http_error_on_robots_denies(status):
    respx.get("https://h.example/robots.txt").mock(return_value=Response(status))
    assert _allowed("https://h.example/jobs/1") is False


@respx.mock
def test_network_failure_on_robots_denies():
    respx.get("https://dns-fail.example/robots.txt").mock(
        side_effect=httpx.ConnectError("name not resolved")
    )
    assert _allowed("https://dns-fail.example/jobs/1") is False
    respx.get("https://slow.example/robots.txt").mock(side_effect=httpx.ReadTimeout("timed out"))
    assert _allowed("https://slow.example/jobs/1") is False


@respx.mock
def test_missing_robots_allows_and_rules_are_honoured():
    respx.get("https://open.example/robots.txt").mock(return_value=Response(404))
    assert _allowed("https://open.example/jobs/1") is True
    respx.get("https://rules.example/robots.txt").mock(
        return_value=Response(200, text="User-agent: *\nDisallow: /jobs/\nAllow: /careers/\n")
    )
    assert _allowed("https://rules.example/jobs/1") is False
    assert _allowed("https://rules.example/careers/1") is True


def test_deny_hosts_never_fetch_robots():
    # no respx route registered: a request would raise; the deny list short-circuits before that
    assert _allowed("https://www.linkedin.com/jobs/view/1") is False
