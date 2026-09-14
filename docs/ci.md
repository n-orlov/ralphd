# Continuous integration: the lanes, where they run, and what they attest

Until this branch, every test execution in this project's history was run either
by the operator's shell or by a job container, and the answer to "was the suite
green at sha X" lived only in self-authored markdown under `artifacts/reports/`
— inside the very tree being tested. #113 is the issue that names the cost of
that: `sweep-sha` drift, the fixed-point self-anchoring ritual, and the
`LAUNCH_SHA` staleness tax. This document describes the first two steps of its
fix.

The tiers themselves — what they cover and what they cost — are
`docs/testing.md`. This file is only about *who runs them and when*.

## The three workflows

| workflow | lanes | runner | trigger | budget |
|---|---|---|---|---|
| `gate.yml` | records lint, `-m fast` (which is a superset of `-m smoke`) | self-hosted `fast-lane` (`int21h-ws-5`/`-6`, exclusively) | every pull request, every push to `main` | 45 min |
| `heavy.yml` | `-m docker` (24 tests), `-m browser` (45 tests), as two separate jobs | self-hosted `ralphd-vps` | every pull request, every push to `main` | 120 / 60 min |
| `nightly.yml` | a six-cell dev-layout matrix (`ubuntu:24.04`, `python:3.11-slim`, `python:3.14-slim` x `PYTHONUSERBASE` exported/unset), each cell running the `fast` tier under `scripts/host_like_dev_layout.sh` inside a plain upstream container (task 077, steer 008) | self-hosted, two runner slots (below) | 02:00 UTC, plus manual dispatch | 120 min per cell (`nightly.yml`'s own `timeout-minutes: 120`, raised from 60 by task 035 -- steer 026 §4 -- because 60 was already marginal at 58 min observed at `bd284e79` with no interference, and task 035's own cure adds real per-attempt wall time to any cell whose startup watchdog has to re-arm under contention) -- sized to the `fast` tier's ~80 s parallel wall clock plus the cell's own image pull and, for the `ubuntu:24.04` cell only, its `apt-get install python3 python3-pip` step, never to the retired unfiltered whole-suite invocation `docs/testing.md`'s two retired dev-layout rows measured (2047-2147 s each, no per-cell axis) |

`gate.yml` was on GitHub's own runners until 14 September 2026, when being the
lane that runs most often stopped being an argument for hosted runners and
became the argument against them: it was the only hosted lane left, and 258 runs
/ 1718 minutes of wall clock in the first two weeks of September put the account
at 90 % of its 2000 included Actions minutes on its own. `heavy.yml` and
`nightly.yml` are self-hosted and cost nothing. So the fast tier moved to the
VPS and the account's hosted-minute bill went to zero.

The two properties that made hosted runners attractive are unaffected. A check
run is created and signed by GitHub's Actions app wherever the runner executes,
so it is still an attestation this repository cannot fabricate — the property
#113 is buying — and the lane still needs neither a docker socket nor a browser,
which is why it asks for neither label.

**The fleet is partitioned, and the VPS is gone** (operator decision, later on
14 September 2026, superseding the VPS partition described earlier that day).
The VPS was a 4-core / 7 GB box and that, not the workflow, was why this lane
took ~12 minutes: the identical `-n 2` recipe is 592 s on the workstation. Both
VPS slots were deregistered and their packages removed. The partition survived
the move; only its halves changed machines:

| slots | labels | takes |
|---|---|---|
| `int21h-ws-5`, `int21h-ws-6` | `fast-lane` only | `gate.yml` only |
| `int21h-ws-1` .. `int21h-ws-4` | `ralphd-vps`, `docker-host`, `browser-runner` | `heavy.yml`, `nightly.yml` only |

Slots 5 and 6 are a **reservation, not spare capacity**, and that is why they
were registered instead of simply adding `fast-lane` to the four that already
existed. `nightly.yml`'s six-cell matrix runs `fail-fast: false` for 67-89
minutes and `heavy.yml` holds two slots for 14-21 minutes at a time; sharing
would let a push's `gate` run queue behind all of it. It remains a pair of
exclusions and neither half is safe to change alone.

**All six slots are on one 12-core host.** They never contend for a slot but
they do contend for CPU, which is priced into `gate.yml`'s worker count (`-n 4`,
not `-n 8`) and into leaving its `timeout-minutes: 45` untrimmed. Contention is
now the main remaining source of a red `gate`: on 14 September, four distinct
timing-sensitive nodes failed once each across ten runs and none reproduced.

Two naming consequences. **`ralphd-vps` is now doubly a misnomer** — it names
workstation slots and there is no VPS in the fleet at all; renaming it means
editing `heavy.yml`, `nightly.yml` and
`tests/test_ci_dev_layout_matrix_guard.py` together, still #189's subject. And
**this table is a snapshot, not the source of truth** — it has been overtaken
within a day of being written twice now. Read the live shape from
`GET /repos/n-orlov/ralphd/actions/runners`.

The interpreter is pinned: `python3.12` explicitly, not `heavy.yml`'s
`python3 -m venv` idiom, because that idiom follows the host default (3.14.5 on
this host). Measured at `d37733c` on the VPS, throwaway full-history clone:
records lint 117 passed in 67 s, `-m fast -n 2` 5681 passed / 4 skipped in
710 s. On the workstation the same recipe is **592 s at `-n 2` and 378 s at
`-n 4`**. The worker count was raised to 4 with the move (`9899016`) and put
back to 2 immediately after (`f122e9c`), because `-n 4` failed both of its
runs on two SIGINT-driving nodes while `-n 2` was green on three consecutive
runs across both hosts. The lane is fast enough without it: a whole job,
including the venv build, measured **7:45** and **8:51** on slots 5 and 6
against the VPS's **12:29** on the same commit.

**What the pin does not do is fix the patch level, and the move cost the fleet
real coverage.** The VPS ran Ubuntu 24.04, whose `python3.12` is **3.12.3**;
the workstation runs Fedora 44, whose `python3.12` is **3.12.14**. Those two
disagree about `argparse` leftover-positional handling — the fix landed in a
3.12 patch after 3.12.3 — and that disagreement is what exposed #182 and kept
this lane red for 14 commits and 3 h 15 m on 14 September 2026, on a host no
container and no developer machine in this project could reproduce. With the
VPS gone, no lane runs 3.12.3. That loss is accepted deliberately and is the
direct motivation for #205: a lane's interpreter should be a property of an
image named in the workflow file, not of whichever host answered the
dispatch.

## The self-hosted runner

> **This section describes the VPS, and it is out of date about the fleet as a
> whole.** Two things have changed under it and only the first is this document's
> current subject. (1) As of 14 September 2026 the VPS's two slots carry
> `fast-lane` and *only* `fast-lane`: `ralphd-vps` and `docker-host` were removed
> from them, so they run `gate.yml` and nothing else — everything below about
> which lanes land here is superseded by the partition described above. (2) Four
> further slots, `int21h-ws-1` to `int21h-ws-4`, were registered on the
> workstation on 11 September 2026 and are where `heavy.yml` and `nightly.yml`
> actually run; they are undocumented here. Reconciling this section with the
> real fleet is #189's subject. Everything below about the VPS *host* — its
> cores, its docker group, #180 — is still accurate about that host.

TWO runner slots registered against this repository (not an org), both
labelled `ralphd-vps` and `docker-host`: `vmi3245594` and `vmi3245594-2`
(operator-provisioned, steer 008). Exactly one of the two, `vmi3245594`, also
carries the `browser-runner` label the operator placed on it (steer 009), which
is what keeps browser work globally serial across every workflow -- see
`heavy.yml`'s browser tier for why a `concurrency:` group cannot substitute.
With two slots live, the docker tier and the browser tier below CAN run
concurrently (`heavy.yml` no longer serialises them against each other; they
are separate jobs with separate check runs so a red one never cancels the
other's evidence), and `nightly.yml`'s six matrix cells run at `max-parallel:
2`, label-agnostic, across both slots. The operator measured load 1.92 on the
host's 4 cores with two jobs in flight and called that comfortable, not three;
nothing here proposes a third slot.

Host: 4 x86_64 cores, 7 GB RAM plus a 4 GB swapfile, docker 29.4.0, Ubuntu
24.04, per slot. Provisioned with Node 22 (from the official tarball, so no third-party
apt repository is added to a host that also serves live traffic),
`@playwright/cli@0.1.19` — the exact binary `tests/test_browser_hub.py` shells
out to — Google Chrome, which is the channel that CLI opens by default, and a
shared browser cache at `/opt/ms-playwright` exported to the runner through its
`.env`.

The runner runs as a user in the `docker` group. That is a deliberate operator
decision, not an oversight: a socket is root-equivalent on its host, so a CI job
on this box is trusted with the box. It is chosen over rootless docker because
`tests/test_engine_uid_boundary_docker.py` and
`tests/test_root_owned_record_operability_docker.py` inspect real uid and
ownership semantics, which a uid-remapping daemon would falsify.

**That user's uid must be 1000, and that is a bug being worked around, not a
requirement worth having.** The image's engine user is uid 1000
(`container/Dockerfile:37`) and the engine drops to that effective uid before
reading `/config/job.yaml`, which `ralphctl start` writes `0700` owned by the
invoking host user — so on any other account every container dies at startup
with `PermissionError: [Errno 13] Permission denied: '/config/job.yaml'` and the
host only sees `Connection refused`. This runner was first installed as a
dedicated uid-1001 account and 13 of the docker tier's 24 tests failed that way.
See **#180**; when it is fixed, move the runner back to a dedicated account and
delete this paragraph. The diagnosability half is **#181**.

Nothing in this suite needs a credential — pi is stubbed (`tests/stub-pi`), and
no test wants an LLM key, an AWS key or the gateway. So the runner holds no
secrets, and a compromised job has nothing to steal.

## Concurrency

- `gate.yml` cancels an older run of the same ref: a superseded PR head is not
  worth a runner.
- `heavy.yml`'s jobs are grouped per PR ref, and **all `main` pushes share one
  group** with `cancel-in-progress`. When several merges land in a row the newest
  `main` is the one that gets attested; intermediate shas keep the check runs
  they earned on their own pull requests.
- `nightly.yml` runs its six matrix cells at `max-parallel: 2` and `fail-fast:
  false` — two runner slots, and one red cell must not cancel another's claim.

## Reading a `cancelled` conclusion: which of the three causes it was

A job's `conclusion` is `cancelled` for three unrelated causes, and the
Checks/Actions API exposes no field that separates them: a human pressed
Cancel, a `cancel-in-progress` group superseded this run, or the job hit its
own `timeout-minutes`. Only the third is a statement about the commit, and it
is the one a judging hat most needs to not mistake for a verdict. Two
mechanisms make it legible, both keyed on one fixed, greppable prefix —
`RALPHD_TIMEOUT_KILL:` — printed to the step log **and** appended to
`$GITHUB_STEP_SUMMARY`, which are surfaces a fresh `GET` already returns:

- **`scripts/run_bounded.sh <seconds> <label> -- <command...>`** wraps every
  step that can consume real wall clock (each tier invocation, each venv
  build, the records lint, each dev-layout cell) in an inner `timeout(1)`
  bound set below the job's own. An overrun is then killed from inside the
  step, which prints the marker and exits 1 — an ordinary `failure`, with the
  reason in the log, instead of a bare `cancelled`. A cancel arriving from
  outside signals `timeout(1)` itself first, so it reports an
  external-signal status rather than its own self-timeout status and prints
  nothing.
- **`scripts/job_timeout_marker.sh <minutes> <label>`** is each job's last
  step, under `if: always()` so it also runs on a cancelled job. It compares
  elapsed wall time against the `timeout-minutes` that job declares — the
  first step stamps `RALPHD_JOB_STARTED_AT` — and prints the marker only when
  the job was still alive as its bound came due, which no human cancel or
  supersession can be. It always exits 0 and never changes an outcome.

So: **a `cancelled` conclusion carrying the marker is a timeout, and one
without it is a cancel.** `tests/test_run_bounded_timeout_marker.py` holds
both scripts to that behaviour and holds every job in all three workflows to
using them — the stamp step first, the classifier last, every long step
bounded, and the inner bounds summing under `timeout-minutes` less a reserve
for the steps no wrapper owns (`actions/checkout` and the environment
reports).

## The sweep-sha practice: retired outright (task 003, v0.20.2, requirement A.1, #113 step 4)

Every lane used to deselect one node id,
test_sweep_sha_guard.py::test_every_declared_sweep_sha_is_current_or_discharged,
because that test asserted every `sweep-sha` declared under
`artifacts/reports/` was literally `HEAD` or discharged to `HEAD` by a
hand-maintained chain -- a claim that is HEAD-relative by construction, so it
could hold on exactly one commit per wave and could never pass on a
pull-request head at all. Task 053 (v0.20) first deleted that test, the
currency check it exercised (check_reports), the discharge chain, and the
self-anchoring acceptance walk that had grown up around the same
unsatisfiable shape (#174), leaving the guard module down to one lint,
sha_missing_problems (test_every_sweep_record_declares_a_sha): every report
that shows a pytest result line declares SOME 40-hex sha, in either the
`sweep-sha` or `archived-sha` form.

Requirement A.1 (v0.20.2) finishes the job: `tests/sweep_sha_guard.py`, its
test module (`test_sweep_sha_guard.py`, under `tests/`) and
`docs/sweep-records.md` are deleted outright. A GitHub-signed check run
keyed to the sha is the attestation review reads now; no record owes any
sha declaration of its own any more. Existing `sweep-sha`/`archived-sha`
blocks under `artifacts/reports/` are untouched, inert history -- no report
is to carry a new one, ever again. No lane deselects anything from that
module any more: a recursive grep of `.github/workflows` and this file for
the two-dash pytest deselect flag followed by that now-deleted node id
finds nothing.

## Not done yet

- **#113 step 3** — teach the review contract that "all lanes green at the
  shipped sha" is verified with `GET /repos/.../commits/{sha}/check-runs`, not by
  parsing a self-authored ledger.
- **Branch protection and the run's token** — the intended shape is required
  status checks with no required reviews (a single owner cannot approve their own
  pull request), and a fine-grained token for the autonomous run carrying
  `contents: write` + `pull_requests: write` and no admin, so branch protection
  is something the run structurally cannot bypass.
- **Measured numbers for the self-hosted lanes.** The budgets in
  `docs/testing.md` were measured on a 28-core box. The first green run of
  `heavy.yml` is what supplies the real figures for four cores; the `docker`
  lane runs serially until then, and `-n 2 --dist loadfile` is the next thing to
  try.
