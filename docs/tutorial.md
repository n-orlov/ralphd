# Tutorial: your first ralphd job

This is a start-to-finish walkthrough for a newcomer: install, sanity-check
your setup, pick an LLM profile, launch a job with skills and credentials,
watch it work, steer it, collect the results, resume it after an interruption,
and browse it in the web hub. Every command below is copy-pasteable as
written — no environment-specific values (fill in your own paths/names where
shown in `<angle brackets>`).

For full flag/option reference see [cli.md](cli.md); for LLM profile details
see [llm-profiles.md](llm-profiles.md); for how the pieces fit together see
[architecture.md](architecture.md).

## 1. Install

```bash
git clone https://github.com/n-orlov/ralphd.git
cd ralphd
pipx install .          # installs the ralphd distribution, which provides ralphctl
```

v0.6 is **not published to PyPI**, so there is no `pipx install ralphd` from an
index yet (see [roadmap.md](roadmap.md)) — install from a checkout as above.
`pip install -e .` in a virtualenv works too and is what the test suite uses.

`ralphctl` needs a working `docker` CLI on your `PATH` (or `podman`, with
`RALPHD_DOCKER=podman` set). It talks to the docker daemon directly — there is
no separate server process to install or run.

## 2. Sanity-check your setup: `ralphctl doctor`

```bash
ralphctl doctor
```

This is the first command to run, and the first thing to run again if
anything later misbehaves. It checks: the docker daemon is reachable, the job
image is present locally, your registry (`~/.ralphd`, or `$RALPHD_REGISTRY`)
is writable, your host pi config exists (needed for `--llm host`, the
default), and your default LLM profile resolves cleanly. It also reports (but
never fails on) stray or dangling containers left over from earlier jobs.

```bash
ralphctl doctor --json   # machine-readable form, same checks
```

If `docker` isn't reachable, fix that first — nothing else in this tutorial
will work without it.

## 3. Pick an LLM profile

By default, `ralphctl start` uses `--llm host`, which forwards your existing
host pi configuration and standard provider env vars (`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, AWS Bedrock vars, etc.) into the job container. If that's
already how you use pi on this machine, you can skip straight to step 4.

To see what's available:

```bash
ralphctl llm profiles
```

This lists the two built-ins (`host`, `none`) plus any named profile file
under `~/.ralphd/llm-profiles/<name>.yaml`. To inspect one before using it
(secret values are always masked):

```bash
ralphctl llm show host
```

To create a named profile (e.g. for a Bedrock or gateway setup), copy one of
the shipped examples and edit the placeholders:

```bash
mkdir -p ~/.ralphd/llm-profiles
cp examples/llm-profiles/bedrock.yaml ~/.ralphd/llm-profiles/bedrock.yaml
# edit ~/.ralphd/llm-profiles/bedrock.yaml, then:
ralphctl llm show bedrock
ralphctl llm test bedrock     # validates resolution + (if docker is up) a real 1-token ping
```

`ralphctl llm test <profile>` never touches a run dir — it's a standalone
throwaway check you can run as many times as you like while getting a profile
right.

## 4. Prepare skills and credentials (optional)

**Skills** are directories containing a `SKILL.md` file that get copied into
the job. You can pass one skill directory, or a directory containing several
skill subdirectories:

```bash
ralphctl start ... --skills ./skills/my-skill        # one skill
ralphctl start ... --skills ./skills                 # a folder of skills, expands to each child
```

One skill ships with ralphd: `examples/skills/toolchain-sibling/` teaches the job
to run work needing a toolchain the image lacks (Go, Rust, a JDK, tmux) in a
sibling container. Add it with `--skills examples/skills/toolchain-sibling`
alongside `--allow-docker` — the phase prompts already carry the short version of
that recipe, the skill adds the copy-pasteable `run.sh`.

**Credentials** follow an env-file convention: one `<name>.env` file per
credential set (`KEY=value` lines), in a directory:

```
creds/
├── github.env       # GITHUB_TOKEN=...
└── sonarqube.env     # SONAR_TOKEN=...
```

```bash
ralphctl start ... --creds ./creds
```

These land at `~/.creds/*.env` (mode `0600`) inside the container. The
job's prompts tell the agent which credential files exist and how to source
one (`set -a; . ~/.creds/github.env; set +a`) — the values themselves are
never printed anywhere, copied into the run dir, or logged.

## 5. Start a job

```bash
ralphctl start \
  --prd ./feature.md \
  --workspace ~/src/widget \
  --skills ./skills \
  --creds ./creds \
  --llm host
```

This prints the run ID (e.g. `brisk-otter-1408`) and returns immediately —
`start` is asynchronous by default. Add `--json` for a machine-readable
result (run ID, container ID, API URL, token presence).

Other useful flags at start time: `--vigilant` (verify each task
individually before it's considered done), `--reflect` (run one extra
self-reflection pass after the job finishes), `--iterations <n>` (budget,
default 25), `--max-approaches <n>` (review-loop retry budget, default 3).
See [cli.md](cli.md#ralphctl-start) for the full list.

## 6. Watch it work

```bash
ralphctl watch brisk-otter-1408
```

A live **event stream** (not a TUI — the CLI ships no curses framework): one
line per event as the run emits it, replayed from the start of the run, so you
see iteration boundaries, task changes, steering and the terminal verdict as
they happen. Read-only; Ctrl+C stops it. With `--json` (or piped to a script)
each line is the raw event object instead — NDJSON.

For a rendered snapshot of the plan, budget and cost instead, use `ralphctl
status`/`ralphctl tasks` (or the hub, `ralphctl ui`). For the raw or historical
transcript, use `logs`
(the "whole-job console" — every iteration merged in order):

```bash
ralphctl logs brisk-otter-1408          # last 50 rendered lines
ralphctl logs brisk-otter-1408 -100     # last 100 lines
ralphctl logs brisk-otter-1408 -150f    # last 150 lines, then follow live
ralphctl logsf brisk-otter-1408         # follow from now (alias for logs -f)
```

Pretty rendering (the default) shows iteration/phase headers, streamed
assistant text, tool calls as compact one-liners, thinking elided to a
marker, and a per-iteration usage/cost footer. Use `--raw` for the
underlying NDJSON if you're piping to another tool.

Check overall progress at any time:

```bash
ralphctl status brisk-otter-1408
ralphctl tasks brisk-otter-1408
```

## 7. Steer it

If the job is heading the wrong way, nudge it without restarting anything:

```bash
ralphctl steer brisk-otter-1408 "Skip the docs task; focus on tests"
```

Steering is applied at the next iteration boundary by default (cheap and
safe). Add `--now` to also interrupt the current iteration so guidance
applies immediately — use this only to stop active harm, not just to
reprioritize.

## 8. Collect the results

Once the job reaches a terminal state (check with `ralphctl status`), pull
its artifacts:

```bash
ralphctl artifacts brisk-otter-1408 ls
ralphctl artifacts brisk-otter-1408 pull ./out/
```

Treat `verdict: "verified"` (visible in `ralphctl status --json`) as the only
real success signal. If the job ended `failed`, read `review-findings.md` and
`notes.md` in the pulled artifacts before retrying.

The container exits by default after completion (`--on-complete exit`); pass
`--on-complete idle` to keep it up (explicit debugging opt-in) so you can
still query it before shutting it down:

```bash
ralphctl stop brisk-otter-1408
```

`stop` only shuts down the container — the run dir (history, artifacts) stays
on disk until you explicitly `ralphctl rm brisk-otter-1408`.

## 9. Resume after an interruption

If the container died or you want to add more iteration budget and continue,
`resume` starts a fresh container over the *same* run dir — the engine
detects the existing `tasks.json` and completed iterations and continues the
job instead of re-planning from scratch:

```bash
ralphctl resume brisk-otter-1408 --iterations +10
```

`resume` reproduces the original `start`'s run-dir/config/workspace mounts and
LLM wiring automatically; you don't need to repeat `--workspace`, `--skills`,
`--creds`, or `--llm`. It refuses (exit `5`) while the run's container is
still alive — `abort` or `stop` it first.

## 10. Browse it in the web hub

For a visual overview of all your runs:

```bash
ralphctl ui
```

This starts a local hub server (prints the URL, e.g.
`http://127.0.0.1:PORT/`) with a run list, a run detail view (task table,
iteration timeline, live log tail, usage/cost panel), and a steering form —
all backed by the same JSON endpoints and container APIs used above. Open the
printed URL in a browser; `Ctrl-C` stops the hub server (it doesn't touch any
running jobs).

## What's next

- Full flag reference: [cli.md](cli.md)
- LLM profile authoring: [llm-profiles.md](llm-profiles.md)
- How the engine, run dir, and container fit together: [architecture.md](architecture.md)
- HTTP API used by all of the above: [api.md](api.md)
