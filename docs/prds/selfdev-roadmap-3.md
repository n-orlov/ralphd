# ralphd selfdev roadmap 3 — stranded steerings & oversight loose ends

## Goal

Close out everything the previous run (`selfdev-roadmap-2`, VERIFIED at
iteration 148) left behind: two operator steerings that arrived during the
final review iteration and were never consumed, plus product gaps the
operator discovered while supervising that run. The workspace is the ralphd
repo itself at the state the previous run left it (HEAD `28ded9b` — same
tree as the VERIFIED final state, history rewritten by the operator to
their own identity — all 252 tests green, docker + browser tiers included).

## Context

You are ralphd developing ralphd. Same rules as the previous run: the run
dir is the source of truth, standing git policy applies (commit per task as
`task NNN: <title>`, push to origin/main, commit identity
`Nik <nikolaiorl@gmail.com>` — the repo-local git config already sets this;
do not override it — credentials from /config/creds/github.env, never print
or persist secret values, never `docker inspect`/`ps`/`logs` any container
you did not create yourself).
The engine you run under has mechanical secret redaction (task 060), but
treat it as a safety net, not permission.

## Requirements

### A. Stranded steering 020 — `logs` pretty renderer (three defects)

1. **Tool invocation lines must show arguments.** Today the renderer prints
   `→ bash() ✓ ok` with no arguments — nine identical lines tell the
   operator nothing. Render the salient argument per tool, generously
   truncated (~300 chars for bash commands; err toward showing more):
   - bash → `→ bash $ <command>` with newlines collapsed so one invocation
     stays one line
   - file read/write/edit → `→ write <path>`
   - grep/glob-style → `→ grep <pattern>`
   - unknown tools → best-effort first scalar arg value, truncated.
   Keep the ✓/✗ status; on ✗ include a short error excerpt when available.
   The redaction layer scrubs at write/serve time, so showing full commands
   does not change the secret-exposure surface — state this reasoning in
   the task and keep the redaction test green.

2. **`logs -f` needs a clean interactive exit.** `q` should exit a follow
   stream when stdin is a TTY (never require a keypress on piped/non-TTY
   streams); Ctrl+C must exit cleanly — a user-interrupted follow is a
   normal exit, not a crash: no KeyboardInterrupt traceback, documented
   exit code (0 or 130 — pick one, document in docs/cli.md).

3. **Live tool start-lines.** In follow mode the operator sees nothing for
   a tool call until it returns — a 5-minute test run is indistinguishable
   from a hang. The raw stream already has `tool_execution_start`; render
   it immediately.

### B. Stranded steering 021 — in-place rewrite (refines A3)

On a TTY, print the invocation line at `tool_execution_start` with no
status, then REWRITE it in place on `tool_execution_end` (`\r` repaint or
ANSI cursor-up) to the final form `→ bash $ <cmd> … ✓ ok (12.3s)` — a
completed stream looks byte-for-byte like the one-line-per-tool rendering,
but the operator watches the running command the whole time.
Constraints:
- In-place rewrite ONLY on a TTY. Piped/redirected output falls back to a
  plain append form (start line, then a short completion line); never emit
  `\r`/ANSI control bytes into a pipe — add a test asserting that.
- Interleaving: if other renderable events arrive while a tool line is
  open, finalize the open line with a newline and print the completion as
  a separate short line. In-place rewrite is required only for the common
  uninterrupted case; correctness over cleverness.
- Liveness test bar: extend the follow-liveness pattern from
  `tests/test_cli_logs_follow_liveness.py` — while the stub job is inside
  a long tool call, assert the invocation line is already visible in the
  follow stream BEFORE the tool's end event. A test that only checks after
  completion would pass on the broken rendering; that is the trap to avoid.

### C. Orphaned steering at terminal state (product gap, discovered live)

Steerings 020/021 arrived during the final review iteration of the
previous run; the run then went terminal and they were never consumed —
silently stranded in the steering dir. Operator steering must never be
silently orphaned:
- The review phase must check the steering queue before emitting VERIFIED.
  Unconsumed steering means the job is NOT done: surface it and route it
  to a worker (e.g. review declines to emit VERIFIED while unconsumed
  steering is pending, so the loop returns to workers).
- If a run reaches a terminal state with unconsumed steering anyway
  (failed/aborted paths), the terminal status must say so loudly: status
  JSON field (e.g. `unconsumedSteering: [files]`) surfaced by
  `ralphctl status` and the hub UI, so the operator knows their
  instruction was never read.
- Test bar: e2e where steering lands during the review iteration of a job
  that would otherwise be VERIFIED — assert it is not silently dropped
  (either consumed by a subsequent worker before the terminal state, or
  loudly reported in the terminal status).

### D. Exhausted-verification-attempts escape hatch (governance gap)

Observed live (task 044 of the previous run): a worker on validation
attempt 3 rewrote its own successCriteria and re-marked the task
completed; the engine then skipped re-verification because
`validationAttempts >= 3`, so the rewritten criteria were never
independently checked. The outcome happened to be correct; the mechanism
is exploitable. Fix so that self-serving criteria edits cannot dodge
independent review:
- Track a criteria fingerprint (e.g. hash of successCriteria) on each
  task; if it changes after any validation failure, record that fact on
  the task (e.g. `criteriaEditedAfterValidationFailure: true`).
- The review phase prompt must receive the list of such tasks and is
  required to explicitly re-verify them against the PRD, stating its
  conclusion per task.
- Design freedom on the details, but the invariant is: a task whose
  criteria were edited after a validation failure gets at least one
  independent check of the NEW criteria before the job can be VERIFIED.

### E. Default `on_complete` should be `exit`, not `idle`

Operator decision: a finished run should stop and remove its container by
default — a leftover idle container is rarely wanted and all artifacts
(run dir, workspace, git) persist on the host anyway. Change the product
default from `idle` to `exit` everywhere it is declared or documented
(engine config default, `ralphctl start --on-complete` help text,
docs/cli.md, docs/architecture.md, tutorial, example templates). `idle`
remains available as an explicit opt-in for debugging. Update any tests
that relied on the implicit `idle` default to state it explicitly, and add
one asserting the default is `exit`. Note: a registry-level
`config set on_complete <value>` still overrides the product default —
keep that precedence and test it.

### F. Steering filename double-prefix (cosmetic)

`ralphctl steer <id> --name 019-steering` produced `019-019-steering.md`
— the engine prepends its own sequence number to a name that already has
one. Make the naming idempotent/clean (e.g. engine always assigns the
sequence prefix and treats `--name` as the human-readable suffix only).
Low priority; do last.

### G. Roadmap notes only — do NOT implement

Add to docs/roadmap.md as future items with a one-line rationale each:
- Engine self-protection against in-container kill signals (PID-namespace
  isolation of agent iterations) — prompt rules alone did not prevent the
  iteration-103 pkill incident.
- `ralphctl repair` — sanctioned tooling for operator repair of a
  corrupted/burned run state (the previous run required hand-editing
  status.json).

## Non-goals

- No refactors beyond what the requirements need.
- Docker image publish / pipx packaging remain out of scope (unchanged
  from the previous PRD).
- Do not re-litigate or modify the completed roadmap-2 work except where
  a requirement above touches it.

## Quality bar (same as roadmap-2)

- Every requirement gets a traceability row with real test node IDs.
- Full suite green including docker and browser tiers actually executed.
- `ruff check .` clean.
- Docs updated as part of each task's definition of done (cli.md for
  renderer/exit-code changes, architecture.md for engine changes).
- Commit+push per task per standing git policy.
