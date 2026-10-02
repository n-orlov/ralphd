"""PRD v0.30 B.1: the engine process never imports the Claude Agent SDK.

Only the wrapper module, `ralphd.harness.claude_code`, imports
`claude_agent_sdk`; it runs as a child process of its own. Importing the
engine and the CLI must leave the SDK unloaded, and no other source file may
import it.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "ralphd"
WRAPPER = SRC / "harness" / "claude_code.py"

_SDK_IMPORT = re.compile(r"^\s*(import\s+claude_agent_sdk\b|from\s+claude_agent_sdk\b)",
                         re.MULTILINE)


def test_importing_the_engine_and_the_cli_leaves_the_sdk_unloaded():
    code = (
        "import sys\n"
        "import ralphd.engine.loop, ralphd.engine.main, ralphd.engine.api\n"
        "import ralphd.engine.runner, ralphd.harness.normalise, ralphd.cli.main\n"
        "print('claude_agent_sdk' in sys.modules)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, timeout=120, cwd=REPO)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"


def test_only_the_wrapper_module_imports_the_sdk():
    importers = sorted(
        p.relative_to(SRC).as_posix() for p in SRC.rglob("*.py")
        if _SDK_IMPORT.search(p.read_text()))
    assert importers == [WRAPPER.relative_to(SRC).as_posix()]
