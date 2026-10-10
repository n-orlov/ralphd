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
| `gate.yml` | `-m fast` (which is a superset of `-m smoke`) | self-hosted `fast-lane` (`int21h-ws-5`/`-6`, exclusively) | every pull request, every push to `main` | 45 min |
| `heavy.yml` | `-m docker` (24 tests), `-m browser` (45 tests), as two separate jobs, plus a third `browser-image` job that publishes the browser tier's pinned image and runs on nothing but an explicit dispatch with `publish_browser_image: true`, which in turn skips the two tiers (task 011) | self-hosted `ralphd-vps` | every pull request, every push to `main` | 120 / 60 min (40 for the dispatch-only publisher) |
| `nightly.yml` | a six-cell dev-layout matrix (`ubuntu:24.04`, `python:3.11-slim`, `python:3.14-slim` x `PYTHONUSERBASE` exported/unset), each cell running `scripts/host_like_dev_layout.sh` with no override inside a plain upstream container (task 077, steer 008) -- the `fast` tier plus (task cure-01-01-2) a supplemental pass over every node that leaves outside `fast`/`smoke`/`docker`/`browser` (the `nightly`-marked nodes and the script's own test module), which `-m fast` alone never selected | self-hosted `ralphd-vps` + `docker-host` (`int21h-ws-1` only, so the cells run one at a time -- see below) | 02:00 UTC, plus manual dispatch | 120 min per cell (`nightly.yml`'s own `timeout-minutes: 120`, raised from 60 by task 035 -- steer 026 §4 -- because 60 was already marginal at 58 min observed at `bd284e79` with no interference, and task 035's own cure adds real per-attempt wall time to any cell whose startup watchdog has to re-arm under contention) -- sized to the `fast` tier's ~80 s parallel wall clock plus the cell's own image pull and, for the `ubuntu:24.04` cell only, its `apt-get install python3 python3-pip` step, never to the retired unfiltered whole-suite invocation `docs/testing.md`'s two retired dev-layout rows measured (2047-2147 s each, no per-cell axis); unchanged by cure-01-01-2, whose supplemental pass adds real wall time inside this same ceiling rather than raising it |

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
VPS slots had their labels and their packages removed, so they are eligible for
nothing and no lane can land on them; the two registrations themselves
(`vmi3245594`, `vmi3245594-2`) are still present and online, which is why
`## The self-hosted runner` below still lists them (re-read live at task 014:
eight registrations, six labelled as partitioned below, two label-free). The
partition survived the move; only its halves changed machines:

| slots | labels | takes |
|---|---|---|
| `int21h-ws-5`, `int21h-ws-6` | `fast-lane` only | `gate.yml` only |
| `int21h-ws-1` | `ralphd-vps`, `docker-host`, `browser-runner` | `heavy.yml`, `nightly.yml` only |
| `int21h-ws-2` .. `int21h-ws-4` | none (parked) | nothing |

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
`-m fast -n 2` 5681 passed / 4 skipped in
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

The live placement belongs to `GET /repos/n-orlov/ralphd/actions/runners`; this is
what it holds:

- `int21h-ws-1` carries `ralphd-vps`, `docker-host` and `browser-runner`. It is the
  **only** slot `heavy.yml` and `nightly.yml` can take, so every heavy job and every
  nightly cell runs one at a time, and never beside another heavy or nightly job.
- `int21h-ws-2` .. `int21h-ws-4` are registered and online with no custom labels, so
  they take nothing.
- `int21h-ws-5` and `int21h-ws-6` carry `fast-lane` only and run `gate.yml`.
- `vmi3245594` and `vmi3245594-2` (the VPS) are registered and online with no custom
  labels, so they take nothing.

All `int21h-ws-*` slots are registrations on one physical 12-core host. A label is the
only placement control that works across workflows: a `concurrency:` group only
serialises within one workflow. So one labelled slot per physical host is what keeps
`heavy` and `nightly` from sharing a machine, and what keeps browser-tier work
globally serial. The cost is throughput: a full `nightly` run is its six cells back to
back. To add capacity, put `ralphd-vps` + `docker-host` on a slot on another physical
host, not on a second slot of this one.

Host (measured on the retired VPS; not yet re-measured on the current
workstation, so treated as history rather than a current spec): 4 x86_64
cores, 7 GB RAM plus a 4 GB swapfile, docker 29.4.0, Ubuntu 24.04, per slot.
Provisioned with Node 22 (from the official tarball, so no third-party apt
repository is added to a host that also serves live traffic),
`@playwright/cli@0.1.19` — the exact binary `tests/test_browser_hub.py` shells
out to — Google Chrome, which is the channel that CLI opens by default, and a
shared browser cache at `/opt/ms-playwright` exported to the runner through its
`.env`.

**That host provisioning is no longer what the browser tier runs against (task
011, requirement H, #205 step 3).** `heavy.yml`'s `browser-tier` declares a
`container:` pinned by digest to this repository's own GitHub Packages container
package `n-orlov/ralphd-browser-ci` (the literal registry reference lives in
`heavy.yml`, which is also the only place a digest should be pinned; note this is
a CI *toolchain* image and has nothing to do with the job image `ralphd:<hash>`,
which is still built from a checkout and published nowhere). It is built from
`ci/browser/Dockerfile` (`mcr.microsoft.com/playwright:v1.62.1-noble` + the same
`@playwright/cli@0.1.19` + real Google Chrome + `python3.12-venv`), and the
tier's own toolchain-assertion step prints the versions that image supplies. The
host's Node/CLI/Chrome are still there and still what an operator uses by hand,
but a drift in them can no longer change what the lane measures — which is the
point: this ambient provisioning drifted once already, silently losing
python3.12.3 in the VPS-to-workstation move (#205's own motivation). Republishing
the image is a dispatch of `heavy` with `publish_browser_image: true`; that job
prints the new digest, which is then pinned in `heavy.yml` by hand in a commit of
its own. The package is private, is linked to this repository because the
workflow (not a personal access token) published it, and is pulled with
`credentials:` from `${{ secrets.GITHUB_TOKEN }}` — the registry has no API that
makes a package public, only its web UI does.

The docker-group/uid-1000 paragraphs below describe the runner-user
model, which is a property of how each slot's agent process is provisioned,
not of which physical box it landed on, so they still apply to the current
workstation slots without amendment.

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

## The elastic runner pool

`ci/runner_scaler.py` is the elastic runner pool's scaler: one stdlib-only Python
module, run as `python3 ci/runner_scaler.py <command>`, that is not part of the
installed `ralphd` package. It polls the GitHub API (`GET /repos/{repo}/actions/runs?status=queued`
and each run's `/jobs?filter=latest`) and starts one container, named
`ci-runner-<job id>`, for each queued job whose labels include the `ci-pool` label. It
serves no job without that label, so the standing runners above keep working, and
they remain until the operator retires them: the scaler never touches a standing
runner and nothing in this repository removes one.

Four commands exist, three subcommands and one mode:

- `once` runs one pass and exits 0.
- `run` runs passes in a loop, one every `poll_interval_s` seconds; `--passes N`
  stops it after `N` passes.
- `status` prints the scaler's containers (those named `ci-runner-*`) as a JSON
  list, each entry carrying the container name, its job id and its age in seconds.
- `--dry-run`, on `once` and `run`, makes a pass a rehearsal.

A dry run prints each start a pass would make, as
`would start ci-runner-<job id> for job <job id>`, and issues no `docker run` or
`docker create`. An unknown command exits non-zero.

### Selection, idempotence and the cap

A pass serves a queued job only when its labels include `ci-pool`; a queued job
without it is left to the standing runners. Jobs are served in ascending job-id
order, so the oldest job is started first. Each started container is named
`ci-runner-<job id>`, and that name is the scaler's only state: a job that
already has a `ci-runner-<job id>` container, in any state, is skipped, so two
passes over the same queue start one container per job. `max_concurrency` (default 4)
caps the containers alive at once. A pass counts the `ci-runner-*` containers that
have not exited and starts only as many jobs as free slots remain, so ten queued
jobs at the default start four, and the next pass starts one job for each container
that has since exited, again in job-id order. `--dry-run` applies the same rules.

### The JIT runner request

Before a pass starts a container it asks GitHub for a just-in-time runner config
for that job: `POST /repos/{repo}/actions/runners/generate-jitconfig`, with `repo`
from the config and a JSON body of three fields. `name` is `ci-<job id>`, `labels`
is the job's own `labels` list as the jobs API reported it, and `runner_group_id`
is `1`. GitHub registers that one single-use runner and returns an
`encoded_jit_config`, which is a secret: the scaler never logs it. The scaler never
registers a standing runner and runs no `config.sh`; a runner exists only for the
one job it was requested for. Each start is preceded by exactly one such request.
A `--dry-run` pass issues none, and a pass over a queue whose jobs already have a
container issues none either. A 5xx reply (or any failed request) starts no
container for that job and takes no concurrency slot; the pass logs it, goes on to
the next job, and the next pass requests again. The request is tested in
`tests/test_runner_scaler_jit.py`.

### The runner image and the run-once exit

`ci/runner/Dockerfile` is the recipe for the image every scaler container runs (the
`image` key of the config names the tag it is built to). It builds `FROM ubuntu@sha256:<digest>`,
the multi-arch index digest of Ubuntu 24.04, so the base never moves under a rebuild,
and no tag in the file is `latest`. The `actions/runner` release comes from the build
`ARG` `RUNNER_VERSION`; the archive is downloaded from that release and verified with
`sha256sum -c` against the 64-hex SHA-256 pinned in the `ARG` `RUNNER_SHA256`, the
value the release notes publish for `actions-runner-linux-x64`. A bump changes
both together. The image installs `git`, `curl`, `jq`, python3 with `venv` and the
docker CLI (`docker-ce-cli`, the client only; no daemon is installed or started),
and ends with `USER 1000`, the base image's `ubuntu` account.

The image has no `ENTRYPOINT` or `CMD`: the scaler's `docker create` names the command,
`/home/ubuntu/run-once.sh /run/ci-runner/jitconfig`. `ci/runner/run-once.sh` reads the
encoded JIT config from that file and does `exec ./run.sh --jitconfig <config>`, so
the runner starts once, takes the one job the config was requested for and exits when
it ends; the container's exit status is the runner's. There is no `config.sh`
registration step in the script or in the argv, because the JIT config already
registered the single-use runner. The scaler removes the exited container by exact
name (see the start deadline and reaping above). The config file is put into the
container as described in the next section. The recipe is checked by parsing,
never by a build: `tests/test_runner_image.py` asserts the digest base, the SHA-256
check, no `latest` tag and `USER 1000` on the file, fails each assertion on a mutated
copy that lacks it, and asserts the start argv.

### The secret path and the container limits

The JIT config is a credential, and the scaler starts a container in three docker
calls, in this order: `docker create`, `docker cp -`, `docker start`. The config
reaches the container as the file `/run/ci-runner/jitconfig` through a tar stream on
the stdin of `docker cp - ci-runner-<job id>:/run`; the tar holds `ci-runner/jitconfig`
owned by uid 1000, mode 0400, in a directory of mode 0500. The value therefore never
appears in any docker argv element, in any `--env`, `-e` or `--env-file` value (an
`--env-file` is visible in `docker inspect`, so none is used), in a scaler log line, in
the `status` output, or in a host-side file: there is none, so there is nothing to
chmod or delete. If `docker cp` or `docker start` fails, the scaler removes the
created container by its exact name and logs the failure (without the value); the job
keeps its queue place and the next pass starts it again.

`docker create` carries `--cpus`, `--memory` and `--pids-limit` from the job's
profile. The default profile is 4 CPUs, `12g` and 4096 pids. A `profiles` map in the
config keys further profiles off a job label, for example:

    profiles:
      browser-runner:
        cpus: 4
        memory: 16g

A job takes the profile of the first of its own labels that keys a `profiles` entry,
else the default. A profile sets any of `cpus`, `memory` (an integer and one of
`b`, `k`, `m`, `g`) and `pids_limit`, and inherits what it omits from the default
profile; a `default` entry in `profiles` replaces the built-in default fields. An unknown
key or a bad value is rejected at load.

At load the scaler refuses a config that oversubscribes the host. With `host_cpus` the
CPUs of the host (default 12), one core is reserved for the host itself, and
`max_concurrency` times the largest `cpus` of the default profile and every profile must
not exceed `host_cpus` minus 1. The error names both numbers: four containers of 4 CPUs
is 16 against the 11 usable on a 12-CPU host, so that config exits 2 without touching
docker or GitHub. Two containers of 4 CPUs load. Because the built-in `max_concurrency`
is 4, a config that sets neither `max_concurrency` nor `host_cpus` is refused; set
`max_concurrency: 2` (as on the 12-CPU host) or a larger `host_cpus`. This is tested in
`tests/test_runner_scaler_secrets_limits.py`.

### The start deadline and reaping

GitHub can assign a queued job to a runner that then never runs it: the container is
up, the job stays `queued`, and nothing else ever frees the slot. The start deadline
is the cure for that case, a job assigned and never run. A container whose job the
API still reports `queued` more than `start_deadline_s` (default 120) after the
container was created is removed, and since the job is still queued with no
container, the next pass starts a new one for it (a fresh JIT request, a new
`ci-runner-<job id>`). The same pass never restarts a job whose container it just
removed, so the replacement is the next pass's.

Every pass reaps before it starts anything. For each `ci-runner-*` container it reads
the job (`GET /repos/{repo}/actions/jobs/{job id}`) and removes the container when
the job is `completed`, when the job is still `queued` past `start_deadline_s`, or,
whatever the job says, when the container is older than `job_deadline_s` (default
7800: the longest workflow `timeout-minutes` is 120, which is 7200 seconds, plus
margin). A job lookup that fails leaves its container for the next pass. A running
job under the deadline, and any container not named `ci-runner-<job id>`, are never
touched. A `--dry-run` pass prints `would remove ci-runner-<job id>` and removes
nothing.

Removal is always one exact name, `docker rm --force ci-runner-<job id>`: the scaler
never removes by `--filter`, `prune`, a wildcard or a label selector, and refuses a
name that is not `ci-runner-<digits>`. Listing may filter, but it does so client-side
on the name prefix. This is tested in `tests/test_runner_scaler_reaping.py` against
an injected clock.

### API failures and the backoff

A GitHub call that fails does not stop the scaler. A connection error, a timeout,
an unreadable reply, a 5xx, a 403 or a 429 makes that pass skip the call, and the
next pass retries it; `run` never exits and never starts a job twice because of
one. After a failed pass the loop waits longer: the wait doubles from
`poll_interval_s` with each consecutive failed pass (5, 10, 20, 40 seconds at the
default) and is capped at 60 seconds, and one clean pass returns it to
`poll_interval_s`. A 403 or 429 that names a wait sets the next wait instead:
`Retry-After` (seconds or an HTTP date) first, else `x-ratelimit-reset` (epoch
seconds, so the wait is the time left until it). That wait is never shorter than
`poll_interval_s` and is bounded at one hour, so a bad header cannot park the loop.
Any other reply (a 404, say) and a docker failure do not back off.

An unreachable API never removes a running container. Reaping needs a successful
read of the container's job, so while the API is down, rate limited or erroring
every `ci-runner-*` container stays exactly as it was, and the pass that sees the
API again reaps what is then due. Likewise no start is missed or doubled: a job
that has a container is skipped, and one that has none is started on the first
pass that can read the queue. This is tested in
`tests/test_runner_scaler_resilience.py` with an injected sleep and clock, so nothing
really waits.

### Whose jobs the scaler serves

One function, `is_trusted(run)` in `ci/runner_scaler.py`, decides whether a run's
jobs are served; nothing else in the scaler decides. The rule: the run's head
repository must be the scaler's own repository, and its `event` must be `push`,
`workflow_dispatch`, `schedule` or `pull_request` (so a `pull_request` counts only
when it comes from the same repository, never from a fork). Everything else is
skipped and logged as `skipping run <id>: not trusted`: a fork's pull request,
`pull_request_target` (it runs with the base repository's secrets against the
head's code, so it is never served), every other event, and any run whose
repository fields are missing. A skipped run gets no JIT request and no container;
its jobs stay queued for the standing runners or wait. Tightening the rule later is
an edit to `is_trusted`. This is tested in `tests/test_runner_scaler_trust.py`.

The config is read from `--config` (default `~/.config/ralphd-ci/scaler.yaml`).
`repo` and `token_file` are required; the token is read from the file `token_file`
names, and a config that holds an inline token is rejected. The other keys are
`api_url`, `poll_interval_s` (5), `max_concurrency` (4), `start_deadline_s` (120),
`job_deadline_s` (7800), `image`, `docker`, `host_cpus` (12) and a `profiles` map. The
scaler imports no YAML library, so the file is a YAML subset: `key: value` scalars
(strings, optionally quoted; integers; `true`/`false`), `#` comments and nested maps
by space indentation. Lists, flow style, anchors and tabs are rejected with a
line-numbered error.

The scaler's tests (`tests/test_runner_scaler_commands.py`,
`tests/test_runner_scaler_queue.py`, `tests/test_runner_scaler_jit.py`,
`tests/test_runner_scaler_reaping.py`, `tests/test_runner_scaler_resilience.py`,
`tests/test_runner_scaler_secrets_limits.py` and `tests/test_runner_scaler_trust.py`) run it against a local
fake GitHub API and a fake `docker` first on `PATH`
(`tests/_runner_scaler_fakes.py`); no test reaches the network or a docker daemon.

## Concurrency

- `gate.yml` cancels an older run of the same ref: a superseded PR head is not
  worth a runner.
- `heavy.yml`'s jobs are grouped per PR ref, and **all `main` pushes share one
  group** with `cancel-in-progress`. When several merges land in a row the newest
  `main` is the one that gets attested; intermediate shas keep the check runs
  they earned on their own pull requests.
- `nightly.yml` runs its six matrix cells at `max-parallel: 2` and `fail-fast:
  false`. With one eligible slot (`## The self-hosted runner` above) the cells
  run one at a time whatever `max-parallel` says; one red cell must not cancel
  another's claim.

## Reading a `cancelled` conclusion: which of the three causes it was

A job's `conclusion` is `cancelled` for three unrelated causes, and the
Checks/Actions API exposes no field that separates them: a human pressed
Cancel, a `cancel-in-progress` group superseded this run, or the job hit its
own `timeout-minutes`. Only the third is a statement about the commit, and it
is the one a judging hat most needs to not mistake for a verdict. Two
mechanisms make it legible, both printing to the step log **and** appending to
`$GITHUB_STEP_SUMMARY`, which are surfaces a fresh `GET` already returns, and
both keyed on one of two fixed, greppable, mutually exclusive prefixes —
`RALPHD_TIMEOUT_KILL:` and `RALPHD_CANCEL_NOT_TIMEOUT:`:

- **`scripts/run_bounded.sh <seconds> <label> -- <command...>`** wraps every
  step that can consume real wall clock (each tier invocation, each venv
  build, each dev-layout cell) in its own bound, set below
  the job's own, and clamps that bound down further to the job's own
  remaining `timeout-minutes` budget (from `RALPHD_JOB_STARTED_AT` and
  `RALPHD_JOB_BOUND_MINUTES`, less a 120 s tail). It runs the wrapped command
  beside a plain background timer for that bound and blocks on whichever
  finishes first. If the bound wins, it records the expiry -- the
  `RALPHD_TIMEOUT_KILL:` marker line and the `*.expired` evidence file into
  `$RALPHD_TIMEOUT_EVIDENCE_DIR` -- **before it sends the command anything at
  all** (task 004, K.3), then escalates TERM first and only KILLs after a
  grace period if the command ignores TERM, and always exits 1 either way:
  an ordinary `failure`, with the reason in the log, instead of a bare
  `cancelled` — a TERM-ignoring command that needs the KILL is reported
  identically to one that dies to the first TERM, never as a bare 137 that
  the mechanism failed to recognise. A cancel arriving from outside this
  script's own bound -- a human, or `cancel-in-progress` -- lands on this
  script itself (which installs no trap) or on the command directly, before
  the bound's timer could ever fire, so it dies by that raw signal, prints
  nothing and leaves no evidence file, however that outside cancel itself
  had to escalate to reach the command.
- **`scripts/job_timeout_marker.sh <minutes> <label> <job-status>`** is each
  job's last step, under `if: always()` so it also runs on a cancelled job.
  It classifies from those two recorded facts and from **no clock inference
  and no exit status of any kind**: an `*.expired` file present means the
  job's wall clock genuinely ran out, so it prints `RALPHD_TIMEOUT_KILL:`
  naming the step that expired; a job status of `cancelled` with no such
  file means a human cancel or a supersession, so it prints
  `RALPHD_CANCEL_NOT_TIMEOUT:`. It reports elapsed seconds as context but
  never decides on them — an earlier version compared elapsed time against
  the declared bound inside a grace window, which called a cancel 44
  minutes into a 45-minute bound a timeout. With no evidence directory
  exported it prints neither line rather than defaulting to one. It always
  exits 0 and never changes an outcome.

So: **a `cancelled` conclusion carrying `RALPHD_TIMEOUT_KILL:` is a timeout,
and one carrying `RALPHD_CANCEL_NOT_TIMEOUT:` is a cancel, however close to
the job's bound it arrived.** `tests/test_run_bounded_timeout_marker.py` holds
both scripts to that behaviour and holds every job in all three workflows to
using them — the stamp step first (start time, declared bound, a freshly
cleared evidence directory), the classifier last with the job's status, every
long step bounded, and the inner bounds summing under `timeout-minutes` less a
reserve for the steps no wrapper owns (`actions/checkout` and the environment
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
sha declaration of its own any more, and this repository tracks no
`artifacts/` at all. No lane deselects anything from that
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
