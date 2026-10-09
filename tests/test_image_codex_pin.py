"""The Codex CLI is pinned in the job image and in the derived-image recipe.

No test here builds an image or touches the network: the recipe is rendered
from the real `container/Dockerfile` text and inspected as a string.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ralphd.cli import image

REPO = Path(__file__).parent.parent
BASE = "ubuntu:24.04"
PIN = "0.156.1"


def real_dockerfile() -> str:
    return (REPO / "container/Dockerfile").read_text()


def test_the_job_dockerfile_pins_codex_next_to_pi():
    text = real_dockerfile()
    assert f"ARG CODEX_VERSION={PIN}\n" in text
    assert '"@openai/codex@${CODEX_VERSION}"' in text
    # the same npm install that installs pi
    install = re.search(r"RUN npm install -g[^\n]*\\\n(?:[^\n]*\\\n)*[^\n]*pi-coding-agent[^\n]*", text)
    assert install and "@openai/codex@${CODEX_VERSION}" in text[install.start():install.end() + 80]


def test_the_image_module_names_the_codex_pin():
    assert image.CODEX_PIN == "CODEX_VERSION"
    assert image.arg_defaults(real_dockerfile())[image.CODEX_PIN] == PIN


def test_the_derived_recipe_installs_the_pinned_codex():
    text = image.render_derived_dockerfile(BASE, real_dockerfile())
    assert f"ARG CODEX_VERSION={PIN}" in text
    assert '"@openai/codex@${CODEX_VERSION}"' in text
    assert text.index("pi-coding-agent@${PI_VERSION}") < text.index("@openai/codex@")


@pytest.mark.parametrize("replacement", [
    "",                              # the ARG line is gone
    "ARG CODEX_VERSION=\n",          # empty
    "ARG CODEX_VERSION=latest\n",    # a keyword, not a version
    "ARG CODEX_VERSION=0.156\n",     # not an exact version
])
def test_a_missing_or_unpinned_codex_version_is_refused(replacement):
    broken = real_dockerfile().replace(f"ARG CODEX_VERSION={PIN}\n", replacement)
    assert broken != real_dockerfile()
    with pytest.raises(image.ImageInputError) as e:
        image.render_derived_dockerfile(BASE, broken)
    assert "CODEX_VERSION" in str(e.value)


def test_a_changed_pin_changes_the_recipe_hash_and_the_tag():
    real = real_dockerfile()
    bumped = real.replace(f"ARG CODEX_VERSION={PIN}", "ARG CODEX_VERSION=0.157.0")
    a = image.render_derived_dockerfile(BASE, real)
    b = image.render_derived_dockerfile(BASE, bumped)
    assert a != b
    same_source = "0" * 64
    ha = image.derived_hash(BASE, same_source, a)
    hb = image.derived_hash(BASE, same_source, b)
    assert ha != hb
    assert image.derived_tag(ha) != image.derived_tag(hb)


def test_the_derived_format_is_past_the_codex_less_recipe():
    assert image.DERIVED_FORMAT == "ralphd-derived-image v2"
    source = Path(image.__file__).read_text()
    comment = source[:source.index("DERIVED_FORMAT =")]
    assert "@openai/codex" in comment.rsplit("\n\n", 1)[-1]


def test_the_real_repo_derives_with_the_codex_pin():
    assert f"ARG CODEX_VERSION={PIN}" in image.derive(REPO, BASE).dockerfile
