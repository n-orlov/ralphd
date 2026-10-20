#!/usr/bin/env bash
# ci/jenkins-tier.sh <lint|fast|docker|browser> -- one test tier, exactly as
# the Jenkinsfile's lane runs it: inside the lane's container, as the agent's
# uid, in the lane's own checkout (ci/jenkins-lane.sh). Every step that can
# take real time is wrapped in scripts/run_bounded.sh with the bound the tier
# has always had (docs/ci.md). A tier with results always describes its run
# for Allure (scripts/allure_env.sh) and leaves allure-results-<tier>/ and
# junit-<tier>.xml in the checkout, whatever the tests did.
set -euo pipefail

tier=${1:?"usage: ci/jenkins-tier.sh <lint|fast|docker|browser>"}

describe() {
  local py=python3
  [ -x .venv/bin/python ] && py=.venv/bin/python
  PYTHON=$py scripts/allure_env.sh "allure-results-$tier" || true
}

report_cpu() {
  echo "cores: $(nproc)"
  if [ -f /sys/fs/cgroup/cpu.max ]; then
    echo "cpu quota (cgroup v2 cpu.max): $(cat /sys/fs/cgroup/cpu.max)"
  else
    echo "cpu quota: not exposed by this cgroup layout"
  fi
}

case "$tier" in
  lint)
    scripts/run_bounded.sh 300 "lint venv build" -- bash -euo pipefail -c '
      rm -rf .venv
      python3 -m venv .venv
      .venv/bin/python -m pip install --quiet --upgrade pip
      .venv/bin/python -m pip install --quiet "ruff>=0.4"
    '
    # The CI summary shows ruff's own output (ci/jenkins-report.sh).
    scripts/run_bounded.sh 300 "ruff check" -- .venv/bin/ruff check . --select E9,F63,F7,F821 2>&1 | tee ruff-report.txt
    ;;

  fast)
    trap describe EXIT
    scripts/run_bounded.sh 300 "venv build" -- bash -euo pipefail -c '
      rm -rf .venv
      python3.12 -m venv .venv
      .venv/bin/python -m pip install --quiet --upgrade pip
      .venv/bin/python -m pip install --quiet -e ".[dev]"
    '
    export PATH="$PWD/.venv/bin:$PATH"
    python -V
    report_cpu
    echo "HOME: $HOME"
    python -m pytest --version
    scripts/run_bounded.sh 1800 "fast tier" -- python -m pytest -q -m fast -n 4 --dist loadfile --durations=15 --alluredir=allure-results-fast --junitxml=junit-fast.xml
    ;;

  docker)
    trap 'describe; docker image rm -f "ralphd:test-${RALPHD_RUN_ID}" "ralphd:test-${RALPHD_RUN_ID}-base" >/dev/null 2>&1 || true' EXIT
    : "${RALPHD_RUN_ID:?the docker tier tags its images with RALPHD_RUN_ID}"
    scripts/run_bounded.sh 600 "venv build" -- bash -euo pipefail -c '
      rm -rf .venv
      python3 -m venv .venv
      .venv/bin/python -m pip install --quiet --upgrade pip
      .venv/bin/python -m pip install --quiet -e ".[dev]"
      .venv/bin/python -V
    '
    report_cpu
    echo "mem: $(free -m | awk '/^Mem/{print $2}') MB"
    docker version --format 'docker client={{.Client.Version}} server={{.Server.Version}}'
    scripts/run_bounded.sh 6000 "docker tier" -- .venv/bin/python -m pytest -q -m docker --durations=15 --alluredir=allure-results-docker --junitxml=junit-docker.xml
    ;;

  browser)
    trap describe EXIT
    scripts/run_bounded.sh 600 "venv build" -- bash -euo pipefail -c '
      rm -rf .venv
      python3 -m venv .venv
      .venv/bin/python -m pip install --quiet --upgrade pip
      .venv/bin/python -m pip install --quiet -e ".[dev]"
    '
    # Chrome cannot use its sandbox in an unprivileged container.
    mkdir -p .playwright
    printf %s '{"browser":{"launchOptions":{"args":["--no-sandbox"]}}}' > .playwright/cli.config.json
    cat .playwright/cli.config.json; echo
    # The toolchain comes from the image (ci/browser/Dockerfile).
    command -v playwright-cli
    playwright-cli --version
    command -v google-chrome
    google-chrome --version
    echo "PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH:-<unset>}"
    report_cpu
    scripts/run_bounded.sh 2400 "browser tier" -- .venv/bin/python -m pytest -q -m browser --durations=15 --alluredir=allure-results-browser --junitxml=junit-browser.xml
    ;;

  *)
    echo "ci/jenkins-tier.sh: unknown tier $tier" >&2
    exit 2
    ;;
esac
