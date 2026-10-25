# CI: the lanes, where they run, and what a maintainer needs to touch

ralphd's CI is the [`Jenkinsfile`](../Jenkinsfile), run by the multibranch job
`ralphd/ci` on the operator's homelab Jenkins
(`https://jenkins.wisent-owl.ts.net/`, tailnet only). It replaced GitHub
Actions and the self-hosted runner pool in October 2026: there are no
workflows, no runner scaler and no GitHub Pages site any more. The tiers
themselves (what they cover and what they cost) are `docs/testing.md`; this
page is only about who runs them and when. It never cites a commit sha or a
build's counts: look at the Jenkins build itself.

## The job

- **`ralphd/ci`**: multibranch, GitHub branch source `n-orlov/ralphd`. A push
  reaches Jenkins through a GitHub webhook and builds that branch at once; an
  hourly branch scan catches anything a missed webhook dropped. Only branches
  with a `Jenkinsfile` are built; pull requests are not discovered (a pull
  request's branch is built as a branch). There is no fork trust gate: only
  branches of this repository are ever built.
- **Agent:** label `linux && amd64 && docker`, a host where the agent user can
  run Docker (root-equivalent on that host, which is fine only because every
  build is trusted code). Every lane runs in its own container through that
  Docker.
- Builds of one branch never overlap (`disableConcurrentBuilds`); a build is
  never cancelled by a newer push, it finishes and the next one starts.
- Builds of different branches never overlap either: the whole pipeline holds
  the lockable resource `ralphd-ci` (plugin `lockable-resources` on the
  controller), and a waiting build queues on it. The lanes and cells are sized
  for the whole agent; on 21 Oct 2026 a nightly whose lanes ran beside another
  branch's dev-layout cells took the fast tier past its 1800 s bound.
- The workspace persists between builds, so the Prepare stage first hands back
  whatever the previous build's containers wrote (`ci/jenkins-reap.sh`) and
  then cleans to the checkout (`git clean -ffdx`).

## Stages

| stage | what it does |
|---|---|
| Prepare | Decides whether this is a nightly build, reaps leftovers, builds the two lane images if their tag is missing: `ci/jenkins-image.sh browser` (from `ci/browser/Dockerfile`) and `ci/jenkins-image.sh docker-tier` (`ci/docker-tier/Dockerfile`: Python 3.12 plus the Docker CLI). Each is tagged `ralphd-<name>-ci:<hash>`, the hash of the files under `ci/<name>/`, so an unchanged directory is never rebuilt and nothing is pulled from or pushed to a registry. |
| Tiers | Four lanes in parallel, below. A failed lane marks the build red; the other lanes still finish. |
| Dev-layout cells | Nightly only, below. |

Each lane is `ci/jenkins-lane.sh <lane> <minutes> <image> <docker options> --
ci/jenkins-tier.sh <tier>`. The lane script makes a fresh `git clone --local
--no-hardlinks` of the build's commit under `.lanes/<lane>/src` (full history,
which several tests read; copied, not hardlinked, because the lane hands its
tree to uid 1000 and a shared pack would then belong to it in the workspace
too, which `fs.protected_hardlinks` stops the next lane from linking), mounts it at the same path inside the container, and runs the
tier as uid 1000 (`ci/jenkins-lane-entry.sh`, `setpriv`; several tests
expect a uid-1000 account, as a ralphd job image has), with the host's docker
group added when the socket is mounted; `ci/jenkins-reap.sh` hands the files
back to the agent's uid. Each container is
labelled with the build, so an aborted build's containers are removed by the
lane's own trap or, at the latest, by the next reap.

| lane | stage timeout | image | limits | command |
|---|---|---|---|---|
| Lint | 15 min | `python:3.12.3` (digest-pinned) | 4 CPUs of 4-11 | `ruff check . --select E9,F63,F7,F821` |
| Fast | 45 min | `python:3.12.3` | CPUs 0-3 (`--cpu-shares=4096`), 12 GB, 4096 pids | `pytest -q -m fast -n 4 --dist loadfile` |
| Docker | 120 min | docker-tier | 4 CPUs of 4-11, 12 GB, 4096 pids, host docker socket | `pytest -q -m docker` |
| Browser | 60 min | browser | 4 CPUs of 4-11, 16 GB, 4096 pids, 1 GB shm | `pytest -q -m browser` |

The lanes share one 12-CPU agent. The Fast lane has CPUs 0-3 to itself, with
four times the default CPU weight against the Docker lane's unpinned sibling
containers; the other lanes share CPUs 4-11. Those are hyperthreads: on
int21h-ws CPUs 0-3 sit on the same four cores as 6-9, so the Fast lane is
isolated from other containers' scheduling, not from their load. Several fast tests hold
millisecond-scale deadlines that miss when the lanes contend (the old runner
scaler avoided that by never admitting more than 11 CPUs of jobs at once).
An agent with fewer than 12 CPUs needs these sets changed.

The Docker lane's sibling containers are started on the agent's Docker, which
is why the lane's checkout sits at the same path inside and outside its
container. The tier tags its images `ralphd:test-<RALPHD_RUN_ID>` (unique per
build) and removes them when it exits.

**Nightly lane:** `main` is built every day at 02:23 (`cron`, on `main` only),
and any build started with the `EXTENDED` parameter (off by default) runs the
same way: after the tiers, the Dev-layout cells stage runs the six
`scripts/host_like_dev_layout.sh` cells (`ubuntu:24.04`, `python:3.11-slim`,
`python:3.14-slim`, each with `PYTHONUSERBASE` exported and unset) through
`ci/jenkins-devlayout.sh`, in two waves of three (one per mode), 2 CPUs and
120 minutes each. Every cell gets `PIP_DEFAULT_TIMEOUT=60` and
`PIP_RETRIES=10`, and the `ci-pip-cache` docker volume as its pip cache. The
build description says "nightly".

**Reruns.** No lane reruns failures globally: there is no `--reruns` in
`pyproject.toml` or in the `Jenkinsfile`. A test marked `flaky_infra` (a
host-only pty or signal race, see `docs/testing.md`) gets exactly one rerun
through `pytest-rerunfailures`; a node that fails twice fails the lane.

**Telegram:** every build posts one message to the homelab notifier through
`notifyTelegram` from the homelab shared library (`@Library('homelab')`), last
in `post { cleanup }` so it carries the final result. Only a red or aborted
build of `main`, or a red nightly, makes a sound; everything else arrives
silently.

## Reports

Everything is on the Jenkins build page, built in `post { always }` so a red
build has them too (`ci/jenkins-report.sh`):

- **Test Result**: the lanes' JUnit files. It never marks a build unstable on
  its own: the lanes decide.
- **Allure**: every lane's (and every cell's) Allure results merged into one
  report by `scripts/allure_report.sh`. Its `history/` is archived as
  `allure-history/` and copied into the next build of the same branch, so
  trends carry over per branch. `scripts/allure_env.sh` writes the commit,
  branch, build and image into each tier's results.
- **CI summary**: the lint verdict and ruff output, the per-tier counts from
  `scripts/allure_summary.py`, and every inner bound that ran out (below),
  rendered as HTML by `ci/jenkins-report.sh`: headline tiles, a tier table,
  and a collapsible ruff section (open when lint failed). Light/dark, no
  JavaScript.
- **Build page summary line**: pass/fail/skip/flaky counts, linking to both
  reports.
- **README badges**: `build` (the plugin's own), and `tests` and `lint`, which
  `post { always }` declares on every build ("not run" until the build fills
  them in). Jenkins stays on the tailnet; the homelab's public nginx proxies
  `https://int21h.mooo.com/jenkins-static/ralphd/ci/<branch>/<id>.svg` to the
  badge endpoint, because the `ralphd` folder grants anonymous
  `Job/ViewStatus` (badges only). The README shows `main`'s, each linked to its
  report in the last completed build; those links open on the tailnet only.
- **Artifacts**: the CI summary, the JUnit files, `allure-history/` and the
  timeout evidence.

## Bounds, timeouts and aborts

Every step that can consume real wall clock (each venv build, each tier, each
cell) runs under **`scripts/run_bounded.sh <seconds> <label> -- <command...>`**,
whose bound is set below the lane's stage timeout and clamped further to what
is left of it (`RALPHD_JOB_STARTED_AT`, `RALPHD_JOB_BOUND_MINUTES`, less a
120 s tail). If the bound wins, it records the expiry first -- a
`RALPHD_TIMEOUT_KILL:` marker line in the log and an `*.expired` evidence file
in the lane's `RALPHD_TIMEOUT_EVIDENCE_DIR` -- then sends TERM, KILL after a
grace period, and exits 1: an ordinary failure, with the reason in the log.

The inner bounds per lane (seconds) are pinned and never raised: lint 300 +
300, fast 300 + 1800, docker 600 + 6000, browser 600 + 2400, a dev-layout cell
6300 (plus 300 for the image pull). Each lane's sum stays under its stage
timeout less a 300 s reserve.

So a red lane is one of three things, and the build tells which:

- a test or lint failure: the tier's own output, and the reports;
- an inner bound that ran out: `RALPHD_TIMEOUT_KILL:` in the log and the
  bound's evidence file listed under "Inner bounds that ran out" in the CI
  summary -- this one is a statement about the commit;
- an abort (a person pressing abort, or the stage timeout itself firing when
  no inner bound did): Jenkins shows the stage as aborted, with no evidence
  file. An aborted lane is not a verdict on the commit.

## Guards

`tests/test_jenkinsfile_guard.py` (fast tier) pins this page's rules, each
against a mutated fixture: the stage order and parallel lanes, each lane's
command, image, limits and timeout, each tier's commands and inner bounds
(`ci/jenkins-tier.sh`), that the bounds fit their lanes, the nightly cron,
`EXTENDED` and cells, the reports, the badges, `notifyTelegram` last in
cleanup, the README badge block, and that the lane images are built locally
by content hash. `tests/test_run_bounded_timeout_marker.py` holds
`scripts/run_bounded.sh` to the behaviour above.

## Changing CI

- A new tier or lane: a stage under Tiers, an arm in `ci/jenkins-tier.sh`, the
  guard's tables, this page and `docs/testing.md`.
- A lane image: edit `ci/browser/` or `ci/docker-tier/`; the next build sees
  a new hash and builds it once on the agent.
- A badge: declare it in `post { always }` and add it to the README block; the
  public proxy needs no change.
- Jenkins itself (plugins, agents, the job, credentials, the badge proxy) is
  configured as code in the operator's homelab repository, not here.
