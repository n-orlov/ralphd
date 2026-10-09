"""The skills index both agent wrappers hand the model as text.

SDK-free on purpose: `ralphd.harness.codex` reaches it without importing
`claude_agent_sdk` (`ralphd.harness.claude_code` re-exports it).
"""

from __future__ import annotations


def skills_index(skills: list[dict]) -> str:
    if not skills:
        return ""
    lines = [("Skills are available for this task. Each is a directory with a "
              "SKILL.md; when a skill's description fits what you are doing, "
              "read its SKILL.md with the Read tool and follow it.")]
    for s in skills:
        desc = f": {s['description']}" if s.get("description") else ""
        lines.append(f"- {s['name']}{desc} ({s['path']})")
    return "\n".join(lines)
