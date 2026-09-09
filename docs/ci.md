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
| `gate.yml` | records lint, sha guards, `-m fast` (which is a superset of `-m smoke`) | GitHub-hosted `ubuntu-latest` | every pull request, every push to `main` | 30 min |
| `heavy.yml` | `-m docker` (24 tests), `-m browser` (45 tests), as two separate jobs | self-hosted `ralphd-vps` | every pull request, every push to `main` | 120 / 60 min |
| `nightly.yml` | both `scripts/host_like_dev_layout.sh` modes, whole suite unfiltered | self-hosted `ralphd-vps` | 02:00 UTC, plus manual dispatch | 180 min per mode |

`gate.yml` is on GitHub's own runners deliberately: it is the lane that runs
most often, it needs neither a docker socket nor a browser, and a check run
signed by GitHub's app is an attestation this repository cannot fabricate —
which is the property #113 is buying.

## The self-hosted runner

One runner, labelled `ralphd-vps` and `docker-host`, registered against this
repository (not an org). It runs **one job at a time**, which is a feature: the
docker and browser lanes serialise instead of contending for the same four
cores, and each still reports its own check run.

Host: 4 x86_64 cores, 7 GB RAM plus a 4 GB swapfile, docker 29.4.0, Ubuntu
24.04. Provisioned with Node 22 (from the official tarball, so no third-party
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
- `nightly.yml` runs its two modes with `max-parallel: 1` and `fail-fast:
  false` — one runner, and one red mode must not cancel the other's claim.

## The one carve-out

Every lane deselects exactly one node id:

    tests/test_sweep_sha_guard.py::test_every_declared_sweep_sha_is_current_or_discharged

That test asserts every `sweep-sha` declared under `artifacts/reports/` is
literally `HEAD` or discharged to `HEAD` by a hand-maintained chain. The claim is
HEAD-relative by construction, so it can hold on exactly one commit per wave —
the sha the wave ends at. It already fails for 17 records at `3bd7199`, where
three report-only commits moved `HEAD` past the v0.19.1 sweep shas, and it can
never pass on a pull-request head. Leaving it in would paint every gate red for
a reason unrelated to the change under review.

Nothing else in that module is deselected: `test_every_sweep_record_declares_a_sha`
and the other 24 tests still run, so a record that declares no sha at all is
still caught.

This is a symptom, not a policy. #174 is the guard's own defect
(self-anchoring reopens its gap on every report-only commit); #113 step 4
retires the discharge-chain machinery altogether once check runs are the
attestation review reads.

## Not done yet

- **#113 step 3** — teach the review contract that "all lanes green at the
  shipped sha" is verified with `GET /repos/.../commits/{sha}/check-runs`, not by
  parsing a self-authored ledger.
- **#113 step 4** — shrink `tests/sweep_sha_guard.py` to a thin "every report
  names its sha" lint, and drop the discharge chains, the fixed-point ritual and
  the `LAUNCH_SHA` constant.
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
