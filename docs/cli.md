# ralphctl — CLI Reference

`ralphctl` is the single entry point for operating ralphd jobs. It is designed to be
driven by a **human at a terminal or by an AI agent** — hence:

- every command supports `--json` for machine-readable output (stable schema);
  default output is human-oriented tables/text
- exit codes are meaningful and documented per command
- no command is interactive unless explicitly marked; prompts are suppressed with
  `--yes` or when stdout is not a TTY
- all state is derivable from `~/.ralphd/` + the container APIs; there is no hidden
  session state

Install: `pipx install ralphctl` (or `uvx ralphctl …`). Requires a working
`docker` CLI (or `podman` with `RALPHD_DOCKER=podman`).

## Quick start

```bash
# Run a job against an existing checkout, forwarding the host's LLM config
ralphctl start --prd ./feature.md --workspace ~/src/widget --llm host

# Watch it
ralphctl watch brisk-otter-1408

# Nudge it
ralphctl steer brisk-otter-1408 "Skip the docs task; focus on tests"

# Collect results when done (container exits by default; add --on-complete idle to keep it up for debugging)
ralphctl status brisk-otter-1408
ralphctl artifacts pull brisk-otter-1408 ./out/
ralphctl stop brisk-otter-1408
```

## Global flags

| Flag | Meaning |
|------|---------|
| `--json` | machine-readable output on stdout, logs to stderr |
| `--quiet` | suppress non-essential output |
| `--registry <dir>` | override `~/.ralphd` |
| `--yes` | assume yes on confirmations |

Global exit codes: `0` success · `1` generic error · `2` usage error ·
`3` run not found · `4` container/API unreachable · `5` operation invalid in
current job state · `130` follow interrupted by Ctrl+C (see `logs -f` below).

## Commands

### `ralphctl start`

Create and launch a job container. Prints the run ID (and with `--json`: run ID,
container ID, API URL, token presence).

```
ralphctl start --prd <file|-> [options]
```

| Option | Default | Meaning |
|--------|---------|---------|
| `--prd <file\|->` | required* | PRD markdown (`-` = stdin); *not required if `--template` supplies a `prd.md` skeleton |
| `--template <name>` | none | load job defaults + optional `prd.md`/skills/creds from `<registry>/templates/<name>/` (see below); any explicit flag on this command overrides the template's value |
| `--workspace <dir>[:name]` | none | bind-mount an existing checkout at `/workspace` (a bare `<dir>` with no `:name`); without any `--workspace` the agent clones PRD-listed repos into the run dir. Repeatable for multi-repo jobs (PRD req 27): every entry beyond the first must carry a `:name`, mounted at `/workspace/<name>` instead (e.g. `--workspace ~/src/api:api --workspace ~/src/web:web`); prompts list every mounted workspace name/path so the agent doesn't have to guess |
| `--run-id <id>` | generated | explicit run ID |
| `--iterations <n>` | 25 | shared iteration budget |
| `--max-approaches <n>` | 3 | review-loop approach limit |
| `--vigilant` | off | per-task verification |
| `--reflect` | off | run one extra `reflect` iteration after the job reaches a terminal state, writing a report + suggested prompt/skill diff to `artifacts/reflection/` (never touches the workspace or run state) |
| `--model <id>` | profile default | default model (pi model ID) |
| `--model-strategy <s>` | quality-first | `quality-first\|cost-optimized\|balanced\|custom` |
| `--model-<phase> <id>` | — | per-phase override (`planning\|worker\|review\|verify`) |
| `--llm <profile>` | `host` | LLM profile ([llm-profiles.md](llm-profiles.md)); falls back to the registry's `default_llm_profile` (`ralphctl config`) if set
| `--llm-env KEY=VAL` | — | ad-hoc env additions to the LLM config (repeatable) |
| `--forward-env NAME\|PREFIX_*` | — | forward host env var(s) into the container, by exact name or prefix glob (repeatable). Required for any non-standard vars — see [llm-profiles.md](llm-profiles.md) |
| `--skills <dir>` | — | mount a skills directory (repeatable) |
| `--creds <dir>` | — | mount a credentials directory (see below) |
| `--allow-docker` | off | mount the host docker socket into the job container — **root-equivalent host access**, see below |
| `--prompt-override <dir>` | — | phase-prompt override directory |
| `--image <ref>` | bundled default | alternative/derived engine image; falls back to the registry's `image` (`ralphctl config`) if set |
| `--on-complete idle\|exit` | exit | post-completion behavior; `idle` is an explicit debugging opt-in; falls back to the registry's `on_complete` (`ralphctl config`) if set |
| `--on-complete-cmd <cmd>` | — | shell command run once by the engine (in-container) on reaching a terminal state; receives `RALPHD_RUN_ID`/`RALPHD_STATE`/`RALPHD_VERDICT` env vars; failures are logged (`events.jsonl`, `level: error`) but never affect the job's verdict or the engine's exit code |
| `--timeout <dur>` | 8h | job wall-clock limit (`45m`, `8h`, `2d`) |
| `--iteration-timeout <dur>` | 45m | per-iteration limit |
| `--port <n>` | auto | host port for the API |
| `--api-bind <addr>` | 127.0.0.1 | host interface to publish on |
| `--api-token <t\|auto>` | none | require bearer auth (`auto` generates + stores) |
| `--env KEY=VAL` | — | extra container env (repeatable) |
| `--detach/--no-detach` | detach | `--no-detach` streams events until completion, exit code mirrors job verdict (0 verified / 1 otherwise) |

Credentials (`--creds <dir>`) — **env-file convention**: prepare every credential
set the job needs as a `<name>.env` file (`KEY=value` lines) in one directory:

```
creds/
├── github.env        # GITHUB_TOKEN=ghp_…
├── jenkins.env       # JENKINS_URL=… JENKINS_USER=… JENKINS_TOKEN=…
└── sonarqube.env     # SONAR_TOKEN=…
```

These land at `~/.creds/*.env` (mode 0600) inside the container, and the phase
prompts tell the agent which files exist and to source the one it needs
(`set -a; . ~/.creds/github.env; set +a`) — values are never auto-exported into
every process. Recognized extras (`gitconfig`, `git-credentials`, `netrc`,
`ssh/`) are placed conventionally; an executable `setup.sh` runs once before the
first iteration. Nothing from this directory is copied into the run dir or logged.

Skills (`--skills <dir>`, repeatable): one directory per skill (must contain
`SKILL.md`). If the given dir has no `SKILL.md` but every immediate child does,
it expands to the children — so both `--skills ./skills/git` and
`--skills ./skills` (a folder of skills) work; anything else is a usage error.
Skills are copied at start (later host edits don't affect the running job) and
scoped per job — there is deliberately no "forward all host skills" mode.

Job templates (`--template <name>`, PRD req 25): a directory
`<registry>/templates/<name>/` (e.g. `~/.ralphd/templates/<name>/`, or under
`RALPHD_REGISTRY` if set) may contain:

```
templates/<name>/
├── job.yaml     # optional: scalar job defaults (see below)
├── prd.md       # optional: PRD skeleton, used when --prd is omitted
├── creds/       # optional: default --creds directory (same env-file convention)
└── <skill-dir>/ # optional: default skill(s), referenced by job.yaml's `skills:` list
```

`job.yaml` may set any of: `iterations`, `max_approaches`, `vigilant`,
`reflect`, `on_complete`, `on_complete_cmd`, `timeout`, `iteration_timeout`, `model_strategy`,
`llm`, `model`, `fast_model`, `thinking`, `skills` (a list of directory names
relative to the template dir), `creds` (a directory name relative to the
template dir), and `prd` (a filename relative to the template dir, default
`prd.md`). Every one of these has a corresponding `start` flag; **an explicit
flag on the command line always overrides the template's value** for that
field (`--skills`/`--creds` override wholesale, not merge). For `image`,
`on_complete`, and `llm`, fields the template doesn't set fall back next to
any matching `ralphctl config` registry default (`image`/`on_complete`/
`default_llm_profile`), and only then to the hardcoded default; every other
field falls straight back to its hardcoded default. An unknown `--template`
name exits `3` naming the expected path; a malformed `job.yaml` (not a
mapping) exits `2`.

Docker siblings (`--allow-docker`) — default **off**. Mounts the host docker
socket (default `/var/run/docker.sock`; override with `RALPHD_DOCKER_SOCK`)
and adds the socket's group to the container so the agent can run
`docker build` / `docker run`. **Warning: the docker socket is root-equivalent
access to the host** — the job can mount any host path and start privileged
containers; grant it only to PRDs you trust as much as your own shell. ralphctl
prints this warning to stderr at launch. Sibling rules the agent is told (and
you should know):

- Containers the job starts are **siblings on the host daemon** — their `-v`
  paths are *host* paths. ralphctl injects `RALPHD_HOST_WORKSPACE`,
  `RALPHD_HOST_RUN_DIR`, and `RALPHD_RUN_ID` so the agent can mount the right
  dirs; container-local paths mount empty and can litter root-owned dirs on
  the host.
- Siblings should carry `--label ralphd.run=<run-id>` (the job container
  always does) — `ralphctl stop`/`rm` reap everything with that label,
  best-effort. `ralphctl doctor` lists stray labeled containers whose run no
  longer exists.
- Prefer `--rm` for short-lived siblings; detached unlabeled containers,
  built images, and volumes outlive the job.

Exit: `0` started · `1` container failed to start · `2` bad options (including
a missing/invalid docker socket with `--allow-docker`).

### `ralphctl runs`

List all runs (live and historical) from the registry.

```
ralphctl runs [--state running|succeeded|failed|aborted] [--limit n]
```

Columns: run ID, state, verdict, phase, iterations used/budget, started, workspace.
`--json` emits the merged `status.json` array.

### `ralphctl status <run-id>`

Full status (mirrors `GET /status`; falls back to the run dir's `status.json` when
the container is gone — indicated by `"live": false` in `--json` mode).

Human output includes a `duration:` line: while the job is still running this
is the **elapsed-so-far** time since `startedAt` (labeled `(elapsed)`); once
the job has reached a terminal state it is the **total run time** from
`startedAt` to `endedAt` (labeled `(total)`). While an iteration is in flight,
an additional `iteration elapsed:` line shows that iteration's own elapsed
time. Both are rendered in a compact human format (`45s`, `3h 12m`, `2d 1h` —
no millisecond noise) via one shared formatting helper used everywhere
durations are shown.

`--json` adds machine-usable duration fields alongside the existing timestamp
fields (nothing existing is removed or renamed): a top-level `durationSeconds`
(elapsed-so-far or total, numeric seconds, same rule as the human line above),
and, when `currentIteration` is present, an `elapsedSeconds` field nested
inside it for that iteration's own elapsed time.

A terminal run (`succeeded`/`failed`/`aborted`) also carries an
`unconsumedSteering` field: a list of steering filenames that were still
pending when the run went terminal (task 006 -- empty in the common,
fully-consumed case, since a terminal run never reads pending steering
again). The human output does not bury this in the JSON: if the list is
non-empty, `ralphctl status` prints an extra `!! UNCONSUMED STEERING: ...`
line (bold red on a TTY) naming the stranded file(s), so an operator running
the plain human command still notices without having to remember `--json`.
The hub run-detail view shows the same fact as a `.steering-warning` banner
on the run summary card.

### `ralphctl watch <run-id>`

Live TUI: task table, phase/approach/iteration header, budget + cost gauges,
scrolling tail of agent output, pending steering. Read-only; `q` quits.
Non-TTY/`--json`: streams SSE events as NDJSON instead (usable by agents).

### `ralphctl logs <run-id> [-N[f]]` / `ralphctl logsf <run-id>`

The **whole-job console** (backed by `GET /logs`): all iterations merged in
order, following across iteration boundaries — the Jenkins-console equivalent.
**Pretty rendering is the default**; `--raw` gives the underlying NDJSON.

Syntax matches `tail`:

```
ralphctl logs <id>            # last 50 rendered lines
ralphctl logs <id> -100       # last 100 lines
ralphctl logs <id> -150f      # last 150 lines, then follow live
ralphctl logs <id> -f         # unbounded backlog, then follow live
ralphctl logsf <id>           # alias for logs -f
```

| Option | Meaning |
|--------|---------|
| `-N` / `-Nf` | tail N lines / tail N then follow (tail-style) |
| `-f`, `--follow` | follow live across iterations until the job ends |
| `--raw` | raw NDJSON passthrough (implies no rendering; for machines) |
| `--iteration n` | restrict to a single iteration's transcript |

With `--follow`, `ralphctl` reads and renders/prints each line off the open
connection as it arrives (both `--raw` and pretty modes) — it does not wait
for the underlying HTTP response to close, which for a running job only
happens once the job itself terminates. `ralphctl watch` streams the same
way (it never buffered). Redirect to a file/pager as usual if you want to
capture the live output while still watching it (e.g. `ralphctl logs <id> -f
| tee out.ndjson`).

Interactive exit from `-f`/`--follow` (task 002):
- On a TTY, press **`q`** to stop following and return — no error, no
  extra output, exit code `0`. This never touches stdin on a non-TTY
  stdin (piped/redirected): a piped `logs -f` is never blocked waiting
  for a key that will never arrive. (The key-watcher thread closes the
  open HTTP response to unblock the main thread's blocking read; the
  main thread's exception handling around that read treats the resulting
  connection-closed error — including a bare `AttributeError` from
  Python's chunked-transfer decoder if the close lands mid-chunk — as
  the expected, non-error `q` outcome, never a traceback.)
- **Ctrl+C** (SIGINT) during a follow always exits cleanly — no
  Python traceback on stderr — at the single documented exit code
  **`130`** (the standard `128+SIGINT` shell convention), whether or not
  stdin is a TTY.
- **SIGTERM** (e.g. a plain `kill <pid>`) during a follow is handled the
  same way: no traceback, terminal left exactly as it was found, exit
  code `128+SIGTERM` (task 016).

Terminal-mode ownership (task 016): on a TTY, `logs -f` puts stdin into
cbreak mode (no echo, single-keypress reads, so `q` doesn't need Enter)
for the duration of the follow, and restores the terminal's prior mode on
*every* exit path — normal completion, `q`, Ctrl+C, SIGTERM, or any other
exception — from a context manager that wraps the whole follow loop in
the main thread. (An earlier version of this saved/restored termios state
from the background key-watcher thread instead; that thread can be torn
down by a main-thread `KeyboardInterrupt` before its own cleanup runs,
which could strand the terminal in no-echo mode after Ctrl+C. Restoration
now happens from code guaranteed to run to completion around the follow,
not from the thread that merely reads keys.)

Pretty rendering shows: iteration/phase boundary headers (number, phase, model),
assistant text as it streams, tool calls as compact one-liners, thinking
elided to a marker, per-iteration usage/cost footer, agent errors
highlighted. When stdout is not a TTY, output is identical minus color.

Each tool one-liner shows the tool's salient argument (task 001), not just
its name -- generously truncated (~300 chars) so the operator can actually
tell one `bash` call from another instead of seeing nine identical
`bash() ✓ ok` lines:
- `bash` → `→ bash $ <command>` -- newlines in the command are collapsed so
  one invocation always stays one line.
- `read`/`write`/`edit` → `→ <tool> <path>`.
- `grep`/`glob`/`find`-style tools → `→ <tool> <pattern>`.
- any other/unknown tool → `→ <tool> <first scalar argument value>`
  (best-effort; whatever the first plain string/number/bool argument is).
The `✓ ok` / `✗ error` outcome is unchanged; on `✗` a short excerpt of the
tool's result is shown when one can be extracted, same as on success
(errors get a bit more room -- ~120 chars -- than success excerpts,
~60 chars, since the detail matters more when something failed).
A plain-string result is used verbatim (as before). A STRUCTURED
(non-string) result -- e.g. the standard
`{"content": [{"type": "text", "text": ...}]}` shape tool results
commonly use, including structured error payloads under an `error`/
`detail` key -- is walked for its first non-empty `text` item to produce
the same short excerpt (task 015). An unrecognized/unknown structured
shape yields NO excerpt at all: this deliberately never falls back to
stringifying or JSON-dumping the whole result object, which would dump
arbitrary structured noise into the pretty renderer instead of a short,
readable line.
This does not change what secrets can leak: redaction
(`src/ralphd/engine/redact.py`) scrubs known secret values out of the
transcript at write/serve time, upstream of rendering, so a full `bash`
command or file path shown here is exactly what a `--raw` reader already
sees -- rendering it is not a new exposure surface (see
tests/test_secret_redaction.py, which asserts this stays true).

**Live tool start-lines (task 003) and in-place TTY rewrite (task 004).**
In a `-f`/`--follow` (live) session the invocation line above prints the
MOMENT the tool call starts (`→ bash $ <command>`, no outcome yet) rather
than only once it finishes -- a long-running tool (e.g. a multi-minute
`bash` command) is no longer silent for its whole duration.

- **On a TTY**, that invocation line is left open with no trailing
  newline (the cursor sits right after it), and once the matching
  `tool_execution_end` arrives the SAME line is rewritten in place
  (`\r` + ANSI erase-to-end-of-line, then the full text again) into the
  final `→ bash $ <cmd> ✓ ok (<excerpt>)` form -- so a finished TTY
  follow session ends up byte-for-byte identical (once those control
  bytes are accounted for) to the buffered one-line-per-tool rendering.
  If any other renderable event (streamed text, a new iteration boundary,
  another tool call) arrives before the outcome does, the open line is
  finalized with a plain newline first and the eventual outcome instead
  prints as its own short completion line (`↳ ✓ ok`), never mid-line.
- **On a non-TTY (piped) stream** there is no cursor to rewind: the
  invocation prints as a plain, complete line immediately, and the
  outcome later prints as its own short completion line (`↳ ✓ ok`,
  plus error excerpt on `✗`) -- piped `logs -f` output never contains a
  `\r` or an ANSI control byte.

In non-follow (buffered) rendering -- `ralphctl logs <id>` without
`--follow`, where the whole transcript is already in hand -- a completed
tool call still renders as exactly the single one-liner it always has;
only a call with no matching end yet (still running when the transcript
was fetched) would show just the invocation line with no completion.
Once an iteration has ended, its "done" summary line includes a `took <duration>`
field (same compact human format as `status`, computed from that iteration's
`startedAt`/`endedAt`); an iteration still in flight has no "done" line yet
(it only appears once the iteration boundary's `end` event exists), so there is
nothing to omit or fake an in-progress duration for. `--raw` mode is unchanged
by this: the underlying `ralphd.iteration` boundary events simply carry the raw
`startedAt`/(on `end`) `endedAt` fields the pretty renderer derives the duration
from.

**`-N` means N RENDERED lines in pretty mode, N raw events in `--raw` mode.**
The engine's `GET /logs?tail=N` (and `GET /iterations/{n}/output?tail=N`)
always trims RAW NDJSON events — that's the wire contract and it never
changes. In pretty mode a raw `tail=N` would be the wrong thing to hand the
renderer: the renderer collapses/skips many raw event types (e.g. a whole
burst of `text_delta`/`toolcall_delta` events becomes one streamed-text
block or one tool one-liner), so trimming raw events *before* rendering
produces a wildly variable, much-smaller-than-N number of *visible* lines.
So the trim is owned by different layers depending on mode:
- `--raw`: the **engine** trims (raw `tail=N`, 1 raw line == 1 line of
  output, unchanged from the wire contract).
- pretty (default): **`ralphctl` itself** trims. It always fetches the full
  untailed transcript from the engine, renders every line, and only then
  keeps the last N *rendered* lines — so `logs <id> -100` always means
  "the last 100 lines you'd actually see", full stop.
- Iteration/phase boundary headers (`── iteration N · ... ──` / the "done"
  summary line) count toward N like any other rendered line — they are not
  given special exemption.
- `-Nf`/`logsf -N` (follow + pretty with a tail): shows exactly N rendered
  lines of backlog first, then keeps following live as normal — the
  backlog fetch/render/trim happens the same way as the non-follow case,
  then a live connection picks up from there without re-showing or
  dropping a line.

### `ralphctl tasks <run-id>`

Task table (or full `tasks.json` with `--json`).

### `ralphctl steer <run-id> [message]`

Send steering. Message from arg, `--file <f>`, or stdin.

| Option | Meaning |
|--------|---------|
| `--now` | also SIGINT the current iteration so guidance applies immediately |
| `--name <slug>` | steering file slug |

The on-disk filename is always `NNN-<slug>.md`, where `NNN` is an
engine-assigned monotonic sequence (never supplied by the caller). If
`--name` already carries its own `NNN-` prefix (e.g. copy-pasted from a
prior steering filename), that prefix is stripped before appending the
engine's own, so the result is never doubled (e.g. `--name 019-steering`
does not yield `022-019-steering.md`).

Exit `0` accepted · `5` job already finished.

### `ralphctl interrupt <run-id>`

SIGINT the current iteration without adding steering.

### `ralphctl pause <run-id>` / `ralphctl unpause <run-id>`

Hold/release the loop at the next iteration boundary.

### `ralphctl abort <run-id> [--reason <text>]`

Terminate the job (state `aborted`, honors on-complete mode).

### `ralphctl stop <run-id>`

Shut down an **idle finished** container (calls `/shutdown`, then `docker rm`).
For a running job, refuses with exit `5` — use `abort` first, or `--force` to
abort+stop in one step. Run dir is never deleted by `stop`.

### `ralphctl rm <run-id>`

Delete a run's registry dir (history, artifacts, workspace-if-internal). Requires
the container to be gone. Asks confirmation unless `--yes`.

### `ralphctl artifacts <run-id> [ls|pull <dest>]`

List or download artifacts. `pull` copies from the (host-mounted) run dir directly;
works with dead containers.

### `ralphctl skills <run-id> [ls|get <name> <dest>|add <dir>|rm <name>]`

Inspect or hot-swap skills on a running job (API-backed; `add` tars and uploads,
`get` downloads one back). Changes take effect next iteration.

```
ralphctl skills <run-id> ls                 # name, origin (mounted/api), file count
ralphctl skills <run-id> get <name> <dest>  # write the skill dir to <dest>
ralphctl skills <run-id> add <dir>          # <dir> must contain SKILL.md; uploaded as <dir>'s basename
ralphctl skills <run-id> rm <name>
```

`add` validates locally that `<dir>/SKILL.md` exists before tarring/uploading
(exit `2` otherwise, naming the dir); the skill's name is `<dir>`'s basename.
`--json` on `ls` emits the raw `GET /config/skills` list. Exit `3` if `<run-id>`
has no run dir at all; exit `4` if the run exists but its container/API is
unreachable.

### `ralphctl creds <run-id> [ls|get <name>|add <file>.env|rm <name>]`

Runtime credential management (API-backed, env-file convention).

```
ralphctl creds <run-id> ls                # name, size, mtime -- never values
ralphctl creds <run-id> get <name>        # prints the file contents to stdout
ralphctl creds <run-id> add <file>.env    # uploads/replaces; name is <file>'s stem
ralphctl creds <run-id> rm <name>
```

`add` requires a `*.env` file (exit `2` otherwise, naming the file); the
credential's name is the file's stem (`github.env` -> `github`), and it lands
at `~/.creds/<name>.env` (mode 0600) in the container immediately, no restart
needed. `get` prints the file (read-back is by design — the API token *is*
the cred boundary); `rm` deletes. `--json` on `ls` emits the raw
`GET /config/creds` list (never a `value` field). Exit `3` if `<run-id>` has
no run dir at all; exit `4` if the run exists but its container/API is
unreachable. The agent sees the updated inventory at the next iteration.

### `ralphctl prompts <run-id> [ls|set <phase> <file>]`

Inspect or hot-swap phase prompts.

```
ralphctl prompts <run-id> ls                    # name, effective source: builtin/mounted/api
ralphctl prompts <run-id> set <phase> <file>    # override <phase>'s prompt, effective next iteration
```

`<phase>` must be one of `planning`, `worker`, `review`, `task-verify` (exit
`2` otherwise, naming the valid set, checked locally before any HTTP call);
`<file>` must exist and be non-empty (exit `2` otherwise). `set` uploads the
file's raw text to `PUT /config/prompts/{phase}`, which takes effect the next
time that phase's prompt is built (never retroactive to an in-flight
iteration). `ls` reflects the new source as `api` immediately after `set`.
`--json` on `ls` emits the raw `GET /config/prompts` list. Exit `3` if
`<run-id>` has no run dir at all; exit `4` if the run exists but its
container/API is unreachable.

### `ralphctl llm`

LLM profile management + mid-run rotation:

```
ralphctl llm profiles                 # list profiles (~/.ralphd/llm-profiles)
ralphctl llm show <profile>           # resolved (redacted) view
ralphctl llm test <profile>           # spin up a throwaway container, 1-token ping
ralphctl llm set <run-id> --profile <p>   # rotate a running job's endpoint/key
```

`profiles` lists the two built-ins (`host`, `none`, tagged `(builtin)`) followed
by every `<name>.yaml` under `<registry>/llm-profiles/`, in that order.
`--json` emits `[{"name": ..., "builtin": true|false}, ...]`.

`show <profile>` fully resolves the profile (same resolution `start --llm
<profile>` performs) and prints it with every `env:` value replaced by
`***REDACTED***` and every `pi:` field that came from a
`${env:}`/`${file:}`/`${cmd:}` reference likewise masked -- literal `pi:`
fields (e.g. `baseUrl`) stay visible so the resolved shape is still useful
for diagnosis. `host`/`none` have no file to resolve; `show` reports them as
built-in with nothing to redact. Exit `3` for an unknown profile name (no such
`<name>.yaml`); a profile that fails to *resolve* (unset `${env:}` var,
unreadable `${file:}`, failing `${cmd:}`) exits `1` with the same diagnostic
`start` would show. `--json` emits the full resolved (redacted) document.

`test <profile>` first resolves the profile on the host exactly like `start`
/ `show` (same exit codes: `3` unknown name, `1` unresolvable reference,
unredacted since nothing is printed) -- no docker needed for this part. If
resolution succeeds and a docker daemon answers `docker version`, it follows
up with a real one-token completion in a throwaway container: `docker run
--rm --label ralphd.llm-test=<name> --entrypoint pi <resolved env/mounts>
<image> -p --mode json --no-session [--model <model>]`, piping a one-line
prompt on stdin (entrypoint overridden straight to `pi`, bypassing
`ralphd-engine` entirely -- this never touches a run dir). Exit `1` with the
container's stderr/stdout on a failed ping. When docker isn't reachable (or
`--no-ping` is given), the ping is skipped and resolution success alone is
reported (exit `0`). `--image`/`--model` override the image/model used for
the ping.

### `ralphctl resume <run-id> [--iterations +N]`

Start a fresh container against an existing run dir (PRD req 16). The engine
detects pre-existing `tasks.json`/completed iterations on startup and
continues the job instead of re-planning; `resume` just has to reproduce
`start`'s docker-run wiring for the *same* mounts:

- `<run-dir>` and `<config-dir>` (creds/skills/pi config already staged
  there from the original `start` survive as-is — nothing to re-derive).
- The workspace(s), if the original `start` used `--workspace` (the host
  path(s) are recorded in `host.json` at `start` time — as `workspace` for a
  single unnamed mount, `workspaces` (name→path) for the multi-repo case —
  and reused verbatim; the positional resume command never needs
  `--workspace` itself).
- The recorded `.api-token`, if any (`-e RALPHD_API_TOKEN=...`), so the
  same client-side token keeps working against the new container.
- The `--llm` wiring resolved at `start` time (task 058, operator steering
  018): whichever env vars and extra mounts `start` added on top of the
  base wiring above — `--llm host`'s forwarded `HOST_LLM_ENV` vars
  (`ANTHROPIC_API_KEY`, `AWS_ACCESS_KEY_ID`, ...) and its `~/.aws` mount if
  present, or a named profile's fully-resolved `env:`/`mounts:` (including
  anything that came from a `${env:}`/`${file:}`/`${cmd:}` reference) — are
  persisted at `start` time to `<config-dir>/llm-wiring.json` (mode `0600`,
  never under the run dir proper, never returned by any HTTP route — the
  same at-rest pattern already used for `<config-dir>/pi/models.json` and
  `<run-dir>/.api-token`) and reproduced byte-for-byte by `resume`,
  regardless of what the operator's *current* shell has (or lacks) at
  resume time. A run started before this existed (no `llm-wiring.json` on
  disk) resumes exactly as it did before — no error, just nothing extra to
  reproduce.

`--iterations +10` adds 10 to the existing budget in `job.yaml` before the
container starts (a bare integer, e.g. `--iterations 30`, sets it
absolutely instead); omit it to just continue with whatever budget remains.
`--allow-docker`, `--image`, `--port`, `--api-bind`, `--no-detach` mirror
`start`'s flags of the same name. The resolved `pi` config and creds/skills
are restored too, since the container entrypoint re-copies `/config/pi`
and the engine re-places `/config/creds` + `/config/skills` on every
startup. Anything from a bare `--forward-env`/`--llm-env`/`--env` (as
opposed to `--llm` itself) at the *original* `start` is not re-derived by
`resume` — pass those again explicitly on the `resume` invocation if
needed (`resume` has no such flags of its own; use `start`'s wiring only
for `--llm`-derived env/mounts, which now survives automatically).

Exit codes: `3` unknown run, `5` the run's container is still alive (a live
engine already holds the run dir's flock; `abort`/`stop` it first), `2` a
malformed `--iterations` value, `1` the underlying `docker run` failed.

### `ralphctl config get <key>` / `ralphctl config set <key> <value>`

Registry-wide defaults (PRD req 25), persisted at `<registry>/config.yaml`
(created on first `set`). Recognized keys: `image`, `on_complete`
(`idle`/`exit` — validated on `set`), `default_llm_profile` (any string;
`ralphctl doctor` resolves it as an LLM profile name, see below).

- `get` prints `<key>: <value>`, or `<key>: (unset)` if the key has never
  been `set` — this is not an error (exit `0` either way).
- `set` overwrites just that one key in `config.yaml`, leaving any other
  keys already set untouched.
- An unrecognized key exits `2` (both `get` and `set`) naming the expected
  keys; an invalid `on_complete` value on `set` also exits `2` and leaves
  `config.yaml` unchanged.
- `--json` on `get`/`set` prints `{"key": ..., "value": ...}` (`value` is
  `null` for an unset `get`).

`ralphctl start` layers these in as the **registry-wide fallback** for the
same-named flags (`--image`, `--on-complete`) and for `--llm` (via
`default_llm_profile`), between an explicit flag/`--template` value and the
hardcoded built-in default: explicit flag > `--template` > `ralphctl
config` default > hardcoded default. `resume`/`llm test`/`doctor`'s own
`--image` flags are unaffected (still default to the hardcoded image).

### `ralphctl doctor`

Preflight checks (`checks` in `--json` output; overall `ok` is the AND of all
of them, exit code `0`/`1` accordingly):

- `docker` — the docker daemon is reachable.
- `image` — the job image (`--image`, default `ghcr.io/.../ralphd:latest`) is
  present locally.
- `registry` — `~/.ralphd` (or `$RALPHD_REGISTRY`) is writable.
- `pi_host_config` — `~/.pi/agent/settings.json` exists (needed for `--llm host`).
- `default_llm_profile` — the registry's `default_llm_profile` (set via
  `ralphctl config set default_llm_profile <name>`; defaults to the builtin
  `host`, which — like `none` — always trivially "resolves") resolves
  cleanly on the host: every `${env:}`/`${file:}`/`${cmd:}` reference in
  `<registry>/llm-profiles/<name>.yaml` succeeds. On failure, `--json`'s
  `defaultLlmProfileError` names the profile and the offending reference.
- `registry_schema` — no malformed registry entries: every
  `<registry>/llm-profiles/*.yaml` parses as YAML, and every run's
  `status.json` parses as JSON with a `schemaVersion` this build recognizes
  (not newer than the engine's own, PRD req 18). Failures are listed in
  `--json`'s `registryIssues` (also the human report, under `! registry
  schema issues:`).

Two dangling-container checks, in both directions — always **non-fatal**
(report-only; never affect `ok`/the exit code):

- `strayContainers` — containers labeled `ralphd.run=<id>` with no matching
  run dir at all (leftovers from `--allow-docker` jobs that were never
  reaped, or a manually deleted run dir).
- `danglingRegistryEntries` — the reverse: a run dir whose `status.json`
  says `state: running` but whose container no longer exists at all (killed
  or `docker rm`'d outside `ralphctl`). Reported as `{runId, container}`;
  suggested remedy is `ralphctl resume <run-id>`.

Designed as the first command an AI agent should run.

### `ralphctl ui [--port N] [--bind ADDR]`

Starts the local web hub server (PRD reqs 21-22) in the foreground: a
stdlib-only HTTP server (`http.server`/`urllib`; no `fastapi`/`uvicorn` on
this path, even though those are dependencies of the engine side of this
same package) that reads `~/.ralphd/runs/*` and proxies each run's *live*
container API when reachable. `--port` defaults to a free ephemeral port;
prints `serving hub at http://<bind>:<port>` on startup. Ctrl-C to stop.

JSON endpoints served under `/api/`:

- `GET /api/runs` — run list (PRD req 21): `{"runs": [{runId, state,
  verdict, phase, approach, iterationsUsed, iterationsBudget, startedAt},
  ...]}`, read straight from every `runs/*/status.json` (no live proxy calls,
  so listing stays cheap regardless of how many runs are dead).
- `GET /api/runs/<id>` — run detail: `{runId, live, status, tasks,
  iterations}`. `status`/`tasks` are proxied live from the run's container
  API (`GET /status`/`GET /tasks`) when its `apiUrl` (recorded in
  `host.json`) answers; otherwise falls back to the on-disk
  `status.json`/`tasks.json` snapshot with `live: false` — a dead run never
  produces an error, just stale-but-valid data. `iterations` is always read
  from disk (`iterations/*/meta.json`). `404` for an unknown run id.
- `GET /api/runs/<id>/logs?tail=N` — server-rendered log tail (task 014):
  fetches the run's FULL raw NDJSON backlog from the live container API
  (`GET /logs`, no `tail` param there), renders it through the exact same
  `ralphd.cli.log_render.render_to_lines` function `ralphctl logs` uses
  (plain text, no ANSI — `tty=False`), THEN trims to the last `tail`
  *rendered* lines (matching the `ralphctl logs` non-follow tail contract:
  `N` means N rendered lines, not N raw events, same as the `-N`/`--tail N`
  syntax below). Returns `{"live": bool, "lines": ["<rendered line>", ...]}`
  — `lines` is `[]` and `live` is `false` if the run's API isn't reachable,
  never an error. The static hub bundle's `app.js` just displays these
  lines (one per DOM element, via `textContent`); it does not reimplement
  any event-to-text rendering rules of its own, so it always renders
  identically to `ralphctl logs` (including collapsing a many-delta
  thinking block to exactly one `[thinking…]` line, the defect this task
  fixed — the pre-014 client-side renderer appended one element per
  `thinking_delta` event with no dedup).
- `POST /api/runs/<id>/steer` — body `{"message": ..., "name": ...}`,
  forwarded to the run's live `POST /steering`. Returns the API's own
  response (`202 {"file": ...}`) on success; `503` with an `error`/`detail`
  if the run's API is unreachable or rejects it; `404` for an unknown run id.
- Any other path is served from the static hub bundle packaged in the
  wheel (`src/ralphd/cli/web/`: `index.html`, `app.js`, `style.css` — plain
  HTML/JS/CSS, no npm/node build step). A path that doesn't match a real
  file under `web/` falls back to `index.html` (SPA-style client-side
  routing via `location.hash`), so e.g. a raw browser refresh on a run's
  detail view still loads the app shell. If the bundle is somehow absent
  from the installed build, non-`/api` paths `404` with a plain-text
  "static hub bundle not installed in this build" message instead of
  crashing.

The bundle itself (open `http://<bind>:<port>/` in a browser):

- **Run list** (`#/`) — table of every run under the registry with state,
  verdict, phase, approach, iteration count and start time, auto-refreshed
  every 4s; click a run id to open its detail view.
- **Run detail** (`#/run/<id>`) — summary card (state/verdict/phase/
  approach/iterations/live-vs-snapshot/duration), a usage/cost panel
  (total tokens+cost plus the `byPhase`/`byApproach` breakdowns from PRD
  req 19 when present), a task table, an iteration timeline (number,
  phase, model, duration once ended), a live log tail rendered with the
  *same* pretty rules as `ralphctl logs` (iteration boundaries, streamed
  text, compact tool one-liners, elided thinking, malformed-line
  markers — reimplemented in `app.js`, not shared code, since the CLI is
  Python and the bundle is browser JS), and a steering form that `POST`s
  to `/api/runs/<id>/steer` and reports the created file name back.

Browser e2e coverage (PRD req 23a): `tests/test_browser_hub.py`, marked
`@pytest.mark.browser`, drives a real Chromium via the external
`playwright-cli` tool (shelled out to, never imported) against a real
`ralphctl ui` server, proving the run list renders fixture runs, the run
detail view renders a real task table and iteration timeline from a live
test engine, and submitting the steering form creates a real file under
the run's `steering/` directory. Screenshots of each view are saved to
`artifacts/screenshots/hub/`. The whole module skips cleanly (all tests
`SKIPPED`, not errored) if the `playwright-cli` binary isn't on `PATH`.
- No build step: the JS is hand-written, talks only to the JSON endpoints
  above via `fetch()`, and ships as-is inside the wheel (verified by
  building the wheel and listing its contents — no `package.json`/
  `node_modules`/bundler output anywhere in the tree).

## Notes for AI agents driving ralphctl

- Always pass `--json`; parse stdout only.
- `start` is asynchronous by default; poll `status` or stream `watch --json`.
  A simple completion wait: `ralphctl watch <id> --json | jq -c 'select(.type=="state")'`.
- Steering is cheap and safe (applies at iteration boundary); `--now` interrupts
  work in progress — use only to stop active harm, not to reprioritize.
- Do not edit files under `~/.ralphd/runs/<id>/` except `steering/`; everything
  else is engine-owned.
- Treat `verdict: "verified"` as the only success signal; `state: "succeeded"`
  without it cannot occur, but `failed` still leaves useful state — read
  `review-findings.md` and `notes.md` before retrying with `resume` or a new job.
