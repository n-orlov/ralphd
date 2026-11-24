"""v0.39 G: the package version is the one this wave ships, in both places a
build reads it (`pyproject.toml` and `ralphd.__version__`), and the version
`docs/api.md` shows for `GET /version` is the same one."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

import ralphd

pytestmark = [pytest.mark.smoke, pytest.mark.fast]

ROOT = Path(__file__).resolve().parent.parent
RELEASE = "0.39.0"


def test_pyproject_and_the_package_name_the_same_release():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert meta["project"]["version"] == RELEASE
    assert ralphd.__version__ == RELEASE


def test_the_api_doc_shows_the_released_version():
    text = (ROOT / "docs" / "api.md").read_text()
    shown = re.search(r'`GET /version` → `\{"ralphd": "([^"]+)"', text)
    assert shown and shown.group(1) == RELEASE


# v0.39 G: the four documents describe the wave in the present tense.
DOC_NEEDLES = {
    "SPEC.md": ["spend_caps", "spend:<hat>:approach", "spend:total",
                "spend.interrupted", "spend.warning", "spend.exhausted",
                "budget <run> --spend", "`spendCaps`",
                "codex_gateway_not_configured", "CLAUDE_CODE_MIN_VERSION",
                "CODEX_MIN_VERSION"],
    "docs/cli.md": ["spend_caps", "--spend-approach", "spend:<hat>:approach",
                    "interrupted", "of a spend-`exhausted` run",
                    "2.1.312", "0.156.1"],
    "docs/api.md": ["spendCaps", "spend.warning", "spend.exhausted",
                    "spend.interrupted", "spend:total",
                    "codex_gateway_not_configured"],
    "docs/solution-tech-spec.md": [
        "spend_caps", "spend:<hat>:approach", "spend.interrupted",
        "spend.warning", "`spendCaps`", "--spend-approach", "interrupt",
        "codex_gateway_not_configured", "2.1.312", "0.156.1"],
}


@pytest.mark.parametrize("doc", sorted(DOC_NEEDLES))
def test_the_documents_describe_spend_caps_and_the_minimums(doc):
    text = (ROOT / doc).read_text()
    missing = [n for n in DOC_NEEDLES[doc] if n not in text]
    assert not missing, f"{doc} does not mention {missing}"
