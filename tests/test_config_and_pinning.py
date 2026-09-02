"""§18: nothing recurring may run on a premium-tier model. Config assertion + codebase grep."""

import re
from pathlib import Path

import pytest

from radar.config import ModelPinningError, assert_model_allowed_for_scheduled_work, get_config

ROOT = Path(__file__).resolve().parent.parent


def test_weights_sum_and_models_pinned():
    cfg = get_config()
    assert abs(sum(cfg.weights.as_dict().values()) - 1.0) < 1e-9
    assert set(cfg.llm.models().values()) <= {"sonnet", "haiku"}


@pytest.mark.parametrize("bad", ["fable", "claude-fable-5", "opus", "claude-opus-5", "", "mythos"])
def test_forbidden_models_fail_loudly(bad):
    with pytest.raises(ModelPinningError):
        assert_model_allowed_for_scheduled_work(bad)


def test_every_claude_p_invocation_pins_a_model():
    """Every real `claude -p` invocation — shell lines in workflows/scripts/plists and subprocess argv
    lists in Python — must carry an explicit --model. Prose/log mentions don't count."""
    offenders = []
    shell_files = [p for ext in ("*.yml", "*.yaml", "*.sh", "*.plist") for p in ROOT.rglob(ext)]
    for path in shell_files:
        if ".venv" in path.parts or "node_modules" in path.parts:
            continue
        for line in path.read_text(errors="ignore").splitlines():
            if re.search(r"\bclaude\s+(-p|--print)\b", line) and "--model" not in line:
                offenders.append(f"{path.relative_to(ROOT)}: {line.strip()[:100]}")
    argv_list = re.compile(r"\[[^\]]*?\"-p\"[^\]]*?\]", re.S)
    for path in ROOT.rglob("*.py"):
        if ".venv" in path.parts or "node_modules" in path.parts:
            continue
        text = path.read_text(errors="ignore")
        for m in argv_list.finditer(text):
            block = m.group(0)
            if ("claude" in block or "binary" in block) and '"--model"' not in block:
                offenders.append(
                    f"{path.relative_to(ROOT)}: argv list without --model: {block[:80]}"
                )
        for line in text.splitlines():
            if (
                re.search(r"(subprocess\.|os\.system|check_output|Popen)", line)
                and re.search(r"claude\s+(-p|--print)", line)
                and "--model" not in line
            ):
                offenders.append(f"{path.relative_to(ROOT)}: {line.strip()[:100]}")
    assert not offenders, "unpinned claude -p invocations:\n" + "\n".join(offenders)


def test_llm_wrapper_pins_model_and_refuses_api_key(tmp_project, monkeypatch):
    from radar.enrich.llm import ClaudeHeadless, LLMUnavailable

    c = ClaudeHeadless(tmp_project, task="classifier", max_calls=1)
    assert c.model == "haiku"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("RADAR_ALLOW_API_KEY", raising=False)
    with pytest.raises(LLMUnavailable):
        c.ask("hello", {"type": "object"})
    # disabled → None, never a subprocess
    tmp_project.llm.enabled = False
    assert ClaudeHeadless(tmp_project, task="enrichment").ask("hello") is None


def test_git_ignores_private_data():
    gi = (ROOT / ".gitignore").read_text()
    assert "data/" in gi and "*.db" in gi
