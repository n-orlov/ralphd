#!/usr/bin/env python3
"""Summarise Allure results as a markdown table, one row per tier.

    scripts/allure_summary.py <results-dir> [<results-dir> ...] [--output FILE]

Every ``*-result.json`` under the given directories is read (a directory may
hold one tier's results or several downloaded artifacts side by side). The
tier is the result's ``parentSuite`` label, which ``tests/conftest.py`` sets
to ``smoke``/``fast``/``docker``/``browser``.

A test is one ``historyId``. When pytest-rerunfailures reruns a node, each
attempt writes its own result file with the same ``historyId``; the attempt
that stopped last decides the test's status. Totals are per test, not per
attempt:

* passed / failed / skipped -- the final status (``broken`` counts as failed);
* flaky -- a test whose final attempt passed after an earlier attempt did not
  (or whose result carries ``statusDetails.flaky``). It is also counted in
  ``passed``: the column says how many of those passes needed a rerun.

The table is followed by the list of rerun nodes: every test with more than
one attempt, with its final status. The markdown goes to ``--output`` (appended,
so it can be ``$GITHUB_STEP_SUMMARY``) or to stdout.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

PASSED, FAILED, SKIPPED = "passed", "failed", "skipped"


def _final(status: str) -> str:
    if status == "passed":
        return PASSED
    if status == "skipped":
        return SKIPPED
    return FAILED  # failed, broken, unknown


def _tier(result: dict) -> str:
    for label in result.get("labels", []):
        if label.get("name") == "parentSuite" and label.get("value"):
            return str(label["value"])
    return "unlabelled"


def _load(dirs: list[Path]) -> list[dict]:
    results = []
    for d in dirs:
        for path in sorted(d.rglob("*-result.json")):
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                results.append(data)
    return results


def summarise(dirs: list[Path]) -> tuple[dict[str, dict[str, int]], list[dict]]:
    """Return ({tier: counts}, [rerun node, ...]) for the results under dirs."""
    attempts: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in _load(dirs):
        key = str(r.get("historyId") or r.get("fullName") or r.get("name"))
        attempts[(_tier(r), key)].append(r)

    totals: dict[str, dict[str, int]] = {}
    reruns: list[dict] = []
    for (tier, _), group in sorted(attempts.items()):
        group.sort(key=lambda r: (r.get("stop", 0), r.get("start", 0)))
        last = group[-1]
        final = _final(str(last.get("status")))
        counts = totals.setdefault(tier, {PASSED: 0, FAILED: 0, SKIPPED: 0, "flaky": 0})
        counts[final] += 1
        earlier_bad = any(_final(str(r.get("status"))) != PASSED for r in group[:-1])
        flagged = bool((last.get("statusDetails") or {}).get("flaky"))
        flaky = final == PASSED and (earlier_bad or flagged)
        if flaky:
            counts["flaky"] += 1
        if len(group) > 1 or flaky:
            reruns.append({
                "tier": tier,
                "node": str(last.get("fullName") or last.get("name")),
                "attempts": len(group),
                "final": final,
                "flaky": flaky,
            })
    return totals, reruns


def render(totals: dict[str, dict[str, int]], reruns: list[dict]) -> str:
    lines = ["## Test results by tier", "",
             "| Tier | Passed | Failed | Skipped | Flaky |",
             "| --- | ---: | ---: | ---: | ---: |"]
    for tier in sorted(totals):
        c = totals[tier]
        lines.append(f"| {tier} | {c[PASSED]} | {c[FAILED]} | {c[SKIPPED]} | {c['flaky']} |")
    lines += ["", "### Rerun nodes", ""]
    if not reruns:
        lines.append("None.")
    for r in reruns:
        verdict = "flaky (passed on a rerun)" if r["flaky"] else r["final"]
        lines.append(f"- `{r['node']}` ({r['tier']}): {r['attempts']} attempts, {verdict}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("results", nargs="+", type=Path, help="Allure results directories")
    ap.add_argument("--output", type=Path, help="append the markdown here (default: stdout)")
    args = ap.parse_args(argv)
    missing = [str(d) for d in args.results if not d.is_dir()]
    if missing:
        print(f"allure_summary.py: not a directory: {', '.join(missing)}", file=sys.stderr)
        return 2
    text = render(*summarise(args.results))
    if args.output:
        with args.output.open("a") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
