#!/usr/bin/env bash
# Describe a CI build to the Allure report: write `environment.properties` and
# `executor.json` into an Allure results directory (docs/testing.md, "Allure
# results"). Allure shows the first as the report's Environment widget and the
# second as the executor link back to the Jenkins build.
#
#   scripts/allure_env.sh <results-dir>
#
# Everything is read from the environment, so a test can pin it:
#   GIT_COMMIT, BRANCH_NAME, JOB_NAME, BUILD_NUMBER, BUILD_URL
#                        -- set by Jenkins (ci/jenkins-lane.sh passes them on);
#   ALLURE_RUNNER_IMAGE  -- the image the lane ran in;
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

if [ -n "${JOB_NAME:-}" ] && [ -n "${BUILD_NUMBER:-}" ]; then
  build_name="$JOB_NAME #$BUILD_NUMBER"
else
  build_name=unknown
fi

json_escape() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }

mkdir -p "$dir"

{
  echo "sha=$(val GIT_COMMIT)"
  echo "branch=$(val BRANCH_NAME)"
  echo "job=$(val JOB_NAME)"
  echo "build=$(val BUILD_NUMBER)"
  echo "python=$python_version"
  echo "runner_image=$(val ALLURE_RUNNER_IMAGE)"
} > "$dir/environment.properties"

{
  printf '{\n'
  printf '  "name": "Jenkins",\n'
  printf '  "type": "jenkins",\n'
  printf '  "buildOrder": "%s",\n' "$(json_escape "$(val BUILD_NUMBER)")"
  printf '  "buildName": "%s",\n' "$(json_escape "$build_name")"
  printf '  "buildUrl": "%s",\n' "$(json_escape "$(val BUILD_URL)")"
  printf '  "url": "%s"\n' "$(json_escape "$(val BUILD_URL)")"
  printf '}\n'
} > "$dir/executor.json"
