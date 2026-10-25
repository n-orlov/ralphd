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
# markdown_html renders the Markdown subset scripts/allure_summary.py writes:
# headings, pipe tables (first row the header), "- " lists and `code` spans.
# A Failed or Flaky cell with a non-zero count is marked.
markdown_html() {
  awk '
  function esc(s) { gsub(/&/, "\\&amp;", s); gsub(/</, "\\&lt;", s); gsub(/>/, "\\&gt;", s); return s }
  function inline(s) {
    s = esc(s)
    while (match(s, /`[^`]*`/)) s = substr(s, 1, RSTART - 1) "<code>" substr(s, RSTART + 1, RLENGTH - 2) "</code>" substr(s, RSTART + RLENGTH)
    return s
  }
  function trim(s) { sub(/^[ \t]+/, "", s); sub(/[ \t]+$/, "", s); return s }
  function close_blocks() {
    if (table) { print "</tbody></table>"; table = 0 }
    if (list) { print "</ul>"; list = 0 }
  }
  /^#+ / {
    close_blocks()
    level = index($0, " ") - 1; if (level < 2) level = 2; if (level > 4) level = 4
    text = $0; sub(/^#+ /, "", text)
    printf "<h%d>%s</h%d>\n", level, inline(text), level
    next
  }
  /^\|[ |:-]*$/ { next }
  /^\|/ {
    n = split($0, f, "|")
    if (!table) {
      close_blocks(); print "<table><thead><tr>"
      for (i = 2; i < n; i++) { hdr[i] = trim(f[i]); print "<th>" inline(hdr[i]) "</th>" }
      print "</tr></thead><tbody>"; table = 1; next
    }
    print "<tr>"
    for (i = 2; i < n; i++) {
      v = trim(f[i]); cls = ""
      if (v ~ /^[0-9]+$/) {
        cls = "num"
        if (v + 0 > 0 && hdr[i] == "Failed") cls = "num bad"
        if (v + 0 > 0 && hdr[i] == "Flaky") cls = "num warn"
      }
      printf "<td%s>%s</td>\n", cls == "" ? "" : " class=\"" cls "\"", inline(v)
    }
    print "</tr>"
    next
  }
  /^- / { if (table) close_blocks(); if (!list) { print "<ul>"; list = 1 }; print "<li>" inline(substr($0, 3)) "</li>"; next }
  /^[ \t]*$/ { close_blocks(); next }
  { close_blocks(); print "<p>" inline($0) "</p>" }
  END { close_blocks() }
  '
}

tile() { # tile <label> <value> <ok|bad|warn|"">
  printf '<div class="tile %s"><div class="v">%s</div><div class="k">%s</div></div>\n' "$3" "${2:--}" "$1"
}

count() { sed -n "s/^$1=//p" ci-summary/counts.txt; }

lint=${LINT_RESULT:-not run}
case "$lint" in
  *[Ff]ail*) lint_cls=bad; lint_open=" open" ;;
  *[Pp]ass*|*[Oo][Kk]*) lint_cls=ok; lint_open="" ;;
  *) lint_cls=""; lint_open="" ;;
esac
fail=$(count fail)
flaky=$(count flaky)
{
  cat <<'EOF'
<!doctype html><html><head><meta charset="utf-8"><title>ralphd CI summary</title>
<style>
:root{--fg:#1f2328;--muted:#59636e;--bg:#fff;--panel:#f6f8fa;--line:#d1d9e0;--ok:#1a7f37;--bad:#cf222e;--warn:#9a6700}
@media (prefers-color-scheme:dark){:root{--fg:#e6edf3;--muted:#9198a1;--bg:#0d1117;--panel:#151b23;--line:#3d444d;--ok:#3fb950;--bad:#f85149;--warn:#d29922}}
body{font:14px/1.5 system-ui,sans-serif;color:var(--fg);background:var(--bg);margin:1.5em;max-width:72em}
h1{font-size:1.5em;margin:0 0 .6em}h2{font-size:1.2em;margin:1.6em 0 .5em;padding-bottom:.25em;border-bottom:1px solid var(--line)}h3,h4{font-size:1em;margin:1.2em 0 .4em}
.tiles{display:flex;flex-wrap:wrap;gap:.6em;margin-bottom:1em}
.tile{border:1px solid var(--line);border-radius:6px;padding:.5em .9em;min-width:6em;background:var(--panel)}
.tile .v{font-size:1.4em;font-weight:600;font-variant-numeric:tabular-nums}.tile .k{color:var(--muted);font-size:.85em}
.tile.ok .v{color:var(--ok)}.tile.bad{border-color:var(--bad)}.tile.bad .v{color:var(--bad)}.tile.warn .v{color:var(--warn)}
table{border-collapse:collapse;margin:.4em 0 1em}th,td{border:1px solid var(--line);padding:.25em .7em;text-align:left}th{background:var(--panel)}
td.num{text-align:right;font-variant-numeric:tabular-nums}td.bad{color:var(--bad);font-weight:600}td.warn{color:var(--warn);font-weight:600}
pre{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:.8em 1em;overflow-x:auto;font-size:12.5px;line-height:1.4}
code{background:var(--panel);border-radius:4px;padding:0 .3em}
details{border:1px solid var(--line);border-radius:6px;margin:.5em 0;padding:.3em .8em}details pre{margin:.5em 0}
summary{cursor:pointer;font-weight:600}
.badge{display:inline-block;border-radius:2em;padding:0 .6em;font-size:.8em;font-weight:600;color:#fff}.badge.ok{background:var(--ok)}.badge.bad{background:var(--bad)}.badge.none{background:var(--muted)}
.muted{color:var(--muted)}
</style></head><body>
<h1>ralphd CI summary</h1>
EOF
  echo '<div class="tiles">'
  tile pass "$(count pass)" ok
  tile fail "$fail" "$([ "${fail:-0}" -gt 0 ] && echo bad || echo ok)"
  tile skip "$(count skip)" ""
  tile flaky "$flaky" "$([ "${flaky:-0}" -gt 0 ] && echo warn)"
  tile lint "$lint" "$lint_cls"
  echo '</div>'
  echo '<h2>Tests by tier</h2>'
  # The summary's own title is replaced by the page's.
  grep -v '^## Test results by tier$' "$summary" | markdown_html
  echo '<h2>Lint</h2>'
  printf '<details%s><summary><span class="badge %s">%s</span> ruff</summary><pre>' \
    "$lint_open" "${lint_cls:-none}" "$(printf '%s' "$lint" | html_escape)"
  if [ -s .lanes/lint/src/ruff-report.txt ]; then
    html_escape < .lanes/lint/src/ruff-report.txt
  else
    echo '(no ruff output: the lint lane did not reach ruff)'
  fi
  echo '</pre></details>'
  echo '<h2>Inner bounds that ran out</h2>'
  expired=(.lanes/*/evidence/*.expired)
  if [ "${#expired[@]}" -eq 0 ]; then
    echo '<p class="muted">None: no lane hit a scripts/run_bounded.sh bound.</p>'
  else
    echo '<ul>'
    for e in "${expired[@]}"; do
      lane=${e#.lanes/}; lane=${lane%%/*}
      printf '<li><span class="badge bad">%s</span> %s</li>\n' "$lane" "$(html_escape < "$e")"
    done
    echo '</ul>'
  fi
  echo '</body></html>'
} > ci-summary/index.html

cat ci-summary/counts.txt
