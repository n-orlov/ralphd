---
name: ralphd
description: Operate ralphd autonomous coding jobs with ralphctl — launch, watch, steer, resume and tear down runs, and read a run's own record to judge whether it is healthy. Use when asked to start a ralphd job, check on one, steer one, resume one after it ends, or diagnose a run that looks stuck, degraded, expensive or finished-but-wrong. Also use when writing or reviewing a PRD for a ralphd run.
---

# Operating ralphd

A ralphd job is a container running an autonomous loop: it plans a task list from
a PRD, then works, verifies and reviews its own tasks until the PRD is satisfied
or a budget runs out. Runs last hours and spend real money, so the operator's
questions are almost always **"is it healthy?"** and **"does it need steering?"**
— not "what does the CLI do".

Two directories hold everything, and both outlive the container:

- `~/.ralphd/runs/<run-id>/` — `status.json`, `events.jsonl`, `tasks.json`,
  `notes.md` (the run's own working memory), `steering/`, `artifacts/`,
  `approaches/NN/` (archived earlier attempts), `iterations/NNNN/`
  (`meta.json`, `prompt.md`, `output.jsonl` per iteration).
- `~/.ralphd/configs/<run-id>/` — `job.yaml`, `creds/`, `skills/`, LLM wiring.
  **Contains secrets.** Never print, log or commit these files; read specific
  keys and redact.

Prefer `ralphctl` over reading these by hand; drop to the files when you need
something the CLI does not surface (an iteration's raw output, an event's exact
timestamp).

## Watching a run

```bash
ralphctl runs                        # every run, newest first
ralphctl status <run>                # state, verdict, phase, approach, iteration, usage, deadline
ralphctl tasks <run>                 # the task table (--all / --approach N for earlier attempts)
ralphctl logs <run> --tail 200       # agent transcript (--follow to tail, --iteration N for one)
ralphctl iteration <run> N           # one iteration: phase, timing, exit reason, tokens, cost
ralphctl cost <run>                  # spend per phase and per approach
ralphctl fault <run>                 # current/last fault: class, retry ladder, outage budget
ralphctl docs <run>                  # notes, review findings, composite PRD, redacted job.yaml
ralphctl artifacts <run>             # what the job left behind
ralphctl watch <run>                 # live view
ralphctl ui --port 8420 --bind 127.0.0.1   # web hub over every run
```

### Judging health

Read these together, not one at a time:

1. **`state` + `phase` + `approach n/m`.** `phase: review` after `COMPLETE`
   means the review hat is checking the work; a rejection replans as
   `approach 2/m` with the old attempt archived under `approaches/01`. **A
   rejection is the harness working**, not a failure — read
   `review-findings.md` in the run dir before reporting alarm.
2. **`iteration elapsed` against the iteration cap.** `iteration_timeout_s` is
   a hard per-iteration kill. Hitting it yields `exitCode: -2`,
   `interrupted: true`, `timedOut: true` and **no task completed** — pure loss.
3. **Tasks validated vs completed**, and whether the count is moving. Minutes
   per task from recent iterations is the honest velocity number.
4. **Spend** against what the operator expected.
5. **Deadline.** Wall clock comes from `job_timeout_s`; when it fires mid-task
   the run ends wherever it is.

### Traps that produce wrong diagnoses

- **A long iteration is not a hung iteration.** Before escalating, look at what
  it is actually doing (`ralphctl logs <run> --iteration N`, and whether child
  processes/sibling containers are producing fresh output). A 49-minute
  iteration running a stability suite is healthy; a 49-minute iteration with no
  output since minute 2 is not.
- **Iteration numbers lag attempts.** Infra-outage retries are not charged as
  iterations, so `iterationsUsed` sits below the attempt number in the live
  status line. They are not missing iterations.
- **`degraded` with `ENOTFOUND` and $0 spent is DNS, not the model.** A job on
  the default bridge network gets Docker's embedded resolver, which cannot use
  the host's local stub resolver, so split-horizon names (tailnet, VPN, private
  gateways) never resolve. It will burn its entire outage budget at zero cost.
  Relaunch with `--network host`.
- **A job can lose its placed skills mid-run** (see the repo's open issues): the
  engine logs `placed skills: …` at startup and the directory can be empty
  minutes later, after which the agent cannot discover them and, e.g., sends no
  notifications. If the operator depends on a skill, verify it is still placed
  (`ralphctl skills <run> ls`) rather than assuming the startup log.
- **Absence of evidence in one channel is not evidence of absence.** Grep for a
  behaviour more than one way before reporting it missing (an HTTP call may be
  `curl` in one iteration and `python3 urllib.request` in the next).
- **A `task.repoll_backoff` deferral is the engine waiting, not a hang.** It
  marks a re-poll that backed off instead of resetting on free tool activity;
  treat it like any other healthy wait, not a stall.
- **A `cure.sweep_reopened` event is the sweep being correctly re-run**, not a
  regression: a cure pass re-opens every validated sweep task so the final
  sweep repeats on the cured sha.

## Writing a PRD

**Read [writing-a-prd.md](writing-a-prd.md) before drafting or editing any
PRD.** In short, a PRD asks for two things only: the main spec is reasonably
complete and does not contradict the live code, and the live code works,
proven by tests that run in CI. Review enforces the PRD's **letter** and
anything it can check it can reject, so:

- write each requirement as a behaviour plus the test that fails without it;
- ask for **no paperwork** — no tracked reports, records, close-outs, audits,
  sha narratives or counts in prose; run evidence stays in the run dir;
- paste a `## Materiality rubric` inline that blocks only on substance
  (behaviour absent or wrong, named test missing or red, spec contradicts
  code, secret exposed) and never on "a claim in a tracked record is false";
- end with one sweep, and commit nothing after it: no CI-run report, record,
  close-out, notes file or doc touch-up; `origin/main`'s head when the run
  ends is the swept sha; issue closure is a comment, not a tracked file. A
  review cure is the only thing that may land after the sweep — it re-runs
  the whole sweep on the cured sha.

Operator corollary: when `review-findings.md` marks a blocking finding
**`curable: false`**, the engine does not stop — it replans and burns the
remaining approaches on the same rock. Amend (`ralphctl amend`) or abort the
moment you see one. And a task-level `--waive` clears one task's criteria only;
review still enforces the PRD requirement behind it — waive the obligation,
not just the task.

## Launching

```bash
ralphctl doctor                      # preflight first
ralphctl start --prd PRD.md --workspace ~/Projects/thing \
  --run-id thing-phase1 --iterations 500 --max-approaches 8 \
  --model-for planning=<ref> --model-for worker=<ref> --thinking high \
  --timeout 720 --iteration-timeout 120 \
  --skills DIR --creds DIR --network host --vigilant --reflect
```

- **`--timeout` and `--iteration-timeout` are MINUTES** (integers), whatever a
  stale example may show.
- **`--network host`** whenever the LLM gateway or any dependency resolves only
  through split-horizon DNS. Harmless otherwise.
- **`--allow-docker`** mounts the host docker socket: root-equivalent host
  access. Only for a PRD you trust, and only when the work genuinely needs
  sibling containers.
- **`--skills` / `--creds` are copied at start and are not recorded in
  `job.yaml`.** When relaunching or reproducing a run, diff against the previous
  run's config dir instead of trusting `job.yaml` to tell you what it had.
- Hats that can take their own model: `planning`, `worker`, `verify`, `review`,
  `reflect`, `router`; per-tier via `--model-for-tier HAT:TIER=REF`
  (`trivial`/`normal`/`hard`).

**Confirm the parameters with the operator before starting.** A launch commits
hours of wall clock and real money; state the run id, model per hat, iteration
and approach budgets, timeout, network and workspace, and let them correct you.

## Steering

```bash
ralphctl steer <run> --file steer-003.txt --name budget-cut
ralphctl steer <run> --list            # pending and applied, works after the container is gone
ralphctl budget <run> +200             # top up iterations in flight
ralphctl amend <run> ...               # record a ruling review must honour
```

Steering is consumed at the next iteration boundary (`steering.received` →
`steering.consumed`); `--now` also interrupts the current iteration, which
throws away that iteration's work — justify it before using it.

A steer that lands well is written like a report, not a wish:

- **Cite evidence.** Exact times, shas, event names, file paths. The run can
  check those; it cannot check "please be careful".
- **Credit what is going well**, specifically. It tells the run which of its own
  habits to keep.
- **Own your corrections.** If an earlier steer was wrong, or you are
  withdrawing an instruction you gave (a scope cut, a deferral), say so
  explicitly and say why the premise changed. A silently-reversed instruction
  leaves the run guessing which one is live.
- **Be actionable at the top.** Front-load what to do next; put the reasoning
  under it.
- Do not ask for work the run has already done — read `notes.md` and
  `ralphctl tasks` first.

## Resuming

`resume` starts a fresh container over the same run dir: tasks, notes, plan,
commits and issue state all persist. It is the same run continuing.

```bash
# a longer wall clock is NOT a resume flag — edit the run's job.yaml first:
#   job_timeout_s: 43200        (the deadline is computed at engine start)
ralphctl resume <run> --iterations +500 --allow-docker
```

- **`resume` has no `--timeout`.** The window comes from `job_timeout_s` in
  `~/.ralphd/configs/<run>/job.yaml`, read when the engine starts — so editing
  it before resuming grants a full fresh window.
- `--iterations +N` adds to the existing budget; a bare integer sets it.
- **`--allow-docker` is not replayed.** If the original run had the socket, pass
  it again or the work that needs siblings will fail. Check what the previous
  container actually had (`docker inspect` its mounts) rather than guessing.
- `--network` and the image default to what the run recorded. **Do not pin
  `--image` without a reason** — the default already reproduces the recorded
  image and keeps its provenance, which pinning discards.
- The resumed container runs the **same image**, so fixes the run itself wrote
  to its workspace are not live in its own engine. Do not let a run record its
  own unshipped fixes as exercised.

## Ending a run

Order matters; each step refuses if the previous one has not happened:

```bash
ralphctl abort <run>            # terminate the job
ralphctl stop <run> --force     # stop refuses while the job is running
ralphctl rm <run> --force       # rm refuses while the container still exists
```

`ralphctl repair <run>` diagnoses (and, guarded, fixes) inconsistent run-dir
state, e.g. after a host crash.

## Cleaning up

Be surgical. A ralphd host usually has several live runs and their sibling
containers on it, plus content-hashed images other runs still need.

- **Never** `docker prune`, and never a label- or wildcard-filtered
  `docker rm`/`rmi`. Remove containers and images by **exact name and exact
  tag** only.
- **Never signal by pattern** (`pkill -f`, `killall`). Resolve a PID, verify
  what it is, then signal that PID.
- Other checkouts on the host may be the live workspaces of other runs. Do not
  edit, stash or clean a working tree without checking which run owns it.
