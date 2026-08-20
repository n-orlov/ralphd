# Real-build tier for the derived job image (task 039, #20 H2)

Recorded: 2026-08-20, inside this selfdev run's own job container
(`selfdev-v06-release`), against the host docker daemon reached through the
mounted socket. Engine: docker 29.5.3, legacy builder (`DOCKER_BUILDKIT=0`).

## Verdict: it runs here, as a test — no operator-verified-on-host fallback

Task 039 allowed for the possibility that a real image build is impossible from
inside a job container and would have to be recorded as verified by hand on the
host. It is not impossible, and nothing here is hand-verified:
`tests/test_image_real_build.py` is a normal `-m docker` module that builds a
real minimal base, calls the **production** resolve
(`main.resolve_job_image(None, base=...)`) over this checkout, and runs the
resulting image.

Why it works where a `docker run -v` test needs the host-path wrapper: a build
context is *streamed to the daemon by the client*, so paths that only exist
inside this container (`/tmp/...`, `/workspace`) are fine — nothing is
bind-mounted — and the containers that verify the image need no mounts at all
to answer `--version`. This supersedes the by-hand
`artifacts/derived-image-smoke.log` written while task 034 landed (run-dir
artifact), which explicitly left this tier to task 039.

## What the tier asserts

| test | what would have to break for it to fail |
| --- | --- |
| `tests/test_image_real_build.py::test_the_production_resolve_builds_a_derived_tag_from_the_minimal_base` | the generated recipe does not build, or the built tag is not `ralphd-derived:<hash>` |
| `::test_the_derived_image_runs_ralphd_engine` | `ralphd-engine` is missing/unimportable, `container/entrypoint.sh` does not reach it (asserted via the engine's own `no PRD at` exit 2), or the uid-1000/`/workspace`/venv-on-PATH run contract is not reproduced on a base that had none of it |
| `::test_the_derived_image_layers_onto_the_base_and_carries_the_pins` | the base was replaced rather than layered onto (marker file), `pi` is not at the version `container/Dockerfile` pins, node is older than the pinned nodesource major, or a tool the engine shells out to (`docker git jq rg ps python3`) is absent |
| `::test_a_second_resolve_of_the_same_base_is_a_pure_cache_hit` | the cache rule is wrong against a real daemon: a second resolve rebuilds (budgeted at 30s against a ~90s build) or the image id moves |
| `::test_the_build_does_not_ask_for_buildkit` | the production builder choice stops defaulting to the legacy builder, which is the only one a job container's static docker client can use |

The base is `debian:bookworm-slim` plus a marker file — deliberately the barest
base with a package manager: no python, no node, no curl, no user 1000. Its tag
is unique per run (`ralphd-test-base:real-build-<uuid>`), because the derived
hash covers the base *reference*: a stable tag would let the second run of this
tier find the derived tag present and assert nothing about building.

## Cost and cleanup

~105s for the module (one cold build; layers cannot be reused across runs
because a fresh base image id invalidates all of them — which is exactly H2's
invalidation rule). Skips cleanly with no socket (verified: 5 skipped, 0.05s,
with `docker` off `PATH`).

Both images are removed by exact tag at teardown; every container the module
starts is `--rm` and carries `ralphd.run=<RALPHD_RUN_ID>` **and**
`ralphd.role=sibling`, so a role-filtered reap finds them and can never match
this job's own container. The derived image is built by production code, which
labels nothing — it is tracked by the tag the resolve returns.

## Evidence

```
$ python -m pytest tests/test_image_real_build.py -m docker -q
.....                                                                    [100%]
5 passed in 105.14s (0:01:45)
```

Full log: `artifacts/task-039-real-build-tier.log` in this run's run dir.

Mutation-checked before commit: removing `ENV PATH=/opt/ralphd-venv/bin:$PATH`
from the generated recipe in `src/ralphd/cli/image.py` still *builds*, and
`::test_the_derived_image_runs_ralphd_engine` fails with `executable file not
found in $PATH` — the tier fails for the reason a broken recipe would fail, not
merely when docker is absent.
