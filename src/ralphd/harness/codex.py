"""The codex harness's SDK-free pure helpers (v0.33 A.5).

This module never imports `claude_agent_sdk` or `ralphd.harness.claude_code`
(which imports the SDK at its top): the engine and the `codex` wrapper reach
it without the Claude SDK installed.
"""

from __future__ import annotations

from typing import Any

# A hat's thinking/effort level (pi's vocabulary) to the `model_reasoning_effort`
# value sent to `codex`. Only `low`, `medium` and `high` are ever sent: they
# are the only values the gateway accepts, so `off` and `minimal` map to
# `low` and `xhigh` maps to `high`.
CODEX_EFFORT_TABLE: dict[str, str] = {
    "off": "low",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
}

# What an unset or unmapped level becomes.
DEFAULT_CODEX_EFFORT = "medium"


def effort_options(thinking: str | None, effort: str | None) -> dict[str, Any]:
    """The `codex` config for a bundle's level ("thinking wins", as for pi
    and claude-code): `{"model_reasoning_effort": <low|medium|high>}`. A
    missing or unmapped level falls back to `medium`."""
    level = thinking or effort
    return {"model_reasoning_effort":
            CODEX_EFFORT_TABLE.get(level or "", DEFAULT_CODEX_EFFORT)}
