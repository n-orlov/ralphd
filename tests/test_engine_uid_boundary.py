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

Three tests below (identity-by-name, the drop's exact call sequence, and the
real black-box `ralphd-engine` run) assert facts that are only true given an
actual `agent` account at uid/gid 1000 -- true in every job container this
project ships, but not on a bare developer host or a stripped CI sandbox with
no such account. `_missing_container_precondition()` names exactly what is
missing and each of the three skips on it rather than failing; task 007's
docker-tier companion, `tests/test_engine_uid_boundary_docker.py`, proves the
skip cannot become permanent by running this exact module inside a real job
container and asserting those three pass there. The rest of this module needs
no such account and always runs.
"""

from __future__ import annotations

import ast
import json
import os
import pwd
import subprocess
from pathlib import Path

import pytest

from ralphd.engine import privsep
from tests.conftest import RALPHD_ENGINE

REPO = Path(__file__).parent.parent
SRC = REPO / "src" / "ralphd" / "engine"
DOCKERFILE = (REPO / "container" / "Dockerfile").read_text()
ENTRYPOINT = (REPO / "container" / "entrypoint.sh").read_text()
SPEC = (REPO / "SPEC.md").read_text()
STUB_OVERLAY = (REPO / "tests" / "docker-hostpath-wrapper"
                / "Dockerfile.stub-overlay").read_text()


def _architecture_text() -> str:
    """`docs/architecture.md`, read lazily rather than at module import.

    The last line of `.dockerignore` is `docs`, so a job container built
    from `container/Dockerfile` (`COPY --chown=agent:agent . /opt/ralphd`)
    never has a copy of this file -- reading it at import time would make
    this whole module uncollectable there, before any test runs, which is
    exactly what broke `tests/test_engine_uid_boundary_docker.py`'s proof
    (#57 reopening, steering note 002): pytest could not even report which
    tests it skipped, only "1 error during collection". Only the two
    architecture-claim tests below need this text, and neither is one of
    the three job-container-gated tests, so they skip instead of failing
    collection when the file is absent."""
    path = REPO / "docs" / "architecture.md"
    if not path.exists():
        pytest.skip("docs/architecture.md is not present -- .dockerignore's "
                     "last line excludes docs/ from the image this test is "
                     "running against, so a job container has no copy")
    return path.read_text()


# --- who can run this module's job-container-only tests -----------------

def _missing_container_precondition() -> str | None:
    """`None` if this process is running inside a job container built from
    `container/Dockerfile` -- which creates an `agent` account at uid/gid
    1000 (`useradd -m -u 1000 -s /bin/bash agent`) and, since requirement I
    (#48), always runs the test suite as that account rather than root;
    otherwise a reason string naming exactly what is missing. A bare
    developer host or a stripped CI sandbox commonly has neither, and the
    three tests below need both to mean anything."""
    try:
        ent = pwd.getpwnam("agent")
    except KeyError:
        return ("no `agent` account on this host -- the job container's "
                "Dockerfile creates one at uid 1000 "
                "(`useradd -m -u 1000 -s /bin/bash agent`)")
    if (ent.pw_uid, ent.pw_gid) != (1000, 1000):
        return (f"an `agent` account exists but is not uid/gid 1000:1000 "
                f"(got {ent.pw_uid}:{ent.pw_gid})")
    if os.getuid() != 1000:
        return (f"not running as uid 1000 (got {os.getuid()}) -- the job "
                f"container always runs its test suite as the agent")
    return None


def test_the_container_precondition_check_fires_without_an_agent_account(monkeypatch):
    monkeypatch.setattr(pwd, "getpwnam",
                        lambda name: (_ for _ in ()).throw(KeyError(name)))
    reason = _missing_container_precondition()
    assert reason is not None
    assert "agent" in reason and "uid 1000" in reason


def test_the_container_precondition_check_fires_on_the_wrong_uid(monkeypatch):
    """Isolate the uid branch: fake a real `agent` account at 1000:1000 so
    the account/uid-gid checks above it pass on ANY host (not just inside a
    job container -- a bare developer host commonly has no `agent` account
    at all, so without this fake the function would return at the
    account-missing branch first and never reach the uid check, which is
    exactly how this test was found to fail on a real host: #57
    reopening), then force `os.getuid()` to disagree."""
    fake_agent = pwd.struct_passwd(
        ("agent", "x", 1000, 1000, "", "/home/agent", "/bin/bash"))
    monkeypatch.setattr(pwd, "getpwnam",
                        lambda name: fake_agent if name == "agent"
                        else (_ for _ in ()).throw(KeyError(name)))
    monkeypatch.setattr(os, "getuid", lambda: 0)
    reason = _missing_container_precondition()
    assert reason is not None
    assert "not running as uid 1000" in reason


def test_the_container_precondition_check_reflects_both_directions(monkeypatch):
    """Neither direction is fixed by which host runs this test: fake the
    facts each way and assert the check flips with them, rather than
    asserting a single hard-coded outcome that only held inside a job
    container (#57 reopening -- the previous version of this test was named
    `..._passes_inside_this_container` and its own docstring stated that
    premise out loud, which is exactly the assumption #57 exists to
    remove; it passed here and failed on a real developer host, where
    `_missing_container_precondition()` is never `None`)."""
    fake_agent = pwd.struct_passwd(
        ("agent", "x", 1000, 1000, "", "/home/agent", "/bin/bash"))
    monkeypatch.setattr(pwd, "getpwnam",
                        lambda name: fake_agent if name == "agent"
                        else (_ for _ in ()).throw(KeyError(name)))

    # Facts say "this is the job container as the agent": None.
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    assert _missing_container_precondition() is None

    # Flip just the uid: a reason naming exactly what disagrees.
    monkeypatch.setattr(os, "getuid", lambda: 0)
    reason = _missing_container_precondition()
    assert reason is not None
    assert "not running as uid 1000" in reason


# --- privsep: who the agent is -------------------------------------------

def test_the_agent_identity_resolves_by_name_first():
    reason = _missing_container_precondition()
    if reason:
        pytest.skip(reason)
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
    reason = _missing_container_precondition()
    if reason:
        pytest.skip(reason)
    calls: list[tuple] = []
    # Root's group list going in is just [0]; the mock mirrors what the real
    # initgroups() does -- replaces the list with /etc/group's entry for the
    # agent's own account, i.e. just its own gid, no supplementary groups --
    # so with no operator-granted group to preserve, setgroups() is never
    # reached at all (see test_the_drop_keeps_an_operator_granted_supplementary_group
    # for the case where it is).
    groups = [0]
    monkeypatch.setattr(privsep.os, "getuid", lambda: 0)
    monkeypatch.setattr(privsep.os, "getgroups", lambda: list(groups))

    def fake_initgroups(name, gid):
        nonlocal groups
        groups = [gid]
        calls.append(("initgroups", name, gid))

    monkeypatch.setattr(privsep.os, "initgroups", fake_initgroups)
    monkeypatch.setattr(privsep.os, "setgroups",
                        lambda gs: calls.append(("setgroups", list(gs))))
    monkeypatch.setattr(privsep.os, "setresgid",
                        lambda r, e, s: calls.append(("setresgid", r, e, s)))
    monkeypatch.setattr(privsep.os, "setresuid",
                        lambda r, e, s: calls.append(("setresuid", r, e, s)))
    ident = privsep.separate_engine_identity()
    assert ident == privsep.AgentIdentity("agent", 1000, 1000)
    assert calls == [("initgroups", "agent", 1000),
                     ("setresgid", 0, 1000, 0),
                     ("setresuid", 0, 1000, 0)]


def test_the_drop_keeps_an_operator_granted_supplementary_group(monkeypatch):
    """`--allow-docker` adds the host's docker gid with `docker run
    --group-add`, and that gid has no /etc/group entry inside the image. So
    `initgroups()` -- which *replaces* the list with what /etc/group says --
    discards it, and every iteration then gets "permission denied while trying
    to connect to the docker API". For a job whose entire toolchain is sibling
    containers that means it can build and test nothing, silently, for its whole
    budget: observed on a real run that burned 45 iterations and failed an
    approach on the stagnation guard with 0 of 72 tasks done.

    An inherited supplementary group is an explicit operator grant made at
    `docker run` time. Group 0 is the one that must not survive -- it is the
    privilege this boundary exists to remove."""
    # Resolved rather than hard-coded: uid 1000 is `agent` in the shipped image
    # but somebody else on a developer host, and this property is about the
    # group list, not the account name.
    ident = privsep.agent_identity()
    assert ident is not None and ident.uid != 0
    granted_docker_gid = 968
    calls: list[tuple] = []
    # As the container starts: root, plus the operator's --group-add.
    groups = [0, granted_docker_gid]
    monkeypatch.setattr(privsep.os, "getuid", lambda: 0)
    monkeypatch.setattr(privsep.os, "getgroups", lambda: list(groups))

    def fake_initgroups(name, gid):
        # What the real one does inside the image: /etc/group knows only the
        # agent's own group, so the granted gid is wiped.
        nonlocal groups
        groups = [gid]
        calls.append(("initgroups", name, gid))

    def fake_setgroups(gs):
        nonlocal groups
        groups = list(gs)
        calls.append(("setgroups", list(gs)))

    monkeypatch.setattr(privsep.os, "initgroups", fake_initgroups)
    monkeypatch.setattr(privsep.os, "setgroups", fake_setgroups)
    monkeypatch.setattr(privsep.os, "setresgid", lambda r, e, s: None)
    monkeypatch.setattr(privsep.os, "setresuid", lambda r, e, s: None)

    assert privsep.separate_engine_identity() == ident
    assert granted_docker_gid in groups, (
        "the operator's --group-add gid must survive the drop, or --allow-docker "
        "silently does nothing for every iteration")
    assert ident.gid in groups, "the agent's own gid must still be present"
    assert 0 not in groups, "group 0 must not survive the drop"
    expected = sorted({ident.gid, granted_docker_gid})
    assert ("setgroups", expected) in calls
    # setgroups is only reachable while still root, so it must come before the
    # uid drop -- same reason initgroups does.
    assert calls.index(("setgroups", expected)) > \
        calls.index(("initgroups", ident.name, ident.gid))


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


# --- task 105: the authoritative task record, root-owned ------------------

def test_root_for_write_is_a_noop_when_unseparated(monkeypatch):
    """No boundary to raise into: the context manager yields False and never
    touches setresuid at all (there's nothing to raise or restore)."""
    monkeypatch.setattr(privsep, "separated", lambda: False)
    calls: list[tuple] = []
    monkeypatch.setattr(privsep.os, "setresuid",
                        lambda r, e, s: calls.append((r, e, s)))
    with privsep.root_for_write() as elevated:
        assert elevated is False
    assert calls == []


def test_root_for_write_raises_and_restores_when_separated(monkeypatch):
    monkeypatch.setattr(privsep, "separated", lambda: True)
    monkeypatch.setattr(privsep, "agent_identity",
                        lambda: privsep.AgentIdentity("agent", 1000, 1000))
    calls: list[tuple] = []
    monkeypatch.setattr(privsep.os, "setresuid",
                        lambda r, e, s: calls.append((r, e, s)))
    with privsep.root_for_write() as elevated:
        assert elevated is True
        assert calls == [(0, 0, 0)]  # raised to root before the write
    assert calls == [(0, 0, 0), (0, 1000, 0)]  # and dropped back after


def test_root_for_write_restores_even_if_the_write_raises(monkeypatch):
    """The restore is in `finally` -- a write that blows up must not leave
    the engine's effective uid at root."""
    monkeypatch.setattr(privsep, "separated", lambda: True)
    monkeypatch.setattr(privsep, "agent_identity",
                        lambda: privsep.AgentIdentity("agent", 1000, 1000))
    calls: list[tuple] = []
    monkeypatch.setattr(privsep.os, "setresuid",
                        lambda r, e, s: calls.append((r, e, s)))
    with pytest.raises(RuntimeError):
        with privsep.root_for_write():
            raise RuntimeError("boom")
    assert calls == [(0, 0, 0), (0, 1000, 0)]


def test_write_root_owned_json_writes_the_real_content_either_way(tmp_path, monkeypatch):
    monkeypatch.setattr(privsep, "separated", lambda: False)
    target = tmp_path / "tasks.json"
    privsep.write_root_owned_json(target, {"tasks": [{"id": "1"}]})
    assert json.loads(target.read_text()) == {"tasks": [{"id": "1"}]}
    assert not target.with_suffix(".json.tmp").exists()  # no tmp litter


def test_write_root_owned_json_chmods_0644(tmp_path, monkeypatch):
    monkeypatch.setattr(privsep, "separated", lambda: False)
    target = tmp_path / "tasks.json"
    privsep.write_root_owned_json(target, {"tasks": []})
    assert (target.stat().st_mode & 0o777) == 0o644


def test_write_root_owned_json_warns_exactly_once_per_process_when_unseparated(
        tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(privsep, "separated", lambda: False)
    monkeypatch.setattr(privsep, "_write_root_owned_json_warned", False)
    target = tmp_path / "tasks.json"
    with caplog.at_level("WARNING"):
        privsep.write_root_owned_json(target, {"tasks": []})
        privsep.write_root_owned_json(target, {"tasks": [1]})
        privsep.write_root_owned_json(target, {"tasks": [1, 2]})
    warnings = [r for r in caplog.records if r.levelname == "WARNING"
                and "no uid boundary" in r.getMessage()]
    assert len(warnings) == 1, warnings  # degrade logs once, not per call
    assert json.loads(target.read_text()) == {"tasks": [1, 2]}  # every write landed


def test_write_root_owned_json_raises_to_root_for_the_write_when_separated(
        tmp_path, monkeypatch):
    """The write itself happens while the effective uid is really 0 -- proven
    here by asserting the elevate/restore calls bracket the write, using the
    same fake-setresuid technique as the boundary tests above (this module
    cannot actually become root to prove file ownership; that property is
    `tests/test_engine_uid_docker.py`'s job, in a real container)."""
    monkeypatch.setattr(privsep, "separated", lambda: True)
    monkeypatch.setattr(privsep, "agent_identity",
                        lambda: privsep.AgentIdentity("agent", 1000, 1000))
    order: list[str] = []
    monkeypatch.setattr(privsep.os, "setresuid",
                        lambda r, e, s: order.append(f"setresuid({r},{e},{s})"))
    target = tmp_path / "tasks.json"
    real_replace = privsep.os.replace
    monkeypatch.setattr(privsep.os, "replace",
                        lambda a, b: (order.append("write"), real_replace(a, b)))
    privsep.write_root_owned_json(target, {"tasks": []})
    assert order == ["setresuid(0,0,0)", "write", "setresuid(0,1000,0)"]
    assert "#48" in STUB_OVERLAY


# --- an unprivileged engine still works, and says it has no boundary -----

def test_an_unprivileged_engine_runs_and_records_that_it_has_no_boundary(tmp_path):
    """Black box: the real `ralphd-engine` binary, started as uid 1000 (no
    PRD, so it exits 2 immediately) must log the missing boundary and must
    not fail on account of the drop it could not do."""
    reason = _missing_container_precondition()
    if reason:
        pytest.skip(reason)
    run_dir, config_dir = tmp_path / "run", tmp_path / "config"
    run_dir.mkdir()
    config_dir.mkdir()
    env = {**os.environ, "RALPHD_RUN_DIR": str(run_dir),
           "RALPHD_CONFIG_DIR": str(config_dir),
           "RALPHD_WORKSPACE_DIR": str(tmp_path / "ws")}
    res = subprocess.run([str(RALPHD_ENGINE)], env=env, capture_output=True,
                         text=True, timeout=60)
    assert res.returncode == 2, res.stdout + res.stderr  # "no PRD", not a crash
    out = res.stdout + res.stderr
    assert "no uid boundary" in out
    assert f"engine is not root (uid {pwd.getpwnam('agent').pw_uid})" in out or \
        "engine is not root" in out


def test_version_and_help_still_touch_nothing(tmp_path):
    """--version exits inside parse_args, before the drop: still no side
    effects of any kind (SPEC 13.4's first self-protection bullet)."""
    engine = str(RALPHD_ENGINE)
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
    arch = _architecture_text()
    body = arch.split("### The uid boundary", 1)[1].split("\n### ", 1)[0]
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
    arch = _architecture_text()
    para = arch.split("**Non-goals**", 1)[1].split("\n\n", 1)[0]
    assert "PID-namespace isolation" in para
    assert "v0.7" in para and "uid boundary" in para
    assert "torn down" in para  # what a namespace would still add
