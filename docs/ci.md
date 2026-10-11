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

Five commands exist, four subcommands and one mode:

- `once` runs one pass and exits 0.
- `run` runs passes in a loop, one every `poll_interval_s` seconds; `--passes N`
  stops it after `N` passes.
- `status` prints the scaler's containers (those named `ci-runner-*`) as a JSON
  list, each entry carrying the container name, its job id and its age in seconds.
- `prune-images` removes old images by exact name (below).
- `--dry-run`, on `once`, `run` and `prune-images`, makes a pass a rehearsal.

A dry run prints each start a pass would make, as
`would start ci-runner-<job id> for job <job id>`, and issues no `docker run` or
`docker create`. An unknown command exits non-zero.

### Selection, idempotence and the cap

A pass serves a queued job only when its labels include `ci-pool`; a queued job
without it is left to the standing runners. Jobs are served in ascending job-id
order, so the oldest job is started first. Each started container is named
`ci-runner-<job id>`, and that name is the scaler's only state: a job that
already has a `ci-runner-<job id>` container, in any state, is skipped, so two
passes over the same queue start one container per job. A container that has
exited is removed at the start of the next pass (see the reaping section), and that
pass still skips its job; a job still `queued` after that is started again by the
pass after it. `max_concurrency` (default 4)
caps the containers alive at once. A pass counts the `ci-runner-*` containers that
have not exited and starts only as many jobs as free slots remain, so ten queued
jobs at the default start four, and the next pass starts one job for each container
that has since exited, again in job-id order. `--dry-run` applies the same rules.

The cap is a host-wide invariant, not a property of one pass's snapshot. Several passes
or scaler processes can serve one pool on a host, and the container list a pass read at
its start can be stale by the time it starts a job (an image build can take minutes,
and another process may have started runners meanwhile). So each start is one step under
an exclusive `flock` on `admission_lock_file` (default: a per-user file in the temp
directory; every scaler process serving the same pool must use the same file). Under that
lock the pass reads the containers again, skips the job if its `ci-runner-<job id>`
container now exists (no JIT request, no `docker create`, so a name conflict never
consumes a runner registration), stops if the live containers already reach
`max_concurrency`, and otherwise requests the JIT config and starts the container
before releasing the lock. The next job's start takes the lock and reads again, so
no more than `max_concurrency` runner containers are admitted at once, and the job-id
order and the one-container-per-job rule are unchanged. A job whose container was reaped
earlier in the same pass is still not restarted in that pass. This is tested in
`tests/test_runner_scaler_admission.py` with stale snapshots forced after the first read
and after an image build, and with two scaler processes on one pool.

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
container issues none either. A failed or timed-out request (no reply, a reply
without an `encoded_jit_config`, or any HTTP error: a 5xx, a 403, a 429, and a 4xx
such as a 401 or a 422 alike) starts no container for that job and takes no
concurrency slot, and it ends the pass (see "API failures and the backoff"): no
other job is asked about or started, and the next pass, after the failure backoff,
requests again. The request is tested in
`tests/test_runner_scaler_jit.py`.

### The runner image and the run-once exit

`ci/runner/Dockerfile` is the recipe for the image every scaler container runs (the
`image` key of the config names the repository it is built to; see the image refresh below). It builds `FROM ubuntu@sha256:<digest>`,
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
registered the single-use runner. The exited container is removed by exact
name by the next pass, as described in the start deadline and reaping section:
exit cleanup is unconditional, so it reads no job and holds whatever the job's
status is (`queued`, `in_progress` or `completed`) and while the API is unreachable.
The config file is put into the
container as described in the next section. The recipe is checked by parsing,
never by a build: `tests/test_runner_image.py` asserts the digest base, the SHA-256
check, no `latest` tag and `USER 1000` on the file, fails each assertion on a mutated
copy that lacks it, and asserts the start argv.

### The secret path and the container limits

The JIT config is a credential, and the scaler starts a container in two docker
calls, in this order: `docker create`, `docker start`. The config reaches the
container as the file `/run/ci-runner/jitconfig`, a read-only bind mount
(`-v <host file>:/run/ci-runner/jitconfig:ro` on the `docker create` argv) of a
temporary host file; it is never copied into the container's filesystem, so no image
or container layer holds it. The host file is created mode 0600, owned by the runner's
uid, in a fresh private directory (mode 0700) made just for it under a tmpfs-style
mount: the config key `secret_dir` if set, else `$XDG_RUNTIME_DIR`, else `/dev/shm`,
else the temporary directory (the mode-0600 temporary-file alternative); a
`secret_dir` that is not a directory is refused. The runner is uid 1000, the
config key `runner_uid` (default 1000, the image's `USER`): when the scaler runs as
another user it hands the empty file to that uid with `chown` before it writes the
value, and if that is refused (the scaler is not root) it fails closed, starts no
container and logs the refusal without the value. The scaler removes the file and its
directory as soon as `docker start` returns, and also when `docker create` or
`docker start` fails; the running container keeps its mount of the unlinked file, so
the runner still reads it. The value therefore never appears in any docker argv
element, in any `--env`, `-e` or `--env-file` value (an `--env-file` is visible in
`docker inspect`, so none is used), in a scaler log line, in the `status` output, or
in a host file that outlives the start. If `docker start` fails, the scaler removes the
created container by its exact name and logs the failure (without the value); the job
keeps its queue place and the next pass starts it again. This is tested in
`tests/test_runner_scaler_secrets_limits.py`, whose fake `docker` records the bind
mount and the file's content, mode, owner and directory mode at the moment of start.

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

### The shared caches, the docker socket and the image refresh

Every container mounts the named docker volume `ci-pip-cache`, writable, at
`/home/ubuntu/.cache/pip`, the runner user's pip cache directory, so pip downloads
survive the container. A job whose labels include `browser-runner` also gets the host's
existing `/opt/ms-playwright` bind-mounted at the same path, read-only (`:ro`); every
other job has no such mount. The scaler checks the volume with `docker volume inspect`
and creates it with `docker volume create ci-pip-cache` when the host lacks it; the
volume is named exactly and no volume is ever removed.

The docker socket follows the job's labels. A job whose labels include `docker-host`
gets `-v /var/run/docker.sock:/var/run/docker.sock`, the same exposure the standing
runners have; a job without that label gets no socket mount at all. A rootless or
nested daemon is out of scope.

The image tag is not the config's `image` verbatim. The `image` key names the
repository; the tag is the first 12 hex digits of a SHA-256 over the recipe, which is
`ci/runner/Dockerfile` and the `run-once.sh` it copies (`runner_dir` in the config
points at that directory and defaults to `ci/runner` beside the scaler). A pass that is
about to start a container asks the host for that exact `repository:hash` with
`docker image inspect`. If the tag is missing it builds it with
`docker build -t repository:hash <runner_dir>`, once, under an exclusive `flock` on the
file named by `lock_file` (default: a per-user file in the temp directory). A second
pass or a second scaler process that finds the lock held waits for it, looks again,
finds the tag built and builds nothing. An edit to the recipe changes the hash and so
the tag: the new tag is built before the next start, and the old tag stays for the
retention command. A dry run builds nothing, and a failed build fails the pass before
any start, leaving the jobs queued for the next pass. A build alone may take longer
than the 120 s every other docker call gets, so its timeout is 3600 s.
`tests/test_runner_scaler_caches_refresh.py` covers the mounts, the volume creation,
the socket rule, the single build with a held lock (the fake `docker` holds a build
until `FAKE_DOCKER_BUILD_GATE` exists) and the new tag for a changed recipe.

### Image retention

`prune-images` keeps the host's image store from growing without bound. It lists the
images with `docker images`, picks the ones that fall under the scaler's own naming
scheme, and removes each with `docker rmi <name:tag>`, one exact `name:tag` per call.
Its argv never carries `prune`, a `--filter` or a wildcard, and an image outside the
scheme is never a candidate. Two kinds of tag are in the scheme, and nothing else is:

- Runner-image tags: `<repository>:<12 hex digits>`, where the repository is the config's
  `image` without its tag (the tag the image refresh builds). The newest `keep` of them
  by creation time (default 3) stay; every older one is removed. The `latest` tag and any
  other tag of that repository are not in the scheme.
- CI job-image tags: `ralphd:test-ci-<run id>-<attempt>` and its `-base` twin, the
  per-run tags `heavy.yml` builds. One is removed once it is more than `retain_days` days
  old (default 14) and no container, in any state, uses it. Docker records no last-use
  time, so the age is the image's creation time.

A tag a container still uses, the tag the current recipe builds, and an image with no
creation time are all kept. A failed `rmi` is logged and the rest go on. `--dry-run`
prints `would remove image <name:tag>` for each and removes none. The command is run by
hand or from a timer; the scaler's own loop never calls it.
`tests/test_runner_scaler_prune.py` lists runner tags, job-image tags and unrelated
images in the fake `docker` and asserts exactly which `rmi` calls are made.

### The start deadline and reaping

GitHub can assign a queued job to a runner that then never runs it: the container is
up, the job stays `queued`, and nothing else ever frees the slot. The start deadline
is the cure for that case, a job assigned and never run. A container whose job the
API still reports `queued` more than `start_deadline_s` (default 120) after the
container was created is removed, and since the job is still queued with no
container, the next pass starts a new one for it (a fresh JIT request, a new
`ci-runner-<job id>`). The same pass never restarts a job whose container it just
removed, so the replacement is the next pass's.

Every pass reaps before it starts anything, in two kinds that differ in what they
need from the API.

- **Unconditional removal.** Two rules read no job and ask the API nothing. The
  hard deadline: a container older than `job_deadline_s` (default 7800: the longest
  workflow `timeout-minutes` is 120, which is 7200 seconds, plus margin) is removed
  whatever its job says. Exit cleanup: a container whose docker state is `exited` or
  `dead` is removed however young it is and whatever its job says, because the
  run-once runner inside it has finished (it ran its job, crashed, or never
  connected), so this is the `--rm`-equivalent cleanup after exit. Both hold while
  the API is down, rate limited or erroring, and both are applied first, before the
  queue is read: an overdue or exited container goes even in a pass that then
  cannot read the queue. A job whose exited container is removed while the job is
  still `queued` is started again by the next pass, like any job with no container.
- **Removal that needs a job read.** For every other `ci-runner-*` container (one
  that has not exited and is younger than `job_deadline_s`) the scaler reads the job (`GET /repos/{repo}/actions/jobs/{job id}`) and removes the
  container when the job is `completed`, or when it is still `queued` past
  `start_deadline_s`. This kind needs a successful read. A job lookup that fails
  (a timeout, a 5xx, a 403, a 429, or a 4xx such as a 404 for an unknown job) ends
  the pass there and leaves its container, and every container not yet read, for
  the next pass, however old it is short of `job_deadline_s`; an unavailable API is
  never itself a reason to remove a running container.

The lifecycle of one runner container, as `tests/test_runner_scaler_reaping.py`
exercises it with an injected clock and the fake docker: a pass starts
`ci-runner-<job id>` for a `queued` job; the run-once runner takes the job and
exits when it ends (or crashes, or never connects), and docker reports the
container `exited`; the next pass, at most one `poll_interval_s` later, removes
it by exact name at the start of that pass while its job is still `queued` or
`in_progress`, without waiting for the job to become `completed` and without a
job read; its slot is free in that same pass for another job. A removal docker
refuses leaves the container, the pass goes on, and the next pass tries again.
These timings come from the tests' injected clock, not from a live docker host,
where the scaler has not been run.

A running job under the deadline, and any container not named
`ci-runner-<job id>`, are never touched. A `--dry-run` pass prints
`would remove ci-runner-<job id>` with the reason and removes nothing.

Removal is always one exact name, `docker rm --force ci-runner-<job id>`: the scaler
never removes by `--filter`, `prune`, a wildcard or a label selector, and refuses a
name that is not `ci-runner-<digits>`. Listing may filter, but it does so client-side
on the name prefix. This is tested in `tests/test_runner_scaler_reaping.py` against
an injected clock, including the hard deadline and exit cleanup while every job
read, or the queue read, fails.

### API failures and the backoff

A GitHub call that fails does not stop the scaler, but it ends the pass. Every
failed call does: a connection error, a timeout, an unreadable or incomplete reply,
an unreadable token file, and any HTTP error status, a 5xx, a 403 or a 429 as much
as a 400, 401, 404 or 422. Such a failure ends the current scaling pass at that
call, wherever it falls: the queue discovery (the run
listing and each run's job list), a job read for reaping, or the JIT request. The
pass then makes no further API request and starts no further container; containers
that were already running, or that this pass had already started, are kept. The
loop retries on the next pass, after the failure backoff below, and that pass
serves every queued job once, so no job is missed or started twice; `run` never
exits. The pass-level retry rule is the same for all of these calls, and the
unconditional removals (the hard deadline and the exit cleanup) have already run
before any call is made. No failed call is skipped over to serve another job in the
same pass. One consequence: a job read that keeps failing (a 404 for a job GitHub
no longer knows, say) holds every pass at that read until its container exits or
passes `job_deadline_s`, when the unconditional removals take it with no read.
After a failed pass the loop waits longer: the wait doubles from
`poll_interval_s` with each consecutive failed pass (5, 10, 20, 40 seconds at the
default) and is capped at 60 seconds, and one clean pass returns it to
`poll_interval_s`. A reply that names a wait sets the next wait instead:
`Retry-After` (seconds or an HTTP date) first, else `x-ratelimit-reset` (epoch
seconds, so the wait is the time left until it). That applies to a 403, a 429
and any 5xx, so a 503 with `Retry-After` is waited out as asked; `x-ratelimit-reset`
is skipped when the reply's `x-ratelimit-remaining` is above 0, because the limit
is then not what failed. A header that is not a finite, non-negative number or an
HTTP date is ignored, and the plain backoff applies. A named wait is never shorter
than `poll_interval_s` and is bounded at one hour, so a bad header cannot park the
loop. The 60-second backoff cap holds for a failure streak of any length: the
doubling stops at the cap, so no exception, an arithmetic overflow included, can
leave `run_loop`.
A docker failure is not an API call: it does not back off, and a failed start
skips only that container.

An unreachable API never removes a running container. Removal other than the
hard deadline and exit cleanup needs a successful read of the container's job, so
while the API is down, rate limited or erroring every `ci-runner-*`
container that has not exited and is younger than `job_deadline_s` stays exactly as it was, and the pass that
sees the API again reaps what is then due. A container past `job_deadline_s`, or one
that has exited, is removed regardless, because those rules read nothing; the
outage is never the reason for either removal. Likewise no start is missed or doubled: a job
that has a container is skipped, and one that has none is started on the first
pass that can read the queue. This is tested in
`tests/test_runner_scaler_resilience.py` with an injected sleep and clock, so nothing
really waits, and the pass-level stop for each failing call (queue discovery, job
reads, JIT generation, over a multi-job queue) in
`tests/test_runner_scaler_pass_failures.py`.

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
`job_deadline_s` (7800), `image`, `docker`, `keep` (3), `retain_days` (14), `host_cpus` (12), `secret_dir`, `runner_uid` (1000), `lock_file`, `admission_lock_file` and a `profiles` map. The
scaler imports no YAML library, so the file is a YAML subset: `key: value` scalars
(strings, optionally quoted; integers; `true`/`false`), `#` comments and nested maps
by space indentation. Lists, flow style, anchors and tabs are rejected with a
line-numbered error.

The scaler's tests (`tests/test_runner_scaler_commands.py`,
`tests/test_runner_scaler_queue.py`, `tests/test_runner_scaler_jit.py`,
`tests/test_runner_scaler_reaping.py`, `tests/test_runner_scaler_resilience.py`,
`tests/test_runner_scaler_secrets_limits.py`, `tests/test_runner_scaler_prune.py`, `tests/test_runner_scaler_unit_runbook.py` and `tests/test_runner_scaler_trust.py`) run it against a local
fake GitHub API and a fake `docker` first on `PATH`
(`tests/_runner_scaler_fakes.py`); no test reaches the network or a docker daemon.

## Adopting the elastic pool

This is the operator's runbook, in the order to do it. The scaler is run by hand
first and by the unit afterwards; nothing here changes a workflow or a standing
runner until its last step.

**Host preconditions the scaler cannot fix.** The host's NIC and network fault
recorded in #303 is an unresolved host-level problem: a runner container that cannot
reach GitHub reliably never takes its job, so the scaler's start deadline reaps it
and the job stays queued. The scaler backs off and retries (see "API failures and the
backoff"), and it does not repair the link. Fix or rule out the network on the host
before adopting the pool. Docker must run on the host, and the user running the unit
must be in the `docker` group.

**The per-job cost the profiles assume.** The default profile bounds one job at 4
CPUs, `12g` of memory and 4096 pids, and the sample config's `browser-runner` profile
raises the memory to `16g`. Those limits are the cost each job is budgeted. They are
not measured by the scaler: compare them with `docker stats` on a live job before
raising a profile. `max_concurrency` times the largest profile's `cpus` must fit in
`host_cpus` minus one reserved core, which is why the sample config sets
`max_concurrency: 2` on a 12-CPU host.

1. **Build the image.** The scaler builds the runner image itself, tagged by a hash
   of the recipe, on the first start of a pass, under a file lock. To find a broken
   recipe or a blocked download before any job waits on it, build the recipe by hand
   from the checkout; this fills the layer cache, so the scaler's own build is then
   quick:

   ```sh
   docker build ci/runner
   ```

2. **Write the config.** Copy `ci/runner/scaler.sample.yaml` to
   `~/.config/ralphd-ci/scaler.yaml`, then put the GitHub token alone in the file
   `token_file` names, mode 0600. The sample config is:

   ```yaml
   repo: n-orlov/ralphd
   token_file: ~/.config/ralphd-ci/github-token
   max_concurrency: 2
   host_cpus: 12
   poll_interval_s: 5
   profiles:
     browser-runner:
       cpus: 4
       memory: 16g
   ```

3. **Rehearse one pass.** The dry run prints the starts a pass would make and issues
   none:

   ```sh
   python3 ci/runner_scaler.py once --dry-run
   ```

4. **Enable the unit.** `ci/runner/ralphd-ci-scaler.service` runs
   `ci/runner_scaler.py run` with `Restart=always` and reads
   `~/.config/ralphd-ci/scaler.yaml`. Copy it to `~/.config/systemd/user/`, point its
   `ExecStart` at the checkout, then:

   ```sh
   systemctl --user daemon-reload
   systemctl --user enable --now ralphd-ci-scaler
   ```

5. **Verify with `status`.** While a `ci-pool` job runs, the scaler's containers are
   listed with their job ids and ages:

   ```sh
   python3 ci/runner_scaler.py status
   ```

6. **Retire the standing runners one at a time.** For one standing runner, remove its
   custom labels first (`ralphd-vps`, `docker-host`, `browser-runner` or `fast-lane`),
   so no new job lands on it, and wait for the job it holds to finish. Then watch that
   the pool takes the next jobs with `status` before removing the next runner. A
   workflow moves to the pool by adding the `ci-pool` label to its `runs-on`, which is
   a change to the workflow and is not part of this runbook.

Housekeeping is a separate command, not part of the loop. Run the rehearsal first,
then the removal:

```sh
python3 ci/runner_scaler.py prune-images --dry-run
python3 ci/runner_scaler.py prune-images
```

The runbook is held to the code by `tests/test_runner_scaler_unit_runbook.py`: every
command and config key named in this section must exist in `ci/runner_scaler.py`,
the unit must parse and carry `Restart=always`, the sample config must load, and a
config holding an inline token is refused.

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
