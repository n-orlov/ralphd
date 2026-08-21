# Issue closure record — ralphd v0.6 (`selfdev-v06-release`)

Requirement **I** of `docs/prds/v0.6-first-release.md` asked this run to close
its own issues, so this file is the receipt: for each of **#14-#22**, the HTTP
status of the closing comment, the HTTP status of the state change, the state
GitHub reported afterwards, and the url of the comment. Reading it beside
`artifacts/reports/issue-traceability.md` (which argues *why* each issue is
closed) is the whole audit trail; `tests/test_issue_traceability.py` holds that
report's per-issue `**Closure:**` lines against this one, so a section there may
claim `closed` only where a section here records it.

## How it was done

- GitHub REST API (`POST /repos/n-orlov/ralphd/issues/<n>/comments`, then
  `PATCH /repos/n-orlov/ralphd/issues/<n>` with `state: closed` and
  `state_reason: completed`), driven by a small `urllib` script from inside the
  job container. No `gh` CLI (this image does not ship one, and it was not
  needed).
- The token was read from the host's git credential helper into the script's
  own environment and used only as a request header: it appears in no command
  argument, no url, no log line, no commit and not in this report.
- Every issue was then **re-read** with a fresh `GET` after the `PATCH`, so the
  *resulting state* column below is what GitHub answers, not what the write
  echoed back.
- All nine issues fully landed, so all nine are closed; none is left open, and
  the traceability report's `**Closure:**` lines say `closed` for exactly these
  nine.

## Summary

| Issue | PRD req | Comment | State change | Resulting state | Closed at (UTC) |
|---|---|---|---|---|---|
| #14 | C | HTTP 201 | HTTP 200 | `closed` (`completed`) | 2026-08-21T10:11:48Z |
| #15 | A | HTTP 201 | HTTP 200 | `closed` (`completed`) | 2026-08-21T10:11:50Z |
| #16 | B | HTTP 201 | HTTP 200 | `closed` (`completed`) | 2026-08-21T10:11:52Z |
| #17 | E | HTTP 201 | HTTP 200 | `closed` (`completed`) | 2026-08-21T10:11:53Z |
| #18 | F | HTTP 201 | HTTP 200 | `closed` (`completed`) | 2026-08-21T10:11:55Z |
| #19 | G | HTTP 201 | HTTP 200 | `closed` (`completed`) | 2026-08-21T10:11:56Z |
| #20 | H | HTTP 201 | HTTP 200 | `closed` (`completed`) | 2026-08-21T10:11:58Z |
| #21 | D | HTTP 201 | HTTP 200 | `closed` (`completed`) | 2026-08-21T10:11:59Z |
| #22 | J | HTTP 201 | HTTP 200 | `closed` (`completed`) | 2026-08-21T10:12:01Z |

---

## #14 — built-in AWS Bedrock price table selected by price_strategy: unpriced gateway routes have no cost an operator can budget with

Requirement **C**: honest cost, derived rates, and a model id you can see.

- **Closing comment:** `POST .../issues/14/comments` -> HTTP **201**, https://github.com/n-orlov/ralphd/issues/14#issuecomment-5368507414
- **State change:** `PATCH .../issues/14` `{state: closed, state_reason: completed}` -> HTTP **200**
- **Resulting state (re-read with a fresh GET, HTTP 200):** **closed**, state_reason `completed`, closed at 2026-08-21T10:11:48Z
- **Evidence the closure rests on:** `artifacts/reports/issue-traceability.md`, section `## #14`

## #15 — tasks flicker out of the hub table: agent's non-atomic tasks.json write plus readers that turn a parse error into an empty list

Requirement **A**: a mid-write `tasks.json` is never served as "no tasks".

- **Closing comment:** `POST .../issues/15/comments` -> HTTP **201**, https://github.com/n-orlov/ralphd/issues/15#issuecomment-5368507712
- **State change:** `PATCH .../issues/15` `{state: closed, state_reason: completed}` -> HTTP **200**
- **Resulting state (re-read with a fresh GET, HTTP 200):** **closed**, state_reason `completed`, closed at 2026-08-21T10:11:50Z
- **Evidence the closure rests on:** `artifacts/reports/issue-traceability.md`, section `## #15`

## #16 — approach is shown without its limit: no surface says whether a failed review starts over or ends the job

Requirement **B**: an approach is shown with its limit, `n/m`, on every surface.

- **Closing comment:** `POST .../issues/16/comments` -> HTTP **201**, https://github.com/n-orlov/ralphd/issues/16#issuecomment-5368507946
- **State change:** `PATCH .../issues/16` `{state: closed, state_reason: completed}` -> HTTP **200**
- **Resulting state (re-read with a fresh GET, HTTP 200):** **closed**, state_reason `completed`, closed at 2026-08-21T10:11:52Z
- **Evidence the closure rests on:** `artifacts/reports/issue-traceability.md`, section `## #16`

## #17 — steering is write-only in the hub: pending and applied entries are never shown

Requirement **E**: steering is readable, not just writable.

- **Closing comment:** `POST .../issues/17/comments` -> HTTP **201**, https://github.com/n-orlov/ralphd/issues/17#issuecomment-5368508187
- **State change:** `PATCH .../issues/17` `{state: closed, state_reason: completed}` -> HTTP **200**
- **Resulting state (re-read with a fresh GET, HTTP 200):** **closed**, state_reason `completed`, closed at 2026-08-21T10:11:53Z
- **Evidence the closure rests on:** `artifacts/reports/issue-traceability.md`, section `## #17`

## #18 — extend click-to-view details across the hub: iterations, run state documents, artifacts, fault and cost breakdowns

Requirement **F**: click to view details, across the run detail page.

- **Closing comment:** `POST .../issues/18/comments` -> HTTP **201**, https://github.com/n-orlov/ralphd/issues/18#issuecomment-5368508395
- **State change:** `PATCH .../issues/18` `{state: closed, state_reason: completed}` -> HTTP **200**
- **Resulting state (re-read with a fresh GET, HTTP 200):** **closed**, state_reason `completed`, closed at 2026-08-21T10:11:55Z
- **Evidence the closure rests on:** `artifacts/reports/issue-traceability.md`, section `## #18`

## #19 — deleting a finished run takes two commands and is impossible from the hub

Requirement **G**: deleting a finished run takes one command, and works from the hub.

- **Closing comment:** `POST .../issues/19/comments` -> HTTP **201**, https://github.com/n-orlov/ralphd/issues/19#issuecomment-5368508618
- **State change:** `PATCH .../issues/19` `{state: closed, state_reason: completed}` -> HTTP **200**
- **Resulting state (re-read with a fresh GET, HTTP 200):** **closed**, state_reason `completed`, closed at 2026-08-21T10:11:56Z
- **Evidence the closure rests on:** `artifacts/reports/issue-traceability.md`, section `## #19`

## #20 — job image is never built and cannot be brought by the job: stale engines run silently, no per-job toolchain

Requirement **H**: the job image is built, and a job may bring its own.

- **Closing comment:** `POST .../issues/20/comments` -> HTTP **201**, https://github.com/n-orlov/ralphd/issues/20#issuecomment-5368508830
- **State change:** `PATCH .../issues/20` `{state: closed, state_reason: completed}` -> HTTP **200**
- **Resulting state (re-read with a fresh GET, HTTP 200):** **closed**, state_reason `completed`, closed at 2026-08-21T10:11:58Z
- **Evidence the closure rests on:** `artifacts/reports/issue-traceability.md`, section `## #20`

## #21 — hub run list has no task progress column: plan completion is one detail page per run

Requirement **D**: task progress in the run list.

- **Closing comment:** `POST .../issues/21/comments` -> HTTP **201**, https://github.com/n-orlov/ralphd/issues/21#issuecomment-5368509058
- **State change:** `PATCH .../issues/21` `{state: closed, state_reason: completed}` -> HTTP **200**
- **Resulting state (re-read with a fresh GET, HTTP 200):** **closed**, state_reason `completed`, closed at 2026-08-21T10:11:59Z
- **Evidence the closure rests on:** `artifacts/reports/issue-traceability.md`, section `## #21`

## #22 — release hygiene: dead cli extra, a version string that contradicts the roadmap, and doc claims no test asserts

Requirement **J**: release hygiene and the doc audit.

- **Closing comment:** `POST .../issues/22/comments` -> HTTP **201**, https://github.com/n-orlov/ralphd/issues/22#issuecomment-5368509311
- **State change:** `PATCH .../issues/22` `{state: closed, state_reason: completed}` -> HTTP **200**
- **Resulting state (re-read with a fresh GET, HTTP 200):** **closed**, state_reason `completed`, closed at 2026-08-21T10:12:01Z
- **Evidence the closure rests on:** `artifacts/reports/issue-traceability.md`, section `## #22`

The closing comment for #22 says plainly that sub-task **043d** ended `failed` in this run's plan while its scope shipped in `b923af2`, `168a041` and task 043e's `cc8c8a2` -- see the *Task 043d* subsection of `artifacts/reports/issue-traceability.md`. #22 is closed because the requirement holds, not because the plan is spotless.

---

## Nothing left open

#14, #15, #16, #17, #18, #19, #20, #21 and #22 are all closed; this run
opened no new issues, and #12 was already closed upstream before the v0.5
wave. The v0.5 issues (#1-#11, #13) were closed by the operator from
`artifacts/reports/issue-traceability.md`'s first wave, under a PRD that
forbade the run to close them itself -- that difference is why this file
exists only for the v0.6 wave.
