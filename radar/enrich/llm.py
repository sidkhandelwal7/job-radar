"""All model-dependent work goes through the vendor CLI in headless mode on a subscription login (§18).

Rules enforced here, not by convention:
  * every invocation pins --model explicitly (config.llm.*; only allow-listed scheduled models pass validation)
  * refuses to run if ANTHROPIC_API_KEY is set — that would bill metered API usage; `--bare` is NOT
    used for the same reason (it restricts auth to API keys; see DECISIONS D27)
  * every response is cached by sha256(model + prompt + schema); a reposted job is never re-analyzed
  * calls are counted per resolved model id and surfaced in `radar health` / the runs table
  * `llm.enabled: false` makes every call return None so scoring degrades to deterministic-only
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from radar.config import Config, assert_model_allowed_for_scheduled_work

log = logging.getLogger("radar.llm")


class LLMUnavailable(RuntimeError):
    pass


@dataclass
class LLMAccounting:
    calls: int = 0
    cache_hits: int = 0
    failures: int = 0
    by_model: dict[str, int] = field(default_factory=dict)
    nominal_cost_usd: float = 0.0
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "failures": self.failures,
            "by_model": dict(self.by_model),
            "nominal_cost_usd": round(self.nominal_cost_usd, 4),
            "seconds": round(self.seconds, 1),
        }


ACCOUNTING = LLMAccounting()


def reset_accounting() -> LLMAccounting:
    global ACCOUNTING
    ACCOUNTING = LLMAccounting()
    return ACCOUNTING


class ClaudeHeadless:
    def __init__(self, cfg: Config, *, task: str, max_calls: int | None = None) -> None:
        self.cfg = cfg
        self.task = task
        self.model = {
            "classifier": cfg.llm.classifier_model,
            "enrichment": cfg.llm.enrichment_model,
            "drafting": cfg.llm.drafting_model,
        }[task]
        assert_model_allowed_for_scheduled_work(self.model)
        self.cache_dir = cfg._abs(cfg.llm.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_calls = max_calls if max_calls is not None else cfg.llm.max_calls_per_run
        self.calls_made = 0
        self.binary = shutil.which("claude")

    @property
    def available(self) -> bool:
        return bool(self.cfg.llm.enabled and self.binary)

    def _key(self, prompt: str, schema: dict[str, Any] | None) -> str:
        h = hashlib.sha256()
        h.update(self.model.encode())
        h.update(b"\x00")
        h.update(prompt.encode("utf-8"))
        h.update(b"\x00")
        h.update(json.dumps(schema or {}, sort_keys=True).encode())
        return h.hexdigest()

    def ask(
        self, prompt: str, schema: dict[str, Any] | None = None, *, timeout: int = 180
    ) -> dict[str, Any] | None:
        """Return structured output (dict) or None (disabled, budget exhausted, or failed)."""
        if not self.cfg.llm.enabled:
            return None
        key = self._key(prompt, schema)
        cpath = self.cache_dir / f"{key}.json"
        if cpath.exists():
            ACCOUNTING.cache_hits += 1
            try:
                return json.loads(cpath.read_text())["output"]
            except (json.JSONDecodeError, KeyError):
                pass
        if not self.binary:
            raise LLMUnavailable("`claude` CLI not found on PATH")
        if os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("RADAR_ALLOW_API_KEY"):
            raise LLMUnavailable(
                "ANTHROPIC_API_KEY is set — refusing to run the headless CLI because it would bill metered API usage (§18). Unset it, or set RADAR_ALLOW_API_KEY=1 if you really mean it."
            )
        if self.calls_made >= self.max_calls:
            return None
        cmd = [
            self.binary,
            "-p",
            "--model",
            self.model,
            "--output-format",
            "json",
            "--no-session-persistence",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--permission-mode",
            "plan",
            "--tools",
            "",
        ]
        if schema:
            cmd += ["--json-schema", json.dumps(schema)]
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env={
                    k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SIMPLE"
                },  # SIMPLE=1 would disable OAuth (D27)
            )
        except subprocess.TimeoutExpired:
            ACCOUNTING.failures += 1
            log.warning("claude -p timed out after %ss", timeout)
            return None
        elapsed = time.monotonic() - t0
        self.calls_made += 1
        ACCOUNTING.calls += 1
        ACCOUNTING.seconds += elapsed
        try:
            env = json.loads(proc.stdout)
        except json.JSONDecodeError:
            ACCOUNTING.failures += 1
            log.warning(
                "claude -p returned non-JSON (rc=%s): %s",
                proc.returncode,
                (proc.stderr or proc.stdout)[:300],
            )
            return None
        resolved = ",".join(sorted((env.get("modelUsage") or {}).keys())) or self.model
        for m in resolved.split(","):
            if "opus" in m or "fable" in m or "mythos" in m:
                raise LLMUnavailable(
                    f"resolved model {m!r} is forbidden for scheduled work (§18) — check `claude` defaults"
                )
            ACCOUNTING.by_model[m] = ACCOUNTING.by_model.get(m, 0) + 1
        ACCOUNTING.nominal_cost_usd += float(env.get("total_cost_usd") or 0)
        if env.get("is_error"):
            ACCOUNTING.failures += 1
            log.warning("claude -p error: %s", str(env.get("result"))[:300])
            return None
        out = env.get("structured_output")
        if out is None:
            try:
                out = json.loads(env.get("result") or "")
            except json.JSONDecodeError:
                ACCOUNTING.failures += 1
                return None
        cpath.write_text(
            json.dumps(
                {
                    "model": self.model,
                    "resolved": resolved,
                    "task": self.task,
                    "prompt_sha": key,
                    "output": out,
                    "cost_usd": env.get("total_cost_usd"),
                    "duration_ms": env.get("duration_ms"),
                }
            )
        )
        return out


def cached_only(cfg: Config, task: str) -> ClaudeHeadless:
    """A client that only ever serves from cache (max_calls=0) — for replays and tests."""
    return ClaudeHeadless(cfg, task=task, max_calls=0)
