#!/usr/bin/env bash
# ci/jenkins-report.sh [<previous allure-history dir>] -- the Jenkins build's
# reports, built after every build (the Jenkinsfile's `post { always }`)
# from whatever the lanes left in .lanes/:
#
#   allure-results/        every lane's and nightly cell's Allure results, merged
#   allure-report/         the Allure report (scripts/allure_report.sh), published
#                          as the build's "Allure" HTML report
#   allure-history/        that report's history/, archived so the next build of
#                          the same branch carries the trend on
#   junit/                 the tiers' JUnit XML, for Jenkins' Test Result page
#   ci-summary/index.html  the lint verdict (LINT_RESULT, set by the
#                          Jenkinsfile) with ruff's output, the per-tier table
#                          and rerun list (scripts/allure_summary.py) and any
#                          lane that ran out of its bound (scripts/run_bounded.sh's
#                          evidence), published as the build's "CI summary"
#   ci-summary/counts.txt  pass=, fail=, skip=, flaky= lines the Jenkinsfile
#                          reads for the build page and the README badges
#
# Runs on the agent (Allure needs the agent's JRE). Never fails because a
# lane left nothing: whatever exists is reported.
set -euo pipefail

history_dir=${1:-}
here=$(cd "$(dirname "$0")/.." && pwd)

rm -rf allure-results allure-report allure-history junit ci-summary
mkdir -p allure-results junit ci-summary

shopt -s nullglob
sources=(.lanes/*/src/allure-results-* .lanes/devlayout/results/*)
for d in "${sources[@]}"; do
  [ -d "$d" ] && cp -r "$d"/. allure-results/
done
for f in .lanes/*/src/junit-*.xml; do
  cp "$f" junit/
done

have_results=0
if [ -n "$(ls -A allure-results)" ]; then
  have_results=1
  "$here/scripts/allure_report.sh" allure-results allure-report "$history_dir"
  if [ -d allure-report/history ]; then
    cp -r allure-report/history allure-history
  fi
fi

summary=ci-summary/summary.md
if [ "$have_results" -eq 1 ]; then
  python3 "$here/scripts/allure_summary.py" allure-results > "$summary"
else
  echo "(no test results: the build stopped before any tier wrote them)" > "$summary"
fi

html_escape() {
  sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

{
  echo '<!doctype html><html><head><meta charset="utf-8"><title>ralphd CI summary</title>'
  echo '<style>body{font-family:system-ui,sans-serif;margin:1.5em}pre{background:#f6f8fa;padding:1em;overflow-x:auto}</style></head><body>'
  echo "<h2>Lint: ${LINT_RESULT:-not run}</h2><pre>"
  if [ -s .lanes/lint/src/ruff-report.txt ]; then
    html_escape < .lanes/lint/src/ruff-report.txt
  else
    echo '(no ruff output: the lint lane did not reach ruff)'
  fi
  echo '</pre>'
  echo "<h2>Tests by tier</h2><pre>"
  html_escape < "$summary"
  echo '</pre>'
  echo '<h2>Inner bounds that ran out</h2><pre>'
  expired=(.lanes/*/evidence/*.expired)
  if [ "${#expired[@]}" -eq 0 ]; then
    echo 'None: no lane hit a scripts/run_bounded.sh bound.'
  else
    for e in "${expired[@]}"; do
      lane=${e#.lanes/}; lane=${lane%%/*}
      printf '%s: %s\n' "$lane" "$(cat "$e")" | html_escape
    done
  fi
  echo '</pre></body></html>'
} > ci-summary/index.html

if [ "$have_results" -eq 1 ]; then
  PYTHONPATH="$here/scripts" python3 - allure-results > ci-summary/counts.txt <<'PY'
import sys
from pathlib import Path

import allure_summary

totals, _ = allure_summary.summarise([Path(sys.argv[1])])
keys = {"pass": allure_summary.PASSED, "fail": allure_summary.FAILED,
        "skip": allure_summary.SKIPPED, "flaky": "flaky"}
for name, key in keys.items():
    print(f"{name}={sum(c[key] for c in totals.values())}")
PY
else
  : > ci-summary/counts.txt
fi
cat ci-summary/counts.txt
