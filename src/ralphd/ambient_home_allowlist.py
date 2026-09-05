"""Requirement C (v0.12 PRD, #83 class, task 006): the reasoned allowlist
of every ambient-`$HOME` read still present under `src/ralphd/`.

Task 002 (place_skills), task 004 (provider_keys/check_hat_routes) and
task 006 itself (place_creds, apply_llm, and every engine-side production
call site of refresh_redaction_map) already moved every read that COULD
take its home from its caller to do exactly that -- a caller that omits
`home=` now gets a loud `TypeError` before any filesystem access, never a
silent fallback to this process's own `$HOME`. What is left below is every
ambient-home-reading occurrence (bare `Path.home`, an `os.environ` read of
the `HOME` name, or an `os.path.expanduser` call on the bare `~` sentinel)
that survives that pass, each because the ambient home genuinely *is* the
correct value there -- not because nobody got around to threading a
parameter.

Excluded by design (not audited here, per the task's own preamble): any
`.expanduser()` call whose argument is a user-supplied path (a CLI flag, a
config value) rather than the bare `~` sentinel -- e.g.
`Path(args.dir).expanduser()`. Those resolve THAT string's leading `~`,
which is a property of string handling, not an ambient-home read; a future
lexical guard (task 007) should match the three exact shapes named above,
narrowly enough to skip this module's own docstring prose about them.

Each entry names the file (relative to `src/ralphd/`), the line at the time
this list was written (line numbers drift with unrelated edits -- a future
guard test should key off the file, not the line, precisely because of
this), and a one-line reason the ambient home is correct there. A test that
fails when a NEW, unlisted ambient read appears is task 007's job, not
this file's -- this file only has to be a superset of what's really there,
with a non-empty reason on every entry.
"""

from __future__ import annotations

AMBIENT_HOME_ALLOWLIST: list[dict[str, str]] = [
    # -- src/ralphd/cli -- the CLI *is* the operator's own host process; a
    # bare `ralphctl` invocation has no "caller" to inherit a home from
    # other than the literal person/script that ran it, on their own
    # machine. Each of these already prefers an explicit override
    # (`RALPHD_REGISTRY`, `--llm host`'s own trust boundary) before falling
    # back to the invoking user's real `$HOME`.
    {
        "path": "cli/main.py",
        "line": "159",
        "reason": ("registry()'s ralphctl-managed-state root: overridable via "
                   "RALPHD_REGISTRY; the ambient fallback is the operator's own "
                   "machine home, which is exactly right for a bare `ralphctl` "
                   "invocation with no override set."),
    },
    {
        "path": "cli/main.py",
        "line": "1823",
        "reason": ("`--llm host` staging (`ralphctl start`): the whole point of "
                   "choosing the `host` LLM profile is to copy the invoking "
                   "operator's OWN `~/.pi/agent` into the new container -- there "
                   "is no other home this could mean."),
    },
    {
        "path": "cli/main.py",
        "line": "1836",
        "reason": ("same `--llm host` staging, `~/.aws` half: mounted read-only "
                   "into the container from the operator's own machine, same "
                   "trust boundary as the `.pi/agent` copy just above it."),
    },
    {
        "path": "cli/main.py",
        "line": "4117",
        "reason": ("`ralphctl skills install`'s `home` used to auto-detect a "
                   "default target dir (`_auto_detect_skills_install_target`) "
                   "when the operator passes no `--target` -- the CLI IS the "
                   "operator's own host process, so their own agent-skills "
                   "directory is exactly the right default, same trust "
                   "boundary as `registry()`'s RALPHD_REGISTRY fallback above."),
    },
    {
        "path": "cli/main.py",
        "line": "4319",
        "reason": ("`ralphctl llm test host`'s own resolution path (`_llm_test_"
                   "resolve`, its docstring: \"this runs entirely on the host, "
                   "same trust level as start\") -- deliberately mirrors the "
                   "`--llm host` staging above so `llm test` previews exactly "
                   "what `start --llm host` would actually copy."),
    },
    {
        "path": "cli/main.py",
        "line": "4323",
        "reason": ("same `_llm_test_resolve` host path, the `models.json` half "
                   "-- same reasoning as the `.aws` mount just above it."),
    },
    {
        "path": "cli/main.py",
        "line": "6060",
        "reason": ("`ralphctl doctor`'s pi_host_config check: sanity-checks that "
                   "the OPERATOR's own `~/.pi/agent/settings.json` exists before "
                   "they run `--llm host`, on the same machine `doctor` itself "
                   "runs on -- there is no other caller to take this from."),
    },
    {
        "path": "cli/main.py",
        "line": "6064",
        "reason": ("same `cmd_doctor`, the operator-skill line (task 023, "
                   "requirement R): `_operator_skill_status` reports on "
                   "whichever agent-skills layout is discoverable under the "
                   "OPERATOR's own home, on the same host `doctor` itself "
                   "runs on -- report-only, same reasoning as pi_host_config "
                   "just above it."),
    },
    {
        "path": "cli/ui_server.py",
        "line": "1287",
        "reason": ("`Handler.registry`'s class-level default. Every real server "
                   "binds a concrete registry via `make_handler_class(reg)` "
                   "(see its own docstring, \"Bound to a registry path via "
                   "make_handler_class()\") before any request is served, so "
                   "this default is never actually read in production -- it "
                   "exists only so the bare `Handler` class type-checks, and "
                   "mirrors `cli.main.registry()`'s own default for the same "
                   "unbound case."),
    },
    # -- src/ralphd/engine -- the engine's own per-process ambient home,
    # read by code that either (a) is the one sanctioned place a caller
    # explicitly asks for "the real ambient one" (skills.ambient_home()),
    # (b) serves a host-side/test caller that has no engine call chain to
    # inherit a home from at all, or (c) is a module-level constant with no
    # caller to inherit from in the first place.
    {
        "path": "engine/skills.py",
        "line": "76",
        "reason": ("backs `ambient_home()` (task 002): the ONE designated "
                   "entry point every real caller (engine/api.py, engine/"
                   "main.py, engine/loop.py) already uses to say, at its own "
                   "call site, \"yes, this process's real ambient $HOME, on "
                   "purpose\" -- the value is this process's own $HOME (same "
                   "container `pi` itself reads from), which no caller could "
                   "sensibly override for skill/cred placement."),
    },
    {
        "path": "engine/redact.py",
        "line": "77",
        "reason": ("backs build_redaction_map()'s optional `home` fallback. "
                   "Every engine-side production caller (main.py, api.py's "
                   "creds/llm PUT/DELETE handlers) now passes `home=` "
                   "explicitly (task 006) instead of relying on this default "
                   "-- but the function is ALSO the host-side reader "
                   "`job_yaml_secrets`/`redact_job_yaml` call for redacting a "
                   "displayed job.yaml (docstring: \"a host-side reader like "
                   "ralphctl docs ... has no live map at all\"), plus direct "
                   "test callers (tests/test_cli_run_documents.py, tests/"
                   "test_self_inflicted_abort.py) that exercise exactly that "
                   "no-engine-context case -- for those the ambient home IS "
                   "the correct value, because there is no engine call chain "
                   "to take it from."),
    },
    {
        "path": "engine/config.py",
        "line": "33",
        "reason": ("OVERLAY_DIR's default, a module-level constant computed "
                   "once at import time exactly like this file's own "
                   "CONFIG_DIR/RUN_DIR/WORKSPACE_DIR (all read straight from "
                   "os.environ with a default) -- there is no caller to a "
                   "module-level assignment; it IS this process's own "
                   "container-local overlay root, overridable via "
                   "RALPHD_CONFIG_OVERLAY_DIR."),
    },
    {
        "path": "engine/loop.py",
        # Was line 733 when this entry was written; tasks 032/033/034's engine
        # work shifted `loop.py` below it and task 027's sweep caught the
        # stale citation, corrected to 866, then task 001 (this wave) to 872.
        # Task 039 re-derived every citation in this file from a fresh scan
        # after the B/C/D/E engine work of tasks 006-013 and the cure tasks
        # 035-038 shifted both `loop.py` and `cli/main.py`: this read is now
        # at 978 -- same method, same read, same reason; line numbers drift
        # with unrelated edits, exactly as this module's own docstring warns.
        "line": "978",
        "reason": ("LoopSupervisor._creds_note() lists the `~/.creds/*.env` "
                   "names place_creds() (this SAME process's ambient home, "
                   "task 006) already placed on this same container, for the "
                   "next phase prompt. LoopSupervisor has no `home` field "
                   "today and dozens of tests construct it positionally "
                   "(`LoopSupervisor(cfg, run, workspace)`) -- threading one "
                   "through purely so a static method could stop reading "
                   "$HOME would be a broad, test-breaking signature change for "
                   "a value that is, and always would be, identical to what "
                   "place_creds() itself just used."),
    },
]
