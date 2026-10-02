"""Unit-level closure test for task 004: `provider_keys()` and
`check_hat_routes()` (`ralphd.engine.llm`) must not be able to rebuild a
home they were not given.

Before this task, both took `home: Path | None = None` and defaulted a
missing `home` to the real ambient `$HOME` (`_home()`) -- exactly the
silent-fallback shape `place_skills()` had before task 002 fixed it (see
`tests/test_skills_placement_requires_home.py`, which this module mirrors).
This module tests the fix directly, at the function boundary, with no
child process involved: both functions now take `home` as a required
keyword-only argument (a caller that omits it gets a `TypeError` before
any filesystem read happens, since Python binds arguments before the
function body ever runs) and, when given an explicit `home`, never
consult `$HOME` at all -- proven here by planting a *poisoned* ambient
`$HOME` whose own `models.json` declares an unrelated provider, then
showing `check_hat_routes` routes cleanly against the explicit `home`'s
own (compatible) `models.json` instead of tripping on the ambient one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ralphd.engine import llm
from ralphd.engine.config import MODEL_HATS


def _write_models_json(home: Path, providers: dict) -> None:
    agent_dir = home / ".pi" / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "models.json").write_text(
        json.dumps({"providers": providers}))


class _FakeCfg:
    """Minimal stand-in for `JobConfig` -- `check_hat_routes` only ever
    touches `model_for()`, `harness_for()` and `model_tiers`."""

    def __init__(self, refs: dict[str, str | None]):
        self._refs = refs
        self.model_tiers: dict = {}

    def model_for(self, hat: str) -> str | None:
        return self._refs.get(hat)

    def harness_for(self, hat: str) -> str:
        return "pi"


def _refs_naming(hat: str, ref: str) -> dict[str, str | None]:
    return {h: (ref if h == hat else None) for h in MODEL_HATS}


def test_provider_keys_without_home_raises_typeerror(monkeypatch, tmp_path):
    fake_home = tmp_path / "poisoned-home"
    _write_models_json(fake_home, {"unrelated-provider": {}})
    monkeypatch.setenv("HOME", str(fake_home))

    with pytest.raises(TypeError):
        llm.provider_keys()  # no home= at all


def test_check_hat_routes_without_home_raises_typeerror(monkeypatch, tmp_path):
    fake_home = tmp_path / "poisoned-home2"
    _write_models_json(fake_home, {"unrelated-provider": {}})
    monkeypatch.setenv("HOME", str(fake_home))

    cfg = _FakeCfg(_refs_naming("verify", "unrelated-provider/some-model"))

    with pytest.raises(TypeError):
        llm.check_hat_routes(cfg)  # no home= at all


def test_check_hat_routes_never_consults_poisoned_ambient_home(
    monkeypatch, tmp_path
):
    """The ambient `$HOME` declares only `unrelated-provider` -- a
    provider `cfg`'s own ref never names. The *explicit* `home` passed in
    declares `good-provider`, which `cfg`'s ref DOES name. If
    `check_hat_routes` ever fell back to (or merely consulted) the
    ambient `$HOME`, it would see only `unrelated-provider` and raise
    `UnroutableHatModel` naming it as the sole available provider. It must
    not: the explicit `home` alone decides, so this must pass clean."""
    poisoned_home = tmp_path / "poisoned-home3"
    _write_models_json(poisoned_home, {"unrelated-provider": {}})
    monkeypatch.setenv("HOME", str(poisoned_home))

    real_home = tmp_path / "real-home"
    real_home.mkdir()
    _write_models_json(real_home, {"good-provider": {}})

    cfg = _FakeCfg(_refs_naming("verify", "good-provider/some-model"))

    llm.check_hat_routes(cfg, home=real_home)  # must not raise


def test_provider_keys_reads_only_the_explicit_home(monkeypatch, tmp_path):
    poisoned_home = tmp_path / "poisoned-home4"
    _write_models_json(poisoned_home, {"unrelated-provider": {}})
    monkeypatch.setenv("HOME", str(poisoned_home))

    real_home = tmp_path / "real-home2"
    real_home.mkdir()
    _write_models_json(real_home, {"good-provider": {}})

    assert llm.provider_keys(home=real_home) == ["good-provider"]


def test_check_hat_routes_still_raises_when_explicit_home_lacks_the_route(
    monkeypatch, tmp_path
):
    """Sanity: the guard doesn't neuter the check itself -- an explicit
    `home` that genuinely lacks the route still raises, poisoned ambient
    `$HOME` or not."""
    poisoned_home = tmp_path / "poisoned-home5"
    _write_models_json(poisoned_home, {"good-provider": {}})
    monkeypatch.setenv("HOME", str(poisoned_home))

    real_home = tmp_path / "real-home3"
    real_home.mkdir()
    _write_models_json(real_home, {"good-provider": {}})

    cfg = _FakeCfg(_refs_naming("verify", "bogus-provider/some-model"))

    with pytest.raises(llm.UnroutableHatModel):
        llm.check_hat_routes(cfg, home=real_home)
