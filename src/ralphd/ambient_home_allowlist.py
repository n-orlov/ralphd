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
        # Task 028 (#157, requirement D.4) added the `est` column's
        # `task_progress_budget_estimate` import above this read, shifting
        # it by +1: 159->160->161. Same read, same reason.
        #
        # Approach-4 task 010 added one `log_render` import beside the other
        # renderer imports above this read, shifting it by +1 again:
        # 161->162. Same read, same reason.
        #
        # Task 004 (v0.21, PRD requirement A.4, #192) added three
        # `GATE_REJECTION_ENDING_*` names to the `from ..engine.state import`
        # block above this read, shifting it by +3: 162->165. Same read,
        # same reason.
        #
        # Task 017 (v0.21, PRD requirement D.7, #196) added
        # `AMENDMENT_WAVE_TARGET` to the same import block above this read,
        # shifting it by +1: 165->166. Same read, same reason.
        #
        # Task 021 (v0.21, PRD requirement D.12, #196) added `repoint_
        # amendment` to the same import block above this read, shifting it
        # by +1: 166->167. Same read, same reason.
        "line": "167",
        "code": "return Path(os.environ.get(\"RALPHD_REGISTRY\", str(Path.home() / \".ralphd\")))",
        "reason": ("registry()'s ralphctl-managed-state root: overridable via "
                   "RALPHD_REGISTRY; the ambient fallback is the operator's own "
                   "machine home, which is exactly right for a bare `ralphctl` "
                   "invocation with no override set."),
    },
    {
        "path": "cli/main.py",
        # Task 041 re-derived every `cli/main.py` line below from a fresh
        # `find_ambient_home_reads()` scan after its own `src/` change (the
        # rubric status line moved to stderr) shifted them all by 9:
        # 1823->1832, 1836->1845, 4117->4126, 4319->4328, 4323->4332,
        # 6060->6069, 6064->6073. Same reads, same reasons; line numbers
        # drift with unrelated edits, exactly as this module's own docstring
        # warns.
        #
        # Task 020 (the run-secrets mount) re-derived them AGAIN from a
        # fresh scan after inserting `run_secrets_key_files` and its
        # constants near the top of `cli/main.py`: 159->160 (drift that
        # predated this task), 1832->1903, 1845->1916, 4126->4250,
        # 4328->4452, 4332->4456, 6069->6193, 6073->6197. Same reads, same
        # reasons.
        #
        # Task 014 (#156, requirement A.4, the progress-budget surface)
        # inserted `_progress_budget_doc()`/`_format_progress_budget_lines()`
        # plus threaded `progress_budget` through `_task_status_width`/
        # `_task_status_cell`/`_print_task_lines`, all well above the four
        # entries below, shifting each of them by +61: 4250->4311,
        # 4452->4513, 4456->4517, 6193->6254, 6197->6258. Same reads, same
        # reasons.
        #
        # Task 019 (#156, requirement B.1/F, the `superseded` status) widened
        # `cmd_amend`'s `--status` validation (`OPERATOR_RULEABLE_TASK_
        # STATUSES`) and `_TASK_STATUSES`, both between the 4517 entry and
        # the two `doctor` entries below, shifting only those two by +15:
        # 6254->6269, 6258->6273. Same reads, same reasons; the three entries
        # above them are untouched.
        #
        # Task 028 (#157, requirement D.4, the `est` column) added the
        # `task_progress_budget_estimate` import above ALL entries below
        # (+1) and the ` (est N)` docstring/suffix in `_print_task_lines`,
        # further below the first three entries but above the last five
        # (+9 more, so +10 total for those five): 1903->1904, 1916->1917
        # (+1 each), 4311->4321, 4513->4523, 4517->4527, 6269->6279,
        # 6273->6283 (+10 each). Same reads, same reasons. Task 036 (#168,
        # requirement F.2) added `_amend_echo_target_or_die`/`_amend_
        # resolve_approach` plus the `--approach`/`--force` argparse flags
        # well above the last two entries below, shifting only those two by
        # +70: 6279->6349, 6283->6353. Same reads, same reasons; the other
        # six entries above them are untouched.
        #
        # Task 044 (#177, `--llm-header`) added the flag's host-side parse/
        # resolve block above ALL entries below (+12), a header-merge block
        # inside the `--llm host` branch between the first two entries
        # (+7 more, so +19 total, for the second entry and everything after
        # it), and turned one `models.json` write into a 3-line merge+write
        # (+2 more, so +21 total, for every entry from the third one on):
        # 1904->1916, 1917->1936, 4321->4342, 4523->4544, 4527->4548,
        # 6349->6370, 6353->6374. Same reads, same reasons.
        #
        # Task 045 (#177, persisting/replaying `--llm-header`) added the
        # `_apply_template` header-precedence block (+16) above BOTH the
        # first two entries (well above `cmd_start`'s `--llm host` staging),
        # shifting them by +23: 1916->1939, 1936->1959. The `resume`
        # `--llm-header` argparse flag (+11) and its resume-time
        # override/replay block (+29), both well above every entry from the
        # third one on but below the first two, add a further +40 there (so
        # +52 total from the entry BEFORE task 044's own baseline): 4342->
        # 4394, 4544->4596, 4548->4600, 6370->6422, 6374->6426. Same reads,
        # same reasons.
        #
        # Task 047 (#177, showing/redacting `--llm-header` on the four
        # surfaces) added a `headerRefNames` set-building block above the
        # first two entries (+40) and threaded/persisted it through the
        # rest, shifting every entry below by +40: 1939->1979, 1959->1999,
        # 4394->4466, 4596->4668, 4600->4672, 6422->6494, 6426->6498. Same
        # reads, same reasons.
        #
        # Task 085 (steer 013 section 2, #183) moved `_container_liveness`'s
        # `FileNotFoundError` catch (task 080) into `sh()` itself and
        # expanded both docstrings, shifting every entry from this one on:
        # 1979->2005, 1999->2025.
        #
        # Approach-4 task 010's one added `log_render` import shifts these
        # two by +1: 2005->2006, 2025->2026. Same reads, same reasons.
        #
        # v0.20.1 task 010 (requirement D.1, `415a069`) recorded a
        # profile-provided header into `llm-wiring.json` in `cmd_start`, a
        # net +30 lines above every entry in this file, shifting each of
        # them by +30: 2006->2036, 2026->2056, 4519->4549, 4721->4751,
        # 4725->4755, 6547->6577, 6551->6581. Same reads, same reasons.
        #
        # v0.20.2 task 010 (requirement B.2, #197) added the `notifier_url`
        # job-dict entry (+8) inside `cmd_start`, above every entry in this
        # file (the `--notifier-url` argparse addition itself sits well
        # below the last of these, so it shifts nothing here), shifting
        # each of them by +8: 2036->2044, 2056->2064, 4549->4557,
        # 4751->4759, 4755->4763, 6577->6585, 6581->6589. Same reads, same
        # reasons.
        #
        # v0.20.2 task 010's cure pass (same requirement B.2) made
        # `notifier_url` a `_TEMPLATE_SCALAR_FIELDS` entry and widened two
        # comment blocks in `cmd_start`, a net +13 lines above every entry
        # in this file, shifting each of them by +13: 2044->2057,
        # 2064->2077, 4557->4570, 4759->4772, 4763->4776, 6585->6598,
        # 6589->6602. Same reads, same reasons.
        #
        # Task 004 (v0.21, PRD requirement A.4, #192) added three names to
        # the `from ..engine.state import` block near the top of this file
        # (+3, above every entry in this file), a new `_format_gate_
        # rejection_ending_lines` helper just above `_format_termination_
        # lines` (+39, above every entry from `4570` on), and a `cmd_status`
        # render-wiring call site (+7, likewise above every entry from
        # `4570` on): 2057->2060, 2077->2080 (+3 only -- both sit above the
        # other two insertions), 4570->4619, 4772->4821, 4776->4825,
        # `6671->6720, 6675->6724 (+49 each -- all five sit below all three
        # insertions). Same reads, same reasons.
        #
        # Task 017 (v0.21, PRD requirement D.7, #196) added
        # `AMENDMENT_WAVE_TARGET` to the same import block, +1 above every
        # entry in this file: 2060->2061, 2080->2081, 4619->4620,
        # 4821->4822, 4825->4826, 6729->6791, 6733->6795 (the last two also
        # carry the +61 from this task's own `cmd_amend`/`cmd_amend_list`
        # edits, both of which sit above them -- see that entry's own
        # comment). Same reads, same reasons.
        #
        # Task 021 (v0.21, PRD requirement D.12, #196) added `repoint_
        # amendment` to the top import block above this read, shifting it
        # by +1: 2061->2062. Same read, same reason.
        "line": "2062",
        "code": "pi_dir = Path.home() / \".pi\" / \"agent\"",
        "reason": ("`--llm host` staging (`ralphctl start`): the whole point of "
                   "choosing the `host` LLM profile is to copy the invoking "
                   "operator's OWN `~/.pi/agent` into the new container -- there "
                   "is no other home this could mean."),
    },
    {
        "path": "cli/main.py",
        # Task 021 (v0.21, PRD requirement D.12, #196): same +1 import-block
        # shift as the entry above it: 2081->2082.
        "line": "2082",
        "code": "aws = Path.home() / \".aws\"",
        "reason": ("same `--llm host` staging, `~/.aws` half: mounted read-only "
                   "into the container from the operator's own machine, same "
                   "trust boundary as the `.pi/agent` copy just above it."),
    },
    {
        "path": "cli/main.py",
        # Task 080 (steer 011 defect 3) added a `try`/`except
        # FileNotFoundError` wrap inside `_container_liveness()`, well
        # above every entry from this one on, shifting each of them by
        # +15: 4466->4481, 4668->4683, 4672->4687, 6494->6509, 6498->6513.
        # Same reads, same reasons.
        #
        # Task 085 (steer 013 section 2, #183) removed that same `try`/
        # `except FileNotFoundError` wrap (now dead code -- the catch moved
        # into `sh()`, which every one of these call sites already goes
        # through) and expanded `sh()`'s own docstring, a net +29 above
        # every entry from this one on: 4481->4510, 4683->4712, 4687->4716,
        # 6509->6538, 6513->6542. Same reads, same reasons.
        #
        # Approach-4 task 010 added one `log_render` import (+1, above all
        # of these) and eight lines inside `_stream_logs_pretty_tailed`
        # (the pending-tool-invocation call and its comment, above all but
        # the first of these), a net +9 for every entry from this one on:
        # 4510->4519, 4712->4721, 4716->4725, 6538->6547, 6542->6551. Same
        # reads, same reasons.
        # Task 004 (v0.21, PRD requirement A.4, #192): import-block shift
        # (+3) plus the new `_format_gate_rejection_ending_lines` helper
        # (+39, inserted just above `_format_termination_lines`) plus the
        # `cmd_status` render-wiring call site (+7, also above this entry):
        # net +49: 4570->4619.
        # Task 017 (v0.21, PRD requirement D.7, #196): +1, same import-block
        # shift: 4619->4620.
        #
        # Task 021 (v0.21, PRD requirement D.12, #196): `repoint_amendment`
        # added to the same import block above this read, +1 more:
        # 4620->4621. Same read, same reason (`cmd_amend`'s own new
        # `--repoint`/`--to` docstring+logic sits well below this read, in
        # `cmd_amend` itself, so it does not shift this entry).
        #
        # Task 025 (v0.21, PRD requirement E.3, #183) added `_report_dead_
        # container_before_bind()` just above `_container_running()` (+33)
        # and two call sites in `cmd_start`'s own status fallback, above
        # every entry from this one on (+2 more), net +35: 4621->4656. Same
        # read, same reason. Its own rejection cure (reading `docker logs`'s
        # stderr too, plus the `_DEAD_CONTAINER_LOG_LINES` constant beside
        # the helper) added 24 more lines in the same region, above every
        # entry from this one on: 4656->4680.
        #
        # Task 032 (v0.21, requirement F.6, #159/#194) added the engine-
        # version line to `cmd_status`'s image-record render (+5), above
        # every entry from this one on: 4742->4747, read off the numbered
        # line (task 035/077).
        "line": "4747",
        "code": "home = Path(os.environ.get(\"HOME\") or os.path.expanduser(\"~\"))",
        "reason": ("`ralphctl skills install`'s `home` used to auto-detect a "
                   "default target dir (`_auto_detect_skills_install_target`) "
                   "when the operator passes no `--target` -- the CLI IS the "
                   "operator's own host process, so their own agent-skills "
                   "directory is exactly the right default, same trust "
                   "boundary as `registry()`'s RALPHD_REGISTRY fallback above."),
    },
    {
        "path": "cli/main.py",
        # Task 004 (v0.21, PRD requirement A.4, #192): +42 from the same
        # `_format_gate_rejection_ending_lines` helper and import-block
        # shift as the entry above, plus +7 from the status-render wiring
        # in `cmd_status` (also above this entry): 4772->4821.
        # Task 017 (v0.21, PRD requirement D.7, #196): +1, same import-block
        # shift: 4821->4822.
        #
        # Task 021 (v0.21, PRD requirement D.12, #196): same +1 import-block
        # shift as the entry above it: 4822->4823.
        #
        # Task 025 (v0.21, PRD requirement E.3, #183): same +35 as the entry
        # above it: 4823->4858, and the same +24 from that task's own
        # stderr-reading cure: 4858->4882.
        #
        # Task 032 (v0.21, requirement F.6, #159/#194): same +5 as the entry
        # above it: 4944->4949, read off the numbered line (task 035/077).
        "line": "4949",
        "code": "aws = Path.home() / \".aws\"",
        "reason": ("`ralphctl llm test host`'s own resolution path (`_llm_test_"
                   "resolve`, its docstring: \"this runs entirely on the host, "
                   "same trust level as start\") -- deliberately mirrors the "
                   "`--llm host` staging above so `llm test` previews exactly "
                   "what `start --llm host` would actually copy."),
    },
    {
        "path": "cli/main.py",
        # Task 004 (v0.21, PRD requirement A.4, #192): same +49 as the
        # entry just above it: 4776->4825.
        # Task 017 (v0.21, PRD requirement D.7, #196): +1, same import-block
        # shift: 4825->4826.
        #
        # Task 021 (v0.21, PRD requirement D.12, #196): same +1 import-block
        # shift as the entry above it: 4826->4827.
        #
        # Task 025 (v0.21, PRD requirement E.3, #183): same +35 as the entry
        # above it: 4827->4862, then the same +24 cure shift: 4862->4886.
        #
        # Task 032 (v0.21, requirement F.6, #159/#194): same +5 as the entry
        # above it: 4948->4953, read off the numbered line (task 035/077).
        "line": "4953",
        "code": "models_json = Path.home() / \".pi\" / \"agent\" / \"models.json\"",
        "reason": ("same `_llm_test_resolve` host path, the `models.json` half "
                   "-- same reasoning as the `.aws` mount just above it."),
    },
    {
        "path": "cli/main.py",
        # v0.20.2 task 011 (requirement B.3, #197) added
        # `_run_notifier_configuration()` and its `NOTIFIER_UNCONFIGURED_
        # RUNS_HEADING` constant, and wired its result into `cmd_doctor`,
        # all above every entry from this one on -- a net +40, shifting
        # each of them by +40: 6598->6638, 6602->6642. That task's cure
        # (per-run notifier state, `_run_notifier_url_configured()`) added
        # 33 more lines in the same region: 6638->6671, 6642->6675. Same
        # reads, same reasons.
        # Task 004 (v0.21, PRD requirement A.4, #192): same +49 (import
        # block +3, `_format_gate_rejection_ending_lines` +39, status-render
        # wiring +7) as the two entries above: 6671->6720.
        # Task 012 (v0.21, PRD requirement D.2, #184) added a `note:` line
        # print block to `cmd_steer_list` above every entry from this one
        # on -- a net +9, shifting each of them by +9: 6720->6729,
        # 6724->6733.
        # Task 017 (v0.21, PRD requirement D.7, #196) added `--wave`'s
        # argparse option, its `cmd_amend`/`cmd_amend_list` docstring and
        # body changes and the `AMENDMENT_WAVE_TARGET` import, all above
        # every entry from this one on -- a net +62, shifting each of them
        # by +62: 6729->6791, 6733->6795.
        # Task 020 (v0.21, PRD requirement D.11, #196) added the STATE
        # column's `_amend_orphaned_task_ids`/`_amend_entry_is_orphaned`
        # helpers and `cmd_amend_list`'s own body changes, all above every
        # entry from this one on -- a net +44, shifting each of them by
        # +44: 6791->6835, 6795->6839.
        #
        # Task 021 (v0.21, PRD requirement D.12, #196) added the
        # `repoint_amendment` import (+1, same as every entry above) and
        # `cmd_amend`'s new `--repoint`/`--to` docstring+logic (+48), both
        # above every entry from this one on -- net +49: 6835->6884,
        # 6839->6888. The new argparse flags themselves sit further down,
        # near the end of `build_parser()`, well below every entry here, so
        # they add no further shift.
        #
        # Task 025 (v0.21, PRD requirement E.3, #183) added `_report_dead_
        # container_before_bind()` and its two `cmd_start` call sites, both
        # well above every entry here -- net +35, shifting each of these
        # two by +35: 6884->6919, 6888->6923. That task's own cure (the
        # `docker logs` stderr read plus `_DEAD_CONTAINER_LOG_LINES`) added
        # 24 more in the same region, above both: 6919->6943, 6923->6947.
        #
        # Task 032 (v0.21, requirement F.6, #159/#194) added the engine-
        # version line to `cmd_status`'s image-record render (+5), above
        # both: 7005->7010, 7009->7014, each read off the numbered line
        # (task 035/077).
        "line": "7010",
        "code": "checks[\"pi_host_config\"] = (Path.home() / \".pi\" / \"agent\" / \"settings.json\").exists()",
        "reason": ("`ralphctl doctor`'s pi_host_config check: sanity-checks that "
                   "the OPERATOR's own `~/.pi/agent/settings.json` exists before "
                   "they run `--llm host`, on the same machine `doctor` itself "
                   "runs on -- there is no other caller to take this from."),
    },
    {
        "path": "cli/main.py",
        # Task 004 (v0.21, PRD requirement A.4, #192): same +49 as the
        # entry just above it: 6675->6724.
        # Task 012 (v0.21, PRD requirement D.2, #184): same +9 as the entry
        # just above it: 6724->6733.
        # Task 017 (v0.21, PRD requirement D.7, #196): same +62 as the entry
        # just above it: 6733->6795.
        # Task 020 (v0.21, PRD requirement D.11, #196): same +44 as the
        # entry just above it: 6795->6839.
        #
        # Task 021 (v0.21, PRD requirement D.12, #196): same +49 as the
        # entry just above it: 6839->6888.
        #
        # Task 025 (v0.21, PRD requirement E.3, #183): same +35 as the entry
        # above it: 6888->6923, then the same +24 cure shift: 6923->6947.
        #
        # Task 032 (v0.21, requirement F.6, #159/#194): same +5 as the entry
        # above it: 7009->7014, read off the numbered line (task 035/077).
        "line": "7014",
        "code": "operator_skill = _operator_skill_status(Path.home())",
        "reason": ("same `cmd_doctor`, the operator-skill line (task 023, "
                   "requirement R): `_operator_skill_status` reports on "
                   "whichever agent-skills layout is discoverable under the "
                   "OPERATOR's own home, on the same host `doctor` itself "
                   "runs on -- report-only, same reasoning as pi_host_config "
                   "just above it."),
    },
    {
        "path": "cli/ui_server.py",
        # Was line 1463 when this entry was written; this wave's hub work
        # above it (task 007, #155: forwarding the run's control token from
        # the steering form and the retry button) shifted the class-level
        # default -- now at 1477, same read, same reason. Task 047 (#177,
        # the `_with_llm_headers` helper) shifted it again, +32: 1477->1509.
        # Task 033 (v0.21, requirement G.1, #159/#194) added the
        # `GET /api/version` route's own module-level version helper above
        # this class (+1 line above the read): 1509->1510, read off the
        # numbered line (task 035/077). Task 039 (I.1)'s task-transition-
        # history read/render code landed above this class too, unanchored
        # at the time (the fast-lane cure for it is this same task, 040):
        # +79: 1510->1589, read off the numbered line (`sed -n '1589p'`).
        "line": "1589",
        "code": "registry: Path = Path.home() / \".ralphd\"",
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
        # Was line 76 (the read inline inside `ambient_home()` itself) when
        # this entry was written. Task 003 (requirement B.3, notify-
        # preference injection) factored the read out into a private
        # `_home()` helper that `ambient_home()` now calls -- same read,
        # same reason, new line; line numbers drift with unrelated edits,
        # exactly as this module's own docstring warns. Task 009 (v0.20.2,
        # PRD requirement B.1, #197) added two lines to
        # `NOTIFY_PREFERENCE_INSTRUCTION` above this read (naming the new
        # `notification.failed` event), shifting it once more -- now at
        # 130, same helper, same read, same reason. Task 024 (v0.21, PRD
        # requirement E.2, #180) added a `from . import privsep` import
        # line above this read, shifting it once more -- now at 131, same
        # helper, same read, same reason.
        "line": "131",
        "code": "return Path(os.environ.get(\"HOME\") or os.path.expanduser(\"~\"))",
        "reason": ("backs `ambient_home()` (task 002) via its `_home()` "
                   "helper (task 003 refactor): the ONE designated "
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
        "code": "return Path(os.environ.get(\"HOME\") or os.path.expanduser(\"~\"))",
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
        # Was line 33; task 023 (#180, requirement E.1) added a
        # `from . import privsep` import line above OVERLAY_DIR, shifting
        # this read down by one -- same read, same reason.
        "line": "34",
        "code": "str(Path(os.environ.get(\"HOME\") or os.path.expanduser(\"~\")) /",
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
        # Task 041 re-derived every citation in this file from a fresh scan
        # after the B/C/D/E engine work of tasks 006-013 and the cure tasks
        # 035-038 shifted both `loop.py` and `cli/main.py`: this read is now
        # at 978 -- same method, same read, same reason; line numbers drift
        # with unrelated edits, exactly as this module's own docstring warns.
        # Task 007 (approach 02, D.1, notifier wiring) shifted `_creds_note`
        # again -- this read is now at 1021, same method, same read, same
        # reason. Task 007 (v0.19, requirement A.3 prompt half, #155) added
        # `_debt_ledger_context`/`_apply_worker_debt_resolution`/
        # `_workspace_head_sha` above `_creds_note` in this same module,
        # shifting this read again -- it is now at 1092, same method, same
        # read, same reason. This wave's engine work above `_creds_note`
        # (tasks 001-011: the live `unconsumedSteering` patch, the SIGTERM
        # pause-gate wake, the terminal-event ordering and the worker
        # contract's `progress` block) shifted it once more -- now at 1112,
        # same method, same read, same reason. Task 012 (PRD requirement
        # A.2, #156) added `_workspace_fingerprint`/`_progress_claim_delta_
        # size` and the `_progress_external_streak`/`_progress_streak_
        # task_id` __init__ fields above `_creds_note` in this same module,
        # shifting this read once more -- now at 1122, same method, same
        # read, same reason. Task 013 (PRD requirement A.3, #156) added the
        # `_last_progress_claim` __init__ field, the `_progress_budget_for`/
        # `_record_progress_budget_usage` methods and their own
        # `PROGRESS_BUDGET_STATUS_KEY` class attribute above `_creds_note` in
        # this same module, shifting this read once more -- now at 1132,
        # same method, same read, same reason. Task 022 (PRD requirement
        # C.1, #156) added a single `from datetime import datetime,
        # timezone` import line above this read, shifting it once more --
        # now at 1133, same method, same read, same reason.
        # Task 030 (PRD requirement E.1, #167) added the `CURE_PASS_CEILING`
        # module-level constant above `_creds_note` in this same module,
        # shifting this read once more -- now at 1142, same method, same
        # read, same reason. Task 035 (PRD requirement F.1/F.4, #172) added
        # `_approach_progress_context` (48 new lines) above `_creds_note` in
        # this same module, shifting this read once more -- now at 1190,
        # same method, same read, same reason. Task 017 (v0.20.1 requirement
        # E.3, #190/#191) added `_check_workspace_version_drift`/
        # `_read_workspace_version` (65 new lines, commit `d432dec`) above
        # `_creds_note` in this same module, shifting this read once more --
        # now at 1255, same method, same read, same reason (re-derived from
        # `git show <sha>:src/ralphd/engine/loop.py`: 1190 at `ee8d6f7`, 1255
        # from `d432dec` on). Task 009 (v0.20.2, PRD requirement B.1, #197)
        # added lines to the notifier docstrings/comments above `_creds_note`
        # in this same module (the `notification.sent`/`notification.failed`
        # event split), shifting this read once more -- now at 1260, same
        # method, same read, same reason. Task 011 (v0.20.2, PRD requirement
        # B.3, #197) added `_check_notifier_configured()` and its call site
        # above `_creds_note` in this same module, a net +27, shifting this
        # read once more -- now at 1287, same method, same read, same
        # reason. Cure-01-02 (v0.20.2 review-cure, requirement B.3 DoD 9)
        # widened `_check_notifier_configured()`'s own docstring (a net
        # +12) above `_creds_note` in this same module, shifting this read
        # once more -- now at 1299, same method, same read, same reason.
        # Task 002 (v0.21, PRD requirement A.2, #192) widened the
        # `CURE_PASS_CEILING` module-level comment above `_creds_note` in
        # this same module (a net +10, documenting `JobConfig.cure_pass_
        # ceiling` replacing the literal at both rejection sites), shifting
        # this read once more -- now at 1309, same method, same read,
        # same reason.
        # Task 012 (v0.21, PRD requirement D.2, #184) added a `_last_steer_
        # broker` attribute (5 new lines, comment + assignment) above
        # `_creds_note` in this same module, shifting this read once more
        # -- now at 1314, same method, same read, same reason.
        # Task 024 (v0.21, PRD requirement E.2, #180) added a `from . import
        # privsep` import line above `_creds_note` in this same module,
        # shifting this read once more -- now at 1323, same method, same
        # read, same reason.
        # Task 030 (v0.21, PRD requirement F.4, #159/#194) added
        # `_record_engine_version()` and its call site above `_creds_note`
        # in this same module, a net +23, shifting this read once more --
        # now at 1346, same method, same read, same reason.
        # Task 035 (v0.21, PRD requirement G.3, #159/#194) added the
        # `workspaceVersion` persistence -- `_record_workspace_version()`,
        # its call site and the widened `_check_workspace_version_drift`
        # docstring -- above `_creds_note` in this same module, a net +40
        # across its two commits, shifting this read once more -- now at
        # 1386, same method, same read, same reason (read off the numbered
        # line with `sed -n 1386p`, never by arithmetic).
        "line": "1386",
        "code": "home = Path(os.environ.get(\"HOME\") or os.path.expanduser(\"~\"))",
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
