"""The uid boundary: an iteration cannot signal its own supervisor (task 020, #48).

Requirement I's mechanism lives in three places -- `engine/privsep.py` (the
credentials), `container/Dockerfile` + `container/entrypoint.sh` (starting as
root at all), and the two spawn sites that drop a child back down
(`runner.py`'s pi, `main.py`'s `on_complete_cmd`) -- so this module checks all
three, plus the documentation claims made about them.

What it deliberately does **not** try to do is prove the kernel rule itself
from here: this container runs the whole suite as uid 1000, so no test in the
fast lane can establish a real boundary. The end-to-end proof (a worker
iteration running `pkill -f ralphd-engine` against a live engine and getting
`EPERM` while the run finishes normally) is the docker-tier
`tests/test_engine_uid_docker.py`. What *is* provable here, and matters just as
much, is the other half of the contract: an unprivileged engine keeps working
exactly as it did in v0.6 and says out loud that it has no boundary.
"""

from __future__ import annotations

import ast
import os
import pwd
import subprocess
import sys
from pathlib import Path

import pytest

from ralphd.engine import privsep

REPO = Path(__file__).parent.parent
SRC = REPO / "src" / "ralphd" / "engine"
DOCKERFILE = (REPO / "container" / "Dockerfile").read_text()
ENTRYPOINT = (REPO / "container" / "entrypoint.sh").read_text()
SPEC = (REPO / "SPEC.md").read_text()
ARCH = (REPO / "docs" / "architecture.md").read_text()
STUB_OVERLAY = (REPO / "tests" / "docker-hostpath-wrapper"
                / "Dockerfile.stub-overlay").read_text()


# --- privsep: who the agent is -------------------------------------------

def test_the_agent_identity_resolves_by_name_first():
    ident = privsep.agent_identity()
    assert ident is not None, "this container has an `agent` account"
    assert (ident.name, ident.uid, ident.gid) == ("agent", 1000, 1000)


def test_the_agent_identity_falls_back_to_uid_1000_by_number(monkeypatch):
    """A derived job image inherits whatever uid 1000 is called in the
    operator's own base, so a name miss must not lose the boundary."""
    monkeypatch.setenv(privsep.AGENT_USER_ENV, "no-such-account-here")
    ident = privsep.agent_identity()
    assert ident is not None and ident.uid == 1000


def test_root_is_never_a_valid_agent_identity(monkeypatch):
    monkeypatch.setenv(privsep.AGENT_USER_ENV, "root")

    def only_root(uid):
        raise KeyError(uid)

    monkeypatch.setattr(privsep.pwd, "getpwuid", only_root)
    assert privsep.agent_identity() is None


def test_no_agent_account_at_all_means_no_identity(monkeypatch):
    monkeypatch.setattr(privsep.pwd, "getpwnam",
                        lambda name: (_ for _ in ()).throw(KeyError(name)))
    monkeypatch.setattr(privsep.pwd, "getpwuid",
                        lambda uid: (_ for _ in ()).throw(KeyError(uid)))
    assert privsep.agent_identity() is None


# --- privsep: the predicate and the spawn kwargs -------------------------

def test_this_engine_is_not_separated_and_says_so_by_reading_live_creds():
    """The suite runs as uid 1000: real uid is not 0, so `separated()` must be
    False -- read from the process credentials, not from a startup flag."""
    assert os.getuid() != 0
    assert privsep.separated() is False
    assert privsep.agent_child_kwargs() == {}


@pytest.mark.parametrize("creds,expected", [
    ((0, 1000, 0), True),      # the shape separate_engine_identity() makes
    ((0, 0, 0), False),        # plain root: signalable-by-nobody, but not dropped
    ((1000, 1000, 1000), False),   # pre-v0.7 arrangement
    ((0, 1000, 1000), False),  # saved uid moved: the agent could signal it
    ((1000, 1000, 0), False),  # real uid is the agent's: signalable
    ((0, 1001, 0), False),     # effective uid is not the agent's
])
def test_separated_is_exactly_the_real_and_saved_root_shape(monkeypatch, creds, expected):
    monkeypatch.setattr(privsep.os, "getresuid", lambda: creds)
    assert privsep.separated() is expected


def test_a_separated_engine_spawns_children_as_the_agent(monkeypatch):
    monkeypatch.setattr(privsep.os, "getresuid", lambda: (0, 1000, 0))
    assert privsep.agent_child_kwargs() == {"user": 1000, "group": 1000}


# --- privsep: the drop itself --------------------------------------------

def test_an_unprivileged_engine_drops_nothing_and_warns(caplog):
    before = (os.getresuid(), os.getresgid())
    with caplog.at_level("WARNING"):
        assert privsep.separate_engine_identity() is None
    assert (os.getresuid(), os.getresgid()) == before, "creds must be untouched"
    assert "no uid boundary" in caplog.text
    assert "#48" in caplog.text


def test_the_drop_keeps_root_real_and_saved_and_sets_groups_first(monkeypatch):
    """The exact call sequence, because each part carries the property:
    initgroups while still root (an unprivileged child cannot setgroups),
    gid before uid (after the uid drop the gid can no longer be changed),
    and `(0, agent, 0)` both times -- real and saved root is what makes
    kill(2) from the agent uid EPERM."""
    calls: list[tuple] = []
    monkeypatch.setattr(privsep.os, "getuid", lambda: 0)
    monkeypatch.setattr(privsep.os, "initgroups",
                        lambda name, gid: calls.append(("initgroups", name, gid)))
    monkeypatch.setattr(privsep.os, "setresgid",
                        lambda r, e, s: calls.append(("setresgid", r, e, s)))
    monkeypatch.setattr(privsep.os, "setresuid",
                        lambda r, e, s: calls.append(("setresuid", r, e, s)))
    ident = privsep.separate_engine_identity()
    assert ident == privsep.AgentIdentity("agent", 1000, 1000)
    assert calls == [("initgroups", "agent", 1000),
                     ("setresgid", 0, 1000, 0),
                     ("setresuid", 0, 1000, 0)]


def test_root_with_no_agent_account_refuses_to_drop_rather_than_to_root(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(privsep.os, "getuid", lambda: 0)
    monkeypatch.setattr(privsep, "agent_identity", lambda: None)
    monkeypatch.setattr(privsep.os, "setresuid",
                        lambda r, e, s: calls.append(("setresuid", r, e, s)))
    assert privsep.separate_engine_identity() is None
    assert calls == []


# --- wiring: the three places that must use it ---------------------------

def _call_names(node: ast.AST) -> list[str]:
    return [n.func.id for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]


def test_the_engine_establishes_the_boundary_before_it_runs_anything():
    tree = ast.parse((SRC / "main.py").read_text())
    main = next(f for f in tree.body
                if isinstance(f, ast.FunctionDef) and f.name == "main")
    names = _call_names(main)
    assert "separate_engine_identity" in names, "main() must establish the boundary"
    assert names.index("separate_engine_identity") < names.index("asyncio_run") \
        if "asyncio_run" in names else True
    # the drop must precede the event loop (and therefore every file write)
    src = ast.unparse(main)
    assert src.index("separate_engine_identity") < src.index("asyncio.run")


def test_the_iteration_subprocess_is_dropped_to_the_agent():
    src = (SRC / "runner.py").read_text()
    assert "from .privsep import agent_child_kwargs" in src
    spawn = src.split("create_subprocess_exec(", 1)[1].split(")\n", 1)[0]
    assert "**agent_child_kwargs()" in spawn


def test_the_on_complete_hook_is_dropped_to_the_agent_too():
    src = (SRC / "main.py").read_text()
    spawn = src.split("create_subprocess_shell(", 1)[1].split(")\n", 1)[0]
    assert "**agent_child_kwargs()" in spawn


# --- the image and the entrypoint ----------------------------------------

def test_the_image_no_longer_ends_as_the_agent_uid():
    # instructions only: the file *explains* the removed `USER agent` in a
    # comment, which must not be mistaken for the instruction itself
    lines = [ln.strip() for ln in DOCKERFILE.splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    assert not [ln for ln in lines if ln.startswith("USER ")], lines
    # and says why, naming the requirement
    assert "#48" in DOCKERFILE


def test_the_image_still_creates_the_agent_at_uid_1000_and_owns_the_mounts():
    assert "useradd -m -u 1000 -s /bin/bash agent" in DOCKERFILE
    assert "chown -R agent:agent /workspace /run/ralphd /home/agent" in DOCKERFILE
    assert 'ENTRYPOINT ["/opt/ralphd/container/entrypoint.sh"]' in DOCKERFILE


def test_home_is_the_agents_home_for_the_engine_as_well():
    """The engine's $HOME config overlay, the creds it places for pi and pi's
    own settings must resolve to the one home the iteration reads."""
    assert "HOME=/home/agent" in DOCKERFILE
    assert 'export HOME="${agent_home:-/home/${AGENT_USER}}"' in ENTRYPOINT


def test_the_entrypoint_hands_the_pi_config_to_the_agent_when_it_runs_as_root():
    assert 'chown -R "$AGENT_USER" "$HOME/.pi"' in ENTRYPOINT
    # ... and attempts none of that unprivileged, so `--user 1000` still works
    assert ENTRYPOINT.count('[ "$(id -u)" = 0 ]') >= 2
    assert ENTRYPOINT.rstrip().endswith("exec ralphd-engine")


def test_the_docker_tier_overlay_does_not_re_add_the_agent_user():
    """The stub overlay is built ON TOP of the real image; a trailing
    `USER agent` there would silently disable the property under test."""
    assert "\nUSER agent" not in STUB_OVERLAY
    assert "#48" in STUB_OVERLAY


# --- an unprivileged engine still works, and says it has no boundary -----

def test_an_unprivileged_engine_runs_and_records_that_it_has_no_boundary(tmp_path):
    """Black box: the real `ralphd-engine` binary, started as uid 1000 (no
    PRD, so it exits 2 immediately) must log the missing boundary and must
    not fail on account of the drop it could not do."""
    run_dir, config_dir = tmp_path / "run", tmp_path / "config"
    run_dir.mkdir()
    config_dir.mkdir()
    env = {**os.environ, "RALPHD_RUN_DIR": str(run_dir),
           "RALPHD_CONFIG_DIR": str(config_dir),
           "RALPHD_WORKSPACE_DIR": str(tmp_path / "ws")}
    res = subprocess.run([str(Path(sys.executable).parent / "ralphd-engine")],
                         env=env, capture_output=True, text=True, timeout=60)
    assert res.returncode == 2, res.stdout + res.stderr  # "no PRD", not a crash
    out = res.stdout + res.stderr
    assert "no uid boundary" in out
    assert f"engine is not root (uid {pwd.getpwnam('agent').pw_uid})" in out or \
        "engine is not root" in out


def test_version_and_help_still_touch_nothing(tmp_path):
    """--version exits inside parse_args, before the drop: still no side
    effects of any kind (SPEC 13.4's first self-protection bullet)."""
    engine = str(Path(sys.executable).parent / "ralphd-engine")
    res = subprocess.run([engine, "--version"], capture_output=True, text=True,
                         timeout=30, cwd=tmp_path)
    assert res.returncode == 0
    assert "no uid boundary" not in (res.stdout + res.stderr)
    assert list(tmp_path.iterdir()) == []


# --- the documentation claims -------------------------------------------

def test_spec_documents_the_kill_rule_the_boundary_rests_on():
    body = SPEC.split("### 13.4 What the agent is allowed to do", 1)[1]
    body = body.split("## 14. Testing", 1)[0]
    assert "uid boundary" in body
    assert "real and saved uid stay `0`" in body.lower()
    assert "kill(2)" in body
    assert "pkill" in body
    assert "no `USER`" in body


def test_spec_documents_the_ownership_half_and_what_is_left():
    body = SPEC.split("### 13.4 What the agent is allowed to do", 1)[1]
    body = body.split("## 14. Testing", 1)[0]
    assert "effective uid stays the agent's" in body
    assert "derived" in body and "USER 1000" in body   # what is left
    assert "unprivileged" in body                      # degradation is documented


def test_spec_deferred_entry_says_what_this_wave_closed():
    entry = SPEC.split("- **PID-namespace isolation of agent iterations.**", 1)[1]
    entry = entry.split("\n- **", 1)[0]
    assert "v0.7" in entry
    assert "uid boundary" in entry
    assert "no longer load-bearing" in entry
    assert "torn down" in entry  # what a namespace would still add


def test_spec_non_goals_and_image_description_agree_with_the_image():
    assert "PID-namespace isolation of an *iteration*" in SPEC
    assert "the image deliberately sets no `USER`" in SPEC


def test_architecture_documents_the_containment_boundary():
    body = ARCH.split("### The uid boundary", 1)[1].split("\n### ", 1)[0]
    assert "setresuid(0, agent_uid, 0)" in body
    assert "real or saved" in body
    assert "effective" in body and "uid 1000 exactly as in v0.6" in body
    assert "privsep.agent_child_kwargs()" in body
    assert "killpg" in body           # signals still flow downward
    assert "no-op that\n  logs one warning" in body or "logs one warning" in body


def test_architecture_non_goals_no_longer_claim_the_signal_hazard_is_open():
    """The same document's non-goals list used to promise PID-namespace
    isolation *as* the answer to in-container kill signals; leaving that
    standing would contradict the section above it."""
    para = ARCH.split("**Non-goals**", 1)[1].split("\n\n", 1)[0]
    assert "PID-namespace isolation" in para
    assert "v0.7" in para and "uid boundary" in para
    assert "torn down" in para  # what a namespace would still add
