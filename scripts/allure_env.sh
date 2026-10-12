#!/usr/bin/env bash
# Describe a CI run to the Allure report: write `environment.properties` and
# `executor.json` into an Allure results directory (docs/testing.md, "Allure
# results"). Allure shows the first as the report's Environment widget and the
# second as the executor link back to the run.
#
#   scripts/allure_env.sh <results-dir>
#
# Everything is read from the environment, so a test can pin it:
#   GITHUB_SHA, GITHUB_REF, GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT,
#   GITHUB_SERVER_URL, GITHUB_REPOSITORY  -- set by GitHub Actions;
#   ALLURE_RUNNER_IMAGE  -- the image the job ran in (the workflow passes the
#                           job container's image, or the runner's OS);
#   PYTHON               -- the interpreter to report (default python3).
# A value that is not set is written as `unknown`: the keys are always present.
set -euo pipefail

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
  echo "usage: $0 <allure-results-dir>" >&2
  exit 2
fi
dir=$1

val() { local v=${!1-}; printf '%s' "${v:-unknown}"; }

python_bin=${PYTHON:-python3}
if python_version=$("$python_bin" -c 'import platform; print(platform.python_version())' 2>/dev/null); then
  :
else
  python_version=unknown
fi

server=${GITHUB_SERVER_URL:-https://github.com}
if [ -n "${GITHUB_REPOSITORY:-}" ] && [ -n "${GITHUB_RUN_ID:-}" ]; then
  run_url="$server/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID"
else
  run_url=unknown
fi

json_escape() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }

mkdir -p "$dir"

{
  echo "sha=$(val GITHUB_SHA)"
  echo "ref=$(val GITHUB_REF)"
  echo "run_id=$(val GITHUB_RUN_ID)"
  echo "run_attempt=$(val GITHUB_RUN_ATTEMPT)"
  echo "python=$python_version"
  echo "runner_image=$(val ALLURE_RUNNER_IMAGE)"
} > "$dir/environment.properties"

{
  printf '{\n'
  printf '  "name": "GitHub Actions",\n'
  printf '  "type": "github",\n'
  printf '  "buildOrder": "%s",\n' "$(json_escape "$(val GITHUB_RUN_ID)")"
  printf '  "buildName": "ci run %s",\n' "$(json_escape "$(val GITHUB_RUN_ID)")"
  printf '  "buildUrl": "%s",\n' "$(json_escape "$run_url")"
  printf '  "url": "%s"\n' "$(json_escape "$run_url")"
  printf '}\n'
} > "$dir/executor.json"
