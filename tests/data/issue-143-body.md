<!-- Verbatim body of GitHub issue n-orlov/ralphd#143, fetched live via the
GitHub API during task 013 (requirement E) of the v0.18 run. Tracked here so
`tests/test_seventh_lint_task013.py` can pin its must-catch fixture strings
against the issue text they are taken from: issue #143 is the in-project
authoritative account of the `deck-3h` / `deck-3i-2` / `deck-spike`
recurrences, whose own run directories are a different product and are not
reachable from this sandbox. Title line first, then the body, unmodified. -->

plan_ingestion: lint refuses structurally unsatisfiable criteria (future shas, last-commit, predeclared event state, pre-PRD audit ranges, exactly-once)

Criteria that cannot be satisfied by construction produced 37 blocking findings across 11 failed runs and reviewers mostly mark them `curable: true`, so the run replans into certain rejection. The recurring shapes:
- a task's criterion names the sha of a *later* commit, or requires "the last commit" while other tasks mandate commits after it (v0.14 ×2, v0.15, v0.17 ×3);
- a report/record must state a future or event-dependent fact ("none newly filed", "both petitions REFUTED", "petition state") that a later event falsifies (v0.17 a3/a4);
- a record row must cite the commit that writes the row (deck-3i-2 ×2);
- a protected-path audit range that begins before the PRD/plan commit itself (deck-3h ×2, 3i-2 ×4, spike ×3);
- "commit exactly once" combined with "correct the report the review faulted" (deck-spike ×4).

Fix (v0.18 requirement E): a seventh lint in `lint_proposal()` flags these shapes lexically (scope cues to the clause, per the #86 lesson), with must-catch fixtures drawn verbatim from the runs above and must-pass siblings (a criterion may name a *past* sha, an existing artifact, or "the code sha this task lands"). The refusal goes through the plan re-ask ladder naming the clause and a satisfiable form. `plan_review.md` gains the same list as a review checklist. The PRD template/README lists the banned shapes.