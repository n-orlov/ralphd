"""Loop supervisor: planning → worker → review, approaches, budgets.

Task 007 (requirement D, #99): a plan whose entire actionable remainder is
doomed (`state.task_blocked_by`'s doomed set covers every task
`task_is_actionable()` still calls workable) raises a named fault --
`LoopSupervisor._doomed_plan_fault` -- rather than starving through three
silent no-progress worker iterations into a `stagnation_exhausted` verdict.
The chosen consequence is `transitions.TABLE["worker"]`'s
`doomed_plan_unverifiable` edge to `review`: this reuses the #33
`blocked_with_failures` mechanism (a reviewer's verdict on work-with-a-hole,
not a dependency-graph replan or a terminal degraded state) so a doomed plan
gets exactly the same kind of adjudication a plan with an unresolved
`failed` task already gets.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import time
from pathlib import Path
from typing import Callable

from . import contract_enforcement
from . import contracts
from . import findings
from . import hat_tools
from . import petitions
from . import plan_ingestion
from . import router
from . import run_record
from . import scheduler
from . import state
from . import tiers
from . import transitions
from .config import (
    CONFIG_DIR,
    DEFAULT_INFRA_RETRY_BACKOFF_S,
    PROMPTS_BUILTIN,
    JobConfig,
    overlay_or_config,
)
from .faults import classify_fault
from .llm import current_env
from .pricing import resolve_pricing
from .runner import IterationResult, PiRunner, last_nonblank_line
from .skills import ambient_home, verify_placement_and_report
from .state import (
    PAUSED_STATE,
    STEERING_APPLIED,
    STEER_APPLICATION_PENDING,
    TASK_FAILURE_VALIDATION_EXHAUSTED,
    TERMINATION_CLASS_OPERATOR,
    TERMINATION_CLASS_SELF,
    TERMINAL_STATES,
    VALIDATION_ATTEMPT_LIMIT,
    RunDir,
    atomic_write_json,
    format_last_tool_call,
    last_tool_call,
    prd_path,
    record_operator_termination,
    record_steering_settlement,
    task_failure_kind,
    task_blocks_completion,
    task_is_actionable,
    utc_from_epoch,
    utcnow,
)

log = logging.getLogger("ralphd.loop")


# Phases whose prompt (worker.md / planning.md) explicitly instructs the
# agent to act on operator steering. "review" and "verify" are pure
# verification roles ("Do NOT fix anything yourself") with no steering
# instructions in their prompts, so steering arriving just before one of
# those iterations must NOT be marked consumed there -- it would otherwise
# be silently discarded (recorded as "consumed" but never actioned). It
# stays pending until the next planning/worker iteration picks it up.
#
# Task 018 (#34): being handed to an actionable phase is necessary but not
# sufficient -- the note is marked consumed only once that iteration has
# finished cleanly (see the at-least-once comment in _run_iteration_once).
#
# Task 012 (requirement I, #98): `steer` joins this set -- the hat whose
# entire job is to read and act on pending steering (`prompts/steer.md`'s
# own step 1: "read the pending steering message(s), in arrival order --
# handle every pending one this iteration") must be handed their actual
# BODY text, not just the "pending, not for this phase" filename-only echo
# `build_prompt` gives review/verify. `worker` stays in the set for the
# pre-existing direct-`_run_iteration_once("worker", ...)` callers (task
# 018's own tests) even though `_step_worker`'s new top-of-loop priority
# check, just below, means an ordinary worker DISPATCH from `_step_worker`
# itself never actually reaches this phase while steering is pending --
# that check routes to `steer` first, every pass.
STEERING_ACTIONABLE_PHASES = {"planning", "worker", "steer"}

# Task 014 (PRD requirement J, #98): the read-only **context echo** of recent
# operator messages, carried in EVERY hat's prompt -- the successor of the
# filename-only "pending, not for this phase" notice below, which only ever
# reached the non-actionable hats and only while a message was still pending.
# The echo is history, not an instruction: it carries NO consumption semantics
# (building a prompt never consumes/settles anything -- that is exclusively
# `RunDir.consume_steering`), and "applied/settled" in it says nothing about
# whether the tasks a message produced are done (`tasks.json` is the single
# source of truth for that, requirement I).
STEERING_ECHO_HEADING = "## Recent operator messages (read-only context)"
STEERING_ECHO_MAX_MESSAGES = 5
STEERING_ECHO_MAX_BODY_CHARS = 2000

# Task 034 (operator-inserted): the STEERING LEDGER's own prompt section --
# carried in EVERY hat's prompt by `build_prompt`, exactly like the
# recent-messages echo, and for the complementary reason. The echo carries
# what the operator SAID; the ledger carries what the harness DID (received,
# consumed, the steer hat's settlement claim and its application status),
# which is the half a verify or review hat needs and had no view of: a
# validator that cannot see an applied steer can fail work for conforming
# to it, and one that cannot see a PENDING steer cannot judge staleness.
#
# Surfaced engine-side, like `_operator_amendment_review_context` and
# `_petitions_review_context`, so no hat's prompt file has to remember to
# ask for it and no hat can be forgotten.
#
# `STEERING_LEDGER_NOT_A_WAIVER` is the semantics sentence, stated in ONE
# place and asserted verbatim in `prompts/task-verify.md` and
# `prompts/review.md` by `tests/test_prompt_steering_durability.py`: the
# ledger INFORMS, it does not waive. Only operator amendments in
# `/config/amendments` waive an obligation -- unchanged by this section's
# existence, and said out loud here so no hat can read "applied" as
# "excused".
STEERING_LEDGER_HEADING = "## Steering ledger (read-only context)"
STEERING_LEDGER_NOT_A_WAIVER = (
    "A steer is NOT a waiver: only operator amendments in "
    "`/config/amendments` waive an obligation.")
STEERING_LEDGER_MAX_NOTES_CHARS = 400

# Task 022 (#30): how many earlier episode endings `status.json` keeps in
# `previousEndings`. Bounded because status.json is rewritten in full on every
# status update, so an auto-resume loop must not be able to grow it without
# limit; 50 episodes of one run is already pathological, and events.jsonl
# keeps the complete append-only transition history either way.
PREVIOUS_ENDINGS_MAX = 50

# Task 032 (operator-inserted, grounding the contract re-ask in the hat's
# actual final text): the iteration-36 regression this task fixes -- the
# steer hat's final text was prose followed by a fenced ```json block
# carrying the correct `{"settled": true}` payload, but the pre-task-032
# re-ask ladder threw that text away and asked a CONTEXT-FREE model call
# for "the corrected JSON payload", which duly fabricated
# `{"response": "steer"}` from nothing (see this run's own
# `artifacts/contract/0001.jsonl`/`0002.jsonl`). `_FENCED_JSON_BLOCK_RE`
# finds every ```json (or bare ```) fenced block in a hat's final text, in
# the order they appear; `_ask_contract_fix` tries each, most-recent-first
# (a hat that first shows its reasoning and then a corrected block near
# the end is the common shape), and uses the first one that both parses
# as JSON *and* validates against the hat's own declared contract schema
# -- with NO model call at all. Only once no fenced block validates does
# the method fall through to an actual model re-ask, and even then the
# prompt now carries the hat's own final text verbatim (bounded to the
# last `CONTRACT_REASK_FINAL_TEXT_MAX_CHARS` characters) alongside the
# error list, so the model is grounded in what the hat actually said
# instead of starting from nothing.
_FENCED_JSON_BLOCK_RE = re.compile(
    r"```(?:json)?[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
CONTRACT_REASK_FINAL_TEXT_MAX_CHARS = 4000


class LoopSupervisor:
    def __init__(self, cfg: JobConfig, run: RunDir, workspace: Path):
        self.cfg = cfg
        self.run = run
        self.workspace = workspace
        # Task 052 (#10): the host-side rate table (usually inlined into
        # job.yaml by `ralphctl start`) is parsed once, here -- the runner
        # consults it only for messages the provider quoted no price for.
        # Task 011 (#14): `resolve_pricing` also layers the built-in AWS
        # Bedrock table behind it when `price_strategy: aws`; with the default
        # `none` it returns exactly the operator map (or None) as before.
        self.runner = PiRunner(workspace,
                               pricing=resolve_pricing(cfg.pricing,
                                                       cfg.price_strategy))
        # Seed from any already-completed iterations on disk (0 for a
        # fresh run dir) so a restarted engine numbers its next iteration
        # N+1 instead of reusing/duplicating past numbers (PRD req 16).
        self.iterations_used = run.max_iteration_number()
        self._pause = asyncio.Event()
        self._pause.set()  # set = not paused
        # Task 015 (#5): the operator's "retry now" doorbell, shaped exactly
        # like _pause -- an asyncio.Event the API sets from its own request
        # handler while the loop task is parked on it. The infra backoff wait
        # therefore races this Event instead of sleeping blind, so POST /retry
        # cuts a 5-minute backoff short in milliseconds instead of leaving an
        # operator who can see the endpoint is healthy again staring at a
        # countdown. Inverted sense from _pause (clear = nothing requested,
        # set = wake up now); armed/cleared per wait.
        self._retry_now = asyncio.Event()
        # True only while the loop is actually parked in an infra backoff
        # wait: what POST /retry can wake (409 otherwise).
        self._infra_waiting = False
        self._abort_reason: str | None = None
        # Task 003 (#11): operator-initiated abort/interrupt bookkeeping,
        # consulted by classify_fault(operator_abort=...) so a SIGINT the
        # *operator* asked for is never mistaken for a provider-side
        # stream abort and retried as an outage. POST /abort sets
        # _abort_reason (sticky: the run is ending); POST /interrupt only
        # ends the current iteration, so its flag is armed per attempt and
        # cleared when the next attempt starts.
        self._operator_interrupted = False
        # Task 018 (#5): *who* recorded the abort reason. `_abort_reason` is
        # set both by POST /abort and by the engine giving up on its own
        # (an exhausted outage budget, a broken environment), and
        # operator_abort_requested cannot tell them apart -- which is fine
        # inside the job loop (either way it is ending) but not for the
        # post-terminal reflect iteration, which must still be retryable
        # after an engine-side give-up and must NOT be retried against an
        # operator who asked the run to stop. See
        # _begin_reflect_retry_window().
        self._operator_abort_recorded = False
        # Task 016 (#47): the signal that is taking this engine down, as text
        # ("15"), or None while nothing has signalled it. Set by
        # abort_on_signal() -- i.e. only by engine/main.py's SIGTERM/SIGINT
        # handler, never by an API abort -- and read by _run_reflection(),
        # which must not manufacture a reflection failure out of the engine's
        # own teardown. See _reflect_skipped_reason().
        self._signal_unwind: str | None = None
        # Task 018 (#5): the fault verdict (and error text) of the most
        # recently finished iteration, recorded by _run_iteration_once() --
        # the one signal that says whether the job just ended *on an
        # infra-shaped failure*, which is what makes reflect wait before its
        # first attempt instead of firing into the same dead endpoint.
        self._last_fault_class: str | None = None
        self._last_fault_error: str = ""
        self._instant_failure_streak = 0
        # Task 008 (requirement B.2, #128): how many times the LAST worker
        # pass through `_step_worker`'s verdict dispatch re-entered itself
        # with a repaired payload from the generic schema site (0 or 1 --
        # never more; see the dispatch loop's own comment). Read back by
        # tests only; the engine itself never branches on it.
        self._last_worker_dispatch_reentries = 0
        # Task 123 (#60): task ids the harness has already written a
        # dead-dependency blocked note for (`_step_worker`), so a plan that
        # stays blocked across several worker iterations in a row does not
        # grow notes.md by one duplicate line per iteration.
        self._blocked_notes_written: set[str] = set()
        # Task 018 (requirement K, #101): the peer plan-review hat's most
        # recent objection reason, set by `_step_plan_review` when it
        # routes back to `planning` for a revision, consumed (and cleared)
        # by `_step_planning`'s very next attempt so the model actually
        # sees what the peer objected to, exactly the way an ingestion
        # rejection's `prior_errors` already reach the same attempt.
        self._pending_plan_review_objection: str | None = None
        # Task 010 (#5): the error signature the current instant-failure
        # streak is made of, plus the memoised verdict for the attempt
        # _check_instant_failure() scored last (the infra-retry wrapper
        # scores every attempt as it happens; the planning/worker call
        # sites then hand the same resolved result back).
        self._instant_failure_sig: str | None = None
        self._instant_scored_result: IterationResult | None = None
        self._instant_scored_tripped = False
        # Task 001a: iterations refunded because they were retried after an
        # infra-classified fault (see _run_iteration_with_infra_retry) --
        # subtracted from self.iterations_used when checking budget_left()
        # so a hung/broken-endpoint retry never costs the job an iteration,
        # while self.iterations_used itself keeps monotonically increasing
        # (so every attempt still gets its own iteration directory/number).
        # Task 023 (#32): seeded from status.json, not from 0 -- see
        # _seed_refunds() for why the counters are persisted rather than
        # folded into the iterations_used seed above.
        self._infra_refunded, self._grace_refunded, self._steer_refunded = (
            self._seed_refunds())
        # Task 033 (#114): the steer hat's live tool broker, non-None only
        # while a steer iteration is in flight (`_run_hat_iteration`) -- it
        # is what makes `_hat_extensions("steer")` non-empty, so no other
        # phase can ever be launched with the steer tool surface.
        self._steer_tool_broker: hat_tools.SteerToolBroker | None = None
        # Task 022 (PRD requirement B.3, #140): the worker hat's own,
        # much narrower broker -- non-None only while a worker iteration is
        # in flight (`_run_worker_iteration`), serving `notify` alone (see
        # `hat_tools.WORKER_WIRED_TOOLS`) -- never the task-table/standing-
        # rules/context tools the steer broker above carries, keeping the
        # worker's authority boundary (requirement H) exactly what it was.
        self._worker_tool_broker: hat_tools.SteerToolBroker | None = None
        # Task 018 (requirement E, #60 pillar 2): sequence number for this
        # process's own router transcripts (`artifacts/router/NNNN.jsonl`) --
        # a plain counter, not an iteration slot: router calls are NEVER
        # charged as work iterations (`self.iterations_used` is untouched by
        # `_ask_router`), so they get no `iterations/NNNN/` directory of
        # their own.
        self._router_calls = 0
        # Task 002 (#60 pillar 3): sequence number for this process's own
        # contract re-ask transcripts (`artifacts/contract/NNNN.jsonl`) --
        # the same shape `self._router_calls` establishes for `_ask_router`,
        # for the same reason: a contract re-ask is a plain counter, never
        # an iteration slot. `_ask_contract_fix` never calls
        # `_run_iteration_once` or touches `self.iterations_used`.
        self._contract_calls = 0
        # Task 008 (#5): the *episode clock* driving infra retries. One
        # "episode" is one continuous outage: consecutive infra-classified
        # attempts with no iteration reaching the model in between. Retries
        # continue while the episode's cumulative backoff wait stays under
        # cfg.infra_outage_budget_s (wall clock, not an attempt count -- a
        # gateway can be down for an hour and the right answer is to keep
        # waiting, not to give up after 3 tries), and the whole episode
        # resets as soon as an iteration gets through again.
        self._infra_episode_attempts = 0
        self._infra_episode_waited_s = 0.0
        self._infra_episode_started_at: float | None = None
        # Task 002: at most one grace review per approach (set of approach
        # numbers that have already had one); its refund counter uses the same
        # mechanism as _infra_refunded (and, since task 023/#32, the same
        # persistence) so a grace review never counts against the job's
        # iteration budget. The *granted* set itself is deliberately still
        # per-process: see _seed_refunds().
        self._grace_review_granted: set[int] = set()
        # Task 005 PART 2 (requirement A.2, #114): the normalised set of
        # open-and-unconfirmed petition obligations the mid-run
        # adjudication episode has ALREADY been given a bounded shot at
        # (`petitions.open_unconfirmed_petitions`, normalised the same way
        # `petitions._normalize_obligation` keys every other obligation
        # comparison in this tree) -- so a petition whose own workspace
        # filing never gains an `Adjudication` field (by that module's own
        # design: only a run-scope CONFIRMED record, never the file
        # itself, marks a petition settled) does not trigger a FRESH
        # bounded episode on every later worker pass. The episode is
        # scheduled again only once a genuinely NEW obligation appears in
        # the open-and-unconfirmed set -- see
        # `_maybe_run_midrun_adjudication_episode`. Deliberately
        # per-process, like `_grace_review_granted`: a resumed engine
        # simply re-derives it from scratch off the SAME workspace file
        # and run-scope record, which is the safe direction (at worst one
        # extra bounded episode after a restart, never a missed one).
        self._petition_adjudication_seen: set[str] = set()
        # Descriptive note for the terminal `reason` when a grace review ran
        # but did NOT result in a VERIFIED verdict -- kept separate from
        # self._abort_reason so the terminal `state` (aborted vs failed)
        # keeps its existing meaning (aborted == an actual abort/infra
        # condition set _abort_reason); this is purely informational text.
        self._terminal_reason_note: str | None = None
        # Requirement B (#72): the deadline-beat-verdict marker computed by
        # `_deadline_beat_verdict_patch()`, stashed here ONLY by the one
        # call site (`_review_unjudged_at_budget_end`) that actually knows
        # review never rendered a judgement this approach -- see that
        # method's docstring for why this must not be recomputed generically
        # at the shared terminal-write call site the way
        # `_unconsumed_steering_patch()` is. Empty for every other tail exit.
        self._deadline_beat_verdict_extra: dict = {}
        # Task 016 (requirement F.3, #146): True iff the terminal `("tail",)`
        # exit this episode is about to write is specifically "the deadline
        # took the last review attempt before it could speak" -- set by
        # `_review_unjudged_at_budget_end` (ordinary self-loop exhaustion)
        # and by `_maybe_grace_review`'s own interrupted-grace-attempt
        # branch, the two and only two call sites that positively know
        # review itself was cut short by the ENGINE rather than having
        # rendered (or failed to render) a real verdict. `_finish_tail_
        # unverified` reads this to write `verdict: "unreviewed"` /
        # `reason: "deadline during review"` instead of the ordinary
        # `unverified` outcome -- never set for any other tail exit.
        self._review_deadline_unreviewed: bool = False
        self._last_task_snapshot: dict = {}
        self.deadline = time.monotonic() + cfg.job_timeout_s
        # Task 011 (#5): wall-clock twin of self.deadline (published as
        # status.json's deadlineAt) plus the running total of time this run
        # spent sitting out infra outages. DECISION: an outage must not eat
        # the job's working time -- self.deadline is wall clock, so a 4-hour
        # gateway outage would silently consume half an 8-hour job and the
        # run would die of "timeout" having done nothing wrong. Every infra
        # backoff wait therefore *extends* both deadlines by exactly the
        # waited seconds (see _account_infra_wait) and is accounted in
        # infraWaitTotalS, so job_timeout_s keeps its plain meaning: time
        # available to the agent, not time available to the agent's network.
        # The total is seeded from status.json so it survives `resume` (the
        # deadline itself is per-process by construction).
        self._deadline_epoch = time.time() + cfg.job_timeout_s
        self._infra_wait_total_s = float(
            run.read_status().get("infraWaitTotalS") or 0.0)
        # Task 016 (#P): the operator-pause twin of _infra_wait_total_s
        # above -- a run parked by POST /pause is not "using up" its job
        # clock any more than one sitting out an infra outage is, so every
        # completed pause wait (measured at the wait site itself, _gate(),
        # never here or in pause()/resume()) credits the same two deadline
        # attributes back and accumulates into this running total, seeded
        # from status.json for the same resume-survival reason.
        self._paused_wait_total_s = float(
            run.read_status().get("pausedTotalS") or 0.0)
        # Task 012 (#5): does status.json currently say health "degraded"?
        # Tracked in memory so the transition back to "ok" is emitted (and
        # written) exactly once, on the iteration that proves the endpoint is
        # back, instead of on every non-infra result of a healthy run.
        self._infra_degraded = False
        # Task 012 (#56): the settled terminal outcome, recorded by _finish()
        # but not yet promoted onto status.json's `state` field -- set only
        # while cfg.reflect is true and the job has finished, cleared by
        # _promote_finishing_state() once the reflect window is over. `None`
        # for the whole life of a run with `reflect: false` (there, _finish()
        # writes the real terminal state immediately and this is never set).
        self._pending_terminal: tuple[str, str, dict] | None = None
        # Task 014 (requirement B, #60 pillar 2): the FSM position
        # `_run_job_core` walks (`engine.transitions.STEP_STATES`) and the
        # ONE runtime primitive allowed to move it -- constructed once, up
        # front, from `cfg.vigilant`, which is the ONLY way the two modes'
        # behaviour differs here (there is no second, hand-written
        # non-vigilant executor). `self._approach` is set for real in
        # `_run_job_core` once `_resume_point()` has run; `None` here is
        # just "not started yet".
        #
        # `cfg.peer_plan_review` (task 018, requirement K, #101) is a
        # SECOND, independent selector passed alongside `cfg.vigilant` --
        # see `transitions.table_for`'s own docstring for why the two axes
        # never interact. `cfg.peer_verdict_gate` (task 020, requirement L,
        # #101) is a THIRD, independent selector on its own axis, same
        # shape.
        self._executor = transitions.TransitionExecutor(
            cfg.vigilant, cfg.peer_plan_review, cfg.peer_verdict_gate)
        self._approach: int | None = None

        # Task 006 (PRD requirement B.2, #111) / task 007 (requirement
        # D.1, finding 5): the operator-escalation seam for a `rejected_
        # incurable`/`rejected_last_approach` pause. In production this is
        # `_notify_via_configured_notifier` (below) whenever `cfg.
        # notifier_url` is actually set -- it reaches that URL through
        # B.3's OWN helper (`hat_tools.send_notification`), never a second
        # HTTP client, so the escalation lands as a `notification.sent`
        # event on `events.jsonl` and in the run record's notifications
        # section, exactly like a hat's own `notify` tool call.
        #
        # `notifier_url` UNSET leaves this `None`, same as before this
        # task -- `send_notification` itself would write a `notification.
        # sent` event even for a missing URL (its own "no notifier URL
        # configured" branch), and requirement D.1's escalation must write
        # NO such event when nothing is configured, so the no-URL case is
        # never dispatched through `send_notification` at all: the
        # `if self.notifier is not None` guard at each call site simply
        # finds nothing to call. The escalation itself
        # (`operator_escalation`, the pause) never depends on any of this
        # either way.
        #
        # A test replaces this attribute with a stub callable AFTER
        # construction, the same post-construction stubbing shape already
        # used for `sup.runner`/`sup._wait_out_backoff` throughout this
        # test suite -- never a constructor parameter, so every existing
        # call site keeps working unchanged.
        self.notifier: Callable[[str], None] | None = (
            self._notify_via_configured_notifier if cfg.notifier_url
            else None)

    def _notify_via_configured_notifier(self, text: str) -> None:
        """Task 007 (PRD requirement D.1, finding 5): the production
        `self.notifier` callable, wired only when `self.cfg.notifier_url`
        is set. Reaches it through `hat_tools.send_notification`, B.3's own
        helper -- the escalation text becomes a `notification.sent` event
        (`textSha256`, `endpoint`, `httpStatus`) on `events.jsonl` and in
        the run record's notifications section, the same as any hat's
        `notify` tool call.

        `send_notification` itself never raises for a connection error or
        a non-2xx status -- both just answer `ok=False` and are recorded.
        The `try`/`except` here is a second line of defence against
        anything else unexpected (e.g. a raising test stub swapped in for
        the poster) reaching all the way out through this call: the pause
        and the `operator_escalation` event this is invoked from have
        already landed on disk by the time this runs, and a broken
        notifier attempt must never turn that into a crashed iteration.
        """
        try:
            hat_tools.send_notification(self.run, text, self.cfg.notifier_url)
        except Exception:                             # pragma: no cover
            log.exception("notifier dispatch failed for run %s",
                          self.cfg.run_id)

    # -- refund bookkeeping (task 023, #32) --------------------------------
    # Task 014 (requirement J, #98): "steer" joins "infra"/"grace" here.
    # A steer hat's own iteration is refunded the instant it returns
    # (`_step_generic_hat`'s call site), for the same underlying reason a
    # grace review's is refunded: "an operator message must not cost the
    # run a work iteration" (PRD requirement J) is a BUDGET fact, and this
    # is this engine's one existing mechanism for "this attempt happened,
    # occupies a real numbered `iterations/NNNN` directory, and is fully
    # observable -- but never counts against `cfg.iterations`." Unlike
    # grace (deliberately still shown as a used iteration number -- see
    # `_run_iteration_once`'s own comment), a steer refund is ALSO
    # subtracted from the published `iterationsUsed` figure: full parity
    # with the router/`_ask_contract_fix` channels' own unchargedness
    # (neither leaves ANY trace in `iterationsUsed`), which is the literal
    # comparison the PRD draws for this case ("uncharged, like the router
    # and the v0.13 contract re-ask").
    REFUND_KINDS = ("infra", "grace", "steer")

    def _seed_refunds(self) -> tuple[int, int, int]:
        """The refunds this run has ALREADY earned, read back from
        status.json's `iterationsRefunded` as `(infra, grace, steer)` --
        `(0, 0, 0)` for a fresh run dir or any run dir written before this
        field existed.

        Task 023 (#32). Both counters used to start at 0 on every engine
        process while `iterations_used` was seeded from the *raw*
        `max_iteration_number()`, so every `ralphctl resume` (and every
        auto-resume after a crash) silently re-charged the refunds earned
        before it: `selfdev-v06-release` earned 27 infra refunds and kept 10.
        A guarantee that expires at the next resume is not a guarantee.

        DECISION -- persist the counters; do NOT seed `iterations_used` from a
        charged count (the PRD allows either). `iterations_used` has two jobs:
        it is the budget tally *and* the number of the next iteration
        directory, and only the first may be discounted. Seeding it from the
        charged count would hand a resumed engine a number a FINISHED
        iteration already occupies -- `begin_iteration_dir()` (task 019, #44)
        would then archive a completed record as though it were a crashed
        attempt, and two different iterations would share one number in the
        merged transcript and every `iterations/NNNN` URL. Refunds are also
        decisions this engine made per attempt and cannot be re-derived from
        the iteration dirs: a grace review is refunded and carries no `infra`
        faultClass at all, and `iterationsUsed` alone cannot say which of the
        two counters a discount came from (grace refunds are deliberately not
        subtracted from it, see _run_iteration_once).

        Deliberately NOT persisted: `_grace_review_granted`. The refund is a
        budget fact that must survive; "this approach already had its one
        grace review" is a per-process guard whose resume behaviour predates
        this task and is unchanged by it (a resumed engine may still grant
        one more) -- SPEC 8.4 records it as a residual limitation rather than
        having this task quietly change two contracts at once.

        Values are clamped into `[0, iterations_used]` (and their sum to
        iterations_used) so a hand-edited or truncated status file can never
        make `budget_left()`'s charged count negative and hand a run an
        unbounded budget.
        """
        recorded = self.run.read_status().get("iterationsRefunded")
        if not isinstance(recorded, dict):
            return 0, 0, 0
        # Sequential clamp, generalising the old two-counter special case
        # (`grace = max(0, iterations_used - infra)`) to however many kinds
        # `REFUND_KINDS` names: each kind is capped by whatever of
        # `iterations_used` the EARLIER kinds have not already claimed, so
        # a hand-edited or truncated status file can never make the kinds'
        # sum exceed `iterations_used`.
        counts: list[int] = []
        claimed = 0
        for kind in self.REFUND_KINDS:
            try:
                value = int(recorded.get(kind) or 0)
            except (TypeError, ValueError):
                value = 0
            value = max(0, min(value, self.iterations_used - claimed))
            counts.append(value)
            claimed += value
        return tuple(counts)

    def _refund_iteration(self, kind: str) -> None:
        """Refund the attempt that just finished: bump the counter
        `budget_left()` subtracts and write both counters to status.json in
        the same breath (task 023, #32), so the credit is on disk before the
        engine does anything that could kill it -- an outage retry is exactly
        the moment a run is most likely to be killed and resumed.
        """
        if kind == "infra":
            self._infra_refunded += 1
        elif kind == "grace":
            self._grace_refunded += 1
        elif kind == "steer":
            self._steer_refunded += 1
        else:  # pragma: no cover - programming error
            raise ValueError(f"unknown refund kind: {kind}")
        self.run.update_status(
            iterationsRefunded={"infra": self._infra_refunded,
                                "grace": self._grace_refunded,
                                "steer": self._steer_refunded})

    # -- control surface (called from API) --------------------------------
    @property
    def operator_abort_requested(self) -> bool:
        """True when this run's current failure signal, whatever it looks
        like, was asked for by the operator (task 003, #11)."""
        return self._abort_reason is not None or self._operator_interrupted

    def interrupt(self) -> bool:
        delivered = self.runner.interrupt()
        if delivered:
            # Only record it when a signal actually reached a running agent
            # -- an interrupt with nothing running changes no iteration's
            # outcome and must not shield the next one from infra retry.
            self._operator_interrupted = True
        return delivered

    def pause(self, reason: str = "") -> None:
        """Hold the loop at the next iteration boundary (task 015's
        `pause`/`resume` seam, `POST /pause`).

        `reason` (task 036, requirement D.1/D.2, #142) is what tells an
        ENGINE-initiated pause apart from an operator one, and it is the
        only thing that makes the pause persistent:

        * `POST /pause` calls this with NO reason and keeps the pre-task-036
          contract exactly -- no status field changes at all, the pause is
          observable through the `log` event alone. An operator who paused a
          run knows they paused it, and treating that as a recorded lifecycle
          state would change what every consumer sees on a routine
          look-at-the-logs pause (docs/api.md `POST /pause`).
        * The engine pausing ITSELF on an escalation edge passes its edge
          name (`rejected_last_approach`), and that pause is persisted to
          status.json by `_persist_pause` below -- nobody typed it, so
          nothing else would tell the host the run is parked waiting for a
          human ruling rather than working.
        """
        self._pause.clear()
        self.run.emit("log", message="paused at next iteration boundary")
        if reason:
            self._persist_pause(reason)

    def _persist_pause(self, reason: str) -> None:
        """Record an engine-initiated pause on status.json IMMEDIATELY (task
        036, requirement D.1/D.2, #142): `state: "paused"` plus
        `pauseReason: "<the edge that paused>"`, written at the escalation
        site itself rather than at the boundary the loop later parks on, so
        a host reading status.json (or `ralphctl status`, or the hub) sees
        the pause the moment the decision is made instead of only after the
        review iteration finishes unwinding.

        Deliberately narrow in three ways:

        * `phase` and `approach` are NOT touched -- requirement D.1's
          "leaves the approach unarchived" is the whole point of this edge,
          and `resume` must re-enter review over the SAME approach. There is
          still no `phase: "paused"` (docs/api.md; the loop parks between
          iterations, `phase` keeps naming the phase that ran last).
        * a run whose outcome is already settled (`finishing` or a terminal
          state) is never rewritten -- `POST /abort` on a paused run calls
          `resume()` to unblock the wind-down, and nothing after the verdict
          is settled may reopen a non-terminal state.
        * the reason is recorded as its own field rather than encoded into
          `state`, so `state`'s value set stays a small enum every consumer
          can switch on (`state.NONTERMINAL_STATES`, which `paused` joins
          for the same liveness reason `finishing` is in it).
        """
        status = self.run.read_status()
        current = status.get("state")
        if current == "finishing" or current in TERMINAL_STATES:
            return
        self.run.update_status(state=PAUSED_STATE, pauseReason=reason)
        self.run.emit("log", message=(
            f"run state recorded `{PAUSED_STATE}` on status.json "
            f"(pauseReason: {reason}); `ralphctl resume {self.cfg.run_id}` "
            "clears it"))

    def resume(self) -> None:
        """Release the loop (`POST /resume`), and clear an engine-recorded
        pause if there is one (task 036, requirement D.1/D.2, #142)."""
        self._pause.set()
        self._clear_persisted_pause()
        self.run.emit("log", message="resumed")

    def _clear_persisted_pause(self) -> None:
        """Undo `_persist_pause`'s status.json write (task 036, requirement
        D.1/D.2, #142): `state` back to `running` and `pauseReason` back to
        `null`, leaving `approach` and `phase` exactly as they were -- the
        run resumes the SAME approach in the review phase the escalation
        paused it in (`_step_review`'s `rejected_last_approach` edge is a
        `review -> review` self-loop, and across a process restart
        `_resume_point`'s `_paused_awaiting_last_approach_ruling` case lands
        the fresh episode back on `review` over the same approach too).

        A no-op unless `state` is actually the recorded pause: an operator
        pause records no state (see `pause` above), so a `POST /resume` on
        one -- or on a run that was never paused at all, which the API
        accepts as a no-op -- must not rewrite `state` to `running` and
        resurrect a `finishing`/terminal run.
        """
        status = self.run.read_status()
        if status.get("state") != PAUSED_STATE:
            return
        reason = status.get("pauseReason")
        self.run.update_status(state="running", pauseReason=None)
        self.run.emit("log", message=(
            f"recorded pause cleared (pauseReason was: {reason}); resuming "
            f"approach {status.get('approach')} in the "
            f"`{status.get('phase')}` phase"))

    def retry_now(self) -> bool:
        """Operator-requested "try the endpoint again right now" (task 015,
        #5). Returns False when the run is not sitting in an infra backoff
        wait -- there is nothing to wake, and the API turns that into a 409
        rather than silently pretending to have done something.

        Deliberately narrow: it does NOT unpause a paused run (that is
        /resume) and does not touch steering. The only state it changes is
        the backoff wait it interrupts, plus the outage-budget episode clock
        reset done by the waiter itself.
        """
        if not self._infra_waiting:
            return False
        wait = self.run.read_status().get("infraWait") or {}
        self.run.emit("infra_retry_now", phase=wait.get("phase"),
                      attempt=wait.get("attempt"), error=wait.get("error"),
                      source="operator",
                      message="manual retry requested: waking the infra "
                              "backoff wait and resetting the outage budget")
        self._retry_now.set()
        return True

    def abort(self, reason: str = "") -> None:
        """End this run because an *operator* asked (POST /abort, which is
        what `ralphctl abort`, `ralphctl stop` and the hub's abort button all
        go through). Records the `operator` termination class -- the one
        auto-resume must never resurrect.
        """
        self._record_abort(reason or "aborted by operator",
                          TERMINATION_CLASS_OPERATOR)

    def abort_on_signal(self, sig) -> None:
        """End this run because a SIGTERM/SIGINT reached the engine and nobody
        claimed it (task 015, #46). Called only from engine/main.py's signal
        handler.

        This used to be plain `abort(f"signal {sig}")`, which recorded an
        *operator* termination and so made the run permanently unrecoverable --
        auto-resume's never-resurrect refusal firing on the one shape it should
        recover from: a run shot from inside its own container (the agent's own
        `pkill -f ralphd-engine`, a `docker stop` of the job from a sibling it
        started). See `state.TERMINATION_CLASS_SELF` for why attribution, not
        provenance, is what decides the class, and what the residual
        misreading is.

        An abort an operator already claimed through the API is never
        downgraded: `ralphctl abort` followed by the container being taken down
        delivers this signal too, and the operator's intent is the one that
        counts. The reason text and the evidence are still recorded, because
        "and then a signal arrived" is true either way.
        """
        claimed = self._operator_abort_recorded
        # Task 016 (#47): remember that the *process* is going down, not just
        # this job. _record_abort() below fires the child killer
        # (runner.interrupt()) and main.py's handler sets its stop event; the
        # container runtime's SIGKILL follows the grace period. Anything the
        # engine starts after this point cannot finish, so the post-terminal
        # reflect phase must not be attempted (and must not leave a tombstone
        # blaming the reflection for the teardown).
        self._signal_unwind = str(sig)
        evidence = last_tool_call(self.run.root)
        detail = format_last_tool_call(evidence)
        if claimed:
            reason = (f"{self._abort_reason} (signal {sig!s} then ended the "
                      f"engine)")
        else:
            # Requirement E: `ralphctl status` must stop reporting a bare
            # `reason: signal 15`, which says nothing an operator can act on.
            # Everything this path knows goes into the text: the class, why the
            # engine believes it, and the run's own last action before it died.
            reason = (
                f"self-inflicted termination: signal {sig!s} ended the engine "
                "with no operator abort recorded, so it came from inside this "
                "run's own container (or a raw `docker stop`, which "
                "`ralphctl stop` is not); this run stays eligible for "
                "auto-resume")
        if detail:
            reason = f"{reason}. {detail}"
        self._record_abort(
            reason,
            TERMINATION_CLASS_OPERATOR if claimed else TERMINATION_CLASS_SELF,
            signal=str(sig), evidence=evidence)

    def _record_abort(self, reason: str, termination_class: str,
                      signal: str | None = None,
                      evidence: dict | None = None) -> None:
        """THE one implementation of "this run is ending on someone's
        instruction": the in-memory abort bookkeeping, the on-disk marker and
        the `termination` status field, in that order, for both classes (task
        015, #46). Two copies of this sequence is how the class and the marker
        would drift apart.
        """
        self._abort_reason = reason
        self._operator_abort_recorded = True
        self._operator_interrupted = True
        # Task 029 (#8): record the operator's intent on disk *now*, before
        # the loop unwinds. If this container dies before it manages to
        # write its terminal state (SIGKILL, `stop --force`, host reboot
        # mid-abort) the run dir would otherwise be indistinguishable from a
        # run whose container crashed -- and `doctor --fix` would helpfully
        # resurrect a job the operator just killed.
        record_operator_termination(self.run.root, "abort",
                                    reason=self._abort_reason,
                                    source="engine",
                                    termination_class=termination_class,
                                    evidence=evidence)
        # ... and in status.json, which is what every reader that is not
        # auto-resume already polls: the class is a field of its own rather
        # than something to be parsed back out of `reason`.
        self.run.update_status(termination={
            "class": termination_class, "action": "abort", "at": utcnow(),
            "signal": signal, "reason": self._abort_reason,
            "evidence": evidence})
        self.runner.interrupt()

    # -- prompts -----------------------------------------------------------
    def prompt_text(self, name: str) -> str:
        # Preference order: runtime overlay (API PUT, writable) > mounted
        # /config (operator-provided, read-only in real containers) > builtin.
        path = overlay_or_config(f"prompts/{name}.md")
        if not path.exists():
            path = PROMPTS_BUILTIN / f"{name}.md"
        return path.read_text()

    def build_prompt(self, phase: str, extra: str = "",
                      prompt_name: str | None = None) -> str:
        # Task 056 (#1): "which file is the PRD" is `state.prd_path`'s call
        # (composite when present), the same one `GET /prd` and the hub's
        # PRD dialog make -- falling back to the canonical path name when
        # neither file exists yet, since this only names a path in a prompt.
        prd = prd_path(self.run.root) or self.run.prd_file
        parts = [self.prompt_text(prompt_name or phase)]
        parts.append("\n\n## Job context\n")
        parts.append(f"- Run state directory: {self.run.root}\n")
        parts.append(self._workspace_note())
        parts.append(f"- PRD file: {prd}\n"
                     f"- Task state file: {self.run.tasks_file}\n"
                     f"- Handoff notes file: {self.run.notes_file}\n"
                     f"- Artifacts directory: {self.run.artifacts_dir}\n")
        docker_note = self._docker_siblings_note()
        if docker_note:
            parts.append(docker_note)
        creds_note = self._creds_note()
        if creds_note:
            parts.append(creds_note)
        pending = self.run.pending_steering()
        if pending:
            if phase in STEERING_ACTIONABLE_PHASES:
                parts.append("\n## Operator steering (MUST take priority)\n")
                for p in pending:
                    parts.append(f"\n### {p.name}\n{p.read_text()}\n")
            else:
                # Non-actionable phase (review/verify): surface as read-only
                # context only -- this phase must not consume it (see
                # STEERING_ACTIONABLE_PHASES), so it stays pending for the
                # next planning/worker iteration to actually act on.
                names = ", ".join(p.name for p in pending)
                parts.append(
                    "\n## Operator steering (pending, not for this phase)\n"
                    f"Steering file(s) {names} are waiting for the operator's "
                    "instructions to be actioned by the next planning/worker "
                    "iteration. This phase does not consume or act on them.\n")
        parts.append(self._steering_ledger_context())
        parts.append(self._recent_steering_echo())
        if extra:
            parts.append("\n" + extra)
        return "".join(parts)

    def _recent_steering_echo(self) -> str:
        """The read-only context echo of RECENT operator messages (task 014,
        PRD requirement J, #98) -- appended to EVERY hat's prompt, whatever
        the phase and whatever each message's state.

        The successor of the `pending, not for this phase` notice above,
        which the requirement asks be kept but which on its own was neither
        *every* hat's (only the non-actionable ones saw it) nor *recent*
        (a message vanished from every prompt the moment it was consumed,
        so a `review`/`reflect` hat could not see what the operator had
        just asked for). Both gaps are closed here in one place: this text
        is phase-independent by construction, so no hat can be forgotten.

        It deliberately carries NO consumption semantics:

        * building a prompt neither consumes nor settles anything -- the
          only writer of `steering/.consumed.json` is
          `RunDir.consume_steering`, called after an ACTIONABLE-phase
          iteration finishes cleanly (`_run_iteration_once`), and nothing
          here touches it;
        * a message's `applied (settled)` label is the harness fact that
          some hat processed it -- never a claim that the tasks it produced
          are `done`. That verdict lives in `tasks.json` alone
          (requirement I's `settled` != `done` split), which the echo says
          out loud so no hat can read "settled" as "finished";
        * whether the hat now reading this may ACT on a still-pending
          message is stated by the sections above (`STEERING_ACTIONABLE_
          PHASES`), never by presence in this list.

        Capped at `STEERING_ECHO_MAX_MESSAGES` most recent messages, each
        body at `STEERING_ECHO_MAX_BODY_CHARS`, so a long-running job with
        dozens of steers cannot crowd out the prompt it is attached to; the
        cap is stated in the text (with the run-dir path of the full file)
        rather than silently applied.
        """
        entries = self.run.steering_entries()
        if not entries:
            return ""
        shown = entries[-STEERING_ECHO_MAX_MESSAGES:]
        lines = [f"\n{STEERING_ECHO_HEADING}\n"]
        if len(shown) < len(entries):
            lines.append(f"The {len(shown)} most recent of {len(entries)} "
                         f"operator messages this run ")
        else:
            lines.append(f"All {len(entries)} operator message(s) of this run ")
        lines.append(
            "(oldest first), as CONTEXT ONLY.\n"
            "\nThis section is read-only and carries no consumption "
            "semantics: reading it neither consumes nor settles anything, "
            "and being listed here is not permission to act. Whether this "
            "phase may act on a still-pending message is stated in the "
            "steering section above (if any); `applied (settled)` means only "
            "that some hat processed the message, never that the work it "
            "asked for is done -- `" + str(self.run.tasks_file) + "` is the "
            "single source of truth for that.\n")
        for entry in shown:
            body = (entry.get("body") or "").strip()
            if len(body) > STEERING_ECHO_MAX_BODY_CHARS:
                path = self.run.steering_dir / entry["file"]
                body = (body[:STEERING_ECHO_MAX_BODY_CHARS]
                        + f"\n[truncated at {STEERING_ECHO_MAX_BODY_CHARS} "
                          f"characters; full text: {path}]")
            state = ("applied (settled)" if entry.get("consumed")
                     else "pending")
            lines.append(f"\n### {entry['file']} — {state}, received "
                         f"{entry.get('ts')}\n{body}\n")
        return "".join(lines)

    def _steering_ledger_context(self) -> str:
        """The steering LEDGER section (task 034), carried in EVERY hat's
        prompt -- worker, verify, review, planning and every other one --
        beside the recent-messages echo (`_recent_steering_echo`).

        One line per steering file of this run, whatever its state: when it
        was received, when an iteration consumed it, whether the steer hat
        claimed it settled, and the application status that claim produced
        (`applied` | `partially-applied` | `pending`, the vocabulary
        `state.STEER_APPLICATION_STATUSES` declares) plus the hat's own
        settlement notes.

        Derived, never asserted: `state.steering_ledger` joins the files on
        disk with the engine's own `steering/.ledger.json` bookkeeping, so a
        steering file nobody has processed yet appears here as `pending` in
        the VERY NEXT iteration's prompt without waiting for a steer
        iteration to write anything.

        Semantics, stated in the text itself rather than left to be inferred:

        * the ledger INFORMS -- a validator must not fail work for
          conforming to an APPLIED steer, and must weigh a PENDING steer
          when judging whether something is stale;
        * it is NOT a waiver (`STEERING_LEDGER_NOT_A_WAIVER`): only
          operator amendments in `/config/amendments` waive an obligation,
          exactly as before this section existed;
        * reading it consumes and settles nothing -- building a prompt
          never does, here as in the recent-messages echo.

        Surfaced from the engine, the same way
        `_operator_amendment_review_context` and `_petitions_review_context`
        surface their own facts: the hat prompts acknowledge the section,
        they do not have to reconstruct it.
        """
        entries = self.run.steering_ledger()
        if not entries:
            return ""
        lines = [f"\n{STEERING_LEDGER_HEADING}\n",
                 f"One entry per operator steering file of this run "
                 f"({len(entries)} total), oldest first: what the operator "
                 f"sent, what the harness did with it, and what the steer "
                 f"hat claimed.\n"
                 "\nHow to read it: `applied` means the steer hat processed "
                 "that message and claimed it settled; `partially-applied` "
                 "means it processed the message without claiming that; "
                 "`pending` means no steer iteration has settled it yet. "
                 "The ledger INFORMS -- do not fail work for conforming to "
                 "an APPLIED steer, and weigh a PENDING steer when judging "
                 "whether a deliverable is stale. "
                 f"{STEERING_LEDGER_NOT_A_WAIVER} An application status is "
                 "never a verdict on the tasks a steer produced either -- "
                 f"`{self.run.tasks_file}` is the single source of truth for "
                 "that. Reading this section consumes and settles nothing.\n"]
        for entry in entries:
            consumed = entry.get("consumed")
            if not consumed:
                consumed = ("consumed (time unrecorded)"
                            if entry.get("state") == STEERING_APPLIED
                            else "not yet consumed")
            else:
                consumed = f"consumed {consumed}"
            notes = (entry.get("notes") or "").strip()
            if len(notes) > STEERING_LEDGER_MAX_NOTES_CHARS:
                notes = notes[:STEERING_LEDGER_MAX_NOTES_CHARS] + " [truncated]"
            lines.append(
                f"\n- `{entry['file']}` — received {entry.get('received')}, "
                f"{consumed}, settled: "
                f"{'yes' if entry.get('settled') else 'no'}, application: "
                f"{entry.get('application') or STEER_APPLICATION_PENDING}")
            if notes:
                lines.append(f"\n  - steer hat's settlement notes: {notes}")
        lines.append("\n")
        return "".join(lines)

    def _workspace_note(self) -> str:
        """Single-workspace jobs (the common case) get the original one-line
        form. Multi-workspace jobs (repeatable `ralphctl start --workspace
        DIR:NAME`, PRD req 27) mount each repo at /workspace/<name> and set
        RALPHD_WORKSPACES=<comma-separated names> on the container; list
        each mounted name/path so the agent knows what's there without
        having to guess from a directory listing."""
        names_env = os.environ.get("RALPHD_WORKSPACES", "")
        names = [n for n in names_env.split(",") if n]
        if not names:
            return f"- Workspace (code) directory: {self.workspace}\n"
        lines = [(f"- Workspaces (code directories), {len(names)} mounted "
                  f"under {self.workspace}:\n")]
        for name in names:
            lines.append(f"  - {name}: {self.workspace / name}\n")
        return "".join(lines)

    @staticmethod
    def _creds_note() -> str:
        """List the credential *.env file names currently placed at
        ~/.creds (never values) plus the sourcing rule, so every phase
        prompt knows what's available without any secret ever appearing
        in the prompt text (PRD req 7). Read fresh each call so runtime
        creds CRUD (PUT /config/creds/{name}) is reflected next iteration.
        """
        home = Path(os.environ.get("HOME") or os.path.expanduser("~"))
        creds_dir = home / ".creds"
        if not creds_dir.is_dir():
            return ""
        names = sorted(p.name for p in creds_dir.glob("*.env"))
        if not names:
            return ""
        lines = ["\n## Credentials\n",
                 "Available credential files (values withheld from this prompt):\n"]
        for name in names:
            lines.append(f"- `~/.creds/{name}`\n")
        lines.append(
            "\nSource only the file(s) you need, in the shell command where "
            "you need them: `set -a; . ~/.creds/<name>.env; set +a`. Values "
            "are never auto-exported into your environment.\n"
            "\n**Never print, `cat`, `echo`, or otherwise dump a credential "
            "file's contents, and never paste a secret value into a command's "
            "arguments** (e.g. as a URL query string, a `--token` flag, or an "
            "inline `curl -H \"Authorization: Bearer <value>\"`). Every tool "
            "call's arguments and stdout are recorded verbatim in this run's "
            "iteration transcript (host-visible, permanent) -- doing either "
            "one permanently persists the secret outside the credential file "
            "itself. Refer to credentials only as `$VARNAME` after sourcing "
            "the file with `set -a; . ~/.creds/<name>.env; set +a`; let the "
            "tool (git, curl, etc.) read the variable from its own "
            "environment rather than echoing it. This also forbids "
            "token-bearing git remote URLs (e.g. `https://<token>@host/...`) "
            "-- configure the credential helper/`~/.git-credentials` instead "
            "so the token never appears in a `git remote`/`git clone` "
            "argument or in `git remote -v` output.\n")
        return "".join(lines)

    @staticmethod
    def _docker_siblings_note() -> str:
        """Guidance appended when the operator granted docker socket access
        (ralphctl start --allow-docker sets the RALPHD_HOST_* env vars).

        Task 035 (#7) added the sibling-only cleanup idiom. The job
        container carries ralphd.run=<run-id> exactly like its siblings (it
        is how `ralphctl stop`/`rm` reap a whole run), so the obvious
        "tidy up my containers" one-liner filtered on that label alone
        deletes the container the agent is running in -- the run dies
        mid-iteration and the iteration's work is lost. Hence: siblings also
        carry ralphd.role=sibling, cleanup filters on BOTH labels, reaping
        is ralphctl's job, and RALPHD_SELF_CONTAINER_ID names the one id
        never to touch. The prompt states the rule *and* the why -- a bare
        prohibition tends to get "optimised" away by the next model.
        """
        host_ws = os.environ.get("RALPHD_HOST_WORKSPACE")
        host_wss = os.environ.get("RALPHD_HOST_WORKSPACES")
        host_run = os.environ.get("RALPHD_HOST_RUN_DIR")
        if not host_ws and not host_wss and not host_run:
            return ""
        run_id = os.environ.get("RALPHD_RUN_ID", "")
        lines = ["\n## Docker siblings\n",
                 ("The host docker socket is mounted: `docker` starts SIBLING "
                  "containers on the HOST daemon, not children of this container.\n"),
                 ("- `-v` paths for siblings are HOST paths. This container's "
                  "paths (/workspace, /run/ralphd) are meaningless to the host "
                  "daemon — mounting them yields EMPTY dirs and can create "
                  "root-owned dirs on the host. Use these host-side equivalents:\n")]
        if host_ws:
            lines.append(f"  - workspace: `$RALPHD_HOST_WORKSPACE` = `{host_ws}`\n")
        elif host_wss:
            for name, path in json.loads(host_wss).items():
                lines.append(f"  - workspace `{name}` (mounted at /workspace/{name} "
                             f"in this container): host path `{path}`\n")
        if host_run:
            lines.append(f"  - run dir: `$RALPHD_HOST_RUN_DIR` = `{host_run}`\n")
        ws_mount = ("$RALPHD_HOST_WORKSPACE" if host_ws
                    else "<host workspace path from above>")
        self_id = os.environ.get("RALPHD_SELF_CONTAINER_ID", "")
        self_id_shown = f" (= `{self_id}`)" if self_id else ""
        lines.append(
            f"- Label every sibling with BOTH `--label "
            f"ralphd.run=$RALPHD_RUN_ID` (= `{run_id}`) and `--label "
            "ralphd.role=sibling` so it gets reaped with this job and can be "
            "told apart from the job container; prefer `--rm` for anything "
            "short-lived.\n"
            "- **Never clean up by the run label alone.** THIS container — the "
            "job itself — also carries `ralphd.run=$RALPHD_RUN_ID` (plus "
            "`ralphd.role=job`), so `docker rm -f $(docker ps -aq --filter "
            "label=ralphd.run=$RALPHD_RUN_ID)` kills the run mid-iteration: "
            "the agent process dies, the iteration's work and transcript are "
            "lost, and the run dir is left non-terminal. Always add the role "
            "filter so the query can only ever match siblings:\n"
            "  - list: `docker ps -aq --filter "
            "label=ralphd.run=$RALPHD_RUN_ID --filter "
            "label=ralphd.role=sibling`\n"
            "  - remove: `docker rm -f $(docker ps -aq --filter "
            "label=ralphd.run=$RALPHD_RUN_ID --filter "
            "label=ralphd.role=sibling)`\n"
            f"- `$RALPHD_SELF_CONTAINER_ID`{self_id_shown} is this job's own "
            "container: never `docker stop`/`rm`/`kill` it, and never pass it "
            "to a command that removes what it lists.\n"
            "- You do not have to reap anything at the end: tearing the run "
            "down is `ralphctl`'s job (`ralphctl stop`/`rm` on the host may "
            "filter on the run label alone, precisely because there it *should* "
            "take the job container with it). Only remove siblings you are done "
            "with mid-run, with the two-filter form above.\n"
            "- Images you build and volumes you create live on the HOST and "
            "outlive this job (`ralphctl stop`/`rm` reap labeled *containers* "
            "only) — label them too, and delete any you did not mean to keep.\n"
            "- **Need a toolchain this image lacks?** Run it in a sibling "
            "instead of trying to install it: this image is deliberately thin "
            "(Python/Node) and `agent` cannot `apt-get`. Proven working this "
            "way: Go build/test, real `tmux` (private `-L` socket + "
            "`capture-pane`), and pty-driven TUI tests.\n"
            "  - Commit a small `ci/Dockerfile` (base image + just that "
            "toolchain) and a `ci/run.sh` wrapper in the target repo, so the "
            "setup is reproducible without you, then build it here: "
            "`docker build -t <repo>-ci --label ralphd.run=$RALPHD_RUN_ID ci/`\n"
            "  - Run each command in a throwaway sibling: `docker run --rm "
            "--user 1000:1000 --label ralphd.run=$RALPHD_RUN_ID "
            "--label ralphd.role=sibling "
            f"-v {ws_mount}:/workspace -w /workspace <repo>-ci <cmd>`\n"
            "  - `--user 1000:1000` is mandatory (this container and the host "
            "user are both uid 1000): a root sibling leaves root-owned files "
            "in the workspace that you can then neither edit nor delete.\n"
            "  - Mount a **named volume** for the toolchain's caches and point "
            "the cache env vars at it (`-v <vol>:/cache -e HOME=/tmp -e "
            "GOMODCACHE=/cache/gomod -e GOCACHE=/cache/gobuild`), or every "
            "run re-downloads its dependencies. Name it after the "
            "repo+toolchain (`<repo>-gocache`) so runs and later jobs share "
            "it, and leave the run label off it; a per-run volume is fine "
            "only if you `docker volume rm` it before finishing. Never make a "
            "shared volume's use conditional on `$RALPHD_RUN_ID` — that "
            "breaks the next run.\n"
            "  - Siblings run on docker's default bridge network with normal "
            "internet (image pulls, dependency downloads) whatever network "
            "this container is on, and need no LLM/gateway access — do not "
            "pass any. Workspace writes are visible both ways immediately.\n")
        return "".join(lines)

    # -- iteration ----------------------------------------------------------
    # Phases protected by the infra-fault retry-with-backoff wrapper.
    #
    # Task 009 (#5): ALL five phases. A gateway/endpoint outage does not
    # care which prompt the engine happens to be running -- task 001a only
    # covered planning/worker because those are the two phases the original
    # incident hit, which left the other three scoring an outage as work:
    # an infra-shaped `review` failure rejected the approach and archived
    # it, an infra-shaped `verify` failure ate the task's bounded
    # error-retry budget (and, once that ran out, risked its
    # validationAttempts), and `reflect` simply lost the reflection report.
    #
    # Precedence (truthfully, as implemented): an infra-classified failure
    # is handled *here* -- retried in place, refunded, the episode clock
    # deciding when to give up -- and consumes none of the phase-local
    # error budgets. `run_iteration()` only returns to the phase's own
    # logic once the result is no longer an infra fault (a success or a
    # genuine work failure), or once the wrapper gave up, in which case
    # `_abort_reason` is set and `budget_left()` is already False -- so
    # `_verify_task`'s MAX_VERIFY_ERROR_RETRIES loop and the review
    # steering loop both exit at once instead of re-charging the same
    # outage against their own budgets. Nothing here touches a task's
    # `validationAttempts` (only an explicit non-error verdict miss in
    # `_verify_task` does). An infra fault that is also an *instant*
    # failure is retried here too (task 010, #5), with task 059's
    # broken-environment carve-out keeping the last word on a *run* of
    # identical instant faults -- see _check_instant_failure().
    #
    # Task 018 (#5): `reflect` is listed here AND actually retried -- the
    # two pieces of the job's own ending that used to make this wrapper a
    # no-op for the post-terminal iteration (a spent episode clock and an
    # engine-recorded abort reason vetoing the infra verdict) are handled in
    # _begin_reflect_retry_window(), and reflect's episode is budgeted
    # separately (_outage_budget_for) because the job is already over.
    INFRA_RETRY_PHASES = ("planning", "worker", "review", "verify", "reflect",
                         # Task 018 (requirement K, #101): `plan_review` is
                         # budget-charged like planning/worker/review (the
                         # commit message and `artifacts/reports/task-018-
                         # requirement-k-budget-decision.md` record why),
                         # so it gets the SAME infra-retry wrapper they do
                         # -- an infra-classified fault here is retried and
                         # refunded, never charged against the run's own
                         # iteration budget or the bounded revision count.
                         "plan_review",
                         # Task 020 (requirement L, #101): `verdict_gate` is
                         # budget-charged the same way, for the same reason
                         # -- real reasoning work over the finished
                         # approach, not bookkeeping (see this task's
                         # commit message and `artifacts/reports/task-020-
                         # requirement-l-rejection-semantics.md`).
                         "verdict_gate")

    async def run_iteration(self, phase: str, extra: str = "",
                             prompt_name: str | None = None,
                             tier: str | None = None):
        """Task 016 (#68 half 2, requirement I/J): `tier` is the effective
        tier THIS dispatch provisions at -- the in-hand task's effective
        tier for a task-scoped call (worker, per-task verify), one of the
        two run-level constants (`state.run_level_verify_tier()`,
        `tiers.DEFAULT_TIER`) for every hat with no task in hand (planning,
        reflect, run-level review, and a worker call made with no task
        selected at all). An unrecognised or omitted `tier` degrades to
        `tiers.DEFAULT_TIER` in `_run_iteration_once` itself, never here --
        this method is pure pass-through, so the infra-retry wrapper
        re-tries the SAME tier on every attempt of the same phase/
        iteration.
        """
        if phase not in self.INFRA_RETRY_PHASES:
            return await self._run_iteration_once(phase, extra, prompt_name, tier)
        return await self._run_iteration_with_infra_retry(phase, extra, prompt_name, tier)

    # Task 001a: escalating backoff defaults live on JobConfig
    # (cfg.infra_retry_backoff_s / cfg.infra_retry_backoff_max_s /
    # cfg.infra_outage_budget_s / cfg.infra_retry_max), overridable via the
    # matching RALPHD_INFRA_* env vars so tests (and operators) don't need a
    # job.yaml edit for every run.
    #
    # Task 008 (#5): the stopping rule is the wall-clock outage budget
    # (episode clock, see __init__), NOT an attempt count. cfg.infra_retry_max
    # is a back-compat cap honoured only when explicitly configured.
    async def _run_iteration_with_infra_retry(self, phase: str, extra: str,
                                              prompt_name: str | None,
                                              tier: str | None = None):
        """Runs `phase` via _run_iteration_once(), retrying THE SAME
        phase/iteration with escalating backoff whenever the result
        classifies as an infra fault (broken LLM endpoint/provider/network
        -- see .faults.classify_fault) rather than a genuine work failure.

        Each infra-classified attempt is refunded (never counted against
        cfg.iterations, see budget_left()) and does NOT touch
        self._instant_failure_streak / the no-progress stagnation guard --
        callers (the planning/worker/review/verify/reflect call sites in
        _run_job_core) see a fully resolved IterationResult exactly as
        before, either a genuine
        success/work-failure or (once the episode ran out of stopping room,
        see below) the last failing attempt with self._abort_reason already
        set to a diagnostic naming the infra fault plainly.

        Task 008 (#5): retries are driven by a wall-clock *episode clock*,
        not an attempt count. Attempts are unlimited by default and stop
        only when the episode's cumulative backoff wait has reached
        cfg.infra_outage_budget_s (default 4h) -- so a 30-minute endpoint
        outage costs the job nothing but time, while a permanently broken
        endpoint still terminates the run with a reason naming the total
        outage duration and the last error. cfg.infra_retry_max, when
        explicitly configured, still caps the attempts (back-compat, and an
        escape hatch for operators who want a hard stop); the episode resets
        on any iteration that reaches the model (_reset_infra_episode).

        Task 010 (#5): an infra fault that is ALSO an *instant* failure
        (sub-INSTANT_FAILURE_MAX_DURATION_S with no observable work) is now
        retried here like any other -- a connection refused by a gateway
        that is still coming up returns in 0.2s and used to end the run on
        the spot. The pre-existing broken-environment carve-out keeps the
        last word on a *run* of them: every such attempt is scored by
        _check_instant_failure(), and once MAX_CONSECUTIVE_INSTANT_FAILURES
        attempts have failed instantly, with no traffic and with the *same*
        error signature, the wrapper stops with that carve-out's diagnosis
        instead of sitting out the whole outage budget on an environment
        that is never coming back. An instant failure that DID reach the
        model (tokens billed) is still handed straight back to the phase's
        own bounded error retry -- see the comment at that return below.

        Task 003 (#11): an operator-initiated abort/interrupt is never an
        infra fault (classify_fault(operator_abort=...) sees
        self.operator_abort_requested), so POST /abort and POST /interrupt
        take effect at once instead of being fought by a retry episode --
        pi reports both as the same bare "aborted" error a provider-side
        stream abort produces.

        Task 015 (#5): each backoff wait is interruptible -- POST /retry
        wakes it immediately (_wait_out_backoff / self._retry_now) and
        restarts the outage-budget clock.
        """
        retry_max = self.cfg.infra_retry_max  # None == no explicit attempt cap
        budget = self._outage_budget_for(phase)
        while True:
            result = await self._run_iteration_once(phase, extra, prompt_name, tier)
            fault = self._classify_result(result)
            if fault != "infra":
                # The agent reached the model -- a success, or a genuine work
                # failure it produced itself. Either way the outage (if there
                # was one) is over: the next one starts from a clean clock.
                self._reset_infra_episode()
                return result
            # Task 010 (#5): decide which mechanism owns an *instant*
            # failure before retrying anything here.
            if (result.duration_s is not None
                    and result.duration_s < self.INSTANT_FAILURE_MAX_DURATION_S
                    and not result.no_traffic_timeout
                    and self._instant_failure_signature(result) is None):
                # An instant failure that DID reach the model (an in-band
                # provider error with tokens billed -- a 0.3s Bedrock 502
                # mid-verify) stays with the phase's own bounded error
                # retry, which records the attempt's outcome and retries it
                # without consuming a validation attempt. Task 010 only
                # moves the *zero-work* instant shape (nothing observable
                # at all: the refused-connection / broken-credential shape)
                # into this wrapper.
                return result
            # Score this attempt against the broken-environment carve-out (a
            # streak of identical instant zero-work failures). True == the
            # streak just reached its threshold and _abort_reason now holds
            # that diagnosis.
            broken_env = self._check_instant_failure(result, self.iterations_used)

            self._refund_iteration("infra")
            if self._infra_episode_started_at is None:
                self._infra_episode_started_at = time.monotonic()
            self._infra_episode_attempts += 1
            attempt = self._infra_episode_attempts
            waited = self._infra_episode_waited_s
            error_desc = result.error_message or "no LLM traffic within startup window"
            # Escalating schedule with the last value repeating, capped by
            # infra_retry_backoff_max_s and by whatever is left of the outage
            # budget, so one episode's cumulative wait never exceeds it.
            backoff_schedule = (self.cfg.infra_retry_backoff_s
                                or DEFAULT_INFRA_RETRY_BACKOFF_S)
            backoff = backoff_schedule[min(attempt - 1, len(backoff_schedule) - 1)]
            backoff = min(backoff, self.cfg.infra_retry_backoff_max_s,
                          max(0.0, budget - waited))
            cap_reached = retry_max is not None and attempt >= retry_max
            budget_spent = waited >= budget
            self.run.emit("infra_retry", phase=phase, attempt=attempt,
                          maxAttempts=retry_max, error=error_desc,
                          noTrafficTimeout=result.no_traffic_timeout,
                          instantFailure=bool(self._instant_failure_streak),
                          backoffS=(None if cap_reached or budget_spent
                                    or broken_env else backoff),
                          waitedS=round(waited, 3), budgetS=budget)
            log.warning("iteration %s classified infra fault (attempt %d, %.0fs of "
                       "the %.0fs outage budget waited): %s",
                       phase, attempt, waited, budget, error_desc)
            # Task 014 (requirement J, #98): the ONE charged-count
            # expression, `iterations_used_charged` -- subtracting infra AND
            # steer refunds. Spelling the subtraction out here by hand used to
            # subtract only `_infra_refunded`, so an infra-faulted attempt
            # following a refunded steer pass silently RE-CHARGED that steer
            # pass in the published figure (`iterationsUsed` 2 while
            # `iterations_used_charged` said 1), undoing exactly the
            # unchargedness this requirement asks for.
            self.run.update_status(
                iterationsUsed=self.iterations_used_charged)
            if broken_env:
                # _check_instant_failure() has already set _abort_reason to
                # the broken-environment diagnosis and logged it: the
                # attempts are still refunded, but this environment is not
                # coming back on its own, so stop now instead of waiting
                # out the outage budget.
                return result
            if cap_reached:
                # Back-compat: reachable only when a cap was set explicitly.
                self._abort_reason = (
                    f"infra fault: {phase} iteration failed after "
                    f"{retry_max} attempts ({error_desc})")
            elif budget_spent:
                outage_s = time.monotonic() - self._infra_episode_started_at
                self._abort_reason = (
                    f"infra fault: {phase} iteration failed throughout a "
                    f"{outage_s:.0f}s infra outage ({attempt} attempts, "
                    f"{waited:.0f}s of the {budget:.0f}s outage budget spent "
                    f"waiting): {error_desc}")
            if self._abort_reason is not None:
                self.run.emit("log", level="error", message=self._abort_reason)
                return result
            cap_note = f"/{retry_max}" if retry_max is not None else ""
            self.run.update_status(currentIteration={
                "phase": phase,
                "note": (f"retrying after infra fault (attempt {attempt}"
                         f"{cap_note}, next in {backoff:.0f}s): "
                         f"{error_desc}")})
            self._begin_infra_wait(phase=phase, attempt=attempt,
                                   error=error_desc, waited_s=waited,
                                   backoff_s=backoff, budget_s=budget)
            elapsed, woken = await self._wait_out_backoff(backoff)
            self._infra_episode_waited_s = waited + elapsed
            self._account_infra_wait(elapsed, phase, attempt)
            self._end_infra_wait()
            if woken:
                # Task 015 (#5): a manual retry restarts the outage-budget
                # episode clock -- the operator asserting "it is back" is new
                # information, and a run that has already sat out most of its
                # budget must not die of budget exhaustion one attempt after
                # being told to try again. The attempt counter is kept, so
                # repeated impatient retries keep escalating the backoff
                # instead of hammering a still-broken endpoint.
                self._infra_episode_waited_s = 0.0
                self._infra_episode_started_at = time.monotonic()

    def _outage_budget_for(self, phase: str) -> float:
        """The wall-clock outage budget one episode of `phase` may spend
        waiting (task 008, #5).

        Every phase gets cfg.infra_outage_budget_s (4h by default) except
        `reflect` (task 018, #5): that iteration runs *after* the job already
        reached its terminal state, so an endpoint that is still down must
        not hold the container open for hours to retry a post-mortem. It
        gets REFLECT_OUTAGE_BUDGET_S at most -- long enough to ride out the
        kind of wobble that killed the job seconds ago, short enough that a
        genuinely dead endpoint ends the run promptly with the failure
        recorded instead of silently discarded.
        """
        budget = self.cfg.infra_outage_budget_s
        if phase == "reflect":
            return min(budget, self.REFLECT_OUTAGE_BUDGET_S)
        return budget

    async def _wait_out_backoff(self, backoff: float) -> tuple[float, bool]:
        """Waits `backoff` seconds unless the operator rings the retry-now
        doorbell first (task 015, #5). Returns (seconds actually waited,
        woken-by-operator).

        The wait is `self._retry_now.wait()` under a timeout rather than a
        plain `asyncio.sleep`, so POST /retry -> Event.set() releases the loop
        task immediately; the seconds actually spent are what gets booked into
        the episode clock, `infraWaitTotalS` and the deadline extension, so a
        cut-short wait is never accounted as a full one.
        """
        self._retry_now.clear()
        self._infra_waiting = True
        started = time.monotonic()
        woken = True
        try:
            await asyncio.wait_for(self._retry_now.wait(), timeout=backoff)
        except TimeoutError:  # asyncio.TimeoutError since 3.11
            woken = False
        finally:
            self._infra_waiting = False
            self._retry_now.clear()
        # Clamped: the timeout path can overshoot by a scheduling tick, and
        # the booked wait must never exceed the backoff the budget planned.
        return min(time.monotonic() - started, backoff), woken

    def _begin_infra_wait(self, *, phase: str, attempt: int, error: str,
                          waited_s: float, backoff_s: float,
                          budget_s: float) -> None:
        """Publishes the degraded half of the status contract (task 012, #5)
        for the backoff wait that is about to start.

        ``state`` deliberately stays ``running`` -- adding a "degraded" state
        value would break every consumer's terminal-state logic (``ralphctl
        watch`` included). The degraded case is carried by two new fields
        instead: ``health`` ("ok" | "degraded") and ``infraWait``, which is
        ``null`` whenever the run is not actually sitting in a backoff wait
        and otherwise says since when, which attempt, the error, the phase,
        when the next attempt is due, and how much of the outage budget is
        spent/left. The same payload goes out as an ``infra_wait`` event so
        the wait is visible in the event stream `ralphctl watch` follows,
        not only to whoever polls /status at the right moment.
        """
        now = time.time()
        remaining = max(0.0, budget_s - waited_s)
        wait = {
            "since": utc_from_epoch(now),
            "attempt": attempt,
            "error": error,
            "phase": phase,
            "nextAttemptAt": utc_from_epoch(now + backoff_s),
            "waitedS": round(waited_s, 3),
            "budgetS": budget_s,
            "remainingS": round(remaining, 3),
        }
        self._infra_degraded = True
        self.run.update_status(health="degraded", infraWait=wait)
        self.run.emit("infra_wait", **wait, backoffS=backoff_s)

    def _end_infra_wait(self) -> None:
        """The backoff wait is over and the next attempt is starting: nothing
        is being waited on any more, so ``infraWait`` goes back to ``null``
        (task 012, #5). ``health`` stays "degraded" until an iteration
        actually reaches the model again (_reset_infra_episode) -- a run whose
        endpoint is still broken has not recovered just because it is between
        two backoffs."""
        self.run.update_status(infraWait=None)

    def _account_infra_wait(self, waited_s: float, phase: str,
                            attempt: int) -> None:
        """Books one finished infra backoff wait (task 011, #5): it adds to
        status.json's ``infraWaitTotalS`` and pushes the job deadline
        (``self.deadline`` and its published wall-clock twin ``deadlineAt``)
        out by exactly the waited seconds, emitting ``deadline_extended`` so
        an extension is auditable rather than a silent clock adjustment.

        The amount booked is the time actually spent in the backoff wait,
        which is also what the outage-budget episode clock counts -- one
        number, so ``infraWaitTotalS`` and the budget arithmetic can never
        disagree (a wait cut short by POST /retry books only the seconds it
        really waited).
        """
        self._infra_wait_total_s += waited_s
        self._extend_deadline_by(waited_s)
        total = round(self._infra_wait_total_s, 3)
        deadline_at = utc_from_epoch(self._deadline_epoch)
        self.run.update_status(infraWaitTotalS=total, deadlineAt=deadline_at)
        self.run.emit("deadline_extended", phase=phase, attempt=attempt,
                      waitedS=round(waited_s, 3), infraWaitTotalS=total,
                      deadlineAt=deadline_at, reason="infra wait")

    def _extend_deadline_by(self, waited_s: float) -> None:
        """Shared clock math for every kind of parked-time credit (task 016,
        #P): moves ``self.deadline`` (monotonic, what ``budget_left()`` and
        the per-iteration timeout formula compare against) and its
        wall-clock twin ``self._deadline_epoch`` out by exactly the same
        amount, so the two can never drift apart. ``_account_infra_wait``
        above also bumps ``self._infra_wait_total_s`` itself (not folded in
        here); ``_account_paused_wait`` below bumps
        ``self._paused_wait_total_s`` instead -- the two totals are tracked
        and published separately (one beside the other in status.json) even
        though the deadline move itself is identical either way.
        """
        self.deadline += waited_s
        self._deadline_epoch += waited_s

    def _account_paused_wait(self, waited_s: float) -> None:
        """Books one finished operator-pause wait (task 016, #P): the pause
        twin of ``_account_infra_wait`` above, sharing its deadline math
        (``_extend_deadline_by``) but its own running total
        (``pausedTotalS``, published beside ``infraWaitTotalS``) and its own
        ``deadline_extended`` ``reason`` ("operator pause", never "infra
        wait") -- an operator reading the event log can tell a pause credit
        from an infra-outage credit apart without cross-referencing anything
        else.

        The amount booked is whatever the caller (``_gate()``, via
        ``_park_until_resumed()``) measured actually parked at the pause
        wait -- this method performs no timing of its own, matching the
        criterion that the parked time is measured at the wait site, not in
        ``pause()``/``resume()`` nor here.
        """
        if waited_s <= 0:
            # A `_gate()` call that never actually parked (the ordinary,
            # overwhelmingly common case: not paused) must credit nothing
            # and emit nothing -- _gate() itself already skips calling this
            # for that case, but a non-positive value reaching here anyway
            # (e.g. a scheduling-tick rounding artefact) must still be a
            # no-op rather than a spurious zero-width credit/event on every
            # ordinary gate call.
            return
        self._paused_wait_total_s += waited_s
        self._extend_deadline_by(waited_s)
        total = round(self._paused_wait_total_s, 3)
        deadline_at = utc_from_epoch(self._deadline_epoch)
        self.run.update_status(pausedTotalS=total, deadlineAt=deadline_at)
        self.run.emit("deadline_extended", waitedS=round(waited_s, 3),
                      pausedTotalS=total, deadlineAt=deadline_at,
                      reason="operator pause")

    def _reset_infra_episode(self) -> None:
        """Ends the current infra-outage episode (task 008, #5): the next
        outage gets the full backoff schedule and the full outage budget
        again, so a job hitting a short glitch every hour is never slowly
        starved of retry budget by the earlier ones. Task 010 (#5): it also
        ends any instant-failure streak -- an iteration that reached the
        model proves the environment works, so the next instant fault
        starts counting from scratch."""
        self._instant_failure_streak = 0
        self._instant_failure_sig = None
        self._infra_episode_attempts = 0
        self._infra_episode_waited_s = 0.0
        self._infra_episode_started_at = None
        if self._infra_degraded:
            # Task 012 (#5): recovery is the one event that clears the
            # degraded half of the status contract -- health back to "ok",
            # infraWait back to null, and an event so an operator watching
            # the stream sees the outage end, not just its start.
            self._infra_degraded = False
            self.run.update_status(health="ok", infraWait=None)
            self.run.emit("infra_recovered", health="ok",
                          infraWaitTotalS=round(self._infra_wait_total_s, 3))

    def _classify_result(self, result: IterationResult) -> str | None:
        """The engine's single fault verdict for one finished iteration:
        ``None`` (not a failure at all), ``"infra"`` or ``"work"``.

        Task 004 (#11): one place derives the classify_fault() inputs from an
        IterationResult, so the verdict recorded in the iteration's meta.json
        and the ``iteration.end`` event (``faultClass``) is by construction
        the same verdict the infra-retry wrapper acts on -- an operator
        reading `faultClass: "infra"` can be sure that is why the attempt was
        retried and refunded.
        """
        return classify_fault(
            error_text=result.error_message or "",
            exit_code=result.exit_code,
            interrupted=result.interrupted,
            timed_out=result.timed_out,
            no_traffic_timeout=result.no_traffic_timeout,
            produced_traffic=bool(result.final_text) or bool(result.usage),
            operator_abort=self.operator_abort_requested,
            # Steering 004: the verdict is decided by the flag above; this one
            # only lets `explain_fault` word the reason honestly (an abort that
            # arrived from outside vs. this engine giving up on its own).
            operator_abort_recorded=self._operator_abort_recorded)

    def _verify_skills_placement(self) -> None:
        """Task 003 (#83 detection): a placement that was verified once at
        startup can still vanish mid-run -- exactly what steer 002/003
        found still happening in this very container, from a source
        (another engine's startup clearing the same ambient `$HOME`) that
        a check run only at THIS engine's own startup can never see.
        Called at the end of every iteration, for every hat, the same
        boundary `_reinject_standing_rules` already uses -- so a mid-run
        loss surfaces within one iteration of happening, without an
        operator having to notice a silent Telegram channel or go look.
        Never raises past this call (see `verify_placement_and_report`'s
        own docstring): one bad check must cost nothing more than itself.
        """
        verify_placement_and_report(self.run, CONFIG_DIR, ambient_home())

    def _reinject_standing_rules(self) -> None:
        """Task 107 (#60 pillar 3): make the notes' `## Standing rules`
        block survive an agent's own notes.md rewrite by CONSTRUCTION,
        not by asking worker.md to preserve it -- called once at the end
        of every iteration (`_run_iteration_once`), for every hat, not
        only `worker`.

        A no-op whenever nothing has ever been pinned
        (`RunDir.read_standing_rules()` returns `""`): an older run, or a
        planning proposal whose `standing_rules` contract field
        (`transitions.HAT_SCHEMAS["planning"]`) was omitted, behaves
        exactly as before this task -- the rest of notes.md stays a free
        agent-writable workspace file either way, this only ever touches
        the one heading-delimited span `state.reinject_standing_rules`
        knows how to find.
        """
        pinned = self.run.read_standing_rules()
        if not pinned.strip():
            return
        try:
            current = self.run.notes_file.read_text()
        except FileNotFoundError:
            current = ""
        updated = state.reinject_standing_rules(current, pinned)
        if updated != current:
            state.atomic_write(self.run.notes_file, updated)

    async def _run_iteration_once(self, phase: str, extra: str = "",
                                  prompt_name: str | None = None,
                                  tier: str | None = None):
        n = self.iterations_used + 1
        # Task 003 (#11): a POST /interrupt shields only the iteration it
        # actually interrupted -- re-arm for this attempt (an abort, which
        # ends the whole run, stays recorded in _abort_reason).
        self._operator_interrupted = False
        self.iterations_used = n
        # Task 019 (#44): NOT `iteration_dir(n)` -- a resumed engine reuses the
        # number of a slot whose engine was killed mid-iteration, so anything
        # that attempt left there is archived to `attempts/NN/` before this one
        # writes. Interaction with task 023 (#32, seeding iterations_used from a
        # CHARGED count rather than the raw max_iteration_number()): that task
        # changes which NUMBER comes next, not what a slot holds, and the two
        # compose either way -- if the crashed attempt stops being re-charged,
        # its number is reused and its files land in `attempts/`; if it stays
        # charged, the next attempt takes a fresh number and archives nothing.
        # Neither seed may read `attempts/` as an iteration: they are records of
        # one, not extra ones.
        itdir = self.run.begin_iteration_dir(n)
        # Task 007 (PRD requirement C): the (model, thinking) bundle --
        # `bundle.thinking`/`bundle.effort` are what actually reaches
        # `runner.PiRunner.run` now, instead of the single global
        # `cfg.thinking` every hat used to share.
        #
        # Task 016 (#68 half 2, requirement J): THE single dispatch call
        # site every phase's model is chosen at -- `tiers.resolve_bundle`
        # applied to the effective tier the caller handed in (defaulted to
        # `tiers.DEFAULT_TIER` here, never above, so an omitted/unrecognised
        # `tier` argument degrades exactly like an unannotated task does,
        # rather than crashing this dispatch). `tiers.default_host_map`
        # widens `self.cfg`'s existing per-hat table (task 007's
        # `bundle_for`, still the only place a `JobConfig` is read for this)
        # into the hat x tier host map the resolver indexes -- there is no
        # OTHER call to `resolve_bundle`/`default_host_map` anywhere in this
        # module, and no second model-resolution pathway beside this one
        # plus the router's own tiny, uncounted `self.cfg.model_for("router")`
        # call in `_ask_router` (task 018) -- the router is not task-scoped
        # and is never tiered (requirement I names planning/reflect/
        # run-level review as the non-task hats; the router is the fourth,
        # pre-existing one, untouched by this task).
        effective_tier = tier if tiers.is_tier(tier) else tiers.DEFAULT_TIER
        host_map = tiers.default_host_map(self.cfg)
        bundle = tiers.resolve_bundle(phase, effective_tier, host_map)
        model = bundle.model
        pending = (self.run.pending_steering()
                   if phase in STEERING_ACTIONABLE_PHASES else [])
        prompt = self.build_prompt(phase, extra, prompt_name=prompt_name)
        (itdir / "prompt.md").write_text(prompt)

        meta = {"number": n, "phase": phase, "model": model,
                "approach": self.run.read_status().get("approach"),
                "startedAt": utcnow(),
                # Task 018 (#34): what this attempt was HANDED (the prompt
                # above carries these note bodies) vs. what it managed to
                # consume -- filled in at the end, and deliberately still `[]`
                # here so an engine that dies mid-iteration leaves a record
                # agreeing with `steering/.consumed.json`: delivered, not
                # applied.
                "steeringDelivered": [p.name for p in pending],
                "steeringConsumed": []}
        atomic_write_json(itdir / "meta.json", meta)
        self.run.emit("iteration.start", number=n, phase=phase, model=model)
        log.info("iteration %d start: phase=%s model=%s", n, phase, model)
        # Task 005 (#11): the *charged* count, not the raw attempt number --
        # an infra-classified attempt is refunded (budget_left()) and must
        # stay refunded in what the operator reads, not be silently
        # re-charged by the next iteration's status write. (A grace review's
        # refund is deliberately NOT subtracted here: it keeps showing as a
        # used iteration number, see test_e2e's grace-review contract.)
        self.run.update_status(phase=phase, iteration=n,
                               iterationsUsed=(n - self._infra_refunded
                                              - self._steer_refunded),
                               currentIteration={"number": n, "phase": phase,
                                                 "model": model,
                                                 "startedAt": meta["startedAt"]})

        timeout = min(self.cfg.iteration_timeout_s,
                      max(self.REFLECT_TAIL_TIMEOUT_S if phase == "reflect"
                          else 60, int(self.deadline - time.monotonic())))
        # Task 001a: the startup-window watchdog applies to exactly the
        # phases the infra-retry wrapper protects -- a phase whose zero-
        # traffic hang nobody would retry should not be killed early
        # either. Since task 009 that is every phase: a hung `review`/
        # `verify`/`reflect` invocation is now cut short at
        # cfg.infra_startup_timeout_s (default 150s) and retried, instead
        # of hanging out the full iteration timeout.
        startup_timeout_s = (min(self.cfg.infra_startup_timeout_s, timeout)
                             if phase in self.INFRA_RETRY_PHASES else None)
        # Task 022 (requirement F): tasks.json status transitions used to
        # be surfaced live, while the agent subprocess was still running,
        # by a 0.25s background poller (`_poll_task_changes`, retired here).
        # In-step liveness is now the log stream alone (`GET /logs?follow`,
        # which tails `output.jsonl` as the subprocess writes it) -- see
        # docs/architecture.md and artifacts/reports/poll-retirement.md for
        # what the poller used to provide and why the log stream already
        # covers it. Task-status "task" events (`_emit_task_changes()`,
        # still called below and at the harness's own at-transition writes)
        # now only ever fire once an iteration has fully ended, matching
        # requirement F's rule that a task's status changes only at a
        # step transition, never while a hat is executing.
        try:
            # Task 033 (#114): a phase-scoped `pi` extension list -- the
            # steer hat's live tool surface and nothing for any other
            # phase. Passed only when non-empty so every other phase's
            # runner call is byte-for-byte what it always was.
            extension_kw = {}
            if exts := self._hat_extensions(phase):
                extension_kw["extensions"] = exts
            result = await self.runner.run(
                prompt, itdir / "output.jsonl", model=model,
                thinking=bundle.thinking, effort=bundle.effort,
                timeout_s=timeout,
                extra_env=current_env(), startup_timeout_s=startup_timeout_s,
                **extension_kw)
        except BaseException as exc:
            # THE CONTAINMENT BOUNDARY for an iteration (task 011, #28).
            #
            # Where: this one `await`, the narrowest scope that covers
            # everything the agent subprocess and its plumbing can throw.
            # Why here and not further out: the code BELOW this block is
            # what makes a failed attempt legible -- meta.json's
            # endedAt/faultClass, iteration.end, the usage accounting,
            # the infra refund. A guard placed around the whole run (see
            # `except Exception` in _run_job_core) can only end the job
            # with an "engine error"; a guard placed here turns any
            # explosion into an ordinary recorded failed iteration and
            # lets the loop carry on.
            #
            # Why BaseException and not Exception: task 010's defect was
            # an `asyncio.CancelledError` leaking out of the runner's own
            # timeout plumbing, and CancelledError is a BaseException --
            # so `except Exception` here could not see it and one
            # iteration blowing its timeout took the whole engine down.
            # The narrow catch was the second half of that bug: the
            # runner is fixed, but nothing structural stopped the next
            # stray cancellation from ending the job, so the boundary is
            # widened deliberately rather than left to trust.
            #
            # Two things are deliberately NOT contained:
            #  * KeyboardInterrupt / SystemExit -- the engine is being
            #    shut down by its supervisor and must unwind, not
            #    swallow it and start another iteration;
            #  * a cancellation that was genuinely requested on THIS
            #    task from outside (`cancelling()` counts cancel() calls
            #    on us, and is 0 for a CancelledError that merely leaked
            #    out of inner plumbing) -- ralphctl/asyncio asked this
            #    coroutine to stop, so honour it.
            cur = asyncio.current_task()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)) or (
                    isinstance(exc, asyncio.CancelledError)
                    and cur is not None and cur.cancelling() > 0):
                raise
            # an engine-side iteration failure (stream error, OS error,
            # a stray cancellation) must cost one iteration, not the
            # whole job
            result = IterationResult(exit_code=None)
            result.error_message = f"engine iteration failure: {exc!r}"

        # Task 004 (#11): record the engine's own fault verdict next to the
        # raw signals it was derived from -- null for a clean iteration,
        # "infra"/"work" otherwise -- so an operator (or a later triage pass
        # over a finished run dir) can see *why* an attempt was retried and
        # refunded without re-deriving the classification from exit codes,
        # error text and token usage by hand.
        fault_class = self._classify_result(result)
        # Task 018 (#34): a steering note is consumed on iteration SUCCESS,
        # not on delivery.
        #
        # The choice is AT-LEAST-ONCE delivery, at-most-once application:
        # `fault_class is None` is exactly "this iteration was not a failure
        # at all" (faults.explain_fault's first rung: no error text, no
        # interrupt, no timeout, a clean exit), so an attempt that failed, was
        # interrupted, timed out or died with the engine leaves its notes
        # PENDING and the next actionable iteration is handed them again. The
        # opposite choice (at-most-once delivery, which is what marking them
        # at iteration start implemented) silently discards an operator's
        # instruction whenever the iteration carrying it dies -- the run then
        # reports it as `applied` and nothing ever acted on it.
        #
        # The ordering IS the guarantee, and it is this: build the prompt from
        # the pending notes -> run the agent -> record the outcome -> only then
        # append to `steering/.consumed.json`. Every step before the last is
        # crash-safe in the operator's favour (the marker is what both
        # `GET /steering` and the on-disk hub fallback read), and the marker
        # write itself is idempotent (`RunDir.consume_steering` skips names
        # already there), so no note can be applied twice even if this ran
        # twice for the same file.
        consumed_now: list[str] = []
        if pending and fault_class is None:
            self.run.consume_steering(pending, n)
            consumed_now = [p.name for p in pending]
        elif pending:
            names = ", ".join(p.name for p in pending)
            self.run.emit("log", level="warning",
                          message=(f"iteration {n} ended {fault_class}: steering "
                                   f"({names}) stays pending for the next "
                                   "actionable iteration"))
        # Task 018 (#5): remember the last iteration's verdict so a following
        # reflect iteration can tell "the job ended because the endpoint was
        # broken seconds ago" from "the job ended for its own reasons".
        self._last_fault_class = fault_class
        self._last_fault_error = result.error_message or ""
        meta.update(endedAt=utcnow(), exitCode=result.exit_code,
                    interrupted=result.interrupted, timedOut=result.timed_out,
                    noTrafficTimeout=result.no_traffic_timeout,
                    sawComplete=result.saw_complete, sawVerified=result.saw_verified,
                    error=result.error_message or None,
                    faultClass=fault_class,
                    # Task 012 (#14): what pi actually used, next to the ref
                    # the engine asked for (`model`, which is null whenever
                    # the operator pinned nothing and pi picked its default).
                    modelResolved=result.model,
                    modelRaw=result.model_raw,
                    steeringConsumed=consumed_now,
                    usage=result.usage)
        atomic_write_json(itdir / "meta.json", meta)
        self._accumulate_usage(result.usage, phase=phase, approach=meta.get("approach"),
                               tier=effective_tier)
        self.run.emit("iteration.end", number=n, phase=phase,
                      exitCode=result.exit_code, interrupted=result.interrupted,
                      sawComplete=result.saw_complete, sawVerified=result.saw_verified,
                      error=result.error_message or None,
                      faultClass=fault_class)
        log.info("iteration %d end: exit=%s complete=%s verified=%s%s",
                 n, result.exit_code, result.saw_complete, result.saw_verified,
                 f" ERROR: {result.error_message}" if result.error_message else "")
        # Task 046 (#55): the marker only counts as the final non-blank line
        # (runner.last_nonblank_line). Failing closed is right, but silently
        # failing closed would leave an operator staring at a run that did not
        # conclude and no record of why -- so say what was seen. This is the
        # only place that distinguishes "never mentioned it" from "mentioned it
        # somewhere other than the end".
        for mentioned, what in ((result.mentioned_complete_not_last, "completion"),
                                (result.mentioned_verified_not_last, "verification")):
            if mentioned:
                self.run.emit(
                    "log", level="warning",
                    message=(f"iteration {n} mentioned the {what} line but did not "
                             f"end with it, so it does not count as the {what} "
                             f"marker; the last non-blank line was "
                             f"{result.last_line[:200]!r} (task 046, #55)"))
        if result.error_message:
            self.run.emit("log", level="error",
                          message=f"iteration {n} agent error: {result.error_message}")
        self._emit_task_changes()
        # Task 012 (#14): promote the observed model id to the run level, so
        # `status.json` (and every surface reading it) names the model this run
        # is actually talking to instead of `model: null`. Only ever written
        # when this iteration observed one: an instant startup failure or a
        # zero-traffic hang must not overwrite a known id with ignorance.
        # Per-iteration ids stay in `iterations/NNNN/meta.json`; this is the
        # latest one, which is what "which model is this run using" means.
        patch = {"currentIteration": None}
        if result.model:
            patch["model"] = result.model
            patch["modelRaw"] = result.model_raw
        self.run.update_status(**patch)
        # Task 107 (#60 pillar 3): run for EVERY hat, not just worker --
        # the pinned `## Standing rules` block must survive a review/
        # verify/reflect iteration's notes.md rewrite exactly as readily
        # as a worker's, and this is the one point every phase already
        # passes through on its way back to `_run_job_core`.
        self._reinject_standing_rules()
        self._verify_skills_placement()
        return result

    @staticmethod
    def _has_reported_price(d: dict) -> bool:
        """True when `d` (an iteration usage dict or a bucket) holds at least
        one *provider-reported* price. Task 049 records a reported price as a
        float `costUSD` (`round(x + 0.0, 6)`, so even a free `0.0` stays a
        float) while the historical nothing-was-billed case adds an int `0` --
        so the type is exactly the "was a price ever quoted" bit, with no
        extra bookkeeping key in the published contract."""
        return isinstance(d.get("costUSD"), float)

    @staticmethod
    def _has_derived_price(d: dict) -> bool:
        """True when `d` holds money DERIVED from the host-side pricing map
        (task 052, #10) rather than quoted by the provider. Kept in its own
        field (`costDerivedUSD`) precisely so the two can never be summed
        into one indistinguishable number."""
        return isinstance(d.get("costDerivedUSD"), float)

    @classmethod
    def _merge_cost_status(cls, bucket: dict, usage: dict) -> str | None:
        """Task 050 (#10): summarise how a *bucket* (total/byPhase/byApproach)
        mixes priced and unpriced iterations, so a total never silently sums a
        subset and calls it the cost.

        `costStatus` is monotone -- unknown cost can never be un-learned:

        * absent -> every iteration in this bucket was priced (or billed
          nothing at all): `costUSD` is the whole truth, and a fully priced
          run's usage is byte-for-byte what it was before this task;
        * `"partial"` -> the bucket mixes reported prices with billed-but-
          unpriced tokens, so `costUSD` is a lower bound (the priced subtotal);
        * `"unknown"` -> tokens were billed and nothing in the bucket ever came
          back with a price, so there is no meaningful cost figure at all;
        * `"derived"` (task 052, #10) -> nothing is unknown, but part of the
          money in this bucket came from the host-side pricing map rather than
          the provider (`costDerivedUSD`), so it must be rendered as derived.
        """
        prev = bucket.get("costStatus")
        # `costPriced is False` == this iteration billed tokens the provider
        # quoted no price for (runner._accumulate_cost); None == no traffic.
        # `state.is_zero_quote` catches the same gap arriving as an implausible
        # ZERO quote (task 049, v0.6) -- including from a pre-v0.6 iteration
        # meta.json replayed into a bucket, which is marked `costPriced: true`.
        # `costDerived is True` == every one of those got a rate from the
        # host-side map, so the gap is filled (derived, not unknown).
        unpriced_gap = ((usage.get("costPriced") is False
                         or state.is_zero_quote(usage))
                        and usage.get("costDerived") is not True)
        has_unknown = prev in ("partial", "unknown") or unpriced_gap
        has_derived = (prev == "derived" or cls._has_derived_price(bucket)
                       or cls._has_derived_price(usage))
        if not has_unknown:
            return "derived" if has_derived else None
        has_price = (prev == "partial" or has_derived
                     or cls._has_reported_price(bucket)
                     or cls._has_reported_price(usage))
        return "partial" if has_price else "unknown"

    @classmethod
    def _merge_usage(cls, bucket: dict, usage: dict) -> dict:
        """Add `usage`'s counters into `bucket` (a per-phase/per-approach/
        overall usage dict) in place, returning it. Shared by every
        accumulation site so byPhase/byApproach sums always equal the
        overall total (PRD req 19).

        Non-numeric markers (task 049's `costPriced`, task 052's `costDerived`)
        are never *added*
        (`False + 0 == 0` would quietly turn the marker into a counter);
        they feed the bucket's own `costStatus` verdict instead (task 050).
        The one bool that is *carried* rather than dropped is `costFree` (task
        049, v0.6): an operator-declared free route is why a bucket's `$0.00`
        over billable tokens is honest, so the declaration has to survive the
        rollup -- otherwise `state.is_zero_quote` would re-read that same zero
        as the implausible-zero anomaly.
        """
        status = cls._merge_cost_status(bucket, usage)
        for k, v in usage.items():
            if isinstance(v, bool):
                continue
            bucket[k] = round(bucket.get(k, 0) + v, 6) if isinstance(v, float) \
                else bucket.get(k, 0) + v
        if usage.get("costFree") is True:
            bucket["costFree"] = True
        if status:
            bucket["costStatus"] = status
        return bucket

    def _accumulate_usage(self, usage: dict, phase: str | None = None,
                           approach: int | None = None,
                           tier: str | None = None) -> None:
        status = self.run.read_status()
        total = status.get("usage", {})
        by_phase = total.get("byPhase", {})
        by_approach = total.get("byApproach", {})
        by_tier = total.get("byTier", {})
        self._merge_usage(total, usage)
        if phase:
            by_phase[phase] = self._merge_usage(by_phase.get(phase, {}), usage)
        if approach is not None:
            key = str(approach)
            by_approach[key] = self._merge_usage(by_approach.get(key, {}), usage)
        if tier:
            # Task 017 (#68 half 2): a per-tier spend/iteration-count bucket,
            # the third breakdown alongside byPhase/byApproach -- same
            # `_merge_usage` accumulator (no new pricing path), plus
            # `iterations` (a plain counter `_merge_usage` never touches,
            # since it is never present on an incoming per-iteration `usage`
            # dict) so a tier's spend and how many iterations produced it
            # are both readable from the one bucket.
            bucket = self._merge_usage(by_tier.get(tier, {}), usage)
            bucket["iterations"] = bucket.get("iterations", 0) + 1
            by_tier[tier] = bucket
        total["byPhase"] = by_phase
        total["byApproach"] = by_approach
        total["byTier"] = by_tier
        self.run.update_status(usage=total)

    def _emit_task_changes(self) -> None:
        tasks = {t["id"]: t.get("status") for t in self.run.read_tasks().get("tasks", [])}
        for tid, status in tasks.items():
            old = self._last_task_snapshot.get(tid)
            if old != status:
                self.run.emit("task", taskId=tid, oldStatus=old, newStatus=status)
        self._last_task_snapshot = tasks

    def _emit_task_status(self, task_id: str, old_status, new_status: str,
                          **extra) -> None:
        """Every direct, immediate "a specific task's status just moved"
        emit (a reported claim applied the instant its hat's subprocess
        exits, or -- task 116 -- the harness's own pre-spawn claim) goes
        through here, not a bare `self.run.emit("task", ...)`, so it also
        updates `_last_task_snapshot` in the same breath. Without that
        second write, the general end-of-iteration `_emit_task_changes()`
        diff (called for every iteration, any hat, at the finalisation
        point BEFORE a hat-applied report or this iteration's pre-spawn
        claim runs) still holds the PRE-transition value for this task the
        next time it runs, at the next iteration's own finalisation --
        and duplicates the exact transition this method already reported,
        one iteration late (caught live: task 116's own
        `test_task_in_progress_visible_while_worker_iteration_running`
        saw both the pre-spawn in-progress claim and the applied-completion
        move emitted a second time, one iteration after each really
        happened)."""
        self.run.emit("task", taskId=task_id, oldStatus=old_status,
                      newStatus=new_status, **extra)
        self._last_task_snapshot[task_id] = new_status

    # Task 022 (requirement F): `_poll_task_changes` -- a 0.25s background
    # poller that ran alongside the agent subprocess re-reading tasks.json
    # so a status transition written mid-iteration (e.g. pending ->
    # in-progress) surfaced as a "task" event immediately, instead of only
    # once the whole iteration finished -- is retired. See
    # artifacts/reports/poll-retirement.md for what it used to provide and
    # why the log stream (`GET /logs?follow`) already covers in-step
    # liveness without it; `_emit_task_changes()` above is still called,
    # but only at the harness's own at-transition writes (never on a timer).

    # -- budget/limits -------------------------------------------------------
    @property
    def iterations_used_charged(self) -> int:
        """The iteration count exactly as published in status.json's
        `iterationsUsed` (raw attempts minus infra AND steer refunds --
        task 014 added the second subtraction, PRD requirement J: a steer
        iteration must leave no trace in this figure, the same as the
        router/contract-fix channels that never touch it at all) -- the
        figure an operator reads, and therefore the one a budget change is
        validated against (task 045, #3)."""
        return self.iterations_used - self._infra_refunded - self._steer_refunded

    @property
    def deadline_epoch(self) -> float:
        """Read-only wall-clock twin of `self.deadline` (task 003, #94 (a)):
        the same value `set_job_deadline` moves and publishes as
        `deadlineAt`. `PATCH /config/budget`'s job-deadline knob resolves
        its `+N`/absolute spec against this figure, the same way the
        `iterations` knob resolves against `iterations_used_charged` above
        -- a public accessor so api.py never has to reach for the
        underscore-named `_deadline_epoch` attribute directly."""
        return self._deadline_epoch

    def set_iteration_budget(self, value: int) -> None:
        """Task 045 (#3): change the iteration budget of a LIVE run.

        budget_left() reads self.cfg.iterations on every turn of the loop, so
        rebinding the field is the whole mechanism: a top-up applies at the
        next iteration boundary with no container restart and no re-read of
        the (read-only mounted) job.yaml. status.json's iterationsBudget is
        rewritten here as well so `GET /status` and `GET /config` can never
        disagree about the number. The caller (api.py) owns validation and
        the audit event.
        """
        self.cfg.iterations = value
        self.run.update_status(iterationsBudget=value)

    def set_max_approaches(self, value: int) -> None:
        """#94 (operator-added scope, steer 003), engine-side setter 1 of 3:
        change the max-approaches budget of a LIVE run, same shape as
        `set_iteration_budget` immediately above.

        `self.cfg.max_approaches` is read at the replan decision in two
        places (`if self._approach < self.cfg.max_approaches:` and its
        `if approach < self.cfg.max_approaches:` twin after a review
        rejection) -- rebinding the field is the whole mechanism, exactly
        like `iterations`/`budget_left()`. status.json's `maxApproaches` is
        rewritten here too (it is otherwise published only once, at run
        start -- see `_run_job_core`'s `update_status(...,
        maxApproaches=self.cfg.max_approaches, ...)`) so `GET /status` can
        never disagree with `GET /config`'s live `cfg.effective()` read.
        The caller (api.py) owns validation (refusing a value below the
        current approach) and the audit event.
        """
        self.cfg.max_approaches = value
        self.run.update_status(maxApproaches=value)

    def set_iteration_timeout(self, value: int) -> None:
        """#94 (operator-added scope, steer 003), engine-side setter 2 of 3:
        change the per-iteration timeout of a LIVE run.

        `self.cfg.iteration_timeout_s` is read once PER ITERATION, inside
        `run_iteration`'s own `timeout = min(self.cfg.iteration_timeout_s,
        ...)` -- so a change here applies to the NEXT iteration this method
        is called for, never to one already in flight (there is no running
        subprocess call to retroactively shorten or lengthen). Unlike
        `iterations`/`max_approaches`, status.json has no field of its own
        for this budget today; `GET /config`'s live `cfg.effective()` read
        (`budgets.iterationTimeoutS`) already reflects the change with no
        extra write, the same way it always has for every other
        `effective()` field. The caller (api.py) owns validation and the
        audit event.
        """
        self.cfg.iteration_timeout_s = value

    def set_job_deadline(self, deadline_epoch: float) -> None:
        """#94 (operator-added scope, steer 003), engine-side setter 3 of 3:
        move the wall-clock end time of a LIVE run.

        Unlike `max_approaches`/`iteration_timeout_s`, this budget has no
        `cfg` field read on every turn -- `self.deadline` (monotonic,
        constructed at `__init__` time) and its wall-clock twin
        `self._deadline_epoch` ARE the entire live representation, the same
        two attributes `_account_infra_wait` above already moves together
        whenever an infra backoff eats into the run's clock. This setter
        applies the same move for an operator-driven change: the delta
        between the new `deadline_epoch` and the current
        `self._deadline_epoch` is added to BOTH `self.deadline` (what
        `budget_left()`'s `time.monotonic() < self.deadline` actually
        compares against) and `self._deadline_epoch` itself, so the two can
        never drift apart the way a change to only one of them would.

        status.json's `deadlineAt` is rewritten here, exactly like
        `set_iteration_budget` rewrites `iterationsBudget` -- it is
        otherwise published only at run start and by `_account_infra_wait`,
        never picked up automatically. The audit event is `deadline_set`,
        deliberately a different event name (and `reason`) than
        `_account_infra_wait`'s `deadline_extended`/"infra wait" pair, so an
        operator-requested change is never mistaken for an automatic
        infra-outage extension when read back from the event log. The
        caller (api.py) owns validation (refusing a deadline set into the
        past) -- this method performs the move unconditionally.
        """
        delta = deadline_epoch - self._deadline_epoch
        self.deadline += delta
        self._deadline_epoch = deadline_epoch
        deadline_at = utc_from_epoch(self._deadline_epoch)
        self.run.update_status(deadlineAt=deadline_at)
        self.run.emit("deadline_set", deadlineAt=deadline_at,
                      reason="operator set_job_deadline")

    def budget_left(self) -> bool:
        # Task 001a: iterations refunded after an infra-classified retry
        # (see _run_iteration_with_infra_retry) never count against the
        # configured budget. Task 014 (requirement J, #98): a steer hat's
        # own iteration joins the subtraction for the same reason.
        charged = (self.iterations_used - self._infra_refunded
                  - self._grace_refunded - self._steer_refunded)
        return (charged < self.cfg.iterations
                and time.monotonic() < self.deadline
                and self._abort_reason is None)

    async def _gate(self) -> float:
        """Every iteration boundary passes through here (task 015's
        `pause`/`resume` seam). Task 016 (#P): a pause is not free -- a run
        parked here is not "using up" its job clock any more than one
        sitting out an infra backoff wait is, so the time actually spent
        parked (measured here, at the wait site itself, never inside
        `pause()`/`resume()`) is credited straight back onto both deadline
        fields via `_account_paused_wait`.

        The `is_set()` check below is deliberate and load-bearing, not an
        optimisation: `_gate()` runs at every single iteration boundary,
        the overwhelming majority of which are NOT paused, and
        `asyncio.Event.wait()` returns immediately (no `await` point at
        all) when the event is already set -- so an unconditional
        before/after `time.monotonic()` pair around it would still measure
        a tiny non-zero duration on every ordinary boundary, crediting a
        spurious sliver of deadline and emitting a `deadline_extended`
        noise event on every iteration of every job that is never paused
        at all. Skipping the measurement entirely whenever nothing is
        actually parked is what keeps a real pause's credit legible instead
        of buried in scheduling-tick noise. Returns the credited seconds
        (0.0 when nothing was actually parked) purely so a test can observe
        it directly -- production call sites (every `await self._gate()`
        elsewhere in this module) discard the value, exactly like they
        always have.
        """
        if self._pause.is_set():
            return 0.0
        waited_s = await self._park_until_resumed()
        self._account_paused_wait(waited_s)
        return waited_s

    async def _park_until_resumed(self) -> float:
        """Actually blocks until `resume()` sets `self._pause` again,
        returning the seconds actually spent parked (task 016, #P). Split
        out from `_gate()` -- mirroring `_wait_out_backoff`'s split from
        `_run_iteration_with_infra_retry` -- purely so a test can stub the
        parking wait itself and drive `_account_paused_wait`'s crediting
        with a controlled elapsed value, without a real multi-hundred-
        second sleep; production code always calls the real version below.
        """
        started = time.monotonic()
        await self._pause.wait()
        return time.monotonic() - started

    # -- main loop ------------------------------------------------------------
    def _resuming_existing_run_dir(self) -> bool:
        """True when this engine process is continuing a run dir that already
        holds real recorded work -- an operator `ralphctl resume`, doctor's
        auto-resume, or a fresh container over a run dir whose prior process
        was killed -- rather than starting a run from scratch.

        THE condition behind both _resume_point()'s skip-planning decision and
        the `resumed` flag on the startup `state` event (task 032, #13), so
        the two can never disagree about what a resume is."""
        return bool(self.iterations_used and self.run.read_tasks().get("tasks"))

    def _archived_approach_awaiting_replan(self, approach: int) -> bool:
        """True iff `approach` was already reviewed and REJECTED by a prior
        episode (`_step_review`'s rejection branch already ran
        `_archive_approach(approach)`), but the crash happened before that
        same episode ever recorded the bump to the next approach (or, if
        `approach` was the last one available, before it recorded the
        run's terminal outcome).

        Task 017 (requirement... resume must reconstruct FSM *position*
        from disk, not just "was planning done"): `_archive_approach` is
        the ONLY thing that ever creates `approaches/<NN>/` (guard: read
        `_step_review`), and it runs strictly after review's verdict is
        already durably on disk -- there is no in-place write between it
        and the next episode's `approach` bump (`_step_planning`'s
        `update_status(approach=...)`) or the run's terminal write, so a
        crash in that window leaves `approaches/<NN>/` present while
        status.json's `approach` field still names the REJECTED approach,
        not the one that should run next. Without this check, resuming
        such a run dir would silently re-enter `worker` over the very
        tasks.json review already rejected -- no gate skipped in the
        normal case, but exactly one skipped here: the approach boundary
        itself.
        """
        return (self.run.root / "approaches" / f"{approach:02d}").is_dir()

    def _crashed_mid_review(self) -> bool:
        """True iff the NEXT iteration slot (`iterations_used + 1` -- the
        one a fresh attempt would reuse; `max_iteration_number()` counts
        only *finished* iterations, so a crashed review invocation's slot
        is never folded into `iterations_used` itself) already holds a
        `review`-phase `meta.json` with no `endedAt`: `_step_review` never
        writes anything to `tasks.json`, so nothing about the plan changed
        between that crash and now, but a plain resume (which always
        re-enters at `worker`, per `_step_planning`'s skip-planning
        unconditional advance) would burn one throwaway worker iteration
        first -- nothing is actionable, every task the crashed review was
        about to judge is still `completed` -- before ever reaching review
        again. That is one iteration of completed-seeming work repeated
        for no reason: the crash left the FSM position at `review`, not at
        `worker`, and resume should land exactly there (guard 25's
        sibling; ledger guard 23's third case).
        """
        d = self.run.root / "iterations" / f"{self.iterations_used + 1:04d}"
        meta_path = d / "meta.json"
        if not meta_path.exists():
            return False
        try:
            meta = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            return False
        return meta.get("phase") == "review" and "endedAt" not in meta

    def _paused_awaiting_incurable_ruling(self, approach: int) -> bool:
        """Task 007 (deferred out of task 006, requirement B.2, #111): True
        iff the run's last recorded activity was `_pause_and_escalate_
        incurable_rejection` self-looping `review` back onto itself for
        `approach` and NOTHING has happened since -- i.e. a prior episode
        was sitting paused on `self._pause` (the same `asyncio.Event`
        `POST /pause`/`POST /resume` already drive) waiting for an
        operator ruling when it died (crash, SIGKILL, a host reboot, a
        plain container restart) without ever waking back up in-process.

        Without this, `_resume_point()` would fall through to its ordinary
        default -- skip planning, re-enter `worker` -- which is wrong on
        both halves of this task's routing requirement: `worker` finds
        nothing actionable (review just rejected exactly the tasks.json
        worker would be looking at) before it ever reaches `review` again,
        and neither `worker` nor a wasted trip through it can ever be
        mistaken for "a fresh approach" (that would need `replan`, which
        this case must also never reach) -- so the risk this predicate
        guards against is a wasted throwaway iteration and a delayed
        review, not an outright wrong-approach spend; naming it here keeps
        that guarantee explicit rather than incidental.

        Detected the same way `_archived_approach_awaiting_replan`/
        `_crashed_mid_review` reconstruct FSM position from disk, not from
        in-memory state that a restart just lost: the most recent
        `operator_escalation` event with `reason == "rejected_incurable"`
        recorded for THIS approach, with no `iteration.start` event on
        record after it -- if a fresh iteration had already started
        (an in-process `resume()` that got all the way to running another
        review attempt, or a second escalation cycle that has since moved
        on), this must not fire again for the now-stale escalation.

        This is also the container-restart half of "operator rulings
        recorded while paused stay visible to the next review": a fresh
        episode landing here still runs `_apply_operator_status_rulings()`
        (-> `state.apply_amendment_status_rulings`) -- called unconditionally
        once per engine EPISODE in `_run_job_core`, ahead of `_resume_point()`
        -- so any `mode: status` ruling recorded while the run sat paused is
        re-applied on exactly this restart, even though the in-process pause
        path never re-runs that call mid-episode (it only runs once, at the
        top of the very episode that reached the pause). A ruling that never
        provoked a restart at all is not left silently unruled either: the
        OTHER ruling channel review's prompt reads, `_operator_amendment_
        review_context()`, hits `CONFIG_DIR` fresh on every `_step_review()`
        call -- in-process resume or restart alike -- so the very next
        review iteration sees it either way.
        """
        escalation_id = None
        iteration_start_id = None
        for ev in state.read_events(
                self.run.root,
                types=("operator_escalation", "iteration.start")):
            if ev.get("type") == "operator_escalation":
                if (ev.get("reason") == "rejected_incurable"
                        and ev.get("approach") == approach):
                    escalation_id = ev.get("id")
            else:
                iteration_start_id = ev.get("id")
        if escalation_id is None:
            return False
        return iteration_start_id is None or iteration_start_id < escalation_id

    def _paused_awaiting_last_approach_ruling(self, approach: int) -> bool:
        """Task 011 (PRD requirement D.1, #142): the exact same shape as
        `_paused_awaiting_incurable_ruling` just above (a crashed/
        restarted episode still sitting paused on an `operator_escalation`
        with no `iteration.start` recorded since), for the `rejected_
        last_approach` reason instead of `rejected_incurable` -- the last-
        approach pause is exactly as resumable, and for exactly the same
        reasons: `worker` has nothing actionable, and landing anywhere
        other than `review` risks reaching `replan` over a NONEXISTENT
        approach the run's own budget has already refused to grant.
        """
        escalation_id = None
        iteration_start_id = None
        for ev in state.read_events(
                self.run.root,
                types=("operator_escalation", "iteration.start")):
            if ev.get("type") == "operator_escalation":
                if (ev.get("reason") == "rejected_last_approach"
                        and ev.get("approach") == approach):
                    escalation_id = ev.get("id")
            else:
                iteration_start_id = ev.get("id")
        if escalation_id is None:
            return False
        return iteration_start_id is None or iteration_start_id < escalation_id

    def _unreviewed_awaiting_resume(self, approach: int) -> bool:
        """Task 016 (PRD requirement F.3, #146): the same crashed/restarted-
        episode shape as `_paused_awaiting_incurable_ruling`/`_paused_
        awaiting_last_approach_ruling` just above, keyed off the
        `review_deadline_unreviewed` event `_review_unjudged_at_budget_end`
        and `_maybe_grace_review`'s own interrupted-grace branch each emit
        the instant they positively know the deadline, not a rejection,
        ended review for `approach` -- True iff the most recent such event
        for THIS approach has no `iteration.start` recorded since (i.e. no
        later episode has already re-entered review over it).

        A resumed episode landing here goes straight to `review` -- never
        `worker` (there is nothing new for the worker to do: every task was
        already completed, that is what got the run to review in the first
        place) and never `replan` (the approach was never archived, so
        replanning it would spend a fresh approach the deadline, not a
        verdict, took) -- so `ralphctl resume` with a fresh iteration/wall-
        clock window asks review again over the SAME approach, exactly the
        PRD's own words for #146.
        """
        unreviewed_id = None
        iteration_start_id = None
        for ev in state.read_events(
                self.run.root,
                types=("review_deadline_unreviewed", "iteration.start")):
            if ev.get("type") == "review_deadline_unreviewed":
                if ev.get("approach") == approach:
                    unreviewed_id = ev.get("id")
            else:
                iteration_start_id = ev.get("id")
        if unreviewed_id is None:
            return False
        return iteration_start_id is None or iteration_start_id < unreviewed_id

    def _resume_point(self) -> tuple[int, bool, str]:
        """Where run_job() should start on this engine invocation (PRD req
        16). A fresh run dir (no tasks.json yet, or no completed
        iterations recorded) starts at approach 1 with planning, exactly
        as before. A run dir that already has a completed planning
        iteration's tasks.json -- e.g. the engine restarted over a stale
        'running' status left by a killed prior process, or over a
        terminal failed/aborted run dir after the operator bumped the
        iteration budget -- resumes the existing approach's worker loop
        directly, skipping planning; iteration numbering continues from
        the existing max (already seeded into self.iterations_used in
        __init__).

        A third case (task 017): the recorded approach was already
        reviewed and rejected by a prior, crashed episode (see
        `_archived_approach_awaiting_replan`) -- the dispatch loop must
        start at the `replan` step, not `worker`, so the approach boundary
        the crashed episode never got to record still happens exactly
        once, through the same guarded transition a live run takes.

        A fourth case (task 017): the prior episode was killed mid-`review`
        invocation, before any verdict was ever recorded (see
        `_crashed_mid_review`) -- the dispatch loop must start at `review`
        directly rather than at `worker`, so resume does not spend one
        throwaway worker iteration re-confirming there is nothing left to
        do before ever reaching review again.

        A fifth case (task 007): the prior episode paused `review` on the
        `rejected_incurable` self-loop and died before an operator's
        `ralphctl resume` ever woke it back up in-process (see
        `_paused_awaiting_incurable_ruling`) -- the dispatch loop must also
        start at `review` directly, for the same reason as the fourth case
        (nothing actionable for `worker` to do) plus one more this case
        adds: landing anywhere other than `review` risks the run reaching
        `replan` -- a FRESH approach -- over findings a fresh approach was
        already shown incapable of curing, which is the exact waste the
        `rejected_incurable` edge exists to avoid in the first place.

        A sixth case (task 016, requirement F.3, #146): the prior episode
        ended this approach `unreviewed` because the deadline took the
        last review attempt (ordinary self-loop exhaustion or the
        off-budget grace attempt) before it could speak (see `_unreviewed_
        awaiting_resume`) -- the dispatch loop must land at `review`
        directly too, for the same reason as the fourth/fifth cases
        (nothing actionable for `worker`, and the approach was never
        archived so `replan` would spend a fresh one the deadline, not a
        verdict, took): a resume with a fresh window asks review again
        over the SAME approach.

        Returns (start_approach, skip_planning_for_start_approach,
        start_step).
        """
        if not self._resuming_existing_run_dir():
            return 1, False, transitions.ENTRY_STATE
        approach = self.run.read_status().get("approach") or 1
        if self._archived_approach_awaiting_replan(approach):
            self.run.emit(
                "log",
                message=(
                    f"resuming existing run-dir state: approach {approach} "
                    "was already reviewed and rejected by a crashed episode "
                    "before it recorded the approach boundary; replanning "
                    "instead of re-entering worker over the rejected tasks"))
            return approach, False, "replan"
        if self._crashed_mid_review():
            self.run.emit(
                "log",
                message=(
                    f"resuming existing run-dir state: approach {approach} "
                    "was mid-review with no verdict recorded when the prior "
                    "episode was killed; resuming directly into review "
                    "instead of re-entering worker over already-completed work"))
            return approach, False, "review"
        if self._paused_awaiting_incurable_ruling(approach):
            self.run.emit(
                "log",
                message=(
                    f"resuming existing run-dir state: approach {approach} "
                    "was paused on a rejected_incurable escalation with no "
                    "operator resume ever reaching this episode; resuming "
                    "directly into review (never a fresh approach) so any "
                    "ruling recorded while paused is seen by the next review "
                    "iteration"))
            return approach, False, "review"
        if self._paused_awaiting_last_approach_ruling(approach):
            self.run.emit(
                "log",
                message=(
                    f"resuming existing run-dir state: approach {approach} "
                    "was paused on a rejected_last_approach escalation with "
                    "no operator resume ever reaching this episode; resuming "
                    "directly into review (never a fresh, nonexistent "
                    "approach) so any ruling recorded while paused is seen "
                    "by the next review iteration"))
            return approach, False, "review"
        if self._unreviewed_awaiting_resume(approach):
            self.run.emit(
                "log",
                message=(
                    f"resuming existing run-dir state: approach {approach} "
                    "ended unreviewed -- the deadline took review before it "
                    "could speak, not a rejection -- with no later episode "
                    "having re-entered review since; resuming directly "
                    "into review (never worker, never a fresh approach) so "
                    "review is asked again over the SAME approach "
                    "(requirement F.3, #146)"))
            return approach, False, "review"
        self.run.emit(
            "log",
            message=(
                f"resuming existing run-dir state: approach {approach}, "
                f"{self.iterations_used} iteration(s) already recorded; "
                "skipping planning"))
        return approach, True, transitions.ENTRY_STATE

    async def run_job(self) -> str:
        """Runs the job to a terminal state, then (if `reflect: true`) one
        extra 'reflect' iteration analyzing the run for prompt/skill
        improvements (PRD req 24). Returns final state: succeeded | failed |
        aborted -- unaffected by the reflect iteration, which runs strictly
        after the outcome below is already settled (though, with `reflect:
        true`, status.json's `state` field itself reads the non-terminal
        `finishing` until reflect is over -- see `_finish()`).

        Task 012 (#56): a process that starts up over a run dir already
        recorded `finishing` (this same job, an earlier episode, settled the
        verdict and then died -- crash, SIGKILL, a host reboot -- before it
        could promote it) has nothing left to decide. Redoing any part of
        the job here would let a dead process's stale opinion overrule one
        that was already settled and written down; the only honest thing to
        do is finish what the crashed episode could not, and stop. See
        _promote_recorded_finishing_state().
        """
        status = self.run.read_status()
        if status.get("state") == "finishing":
            return self._promote_recorded_finishing_state(status)
        state = await self._run_job_core()
        if self.cfg.reflect:
            await self._run_reflection()
        self._promote_finishing_state()
        return state

    def _finish_tail_unverified(self) -> str:
        """The ordinary `("tail",)`-exit terminal write from
        `_run_job_core`'s dispatch loop -- factored out to a single named
        method (task 006, requirement B, #72) so a test can drive exactly
        this write from a hand-built fixture (`self._abort_reason`,
        `self._terminal_reason_note`, `self._deadline_beat_verdict_extra`,
        tasks.json, events.jsonl) instead of only through a full scripted
        `_run_job_core()` run. Behaviour is unchanged from before this
        extraction: `state` is `aborted` iff `self._abort_reason` is set,
        else `failed`; `verdict` is always `unverified` here -- this path
        can never grant `verified` (a second test pins that) -- `reason`
        prefers the abort reason over the free-text terminal note, and
        both status.json patches (`_unconsumed_steering_patch`,
        `_deadline_beat_verdict_extra`) are applied every time, each a
        no-op unless its own condition actually holds.
        """
        final_state = "aborted" if self._abort_reason else "failed"
        # Task 016 (requirement F.3, #146): a deadline that lands mid-
        # review (ordinary self-loop exhaustion or the off-budget grace
        # attempt) is `unreviewed`, not `unverified` -- never overrides an
        # actual operator abort, which keeps its own reason and verdict
        # unchanged.
        if self._review_deadline_unreviewed and not self._abort_reason:
            self._finish(final_state, "unreviewed",
                        reason="deadline during review",
                        **self._unconsumed_steering_patch(),
                        **self._deadline_beat_verdict_extra)
            return final_state
        self._finish(final_state, "unverified",
                    reason=self._abort_reason or self._terminal_reason_note,
                    **self._unconsumed_steering_patch(),
                    **self._deadline_beat_verdict_extra)
        return final_state

    def _finish(self, state: str, verdict: str, **fields) -> str:
        """THE terminal-outcome write (task 012, #56) -- every place
        `_run_job_core` decides the job's fate calls this instead of writing
        `state`/`verdict` on status.json directly, so the reflect window has
        exactly one implementation to get right.

        With `reflect: false` the outcome is written as the real terminal
        state immediately, unchanged from before this task.

        With `reflect: true` the outcome is *settled* (this method decided
        it, for good -- nothing after this call can change `state` or
        `verdict` again) but not yet the terminal state on disk: the post-
        terminal reflect iteration still has to run, and until now that
        iteration ran while status.json already claimed the job was over --
        `rm`/the hub's delete button/`ralphctl status`'s `ended:` line all
        treated a run that was still doing agent work as finished. Instead
        status.json gets the non-terminal `finishing` state (a member of
        `NONTERMINAL_STATES`, so every zombie/liveness check still treats an
        engine that dies in this window as owning the run) plus `verdict`
        (the settled value -- callers rely on this being readable
        immediately, e.g. `ralphctl status --json`'s exit code) and
        `pendingTerminalState` (the value `state` will become the moment the
        window closes). `_promote_finishing_state()` (this same process,
        after reflect returns) or `_promote_recorded_finishing_state()` (a
        resumed process, reading this exact record back) is what writes the
        real terminal state; both read `self._pending_terminal`/status.json
        rather than re-deciding anything.
        """
        fields.setdefault("endedAt", utcnow())
        if self.cfg.reflect:
            self._pending_terminal = (state, verdict, dict(fields))
            self.run.update_status(state="finishing", phase=None,
                                   verdict=verdict, pendingTerminalState=state,
                                   **fields)
            self.run.emit("state", state="finishing")
        else:
            self.run.update_status(state=state, verdict=verdict, phase=None,
                                   **fields)
        self._write_run_record()
        return state

    def _write_run_record(self) -> None:
        """Task 006 (PRD requirement B.1, #140): write/rewrite `run-
        record.md` + `run-record.json` into `artifacts/` from the run's
        own engine-owned records -- called from `_finish()` (every entry
        to `finishing`, reflect on or off) and from `_promote_finishing_
        state()`/`_promote_recorded_finishing_state()` (the later terminal
        transition that rewrites both, per PRD requirement B.1). Reads
        `CONFIG_DIR` fresh on every call, exactly like `_operator_
        amendment_review_context()` does, so a test can monkeypatch
        `loop_mod.CONFIG_DIR` the same way. Best-effort: a run dir the
        record write cannot reach (a read-only mount, a concurrent delete)
        logs and moves on rather than taking the whole terminal write
        down with it -- the status.json write this accompanies has
        already happened by the time this runs.
        """
        try:
            run_record.write_run_record(
                self.run, run_id=self.cfg.run_id,
                max_approaches=self.cfg.max_approaches,
                config_root=CONFIG_DIR)
        except OSError as exc:
            log.warning("could not write run-record.md/.json: %s", exc)

    def _promote_finishing_state(self) -> None:
        """After `_run_reflection()` returns (whether it ran, failed, or was
        skipped outright because a signal was already taking the engine
        down -- `_run_reflection` covers all three without this caller
        needing to know which), moves status.json from the non-terminal
        `finishing` state `_finish()` recorded to the real terminal state
        the job was always going to reach. A no-op when `reflect` is false
        (`_finish()` already wrote the terminal state directly, so
        `_pending_terminal` was never set) -- called unconditionally from
        `run_job()` so there is exactly one call site, not one per branch.
        """
        if self._pending_terminal is None:
            return
        state, verdict, fields = self._pending_terminal
        self._pending_terminal = None
        self.run.update_status(state=state, verdict=verdict, phase=None,
                               pendingTerminalState=None, **fields)
        self._write_run_record()

    def _promote_recorded_finishing_state(self, status: dict) -> str:
        """`run_job()`'s resume-time counterpart to `_promote_finishing_state`:
        the SAME promotion, read back from disk instead of from
        `self._pending_terminal`, because this is a fresh process that never
        called `_finish()` at all -- a prior episode did, then died before
        it could promote its own settled outcome.

        Deliberately does not re-run reflect: reflect already had its one
        attempt (or was skipped for a documented reason) in the episode that
        died, and re-spawning an agent to reflect on a job that already has
        a settled, written-down outcome is exactly the kind of `finishing`-
        window side effect this task exists to bound, not extend. An
        operator who wants the reflection redone can inspect
        `artifacts/reflection/` and re-run it by hand; `ralphctl repair
        --set-state <state>` reaches the identical terminal outcome without
        starting an engine at all, since `pendingTerminalState` is sitting
        right there in status.json for a human or a script to read.
        """
        state = status.get("pendingTerminalState")
        verdict = status.get("verdict")
        if state not in TERMINAL_STATES:
            # Should never happen (state.py's own write contract), but a
            # hand-edited or corrupted status.json must not crash the
            # engine -- fail loudly and terminally instead of looping.
            self.run.update_status(
                state="failed", verdict="unverified", phase=None,
                endedAt=utcnow(),
                reason=("resumed a `finishing` run dir with no valid "
                        f"recorded pendingTerminalState ({state!r})"))
            self._write_run_record()
            return "failed"
        self.run.emit(
            "log",
            message=(f"resuming a run dir recorded `finishing`; promoting "
                     f"directly to the settled outcome {state!r} without "
                     "redoing any of the job (task 012, #56)"))
        self.run.update_status(state=state, verdict=verdict, phase=None,
                               pendingTerminalState=None)
        self._write_run_record()
        return state

    # Task 018 (#5): the post-terminal reflect iteration's own outage budget
    # cap -- see _outage_budget_for(). The job is over by the time reflect
    # runs; five minutes is enough to ride out the wobble that just killed it
    # without keeping a finished run's container alive for hours.
    REFLECT_OUTAGE_BUDGET_S = 300.0

    # Task 008 (requirement C, #72): the reflect iteration's own per-attempt
    # wall-clock floor, independent of self.deadline -- see
    # _run_iteration_once's `timeout` computation. `finishing`/`reflect` is
    # the run's own account of itself (PRD req 24) and it is the first thing
    # a deadline that fires mid-run eats: once self.deadline is already in
    # the past, the ordinary formula (`max(60, deadline - now)`) gives EVERY
    # phase, including reflect, exactly the 60s floor -- nowhere near enough
    # for a real post-mortem write (v0.9's iteration 185, `selfdev-
    # v09-fsm-loop`, died this way). Reflect now gets at least this many
    # seconds for its one attempt even when the deadline is long gone (still
    # capped by cfg.iteration_timeout_s), which is a hard, fixed extension --
    # not a moving target, since _run_reflection() runs this exactly once per
    # process. Rejected alternatives (a reserved tail budget carved out of
    # job_timeout_s up front; extending self.deadline itself the way
    # _account_infra_wait does for infra waits) are written down in
    # artifacts/reports/task-008-finishing-tail-budget.md.
    REFLECT_TAIL_TIMEOUT_S = 600.0

    async def _run_reflection(self) -> None:
        """One extra 'reflect' iteration after the job has already reached a
        terminal state (PRD req 24). Runs unconditionally (not gated by
        budget_left(), which is normally already exhausted by the time a job
        reaches a terminal state) exactly once. The reflect prompt instructs
        the agent to write only under artifacts/reflection/ and touch
        nothing else; the engine's own bookkeeping restores status.json's
        `phase` field to None afterward so a terminal job never appears to
        still be "in phase reflect" once this returns.

        Task 018 (#5): reflect really does go through the infra-retry wrapper
        now (`reflect` has been in INFRA_RETRY_PHASES since task 009, but two
        pieces of the job's own ending made the wrapper a no-op for it -- see
        _begin_reflect_retry_window()), and when the job just ended on an
        infra-shaped failure the first attempt is delayed instead of firing
        into the same dead endpoint in the same second the breaker tripped
        (_reflect_pre_attempt_wait). Neither changes the guarantees this
        method has always had: exactly one reflect phase, strictly after the
        terminal state, unable to change it.

        Task 019 (#5): the result is no longer discarded --
        _record_reflect_outcome() reads it and leaves a verdict behind
        (status.json `reflect`, plus artifacts/reflection/FAILED.md when it
        failed), because a silently swallowed reflect failure looks exactly
        like `reflect: false` from the outside.

        Task 016 (#47): none of it happens when a signal is already taking the
        engine down. Spawning a fresh agent while the child killer has fired
        and SIGKILL is on its way produces a reflect "failure" the engine
        manufactured itself, complete with an artifacts/reflection/FAILED.md
        tombstone blaming the reflection for the operator's `docker stop`. On
        that path the phase is recorded as *not attempted* instead
        (_record_reflect_not_attempted()).
        """
        if self._signal_unwind is not None:
            self._record_reflect_not_attempted(attempted=False)
            return
        self.run.emit("phase", phase="reflect")
        window = self._begin_reflect_retry_window()
        result: IterationResult | None = None
        try:
            await self._reflect_pre_attempt_wait()
            # Task 016 (requirement I): reflect has no task in hand -- its
            # ladder's designated tier is `tiers.DEFAULT_TIER`, unaffected
            # by any task's tier in the plan.
            result = await self.run_iteration("reflect", tier=tiers.DEFAULT_TIER)
        finally:
            self._end_reflect_retry_window(window)
            self.run.update_status(phase=None)
            if self._signal_unwind is not None:
                # The signal arrived mid-attempt: whatever the iteration
                # returned describes the teardown, not the reflection.
                self._record_reflect_not_attempted(attempted=True)
            else:
                # Task 017 (requirement Q, #96): give an unparseable/
                # malformed reflect payload the same re-ask ladder every
                # other contract site has, BEFORE `_record_reflect_outcome`
                # ever reads it as a failure -- see `_reflect_contract_
                # ladder`'s own docstring for the REFLECT_TAIL_TIMEOUT_S
                # bound and the v0.12 requirement G guarantee it preserves.
                result = await self._reflect_contract_ladder(result)
                self._record_reflect_outcome(result)

    def _reflect_skipped_reason(self, attempted: bool) -> str:
        """Why no reflection verdict exists, for a run whose engine was
        signalled (task 016, #47). Names the signal, because "reflect did not
        run" with no cause reads like the phase was never enabled."""
        sig = self._signal_unwind
        if attempted:
            return (f"signal {sig} ended the engine during the reflect phase, "
                    "so the iteration was cut short by this engine's own "
                    "shutdown rather than by anything about the reflection")
        return (f"signal {sig} ended the engine before the reflect phase could "
                "start, so no reflect iteration was attempted")

    def _record_reflect_not_attempted(self, attempted: bool) -> None:
        """Records "there is no reflection verdict, and that is the engine's
        own doing" (task 016, #47): `reflect: {ok: null, attempted, skipped}`
        in status.json plus a `reflect_skipped` event, and deliberately NO
        artifacts/reflection/FAILED.md -- the tombstone means "the reflection
        was tried and failed", which on this path is false.

        `ok: null` keeps every existing consumer's gating intact: `ralphctl
        status` and the hub both act on `ok is False`, so neither reports a
        failure that did not happen (they gain a distinct "not attempted"
        line instead, main.py's _format_reflect_lines).
        """
        reason = self._reflect_skipped_reason(attempted)
        self.run.update_status(reflect={"ok": None, "attempted": attempted,
                                        "error": None, "skipped": reason,
                                        "endedAt": utcnow()},
                              phase=None)
        log.warning("reflect phase not attempted: %s", reason)
        self.run.emit("reflect_skipped", attempted=attempted,
                      signal=self._signal_unwind, reason=reason)

    # Task 019 (#5): the post-mortem the reflect phase is supposed to leave
    # behind. "reflect ran but produced no report" is a failure too -- that
    # is exactly the shape PRD incident 2 had (the phase fired into a dead
    # endpoint and the run dir looked like reflect had never been enabled).
    REFLECT_REPORT = "report.md"

    # The tombstone _record_reflect_outcome() leaves when the phase produced no
    # report -- and, since task 017 (#43), removes again the moment an attempt
    # finally does. It is an assertion ("the reflection was tried and left you
    # nothing"), not a log line, so it may only exist while it is true.
    REFLECT_TOMBSTONE = "FAILED.md"

    # Task 014 (#87): the two spellings a reflect failure can carry, decided
    # once here and used nowhere else. #87's own incident (a deadline-fired
    # reflect returning an unparseable contract payload) is a DEGRADED
    # reflection: the iteration ran to a clean exit and left an untrustworthy
    # or missing deliverable (no report, an invalid/unparseable contract, or
    # a contract that itself disclaims report_written). FATAL is reserved for
    # the other shape -- the iteration itself never produced a trustworthy
    # attempt at all (it did not run to completion, was interrupted, timed
    # out, errored, or exited nonzero).
    #
    # Both are recorded exactly the same way today (`reflect: {ok: false,
    # error, errorClass}`, never touching `state`/`verdict`/`reason` -- see
    # `_record_reflect_outcome` and `_finish`/`_promote_finishing_state`,
    # ledger guards 19/22): this task's decision is that a reflect failure
    # never fails the run, of EITHER class, full stop -- the FSM promotes
    # `self._pending_terminal`, settled by `_finish()` strictly before
    # `_run_reflection()` is even called, and nothing reflect does is
    # consulted by `_promote_finishing_state()`. `REFLECT_FAULT_FATAL` exists
    # so that if a *future* change ever wants to let some reflect failure
    # fail a run, it has to say so under this name -- never by quietly
    # reusing `REFLECT_FAULT_DEGRADED`, which every existing status.json/CLI/
    # hub consumer already reads as "the job is unaffected". No code path
    # produces `REFLECT_FAULT_FATAL` today in a way that changes the job's
    # outcome; tests/test_reflect_deadline_boundary.py pins the split (both
    # spellings occur, for different underlying result shapes, and neither
    # ever touches state/verdict) and the fact that the two are not spelled
    # identically.
    REFLECT_FAULT_DEGRADED = "degraded"
    REFLECT_FAULT_FATAL = "fatal"

    async def _reflect_contract_ladder(
        self, result: IterationResult | None
    ) -> IterationResult | None:
        """Task 017 (requirement Q, #96): give `_reflect_verdict`'s payload
        the same re-ask ladder every other contract site already has
        (`contract_enforcement.enforce_contract`) instead of leaving an
        absent/malformed `reflect` payload to degrade with no second
        chance. Called once from `_run_reflection`, strictly before
        `_record_reflect_outcome` ever reads `result`.

        Skips the ladder entirely (returns `result` unchanged) whenever
        `_reflect_failure` would never have reached `_reflect_verdict` in
        the first place: `result is None`, or
        `artifacts/reflection/report.md` does not exist. Neither of those
        is a payload-shape problem a re-ask could fix -- there is no
        attempt to correct -- so they still fall straight through to
        `_reflect_failure`'s existing FATAL/DEGRADED classification,
        unchanged by this task. Also skips it (returns `result` unchanged)
        when the payload is already valid: `enforce_contract`'s own retry
        loop is for recovering a BAD attempt, not for re-litigating a good
        one.

        REFLECT_TAIL_TIMEOUT_S interaction (states and bounds it, per the
        PRD): each re-ask goes over `_ask_contract_fix`, an UNCOUNTED call
        on its own short `cfg.router_timeout_s` (60s default) -- never the
        reflect subprocess's own `REFLECT_TAIL_TIMEOUT_S`-bounded `timeout`
        (loop.py's `run_iteration`/`_run_iteration_once`). The reflect
        subprocess has already exited by the time this method is reached,
        so a re-ask cannot extend ITS timeout at all. What it can extend is
        the wall clock between that exit and `_record_reflect_outcome`
        being called -- and that is bounded too: `enforce_contract` makes
        at most `cfg.contract_retry_max` (2 default) extra `_ask_contract_
        fix` calls before giving up, so the worst case this whole ladder
        can add is `contract_retry_max * router_timeout_s` (240s by
        default) -- a fixed quantity set by config, never proportional to
        anything unbounded, and never re-armed: `_run_reflection` calls
        this exactly once per process, matching its own "runs
        unconditionally... exactly once" guarantee.

        v0.12 requirement G is unmoved: this method only ever hands back
        the SAME `result` (unchanged) or a corrected copy of it
        (`dataclasses.replace(result, final_text=...)`) for
        `_reflect_failure`/`_reflect_verdict` to re-parse exactly as they
        always have. Neither of those, nor this method, ever touches
        `state`/`verdict`/`reason` -- `_promote_finishing_state()`/
        `_finish()` have already settled the job's own outcome by the time
        `_run_reflection` is even called (see `REFLECT_FAULT_FATAL`'s own
        comment). A ladder that never resolves returns `result` unchanged,
        so an unparseable reflect ends exactly where it always did:
        DEGRADED, not fatal, and the work's verdict untouched.
        """
        if result is None:
            return result
        report = self.run.artifacts_dir / "reflection" / self.REFLECT_REPORT
        if not report.exists():
            return result
        _payload, contract_errors = self._reflect_verdict(result)
        if not contract_errors:
            return result
        first_attempt_text = last_nonblank_line(result.final_text)
        event_loop = asyncio.get_running_loop()

        def respond(attempt: int, prior_errors: list[str] | None) -> str | None:
            if attempt == 0:
                return first_attempt_text
            raw, _usage = asyncio.run_coroutine_threadsafe(
                self._ask_contract_fix("reflect", prior_errors or [],
                                       final_text=result.final_text),
                event_loop).result()
            return last_nonblank_line(raw)

        enforcement = await asyncio.to_thread(
            contract_enforcement.enforce_contract,
            "reflect", respond,
            max_retries=self.cfg.contract_retry_max,
            state=self.run,
            extra_validate=transitions.reflect_report_errors,
        )
        if enforcement.ok:
            return dataclasses.replace(
                result, final_text=json.dumps(enforcement.payload))
        return result

    def _reflect_verdict(self, result: IterationResult) -> tuple[dict | None, list[str]]:
        """Task 121 (#60 pillar 3, fact 9): parse the reflect hat's final
        line as a `reflect` contract payload (`{"report_written": ...,
        "suggestions_written": ..., "notes": ...}`,
        `transitions.HAT_SCHEMAS["reflect"]`) -- `reflect.md` no longer ends
        its reply with the bare `<promise>REFLECTED</promise>` sentinel
        (never scored by the engine to begin with); `_reflect_failure()`
        below reads this payload ON TOP OF, never instead of, the existing
        `artifacts/reflection/report.md` file-presence check -- a validated
        claim is now required in addition to the file actually being there,
        mirroring `_review_verdict`'s split between schema validation
        (`contracts.validate_payload`) and the semantic sibling check
        (`transitions.reflect_report_errors`).
        """
        try:
            parsed = json.loads(last_nonblank_line(result.final_text))
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            payload = parsed
            contract_errors = (contracts.validate_payload("reflect", payload)
                                or transitions.reflect_report_errors(payload))
        else:
            payload = None
            contract_errors = [
                "response: no parseable 'reflect' contract payload "
                "(absent or malformed output)"]
        return payload, contract_errors

    def _reflect_failure(
        self, result: IterationResult | None
    ) -> tuple[str | None, str | None]:
        """The reflect iteration's failure text and fault class, or
        ``(None, None)`` if it succeeded.

        Task 019 (#5). Deliberately *not* classify_fault(): this is not about
        whose fault it was (the retry wrapper already spent its budget on
        that question by the time we get here) but about whether the operator
        ended up with a post-mortem. So a missing report counts as a failure
        even on a clean exit -- an agent that exits 0 having written nothing
        leaves the same empty artifacts/reflection/ as a broken endpoint.
        The one thing that is *not* reported as a reflect failure is the
        promise line: the report on disk is the deliverable.

        Task 121 adds a second, independent requirement layered on top of
        the file check above, never replacing it: the reflect hat's own
        `reflect` contract payload (`_reflect_verdict`) must also validate
        and must claim `report_written: true`. A report that exists on disk
        but comes with no valid contract (or with the contract itself
        claiming it was not written) is still a reflect failure -- the
        validated claim, not the file's mere existence, is now the thing
        the harness trusts, exactly the move task 116/118/119 already made
        for worker/verify/review.

        Task 014 (#87) adds the fault-class half: every branch below that
        fires because the ITERATION ITSELF never produced a trustworthy
        attempt (no result, interrupted, timed out, an in-band error, a
        nonzero exit) returns `REFLECT_FAULT_FATAL`; every branch that fires
        because the iteration ran cleanly but the DELIVERABLE is missing or
        invalid (no report file, an unparseable/invalid contract, or a
        contract that disclaims `report_written`) returns
        `REFLECT_FAULT_DEGRADED` -- the exact shape of #87's own incident.
        Neither class changes what `_record_reflect_outcome` does with the
        result: see `REFLECT_FAULT_FATAL`'s own comment for why.
        """
        if result is None:
            return ("the reflect iteration did not run to completion",
                    self.REFLECT_FAULT_FATAL)
        error = (result.error_message or "").strip()
        if result.interrupted:
            return (error or "the reflect iteration was interrupted",
                    self.REFLECT_FAULT_FATAL)
        if result.timed_out or result.no_traffic_timeout:
            return (error or "the reflect iteration timed out",
                    self.REFLECT_FAULT_FATAL)
        if error:
            return error, self.REFLECT_FAULT_FATAL
        if result.exit_code not in (0, None):
            return (f"the reflect agent exited {result.exit_code}",
                    self.REFLECT_FAULT_FATAL)
        report = self.run.artifacts_dir / "reflection" / self.REFLECT_REPORT
        if not report.exists():
            return (f"the reflect iteration wrote no artifacts/reflection/"
                    f"{self.REFLECT_REPORT}", self.REFLECT_FAULT_DEGRADED)
        payload, contract_errors = self._reflect_verdict(result)
        if contract_errors:
            return ("the reflect iteration's outcome contract was invalid: "
                    + "; ".join(contract_errors), self.REFLECT_FAULT_DEGRADED)
        if payload.get("report_written") is not True:
            return ("the reflect iteration's own contract claims "
                    f"report_written is not true even though "
                    f"{self.REFLECT_REPORT} exists on disk",
                    self.REFLECT_FAULT_DEGRADED)
        return None, None

    def _record_reflect_outcome(self, result: IterationResult | None) -> None:
        """Records the reflect phase's verdict in the run state (task 019, #5):
        `reflect: {ok, error, endedAt}` in status.json either way, plus
        artifacts/reflection/FAILED.md naming the error when it failed, so a
        failed post-mortem is visible on disk next to where the report would
        have been (and to `ralphctl status` / the hub, task 020).

        Never touches the terminal state, verdict or reason: reflect runs
        after the job is over and must not be able to rewrite how it ended.

        Task 017 (#43): a *successful* attempt also removes any tombstone an
        earlier one left behind (_clear_reflect_tombstone). Recording success
        in status.json while leaving FAILED.md on disk is how
        selfdev-v06-release ended up `succeeded / verified` with a file
        claiming `terminal state: aborted (verdict unverified)` beside a
        perfectly good report -- and with `reflect-failed` offered as an
        artifact alias next to it.

        Task 014 (#87): `error` alone no longer says everything -- `_reflect_
        failure` also returns a fault class (`REFLECT_FAULT_FATAL` /
        `REFLECT_FAULT_DEGRADED`), recorded as `reflect.errorClass` and named
        in FAILED.md, so a reader can tell #87's own shape (a clean reflect
        that left no valid deliverable -- DEGRADED) from the reflect process
        itself breaking (FATAL) without the two ever being spelled the same
        way. `errorClass` is omitted entirely on success, so the exact-match
        `reflect == {"ok": True, "error": None, "endedAt": ...}` shape every
        existing consumer already checks is unchanged.
        """
        error, fault_class = self._reflect_failure(result)
        reflect_fields = {"ok": error is None, "error": error,
                          "endedAt": utcnow()}
        if fault_class is not None:
            reflect_fields["errorClass"] = fault_class
        self.run.update_status(reflect=reflect_fields)
        if error is None:
            log.info("reflect iteration wrote artifacts/reflection/%s",
                     self.REFLECT_REPORT)
            self._clear_reflect_tombstone()
            self.run.emit("reflect_done", ok=True)
            return
        log.warning("reflect iteration failed (%s): %s", fault_class, error)
        outdir = self.run.artifacts_dir / "reflection"
        outdir.mkdir(parents=True, exist_ok=True)
        status = self.run.read_status()
        # Task 012 (#56): while the reflect window is open, status.json's
        # `state` reads the non-terminal `finishing`, not the job's real
        # outcome -- `pendingTerminalState` (or, if that is somehow absent,
        # `state` itself) is the value this tombstone means to report.
        reported_state = status.get("pendingTerminalState") or status.get("state")
        class_note = ("the reflect iteration itself never finished cleanly"
                      if fault_class == self.REFLECT_FAULT_FATAL else
                      "the reflect iteration ran cleanly but left no valid "
                      "deliverable -- issue #87's own shape")
        (outdir / self.REFLECT_TOMBSTONE).write_text(
            "# Reflection failed\n\n"
            f"The post-terminal `reflect` iteration did not produce a report.\n\n"
            f"- error: {error}\n"
            f"- class: {fault_class} ({class_note})\n"
            f"- when: {utcnow()}\n"
            f"- run: {self.cfg.run_id}\n"
            f"- terminal state: {reported_state} "
            f"(verdict {status.get('verdict')})\n\n"
            "The job's own terminal state above is unaffected by this failure,\n"
            "of either class: a reflect failure never changes state, verdict\n"
            "or reason -- see _record_reflect_outcome/_finish in loop.py.\n"
            "See the last `reflect` iteration under `iterations/` for the\n"
            "transcript, and `infra_wait`/`infra_retry` events for the retries\n"
            "that were already spent on it.\n")
        self.run.emit("reflect_done", ok=False, error=error, errorClass=fault_class)

    def _clear_reflect_tombstone(self) -> bool:
        """Removes a stale artifacts/reflection/FAILED.md, returning whether
        there was one (task 017, #43).

        Called on the ONE path where the tombstone's claim has just become
        false: an attempt that ran to completion and left a report. The
        tombstone is written per attempt but the run dir outlives the attempt,
        so a failed episode's file otherwise sits beside the next episode's
        report forever -- and `artifacts ls` derives its rows from the files on
        disk, so removing it is also what retires the `reflect-failed` alias
        (ARTIFACT_ALIASES, engine/state.py). No new alias state to keep in
        sync: the file IS the claim.

        Deliberately NOT done on the no-verdict path
        (_record_reflect_not_attempted, task 016): a signal that stopped this
        engine before it could reflect does not make an earlier attempt's
        failure untrue, and there is no report to contradict it. Only a
        success falsifies a tombstone.

        Best effort: reflect runs while the job is already terminal, so a
        read-only or vanished artifacts dir must not turn a successful
        post-mortem into a crash.
        """
        stale = self.run.artifacts_dir / "reflection" / self.REFLECT_TOMBSTONE
        try:
            stale.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            log.warning("could not remove stale artifacts/reflection/%s: %s",
                        self.REFLECT_TOMBSTONE, exc)
            return False
        log.info("removed the stale artifacts/reflection/%s tombstone left by "
                 "an earlier reflect attempt", self.REFLECT_TOMBSTONE)
        self.run.emit("reflect_tombstone_cleared",
                      path=f"reflection/{self.REFLECT_TOMBSTONE}")
        return True

    def _begin_reflect_retry_window(self) -> tuple[str | None, bool]:
        """Gives the post-terminal reflect iteration a real retry window
        (task 018, #5). Returns the state _end_reflect_retry_window() restores.

        `reflect` has been listed in INFRA_RETRY_PHASES since task 009, yet
        incident 2 in the v0.5 PRD still lost its post-mortem: two pieces of
        the job's *own* ending make the wrapper a no-op for the iteration
        that follows it.

        - The **episode clock**: a job that died of an infra outage arrives
          here with the episode's whole outage budget already spent, so the
          first reflect fault would immediately score "budget exhausted" and
          never be retried. The outage that killed the job is over as far as
          reflect is concerned -- it is a new, separately budgeted attempt
          (_outage_budget_for("reflect")), so the episode starts clean.
        - The **abort reason**: `operator_abort_requested` is true for *any*
          recorded abort reason, including the wrapper's own give-up, and
          classify_fault(operator_abort=True) never returns "infra" (task
          003's carve-out). So every reflect failure after an engine-side
          give-up would be scored "work" and handed straight back. An
          engine-recorded reason is therefore parked for the duration of
          reflect and restored afterwards, leaving the job's terminal
          `reason` exactly as _run_job_core() wrote it.

        An *operator*-initiated abort keeps its veto in full: if the operator
        stopped this run, reflect must not sit in backoff retrying against
        them -- one attempt, whatever it returns.
        """
        self._infra_episode_attempts = 0
        self._infra_episode_waited_s = 0.0
        self._infra_episode_started_at = None
        if self._operator_abort_recorded:
            return None, False
        parked, self._abort_reason = self._abort_reason, None
        return parked, True

    def _end_reflect_retry_window(self, window: tuple[str | None, bool]) -> None:
        """Restores the abort reason parked by _begin_reflect_retry_window()
        (task 018, #5), so a reflect iteration can never rewrite the reason
        the job terminated with -- including when the reflect attempts
        themselves ran out of their own (short) outage budget."""
        parked, restore = window
        if restore:
            self._abort_reason = parked

    async def _reflect_pre_attempt_wait(self) -> None:
        """Waits out one backoff step before the *first* reflect attempt when
        the job just ended on an infra-shaped failure (task 018, #5).

        PRD incident 2: four consecutive "Connection error." iterations
        failed the last approach and `_run_reflection` then launched into the
        same dead gateway in the same second, so a 105-iteration run produced
        no post-mortem at all. Retrying is not enough on its own -- the first
        attempt is the one most likely to hit the outage that is still in
        progress, and on a short reflect budget it can consume most of it.

        The wait is the same interruptible mechanism as any other infra wait
        (POST /retry cuts it short) and is published/accounted the same way,
        so an operator seeing the container still alive after the terminal
        state can read why in /status and in the event stream.

        An operator who aborted the run gets no delay at all: they asked for
        this to stop, and reflect's single attempt (see
        _begin_reflect_retry_window) should not be held up by a countdown."""
        if self._last_fault_class != "infra" or self._operator_abort_recorded:
            return
        budget = self._outage_budget_for("reflect")
        schedule = self.cfg.infra_retry_backoff_s or DEFAULT_INFRA_RETRY_BACKOFF_S
        delay = min(schedule[0], self.cfg.infra_retry_backoff_max_s, budget)
        if delay <= 0:
            return
        error = self._last_fault_error or "no LLM traffic within startup window"
        self.run.emit("reflect_infra_delay", phase="reflect", delayS=delay,
                      error=error, budgetS=budget)
        log.warning("the job ended on an infra fault (%s); waiting %.0fs before "
                    "the first reflect attempt", error, delay)
        # attempt 0: the delay happens *before* reflect's first attempt, so it
        # is not one of the episode's retries (which start numbering at 1).
        self._begin_infra_wait(phase="reflect", attempt=0, error=error,
                               waited_s=0.0, backoff_s=delay, budget_s=budget)
        elapsed, woken = await self._wait_out_backoff(delay)
        self._infra_episode_waited_s = 0.0 if woken else elapsed
        self._account_infra_wait(elapsed, "reflect", 0)
        self._end_infra_wait()

    def _previous_endings(self) -> list[dict]:
        """The endings of this run's EARLIER episodes (task 022, #30), read
        off status.json just before this episode's `running` write re-bases
        the ending fields.

        An "episode" is one engine process over one run dir -- a fresh start
        or a resume. `endedAt` and `reason` describe how an episode *ended*,
        so they must never be readable as this episode's own: on
        `selfdev-v06-release` they were not reset, and the run finished
        `succeeded / verified` while `ralphctl status` still printed
        `reason: signal 15` from a `pkill` two episodes earlier -- a corrupted
        permanent record, not a cosmetic glitch. Mid-run the same bug showed
        an `ended` timestamp *before* `started` (`startedAt` is rewritten
        every episode; `endedAt` was not). The reset therefore belongs in the
        one write that opens the episode, beside the other fields it already
        re-bases (`verdict`, `health`, `infraWait`) -- and because that write
        happens once per episode, a terminal run's `endedAt`/`reason` are
        still exactly what its FINAL episode wrote.

        Decision -- previous endings are KEPT as history, not discarded,
        because the value being superseded is evidence and nothing else in the
        run dir holds it: the terminal `state` event names the state but
        carries no `reason` and no verdict, so plain deletion would make "this
        run was killed two episodes ago, here is why" unrecoverable. The list
        is bounded (PREVIOUS_ENDINGS_MAX) and each entry is small.

        The entry deliberately does not include the previous `state`: by the
        time the loop opens an episode, `engine/main.py`'s first write has
        already moved `state` to `starting`, so this reader cannot see it, and
        the terminal `state` *event* in events.jsonl is its authoritative
        record anyway. What the event does not carry -- reason and verdict --
        is exactly what is kept here.

        `termination` is deliberately NOT reset: it records *who* stopped the
        run and is what doctor/auto-resume reason about (§8.4), a later abort
        overwrites it wholesale, and clearing it here would erase the only
        status-side trace of an operator's intent.
        """
        status = self.run.read_status()
        endings = [e for e in (status.get("previousEndings") or [])
                   if isinstance(e, dict)]
        # A crashed episode left no ending at all (this same write cleared the
        # fields when it started), so nothing is appended and no ending can be
        # recorded twice.
        if status.get("endedAt") or status.get("reason"):
            endings.append({"endedAt": status.get("endedAt"),
                            "reason": status.get("reason"),
                            "verdict": status.get("verdict")})
        return endings[-PREVIOUS_ENDINGS_MAX:]

    async def _run_job_core(self) -> str:
        """Returns the settled final state: succeeded | failed | aborted.

        Task 012 (#56): with `reflect: true` this is the value `_finish()`
        decided, not necessarily what `state` reads on status.json the
        instant this coroutine returns -- see `_finish()`/run_job() for the
        `finishing` window that covers the reflect iteration still ahead.

        Task 014 (requirement B, #60 pillar 2): this is now a plain
        dispatch loop over `engine.transitions`' declared table -- the
        `planning` / `worker` / `review` / `replan` branch-and-while
        sequencing that used to live here directly has been replaced, not
        wrapped, by the `_step_*` handlers below. Each handler runs exactly
        the guard computation the ledger cites for its state and returns
        which declared edge fired; `self._advance()` is the ONLY place a
        state actually changes, and it refuses (`transitions.IllegalTransition`)
        anything the table does not list for `self.cfg.vigilant`'s variant.
        `verify` is walked the same way, nested inside `_step_worker` (it
        always returns straight to `worker`, per the table, before that
        method's own state-changing return). The two exceptional escapes
        that predate the table -- budget exhaustion (with its grace-review
        attempt) and the instant-failure fail-fast abort -- are not
        table-declared edges (the PRD's minimum transition list does not
        name them) and remain short-circuit returns straight to the tail,
        exactly as before.
        """
        # Task 022 (#30): read the ending fields before overwriting them.
        previous_endings = self._previous_endings()
        self.run.update_status(state="running", startedAt=utcnow(),
                               deadlineAt=utc_from_epoch(self._deadline_epoch),
                               infraWaitTotalS=round(self._infra_wait_total_s, 3),
                               pausedTotalS=round(self._paused_wait_total_s, 3),
                               health="ok", infraWait=None,
                               # Task 036 (requirement D.1/D.2, #142): a fresh
                               # episode is not parked on anything -- an
                               # engine-recorded pause belongs to the process
                               # that was holding `self._pause`, and leaving
                               # its reason behind beside `state: "running"`
                               # would strand a stale ruling-wanted marker on
                               # a run that is working again. (The pause is
                               # not lost: `_resume_point`'s
                               # `_paused_awaiting_last_approach_ruling` case
                               # still routes this episode back into review
                               # over the same approach.)
                               pauseReason=None,
                               iterationsBudget=self.cfg.iterations,
                               maxApproaches=self.cfg.max_approaches,
                               onComplete=self.cfg.on_complete, verdict=None,
                               # Task 022 (#30): the ending fields are
                               # episode-scoped -- see _previous_endings.
                               endedAt=None, reason=None,
                               previousEndings=previous_endings)
        # Task 032 (#13): the move to `running` is a *state* event, not just a
        # status.json field. events.jsonl is append-only across resumes and
        # followers replay it from id 0, so without this a resumed run's log
        # would still end on the *previous* episode's terminal `state` event
        # -- and every consumer that reconciles against the log (`ralphctl
        # watch`/`logs -f`, task 031) would have to infer the restart from
        # unrelated event types. Emitted unconditionally, not only on resume:
        # one code path is easier to trust than two, a non-terminal state
        # event never ends anyone's stream, and a fresh run's log now opens
        # with its own lifecycle transition. `resumed` says which case it is.
        self.run.emit("state", state="running",
                      resumed=self._resuming_existing_run_dir())
        # Task 005 (requirement E, #99): apply any operator status ruling
        # recorded on the read-only amendments mount BEFORE this episode
        # decides anything -- ahead of `_resume_point()` and therefore ahead
        # of the scheduler's first pick, so a ruling that skips a doomed
        # task or hands one back to the worker is already on tasks.json by
        # the time either reads it. This is the "applied by the engine at
        # load time" half of requirement E; `state.apply_amendment_status_
        # rulings` is the ONLY applier (the steer hat's status tool calls the
        # same function, never a second copy of these rules).
        self._apply_operator_status_rulings()
        start_approach, skip_planning, step = self._resume_point()
        self._approach = start_approach
        try:
            while True:
                if step == "planning":
                    outcome = await self._step_planning(skip_planning)
                    skip_planning = False
                elif step == "replan":
                    outcome = self._step_replan()
                elif step == "worker":
                    outcome = await self._step_worker()
                elif step == "review":
                    outcome = await self._step_review()
                elif step == "cure":
                    # Task 004 (PRD requirement C.1, finding 4): a
                    # dedicated handler, not the generic
                    # `_step_generic_hat` fallback below -- `cure` has no
                    # hat of its own (`transitions.STATE_HAT["cure"]` is
                    # `None`, exactly like `replan`/`finishing`) and takes
                    # its one declared edge unconditionally.
                    outcome = await self._step_cure()
                elif step == "paused":
                    # Task 004 (PRD requirement D.1, finding 4): likewise
                    # dedicated, not generic -- `paused` has no hat either
                    # and its own edge is gated on the existing pause
                    # machinery (`self._pause`), not on any hat reply.
                    outcome = await self._step_paused()
                elif step == "plan_review":
                    # Task 018 (requirement K, #101): a dedicated handler,
                    # NOT the generic `_step_generic_hat` fallback below --
                    # this hat picks its own outgoing edge deterministically
                    # from its own `verdict` field whenever that verdict IS
                    # decidable, exactly like `_step_worker`/`_step_review`
                    # already do for theirs. Task 019 (requirement M) is
                    # what makes this the FIRST state to reach
                    # `router.decide()` in PRODUCTION: `_step_plan_review`
                    # itself calls the shared router call site
                    # (`_route_with_router`) for the one case its own
                    # verdict cannot decide -- an unusable peer reply with
                    # revisions still available -- rather than inventing a
                    # verdict the peer never gave.
                    outcome = await self._step_plan_review()
                elif step == "verdict_gate":
                    # Task 020 (requirement L, #101): a dedicated handler,
                    # NOT the generic `_step_generic_hat` fallback below --
                    # that fallback reaches `engine.router` for ANY state
                    # with more than one legal edge (unconditionally), but
                    # this hat's own `verdict` field always decides its
                    # edge deterministically (verified/rejected, both
                    # always decidable from the payload alone) exactly
                    # like `_step_review`/`_step_plan_review` decide theirs
                    # -- there is no undecidable case here that would need
                    # the router at all.
                    outcome = await self._step_verdict_gate()
                else:
                    # Task 015 (requirement B): any OTHER state the table
                    # names is driven by the one generic fallback below --
                    # this is what lets a genuinely new hat run without a
                    # further edit here. `transitions.successors` returning
                    # empty for a state nobody declared is still an engine
                    # bug (the executor already refuses any target outside
                    # the declared table, so a step handler proposing one
                    # cannot reach this line), hence the assertion rather
                    # than a silent no-op.
                    edges = transitions.successors(step, self.cfg.vigilant)
                    if not edges:  # pragma: no cover - defensive only
                        raise AssertionError(f"no step handler for state {step!r}")
                    outcome = await self._step_generic_hat(step, edges)
                kind = outcome[0]
                if kind == "done":
                    return outcome[1]
                if kind == "tail":
                    break
                _, target, guard = outcome
                step = self._advance(step, target, guard)

            return self._finish_tail_unverified()
        except Exception as exc:  # engine bug — record, don't vanish
            # Deliberately still `Exception`, not BaseException: the
            # per-iteration containment boundary in _run_iteration_once
            # (task 011, #28) is where a stray cancellation is absorbed, so
            # anything reaching here that is NOT an Exception is a real
            # shutdown request (KeyboardInterrupt/SystemExit, or a cancel()
            # on this coroutine) and must unwind the engine rather than be
            # rewritten into a terminal "engine error" verdict. A refused
            # transition (transitions.IllegalTransition) is deliberately
            # caught here too: an engine bug that proposes an edge the
            # table does not list is exactly the class of error this
            # boundary exists for.
            self.run.emit("log", level="error", message=f"engine error: {exc!r}")
            self._finish("failed", "unverified",
                        reason=f"engine error: {exc}",
                        **self._unconsumed_steering_patch())
            return "failed"

    def _advance(self, state: str, target: str, guard: str) -> str:
        """The one place `_run_job_core`'s dispatch loop moves its FSM
        position between the step states named in `engine.transitions`.
        Delegates entirely to `self._executor` (a
        `transitions.TransitionExecutor`, constructed once in `__init__`
        from `self.cfg.vigilant`) -- this method holds no transition logic
        of its own, so there is exactly one place an illegal edge can be
        refused, and it is not here.
        """
        return self._executor.advance(state, target, guard)

    async def _run_hat_iteration(self, state: str, hat: str):
        """Task 033 (operator-inserted scope, #114): run one generic hat's
        iteration, with the steer hat's TOOLS LIVE for the duration of its
        own iteration and for no other hat's.

        `state.STEER_HAT_TOOLS` declared the steer hat's authority boundary
        (task 010, requirement H, #98) as library functions nothing handed to
        the hat, so the hat could only ever describe the task-table edits an
        operator message required and ask the operator to apply them
        host-side (this run did that three times, e.g.
        `artifacts/steer-001-proposed-task-edits.json`). Here the same
        callables become callable TOOLS: `hat_tools.SteerToolBroker.place()`
        writes the generated `pi` extension the steer iteration alone is
        launched with (`_hat_extensions`, below), and `broker.serve()` runs
        as one asyncio task IN THIS PROCESS -- the uid that owns
        `tasks.json` under the #60 privsep boundary -- for exactly as long
        as the hat's own subprocess is alive. The hat's process writes a
        request and reads an answer; the engine performs every write, so
        every applier guarantee (`apply_task_status_change` for a status
        move, `origin=steer`/`status=pending` on an insert, a delete refused
        while another task's `dependsOn` names the target) is kept by
        construction rather than re-implemented on the hat's side.

        A final `serve_once()` after the iteration returns drains a request
        the hat spooled in its last moments, so a call made just before the
        subprocess exited is answered and applied rather than silently
        dropped -- and, because it runs strictly after `run_iteration()`
        returned, no tool write ever lands WHILE the hat executes past that
        point either.
        """
        if state != "steer":
            return await self.run_iteration(state, prompt_name=hat,
                                            tier=tiers.DEFAULT_TIER)
        broker = hat_tools.SteerToolBroker(
            self.run, vigilant=bool(self.cfg.vigilant),
            steering=tuple(p.name for p in self.run.pending_steering()),
            notifier_url=self.cfg.notifier_url)
        broker.place()
        self._steer_tool_broker = broker
        serving = asyncio.create_task(broker.serve())
        try:
            return await self.run_iteration(state, prompt_name=hat,
                                            tier=tiers.DEFAULT_TIER)
        finally:
            serving.cancel()
            try:
                await serving
            except asyncio.CancelledError:
                pass
            broker.serve_once()
            self._steer_tool_broker = None

    def _hat_extensions(self, phase: str) -> list[str]:
        """Task 033: the `pi` extensions THIS phase's subprocess is launched
        with -- the steer hat's generated tool extension while a broker is
        serving it, and nothing at all for every other phase but `worker`
        (task 022, PRD requirement B.3: the worker's own, much narrower
        `notify`-only extension while ITS broker is serving). Scoped to the
        phase rather than placed in the agent's global `~/.pi/agent/
        extensions/` on purpose: the tool surface is the authority boundary
        (PRD requirement H), so a verify/review/reflect/planning iteration
        must never find any of these tools registered, and worker never
        finds the steer hat's task-table/standing-rules/context tools.
        """
        broker = getattr(self, "_steer_tool_broker", None)
        if phase == "steer" and broker is not None:
            return [str(broker.extension_file)]
        worker_broker = getattr(self, "_worker_tool_broker", None)
        if phase == "worker" and worker_broker is not None:
            return [str(worker_broker.extension_file)]
        return []

    async def _run_worker_iteration(self, extra: str, tier: str | None):
        """Task 022 (PRD requirement B.3, #140): wire the `notify` tool
        live for exactly the duration of one worker iteration, the same
        seam `_run_hat_iteration` already uses for the steer hat -- a
        `hat_tools.SteerToolBroker` restricted to `hat_tools.
        WORKER_WIRED_TOOLS` (just `notify`; never task-insert/update/
        delete/standing-rules, which stay the steer hat's alone) served by
        an asyncio task alive in this process for exactly as long as the
        worker's own subprocess is, plus one final `serve_once()` drain
        after it returns so a call made in the iteration's last moments is
        still answered.
        """
        broker = hat_tools.SteerToolBroker(
            self.run, vigilant=bool(self.cfg.vigilant),
            tools=frozenset(hat_tools.WORKER_WIRED_TOOLS),
            notifier_url=self.cfg.notifier_url, source="worker")
        broker.place()
        self._worker_tool_broker = broker
        serving = asyncio.create_task(broker.serve())
        try:
            return await self.run_iteration("worker", extra=extra, tier=tier)
        finally:
            serving.cancel()
            try:
                await serving
            except asyncio.CancelledError:
                pass
            broker.serve_once()
            self._worker_tool_broker = None

    async def _step_generic_hat(self, state: str, edges: tuple) -> tuple:
        """Task 015 (requirement B, #60 pillar 2): the ONE hardcoded
        fallback for any state `engine.transitions` declares that is not
        one of the four hand-written `_step_*` methods above. This is what
        makes the PRD's promise literal -- "adding a hypothetical hat is a
        table entry plus a prompt plus a schema, with no edit to the
        transition executor" -- and, this method is the proof, no edit to
        THIS dispatcher either: once it exists, a new state that shows up
        in `transitions.TABLE`/`VIGILANT_DELTA` with `transitions.STATE_HAT`
        and `transitions.HAT_SCHEMAS` entries beside it runs end to end
        without another line changing here or in `engine.transitions`.

        A state with exactly one legal edge is resolved exactly as task 015
        shipped it: an unconditional transition taken the instant the hat's
        own payload validates, no model consulted at all. Task 018
        (requirement E, #60 pillar 2) is what widens this to a state with
        MORE than one legal edge -- "a hat whose transition depends on WHAT
        the hat reported" is no longer a case this method refuses; it is
        the one case `engine.router` exists for. The state's own
        `transitions.ROUTER_FALLBACK` entry MUST be declared before that
        happens (an ambiguous table entry without one is a programming
        error caught by `router.decide()` itself, not a runtime hazard).
        Deterministic-first still holds even here: the router is reached
        ONLY on this branch, for a state with a genuine choice -- never for
        any of the four hand-written `_step_*` methods, which compute their
        own guard from disk state and never import this module's router
        call at all.

        The hat's response is validated against its declared schema via
        `contracts.validate_payload` (task 009's schema, the *only*
        validator code path, fact 16/#65), reached through
        `contract_enforcement.enforce_contract`'s full N-then-agent-fault
        re-prompt ladder (v0.13 task 003, #92) rather than a single pass-
        or-abort check: a schema violation is carried back to the SAME hat,
        verbatim, up to `contract_retry_max` extra times before the ladder
        is exhausted -- and exhaustion is an agent-fault iteration that
        retries this state in place (never `self._abort_reason`, never
        `("tail",)` for that reason alone), not a silent edge taken anyway.
        The router's OWN re-prompt loop, below, is unrelated: it re-prompts
        the ROUTER, not the hat, about which edge to take, and every one of
        ITS attempts is a plain `_ask_router` call -- never a work
        iteration.)

        Task 004 (requirement A, #72): the hat's own iteration can be cut
        short by the ENGINE (`_iteration_cut_short_by_engine` -- SIGINT,
        the iteration timeout, the startup watchdog, or an abnormal exit)
        exactly like review's before task 003's fix, and with no per-state
        `*_interrupted` edge declared in the table for a hypothetical hat
        (that would defeat the whole "no edit to the transition executor"
        promise this method exists to keep) the ONLY generic-safe response
        is to retry the SAME hat call in place -- never advance, never
        touch `self._advance()` at all, same as `worker`'s undeclared
        `no_verdict_yet` self-loop -- while budget/wall clock remain, and,
        once they do not, end the run via the ordinary tail WITHOUT ever
        setting `self._abort_reason`: an engine-side cutoff must not read
        as "the hat violated its contract" (which is what happened before
        this fix -- an absent/malformed payload from an interrupted
        attempt fell straight into the `errors` branch below and aborted
        the run over a contract violation nobody committed).
        """
        hat = transitions.STATE_HAT.get(state)
        if not hat:
            raise AssertionError(
                f"state {state!r} has no hat declared in transitions.STATE_HAT")
        approach = self._approach
        await self._gate()
        self.run.emit("phase", phase=state, approach=approach)
        while True:
            # Task 016 (requirement I): a generic hat (planning/worker/
            # verify/review/reflect all have their own dedicated call
            # sites; a hypothetical future hat lands here) is never
            # task-scoped by construction -- its ladder's designated tier
            # is `tiers.DEFAULT_TIER`.
            #
            # Task 034: which steering files THIS steer pass was handed,
            # captured BEFORE the call -- `_run_iteration_once` marks them
            # consumed once the iteration finishes cleanly, so by the time
            # the hat's settlement payload is read below
            # `pending_steering()` no longer names them. This list is what
            # the settlement claim is recorded against in the ledger.
            delivered_steering = ([p.name for p in self.run.pending_steering()]
                                  if state == "steer" else [])
            result = await self._run_hat_iteration(state, hat)
            if state == "steer":
                # Task 014 (requirement J, #98): refund THIS attempt the
                # instant it returns -- every pass through this while loop
                # (an engine-cut-short retry, a ladder-exhausted-but-
                # budget-remains retry, or the eventual successful pass)
                # gets its own real `iterations/NNNN` slot (full
                # observability, exactly like any other hat) but NEVER
                # counts against `cfg.iterations`: "an operator message
                # must not cost the run a work iteration." Refunding here,
                # not inside `run_iteration`/`_run_iteration_once`, keeps
                # the charging decision a property of WHICH hat this is
                # (`state == "steer"`), the same place task 013's
                # `settled` handling already special-cases this hat, rather
                # than a second copy of "which phases are uncharged" logic.
                self._refund_iteration("steer")
                # `_run_iteration_once`'s own status write above happened
                # BEFORE this refund (the refund is only known once the
                # call returns), so it published `iterationsUsed` computed
                # from the PRE-refund count -- one refund stale. Re-publish
                # it now so a caller reading status.json immediately after
                # this steer pass never sees a figure that still counts it,
                # even if no further iteration follows to correct it.
                self.run.update_status(iterationsUsed=self.iterations_used_charged)
            if self._iteration_cut_short_by_engine(result):
                if not self.budget_left():
                    self._terminal_reason_note = (
                        f"{state!r} ({hat!r} hat) was repeatedly cut short by "
                        "the engine with no budget or wall clock left to ask "
                        "again; no contract payload was ever judged, so this "
                        "is not read as a contract violation (requirement A, "
                        "#72)")
                    return ("tail",)
                await self._gate()
                continue

            # Task 003 (requirement A/C, #92): the hat's own schema pass is
            # now enforced through `contract_enforcement.enforce_contract`'s
            # re-prompt ladder instead of a single pass-or-abort check.
            # Attempt 0 reuses THIS already-completed work iteration's own
            # final line -- no extra hat call, no double-charge; every
            # further attempt is an uncharged `_ask_contract_fix` re-ask
            # that states the previous attempt's specific errors verbatim
            # and asks for nothing but the corrected payload, never the
            # hat's task prompt again. `enforce_contract`'s own retry loop
            # is synchronous, so it runs off the event loop via
            # `asyncio.to_thread`, exactly as `_step_planning` already
            # bridges `enforce_plan_ingestion`'s identical shape
            # (`asyncio.to_thread` + `run_coroutine_threadsafe`, this
            # file's own precedent, not reinvented here).
            first_attempt_text = last_nonblank_line(result.final_text)
            event_loop = asyncio.get_running_loop()

            def respond(attempt: int, prior_errors: list[str] | None) -> str | None:
                if attempt == 0:
                    return first_attempt_text
                raw, _usage = asyncio.run_coroutine_threadsafe(
                    self._ask_contract_fix(hat, prior_errors or [],
                                   final_text=result.final_text),
                    event_loop).result()
                return last_nonblank_line(raw)

            def task_status_extra_validate(payload: dict) -> list[str]:
                # Task 004 (requirement F, #92 site 2): a hat's reported
                # `task_status` for a specific `task_id` is layered onto
                # `enforce_contract`'s SAME retry ladder as a SEMANTIC
                # check, exactly the shape `contract_enforcement.
                # parse_response`'s `extra_validate` hook exists for (fact
                # 16/#65 stays satisfied -- `contracts.validate_payload`
                # is still the only schema validator; this is a second
                # REASON the same loop can refuse an attempt, never a
                # second retry loop). A payload that proposes no move at
                # all (no `task_id`, or no `task_status` key present) is
                # never in violation here -- the guard below is the same
                # one the pre-task-004 call site checked before ever
                # calling `_apply_reported_task_status`.
                if payload.get("task_id") and "task_status" in payload:
                    return self._apply_reported_task_status(payload)
                return []

            enforcement = await asyncio.to_thread(
                contract_enforcement.enforce_contract,
                hat, respond,
                max_retries=self.cfg.contract_retry_max,
                state=self.run,
                extra_validate=task_status_extra_validate,
            )
            if enforcement.ok:
                payload = enforcement.payload
                break
            # Task 003 (requirement C): an exhausted ladder is an
            # agent-fault iteration, never SOLELY a contract-violation
            # abort -- requirement C's own invariant is qualified: "a
            # contract violation may never be the SOLE reason a run ends
            # WHILE AN APPROACH AND ITERATION BUDGET REMAIN." While budget
            # remains, `_abort_reason` stays unset and this retries the
            # SAME hat call in place, exactly as it already retries for an
            # engine-side cutoff just above (no declared self-loop edge to
            # take for a generic-dispatcher state, per task 001's cutover
            # map -- the schema/semantic check happens before either the
            # single-edge advance or the router is ever reached, so no
            # edge's guard can be computed and the router has nothing to
            # ask about yet).
            #
            # Once budget is ALSO gone at the moment the ladder gives up,
            # ending here is no longer solely because of the violation --
            # the run was ending regardless -- so the ending is allowed to
            # name what the last attempt actually was (an unrepaired
            # contract violation) rather than a generic budget-exhaustion
            # note that would hide it. `tests/test_interrupt_not_
            # substantive_outcome_task004.py`'s test 3 pins exactly this
            # boundary, distinct from test 2's engine-side-cutoff case
            # (requirement D, unconditional: an engine-side cutoff is
            # NEVER read as a contract violation, budget or not).
            if not self.budget_left():
                # v0.13 task 012 (#92): the ONE shared derivation --
                # `state.contract_fault_for_iteration` builds the very same
                # text from `EVENT_VIOLATION`/`EVENT_AGENT_FAULT` events for
                # `ralphctl iteration N`, so this run's own terminal
                # `reason:` line and a per-iteration reconstruction can never
                # spell the same fact two different ways.
                message = contract_enforcement.violation_message(
                    hat, enforcement.errors)
                self.run.emit("log", level="error", message=message)
                self._abort_reason = message
                return ("tail",)
            await self._gate()
        # Task 013 (requirement I, #98): the steer hat's own `settled` claim
        # (`transitions.HAT_SCHEMAS["steer"]`'s required boolean) is now
        # READ, not merely schema-checked and discarded -- recorded as a
        # harness-observable fact SEPARATE from `done` (the ordinary
        # lifecycle's later verdict on the tasks this iteration touched or
        # created, which lives on those tasks' own `status` field plus the
        # `origin` marker task 011 stamps -- `state.is_steering_origin` /
        # `state.task_is_terminal`). `settled` says only "the hat processed
        # this iteration's pending messages and applied its changes"; it
        # says nothing about whether the resulting tasks will pass -- the
        # exit gate (`_steering_origin_outstanding`, called from
        # `_step_review`/`_maybe_grace_review`) is what waits on THAT, never
        # on this claim. Recorded twice, deliberately: an event (the
        # append-only audit trail) and a status.json field (the latest
        # snapshot, same pattern as `currentIteration` above) -- so both
        # "when did every settle claim happen" and "what is the most recent
        # one" are readable without replaying events.jsonl.
        if state == "steer":
            settled_claim = bool(payload.get("settled"))
            self.run.emit("steer.settled", settled=settled_claim,
                          notes=payload.get("notes"))
            self.run.update_status(steerSettled={
                "settled": settled_claim,
                "at": utcnow(),
                "notes": payload.get("notes"),
            })
            # Task 034: the same claim, recorded PER STEERING FILE in the
            # run-scope steering ledger, so every following iteration of
            # every hat can see which message it applies to -- the
            # `steerSettled` status field above is only ever the most
            # recent claim, with no way to tell what it was about.
            if delivered_steering:
                record_steering_settlement(
                    self.run.root, delivered_steering,
                    settled=settled_claim,
                    notes=payload.get("notes"),
                    application=payload.get("application"))
                # Task 006 (requirement B.1, #122): `_run_iteration_once`
                # already banked `delivered_steering` as consumed the
                # instant this iteration finished cleanly -- it has no way
                # to know, at that point, what the hat's own payload will
                # say. An honest `settled: false` here must not leave the
                # message silently applied anyway, so it is UNDONE now,
                # bounded by a per-message redelivery counter.
                if not settled_claim:
                    self._handle_unsettled_steering(delivered_steering)
        # Task 019 (requirement F, #60 pillar 3), cut over by v0.13 task 004
        # (#92 site 2): a hat's reported `task_status` for a specific
        # `task_id` is a proposed task-state CHANGE, applied by
        # `task_status_extra_validate` above -- inside `enforce_contract`'s
        # SAME retry ladder, as the `extra_validate` hook `parse_response`
        # runs once a payload is already schema-valid. By the time
        # `enforcement.ok` is True here, any legal move it proposed has
        # already been written to disk (`_apply_reported_task_status`'s own
        # write, task 013's sole writer) -- there is nothing left to apply
        # a second time. This still happens strictly AFTER `run_iteration()`
        # above returned -- the hat's own subprocess has already exited by
        # the time any status lands on disk, so there is no window in which
        # the record changes WHILE the hat executes (task 019's second
        # acceptance test drives exactly this and samples the record
        # throughout) -- and an illegal move is refused through the exact
        # same ladder-then-agent-fault path a schema violation takes (task
        # 003's budget-boundary rule just above), never a second retry loop.
        if len(edges) == 1:
            edge = edges[0]
            self._advance(state, edge.target, edge.guard)
            return ("advance", edge.target, edge.guard)
        # Task 018: a genuine choice -- the router's one constrained
        # question. Task 019 (requirement M) moved the call itself into
        # `_route_with_router` below, shared with `_step_plan_review`'s
        # own call site of the time (`plan_review` was the first
        # *declared* router-eligible state, a table entry with a genuine
        # multi-edge choice on a real reply). Task 016 (requirement D.2,
        # #117 half B) later replaced `_step_plan_review`'s call with a
        # deterministic policy -- `_route_with_router` itself, and this
        # generic dispatcher's use of it, are untouched.
        target, guard, _outcome = await self._route_with_router(state, edges)
        self._advance(state, target, guard)
        return ("advance", target, guard)

    def _handle_unsettled_steering(self, names: list[str]) -> None:
        """Task 006 (requirement B.1, #122): a clean steer iteration whose
        payload honestly reports `settled: false` must not leave the
        message it names silently consumed anyway.

        `_run_iteration_once` already banked `names` as applied the moment
        this iteration finished cleanly (`fault_class is None` is the ONLY
        thing it checks -- it runs strictly before the payload is even
        parsed, so it cannot know what `settled` will say). This method is
        the one place that reverses that write once the payload IS known,
        bounded by a per-message redelivery counter
        (`steerRedeliveries[<name>]` in status.json, disk-backed and
        survives a resume, the same shape `planReviewRevisions` already
        uses for `peer_plan_review_max_revisions`'s own bounded loop):

          * below `self.cfg.steer_redelivery_max` redeliveries so far for a
            given name -- unconsume it (`RunDir.unconsume_steering`), so
            `pending_steering()` names it again and the next actionable
            iteration (steer takes ABSOLUTE priority over worker, task 012)
            is handed it again;
          * at the cap already -- stop redelivering it. It is instead
            recorded settled-by-exhaustion in the steering ledger, with the
            reason, through `RunDir.mark_steering_settled_by_exhaustion`,
            and stays consumed for good: fail-closed on a bounded loop,
            never redelivered forever.

        `settled: true` never reaches this method at all -- the ordinary
        consumption inside `_run_iteration_once` is untouched and still
        applies exactly once, as before this task.
        """
        redeliveries = dict(
            self.run.read_status().get("steerRedeliveries") or {})
        for name in names:
            used = int(redeliveries.get(name, 0))
            if used < self.cfg.steer_redelivery_max:
                redeliveries[name] = used + 1
                self.run.unconsume_steering([name])
            else:
                reason = (
                    f"{name!r} was redelivered {used} time(s) after a "
                    "clean steer iteration without ever claiming "
                    "settled: true, reaching the declared cap "
                    f"(steer_redelivery_max={self.cfg.steer_redelivery_max}) "
                    "-- marked settled-by-exhaustion rather than "
                    "redelivered forever")
                self.run.mark_steering_settled_by_exhaustion(
                    [name], reason=reason)
                self.run.emit("log", level="warning", message=reason)
        self.run.update_status(steerRedeliveries=redeliveries)

    async def _route_with_router(
            self, state: str, edges: tuple, *,
            peer_plan_review: bool = False,
            guard_for_target=None) -> tuple:
        """The ONE place `engine.router.decide()` is called in production
        (task 018, requirement E; widened to a real state by task 019,
        requirement M). Returns `(target, guard, outcome)`; the caller does
        its own `_advance`, because what a state does BESIDE taking the
        edge (spending a plan-review revision, say) is the caller's
        business, not this method's.

        The answer is validated against both the table's legal transitions
        and the router's own schema (`engine.router.ROUTER_SCHEMA`),
        re-prompted on an invalid answer, and falls back to the state's OWN
        declared `transitions.ROUTER_FALLBACK` guard (never an illegal
        transition) after `contract_retry_max` extra attempts or a hung
        call.

        `peer_plan_review` selects the caller's table variant on the peer
        gate's axis, so `plan_review`'s real edges are resolved by
        `transitions.successors` itself rather than being passed in as a
        hand-written list.

        `guard_for_target` exists because the router answers with a TARGET
        STATE, and a state may declare two edges to the SAME target under
        different guards (`plan_review` -> `worker` was both
        `plan_review_approved` and `plan_review_revision_bound_reached`
        while `_step_plan_review` still called through here -- task 016,
        requirement D.2, #117 half B, replaced that call site with a
        deterministic policy, so no current production caller passes
        `guard_for_target`, but the parameter stays for the next state
        that needs the same disambiguation). Where it is used, only the
        caller knows which guard truthfully describes the move -- the
        recorded decision carries the caller-resolved guard, so
        `artifacts/router/decisions.jsonl` can never disagree with the edge
        the run actually took.
        """
        router_calls_before = self._router_calls
        outcome = await router.decide(
            state, self.cfg.vigilant, ask=self._ask_router,
            fallback_guard=transitions.fallback_guard(state),
            retry_max=self.cfg.contract_retry_max,
            timeout_s=self.cfg.router_timeout_s,
            peer_plan_review=peer_plan_review)
        guard = outcome.guard
        if guard_for_target is not None:
            guard = guard_for_target(outcome.target)
        self.run.emit(
            "log", level=("warning" if outcome.used_fallback else "info"),
            message=(f"router selected {state!r} -> {outcome.target!r} "
                     f"(guard={guard!r}, attempts={outcome.attempts}"
                     + (", FELL BACK to the declared deterministic choice "
                        "after exhausting the router's retry budget"
                        if outcome.used_fallback else "") + ")"))
        # Task 022 (P, #80): one line in `artifacts/router/decisions.jsonl`
        # per COMPLETED decision (never per `_ask_router` attempt), so a
        # surface can list router calls without hunting through raw
        # transcripts -- `state.router_call_entries` is the one reader.
        # Never charges an iteration or an approach: this is a plain
        # `RunDir` write, the same one `self.run.emit("log", ...)` just
        # above already made, not a hat call through `run_iteration`.
        self.run.record_router_decision({
            "state": state,
            "vigilant": self.cfg.vigilant,
            "candidateEdges": [{"target": e.target, "guard": e.guard}
                               for e in edges],
            "chosenTarget": outcome.target,
            "chosenGuard": guard,
            "usedRouter": outcome.used_router,
            "usedFallback": outcome.used_fallback,
            "fallbackGuard": transitions.fallback_guard(state),
            "attempts": outcome.attempts,
            "model": self.cfg.model_for("router"),
            "usage": outcome.usage or {},
            "transcripts": [f"{n:04d}.jsonl" for n in
                            range(router_calls_before + 1, self._router_calls + 1)],
        })
        return (outcome.target, guard, outcome)

    def _apply_reported_task_status(self, payload: dict) -> list[str]:
        """Task 019 (requirement F): validate and (if legal) apply one
        hat-reported `task_status` move for `payload["task_id"]`.

        Checked against `transitions.task_status_report_errors` -- the
        declared task-status table for this run's variant -- given the
        task's CURRENT on-disk status; an unknown `task_id` or an illegal
        move both come back as a non-empty errors list, which
        `_step_generic_hat` treats exactly like a schema violation (a
        contract violation, ending the run rather than silently taking the
        move anyway). A no-op report (`task_status` already matches
        `current`) is legal by construction
        (`is_legal_task_status_transition`) and simply writes nothing.

        This is the ONLY place a hat's reported task-status claim reaches
        disk: through `RunState.write_tasks` (task 013's sole writer), and
        only once, after the hat's own subprocess has already exited.
        """
        task_id = payload.get("task_id")
        target = payload.get("task_status")
        tasks_data = self.run.read_tasks()
        current = None
        for t in tasks_data.get("tasks", []):
            if t.get("id") == task_id:
                current = t.get("status", "pending")
                break
        if current is None:
            return [f"task_status: task_id {task_id!r} not found in tasks.json"]
        errors = transitions.task_status_report_errors(payload, current, self.cfg.vigilant)
        if errors:
            return errors
        if target != current:
            for t in tasks_data.get("tasks", []):
                if t.get("id") == task_id:
                    t["status"] = target
                    # Task 009 (requirement C.1, #123): the one declared
                    # move whose contract carries an extra payload field --
                    # `transitions.task_status_report_errors` already
                    # refused this call above unless `justification` is a
                    # non-empty string, for exactly this move -- and the
                    # harness (never the worker) is the one that writes it
                    # to disk, into the task's own `notes` field, in the
                    # SAME write this status flip already makes.
                    if current == "failed" and target == "skipped":
                        t["notes"] = payload.get("justification")
                    break
            self.run.write_tasks(tasks_data)
            self._emit_task_status(task_id, current, target)
        return []

    def _apply_worker_task_status_report(self, payload: dict,
                                         assigned_task_id: str | None) -> list[str]:
        """Task 011 (requirement C.1, #123): the semantic half of a
        WORKER's `task_status` report -- the channel worker.md's failed-task
        escape instructs -- layered on `_apply_reported_task_status` just
        above rather than replacing it (one applier, one writer).

        The one thing this wrapper adds over the generic hat channel: a
        worker may never report a `task_status` for the task the HARNESS
        assigned it this iteration. That task's own status is the verdict
        channel's business (`verdict: "complete"`/`"unsatisfiable"`, whose
        target the harness picks itself -- `_apply_worker_completion_
        report`), and a payload carrying BOTH for the same id is a
        contradiction ("this task is done" + "this task is skipped") that
        must be refused and re-asked, never half-applied in whichever
        order the branches happen to run. Nothing legitimate is lost: the
        escape is by construction about a DIFFERENT task -- a `failed` one,
        which `scheduler.select_next_task` never assigns (`failed` is not
        in `state.TASK_ACTIONABLE_STATUSES`).

        Everything else -- unknown `task_id`, an edge the declared table
        for this variant does not carry, the missing-`justification`
        condition on `failed -> skipped`, and the write of that
        justification into the task's own `notes` -- is `_apply_reported_
        task_status`/`transitions.task_status_report_errors`' business,
        unchanged and not re-derived here.
        """
        task_id = payload.get("task_id")
        if not task_id:
            return ["task_status: reported with no task_id naming the task "
                    "to move"]
        if assigned_task_id is not None and task_id == assigned_task_id:
            return [f"task_status: task {task_id!r} is the task assigned to "
                    "this iteration -- report its own outcome through "
                    "`verdict` instead; the task_status channel is for "
                    "resolving a different, already-failed task"]
        return self._apply_reported_task_status(payload)

    def _apply_unsatisfiable_report(self, payload: dict) -> list[str]:
        """Task 025 (requirement H, #63): apply a worker's `verdict:
        "unsatisfiable"` report -- a task's criteria cannot be satisfied as
        written, with a reason -- the moment its own subprocess has
        already exited, same discipline as `_apply_reported_task_status`
        just above (only one write reaches disk, through `RunState.
        write_tasks`, task 013's sole writer, and it happens strictly
        after the hat's own subprocess has exited).

        Schema validation (`contracts.validate_payload`) is the caller's
        job; this is the semantic half: `transitions.
        worker_unsatisfiable_report_errors` checks `task_id`/`reason` are
        both non-empty, `task_id` must resolve against tasks.json, and the
        move to `skipped` must be LEGAL from the task's current status in
        this run's variant -- the exact same declared task-status table
        task 019's plain status reports are checked against
        (`transitions.task_status_report_errors`), not a second one. The
        move IS the state change this task's acceptance asks for: it lands
        in the SAME tasks.json write this method already has to make for
        the reason text, so `_step_worker`'s existing before/after diff
        sees genuine progress with no further plumbing.
        """
        errors = transitions.worker_unsatisfiable_report_errors(payload)
        if errors:
            return errors
        task_id = payload.get("task_id")
        reason = payload.get("reason")
        tasks_data = self.run.read_tasks()
        current = None
        for t in tasks_data.get("tasks", []):
            if t.get("id") == task_id:
                current = t.get("status", "pending")
                break
        if current is None:
            return [f"task_status: task_id {task_id!r} not found in tasks.json"]
        status_errors = transitions.task_status_report_errors(
            {"task_status": "skipped"}, current, self.cfg.vigilant)
        if status_errors:
            return status_errors
        for t in tasks_data.get("tasks", []):
            if t.get("id") == task_id:
                t["status"] = "skipped"
                t["unsatisfiableReason"] = reason
                break
        self.run.write_tasks(tasks_data)
        self._emit_task_status(task_id, current, "skipped",
                               unsatisfiableReason=reason)
        # Fact 19's hazard: log the violation-shaped moment LOUDLY, at the
        # instant it happens, not just as a quiet status flip an operator
        # would have to notice by diffing tasks.json.
        self.run.emit(
            "log", level="warning",
            message=(f"worker reported task {task_id!r}'s criteria cannot "
                      f"be satisfied as written: {reason} (task 025, #63); "
                      "marked skipped"))
        return []

    def _apply_worker_petition_declaration(self, payload: dict) -> list[str]:
        """Task 005 PART 1 (#114, closes task 004's residual gap -- see
        004's `validationNotes`, operator steer 001): the production write
        path for a worker's OPTIONAL structured impossibility declaration
        (`payload["petition"]`), independently of `payload["verdict"]`'s
        own value -- a worker may declare a petition alongside
        `"no_verdict"`, `"unsatisfiable"`, `"complete"` or
        `"plan_complete"` alike (`HAT_SCHEMAS["worker"]`'s own docstring).

        Same two-layer split every sibling `_apply_*` method in this class
        already uses: the SEMANTIC check first
        (`transitions.worker_petition_report_errors` -- schema shape is
        `contracts.validate_payload`'s job, already run by
        `contract_enforcement.parse_response` before this hook is ever
        called), then the engine's own write path
        (`petitions.file_worker_petition`, never the worker itself) --
        which is also where a duplicate declaration against an obligation
        already filed is refused rather than filed (docs/petition-
        format.md's one-petition-per-obligation rule). A payload carrying
        no `petition` key at all is never in violation here, matching
        every sibling check's "nothing declared, nothing to check" shape.

        Called from `_step_worker` through `contract_enforcement.
        enforce_contract`'s re-ask ladder exactly like
        `_apply_unsatisfiable_report`/`_apply_worker_completion_report`:
        a malformed declaration is carried back to the SAME worker and
        re-asked rather than silently dropped or filed, and a duplicate
        is refused the same way -- neither ever reaches
        `petitions.file_worker_petition`'s write in that case, so nothing
        new lands in the wave's `<wave>-petitions.md` for either failure
        mode. A valid declaration is filed EXACTLY ONCE: this method is
        the ONLY call site `_step_worker` gives `petitions.
        file_worker_petition` for a worker's own declaration (as opposed
        to `petitions.file_exhaustion_petition`, the SEPARATE writer
        `_verify_task` already used for validation-ladder exhaustion
        before this task, untouched by this change), and `enforce_
        contract` calls its `extra_validate` hook at most once per
        attempt, succeeding (and stopping) the first time it returns no
        errors.
        """
        errors = transitions.worker_petition_report_errors(payload)
        if errors:
            return errors
        petition = payload.get("petition")
        if not isinstance(petition, dict):
            return []
        return petitions.file_worker_petition(
            self.workspace, petition["obligation"], petition["type"],
            petition["evidence"])

    def _worker_assignment_extra(self, task: dict | None) -> str:
        """Task 116 (#60 pillar 3, fact 9): the one thing a worker
        iteration needs told to it now that the HARNESS -- not the agent --
        runs `engine.scheduler.select_next_task` and owns every task-status
        write: which task_id it has been assigned this iteration, already
        persisted as `in-progress` on disk (if it was not already) strictly
        before this text is even built. `task` is `None` only when nothing
        was selectable at all (an empty/fully-resolved plan, or the #33
        "nothing actionable but something failed" case) -- worker.md's own
        "say so and stop" path, not a task_id to guess at.

        Task 015 (requirement F.2, #145): when `task` is a re-dispatch at
        `validationAttempts > 0`, the returned text also carries a
        "## Why the previous attempt was rejected" block with the attempt
        count and `validationNotes` verbatim -- absent entirely at
        `validationAttempts == 0` (see below).
        """
        if not isinstance(task, dict):
            return (
                "## Task assigned this iteration\n\n"
                "No task is currently selectable (nothing pending, "
                "in-progress or validation-failed with satisfied "
                "dependencies). Read tasks.json and the notes file to "
                "understand why and report back -- do not invent a "
                "task_id.\n")
        block = (
            "## Task assigned this iteration\n\n"
            f"- **id**: {task.get('id', '')}\n"
            f"- **title**: {task.get('title', '')}\n"
            f"- **status**: {task.get('status', '')}\n"
            f"- **successCriteria**: {task.get('successCriteria', '')}\n")
        # Task 015 (requirement F.2, #145): a re-dispatch at
        # `validationAttempts > 0` is a RETRY, not a fresh pick -- the
        # worker needs told, in its own prompt, exactly what the verifier
        # rejected last time, not just the task's unchanged
        # `successCriteria`. Carry `validationNotes` VERBATIM (never
        # summarised or truncated -- the worker, not this method, is the
        # one that judges whether the notes are addressed or petitioned)
        # under a heading naming the requirement's own wording. Absent
        # entirely at `validationAttempts == 0` (a first dispatch, or a
        # task dict with no such key) so the block's mere presence is
        # itself the signal a lexical test can pin.
        attempts = task.get("validationAttempts", 0)
        if attempts:
            notes = task.get("validationNotes", "")
            block += (
                "\n## Why the previous attempt was rejected\n\n"
                f"- **validationAttempts**: {attempts}\n"
                f"- **validationNotes**: {notes}\n")
        return block

    def _apply_worker_completion_report(self, payload: dict,
                                         assigned_task_id: str | None) -> list[str]:
        """Task 116 (#60 pillar 3, fact 9): translate a worker's own
        `{"verdict": "complete", "task_id": ...}` report into the SAME
        declared task-status move `_apply_reported_task_status` already
        applies for review/reflect -- not a second mechanism. Both the
        task moved and the target status are the HARNESS's decision, never
        taken from the agent's word alone: the task is always the one
        `engine.scheduler.select_next_task` assigned THIS iteration, before
        the subprocess was even spawned (`assigned_task_id`), and the
        target is `awaiting-validation` under vigilant / `completed`
        otherwise. A payload naming a *different* task_id is not silently
        redirected onto it -- that would let a stray or fabricated task_id
        claim a move the harness never assigned -- it is a contract
        violation instead, exactly like any other reported move the
        declared table refuses: a payload that disagrees with reality is
        still just a claim (task 116's acceptance).
        """
        if not assigned_task_id:
            return ["verdict 'complete' reported with no task assigned "
                    "this iteration (nothing selectable for a worker to "
                    "complete)"]
        reported_id = payload.get("task_id")
        if reported_id and reported_id != assigned_task_id:
            return [f"verdict 'complete' names task_id {reported_id!r} but "
                    f"task {assigned_task_id!r} is the one this iteration "
                    "was assigned"]
        target = "awaiting-validation" if self.cfg.vigilant else "completed"
        return self._apply_reported_task_status(
            {"task_id": assigned_task_id, "task_status": target})

    def _apply_worker_plan_complete_report(self, payload: dict,
                                            assigned_task_id: str | None,
                                            tasks_doc: dict) -> list[str]:
        """Task 015 (requirement O, #100): the semantic half of the new
        `verdict: "plan_complete"` claim -- the whole-plan sibling of
        `_apply_worker_completion_report` just above, refused on exactly
        the same two facts the retired `<promise>COMPLETE</promise>`
        marker's own `honoured_complete` cross-check used:

        1. **structural** -- `plan_complete` is valid only when the
           harness assigned NO task this iteration (`assigned_task_id` is
           `None`, i.e. `scheduler.select_next_task` found nothing
           pickable). A worker handed real work this iteration has no
           business claiming the whole plan is done in the same breath --
           that claim belongs to "complete" (this task's own completion),
           never to this field.
        2. **semantic** -- cross-checked against `_outstanding_for_
           completion(tasks_doc)`, the identical helper `honoured_
           complete` already consulted for the marker: any task not
           `completed`/`skipped`/`validated` refuses the claim, naming
           the outstanding tasks so the reviewer/operator can see why.

        A payload failing either check returns here rather than applying
        anything -- `_step_worker`'s caller treats a non-empty return
        exactly like a schema violation, carrying the claim back through
        `contract_enforcement.enforce_contract`'s re-ask ladder instead of
        silently taking it, or silently reading it as a no-progress
        iteration.
        """
        if assigned_task_id is not None:
            return [f"verdict 'plan_complete' is only valid when no task "
                    f"was assigned this iteration; task {assigned_task_id!r} "
                    "was assigned"]
        outstanding = self._outstanding_for_completion(tasks_doc)
        if outstanding:
            names = ", ".join(
                f"{t.get('id', '?')} ({t.get('status', 'unknown')})"
                for t in outstanding[:10])
            return [f"verdict 'plan_complete' reported with "
                    f"{len(outstanding)} task(s) not completed or "
                    f"skipped: {names}"]
        return []

    def _apply_worker_proposed_tasks(self, proposed_tasks: list,
                                     assigned_task_id: str | None,
                                     approach: int) -> None:
        """Task 021 (requirement N, #102): ingest a worker's optional
        `proposed_tasks` split proposal (`transitions.HAT_SCHEMAS["worker"]`)
        through `plan_ingestion.lint_worker_split` -- the EXISTING lint
        (`validate_proposal_shape`/`lint_proposal`/`lint_own_terminal_
        state`) reused, never a second one -- then, only once that lint is
        clean AND this approach's `worker_split_budget_per_approach` still
        has room, `state.insert_worker_split_tasks` (worker-origin marking,
        `splitFrom` stamping).

        Deliberately NOT routed through `contract_enforcement.enforce_
        contract`'s re-ask ladder the way `_apply_unsatisfiable_report`/
        `_apply_worker_completion_report` are: those two are the worker's
        PRIMARY verdict for the iteration and a bad one must come back to
        the same worker to fix. `proposed_tasks` is an optional side
        channel that can accompany ANY verdict (including a plain
        `no_verdict`, which has no ladder of its own at all) -- an invalid
        or over-budget proposal is refused and logged instead
        (`EVENT_VIOLATION`-shaped, `hat="worker"`), leaving the rest of
        this iteration's own verdict handling completely unaffected. Never
        raises, never advances a state, never touches `stagnant`.
        """
        if not isinstance(proposed_tasks, list) or not proposed_tasks:
            return
        tasks_doc = self.run.read_tasks()
        existing_ids = {t.get("id") for t in tasks_doc.get("tasks", [])
                        if isinstance(t, dict)}
        errors = plan_ingestion.lint_worker_split(
            proposed_tasks, assigned_task_id=assigned_task_id,
            existing_ids=existing_ids)
        if errors:
            self.run.emit(
                contract_enforcement.EVENT_VIOLATION, hat="worker",
                message="worker's proposed_tasks split was refused",
                errors=list(errors))
            return
        usage = dict(self.run.read_status().get("workerSplitProposals") or {})
        used = int(usage.get(str(approach), 0))
        budget = self.cfg.worker_split_budget_per_approach
        if used + len(proposed_tasks) > budget:
            self.run.emit(
                contract_enforcement.EVENT_VIOLATION, hat="worker",
                message=(
                    f"worker's proposed_tasks split was refused: approach "
                    f"{approach} has already used {used}/{budget} of its "
                    f"split budget and this proposal would add "
                    f"{len(proposed_tasks)} more"),
                errors=[])
            return
        inserted = state.insert_worker_split_tasks(
            self.run, proposed_tasks, assigned_task_id)
        if not inserted:
            return
        usage[str(approach)] = used + len(inserted)
        self.run.update_status(workerSplitProposals=usage)
        self.run.emit(
            "log", level="info",
            message=(f"worker proposed a split of task {assigned_task_id}: "
                      f"{len(inserted)} new task(s) created "
                      f"({', '.join(str(i) for i in inserted)})"))

    def _record_worker_iteration_verdict(self, iteration_number: int,
                                          verdict: str,
                                          task_id: str | None) -> None:
        """Task 123 (#60): task 116 moved the worker's per-task completion
        signal off the bare `<promise>COMPLETE</promise>` line onto a JSON
        verdict whose last line is never that literal sentinel, so
        `IterationResult.saw_complete` -- which only ever recognised the
        exact sentinel text, and is now purely an operator-facing
        diagnostic (task 015, #100) -- can no longer tell an operator (or a
        black-box test, e.g. `tests/test_crash_consistency.py`) which
        iteration actually applied a task's completion. Record it directly
        in that iteration's own `meta.json`, mirroring how `_verify_task`
        already enriches a verify iteration's meta with `verifiedTask`/
        `verifyOutcome` rather than inventing a second convention. Called
        once a per-task verdict's contract has genuinely been applied to
        tasks.json (never for a schema violation or a bare 'no_verdict'),
        and once more for the whole-plan "plan_complete" claim (task 015)
        -- which applies nothing to tasks.json itself, task_id=None,
        purely so that ending iteration's own meta.json also carries an
        appliedVerdict an operator or test can read.
        """
        itdir = self.run.iteration_dir(iteration_number)
        meta_path = itdir / "meta.json"
        if not meta_path.exists():
            return
        try:
            meta = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            meta = {}
        meta["appliedVerdict"] = verdict
        meta["appliedTaskId"] = task_id
        atomic_write_json(meta_path, meta)

    async def _ask_router(self, prompt: str) -> tuple[str, dict]:
        """The router's own model call (task 018, requirement E): one tiny,
        UNCOUNTED question -- no iteration slot, no `iterations_used`
        increment, no `meta.json` (contrast `_run_iteration_once`, which
        does all three for a real hat). "Router calls ... are not charged
        as work iterations" is true here by construction, not by a
        separate check: this method never calls `_run_iteration_once` or
        touches `self.iterations_used` at all.

        Usage IS accounted, separately: `_accumulate_usage(..., phase=
        "router")` folds into `usage.byPhase["router"]` so the router's
        cost is visible, not hidden (requirement E's acceptance) -- while
        staying out of every OTHER phase's bucket and out of the overall
        iteration count.
        """
        router_dir = self.run.artifacts_dir / "router"
        router_dir.mkdir(parents=True, exist_ok=True)
        self._router_calls += 1
        transcript = router_dir / f"{self._router_calls:04d}.jsonl"
        model = self.cfg.model_for("router")
        result = await self.runner.run(
            prompt, transcript, model=model,
            timeout_s=self.cfg.router_timeout_s, extra_env=current_env())
        if result.usage:
            self._accumulate_usage(result.usage, phase="router")
        return (result.final_text or ""), (result.usage or {})

    @staticmethod
    def _extract_validated_fenced_payload(hat: str, final_text: str | None) -> dict | None:
        """Task 032: scan `final_text` (the hat's own last completed
        response, prose and all -- never the already-line-trimmed
        `last_nonblank_line` a caller may also be holding) for a fenced
        ```json block that both parses as JSON and validates against
        `hat`'s own declared contract schema. Tried most-recent-block-
        first: a hat that narrates its reasoning and then closes with the
        corrected payload is the common shape this exists for (the
        iteration-36 regression), and an earlier block is more likely to
        be a worked example than the final answer. Returns the FIRST
        candidate that validates, or `None` if there is none -- callers
        take `None` to mean "a model re-ask is still required", never an
        error on its own."""
        if not final_text:
            return None
        candidates = _FENCED_JSON_BLOCK_RE.findall(final_text)
        for block in reversed(candidates):
            try:
                payload = json.loads(block.strip())
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            if contracts.validate_payload(hat, payload):
                continue
            return payload
        return None

    async def _ask_contract_fix(
        self, hat: str, errors: list[str], final_text: str | None = None,
    ) -> tuple[str, dict]:
        """The contract-repair channel (task 001's requirement B decision,
        task 002): one tiny, UNCOUNTED question, built directly on
        `_ask_router`'s precedent immediately above -- no iteration slot,
        no `iterations_used` increment, no `meta.json` (contrast
        `_run_iteration_once`, which does all three for a real hat). This
        method never calls `_run_iteration_once` or touches
        `self.iterations_used` at all; that is true by construction, not by
        a separate check.

        Usage IS accounted, separately: `_accumulate_usage(..., phase=
        "contract")` folds into `usage.byPhase["contract"]`, distinct from
        `"router"` because the two channels answer structurally different
        questions (which edge to take, versus what the corrected payload
        is) -- merging them would hide which one a run actually spent its
        uncharged-call budget on.

        The prompt states `errors` verbatim and asks ONLY for the corrected
        final payload -- it never re-issues `hat`'s own task prompt and
        never invites further workspace work, so a caller cutting one of
        the five refusal sites over to this channel cannot accidentally
        hand the hat a second bite at doing MORE than fix its last
        response's shape.

        Wired in: `_step_generic_hat`'s schema/task_status site (tasks
        003 `2be8b86`/004 `edbcb79`) and worker's 3 verdict sites (005
        `1311548`, 006 `8599b7b`, 007 `aa0a7b1`) all pass this as `respond`.

        Model decision (task 004, steer 002 item 3, option (a)): the re-ask
        runs on `hat`'s OWN model -- `self.cfg.model_for(hat)`, never a
        fixed "contract" lookup. Before this task it resolved
        `self.cfg.model_for("contract")`; `"contract"` is not a `MODEL_HATS`
        member, so `config.py`'s `UNRECOGNISED_HAT_FALLBACK` ("planning")
        silently repaired every hat's payload with PLANNING's model
        regardless of which hat produced the violation -- a `sonnet` worker's
        malformed verdict fixed by `opus`, undesigned and undocumented.
        `model_for(hat)` keeps the repair inside the tier that made the
        mistake: cheaper, symmetric, and a hat's own `--model-for`/`models:`
        entry now governs the repair of its own output. Pinned by
        `tests/test_contract_reask_model_task004.py`.

        Task 032 (operator-inserted, grounding the re-ask in the hat's
        actual final text -- the iteration-36 regression): `final_text` is
        the hat's own most recently completed response, verbatim, if the
        caller has one (every existing call site does -- see `loop.py`'s
        `respond()` closures). Two things change from here:

        1. FENCED-PAYLOAD EXTRACTION, before any model call at all --
           `_extract_validated_fenced_payload` scans `final_text` for a
           fenced ```json block that parses AND validates against `hat`'s
           own contract schema. When one exists, THIS method returns it
           directly (re-serialised through `json.dumps`, `usage={}`) and
           `self.runner.run` is never invoked -- no transcript file, no
           `_contract_calls` increment, no `"contract"`-phase usage. The
           payload was already there, correctly, in text the hat itself
           produced; asking the model again to reproduce it can only
           either repeat it (wasted call) or, as iteration 36 showed,
           fabricate something worse.
        2. GROUNDED RE-ASK, when no fenced block validates and a model
           call is still genuinely needed -- the prompt now carries
           `final_text` verbatim, bounded to the last
           `CONTRACT_REASK_FINAL_TEXT_MAX_CHARS` characters, ALONGSIDE
           the error list (both, never one instead of the other), so the
           model is repairing what the hat actually said instead of
           starting from nothing the way the pre-task-032 prompt did. The
           demand stays exactly what it always was: ONLY the corrected
           JSON payload, no further workspace work.

        Pinned by `tests/test_contract_reask_grounding.py`.
        """
        fenced_payload = self._extract_validated_fenced_payload(hat, final_text)
        if fenced_payload is not None:
            return json.dumps(fenced_payload), {}
        contract_dir = self.run.artifacts_dir / "contract"
        contract_dir.mkdir(parents=True, exist_ok=True)
        self._contract_calls += 1
        transcript = contract_dir / f"{self._contract_calls:04d}.jsonl"
        excerpt = (final_text or "")[-CONTRACT_REASK_FINAL_TEXT_MAX_CHARS:]
        prompt = (
            f"Your previous response for the {hat!r} step did not satisfy "
            "its required response contract:\n\n"
            + "\n".join(f"- {e}" for e in errors)
            + (
                "\n\nYour previous response for this step was (verbatim, "
                "possibly truncated to its last "
                f"{CONTRACT_REASK_FINAL_TEXT_MAX_CHARS} characters):\n\n"
                + excerpt
                if excerpt else ""
            )
            + "\n\nReply with ONLY the corrected final JSON payload that "
            "fixes every error listed above. Do not repeat or restate your "
            "previous reasoning, do not perform any further workspace "
            "work, and do not include anything other than the corrected "
            "JSON object itself."
        )
        model = self.cfg.model_for(hat)
        result = await self.runner.run(
            prompt, transcript, model=model,
            timeout_s=self.cfg.router_timeout_s, extra_env=current_env())
        if result.usage:
            self._accumulate_usage(result.usage, phase="contract")
        return (result.final_text or ""), (result.usage or {})

    async def _one_planning_attempt(self, extra: str):
        """Run ONE `planning` iteration (with `extra` folded into the
        prompt -- task 120's contract re-prompt carries the previous
        attempt's specific validation errors here), retrying in place for
        as long as it keeps hitting instant startup/infra failures -- an
        agent process that never reached the model at all is not a genuine
        "the plan proposal was rejected" situation, and must not be handed
        to `plan_ingestion.enforce_plan_ingestion` as one of ITS attempts
        (that budget is for genuine contract violations, not environment
        flakiness). Returns the resolved `IterationResult`, or `None` once
        `self._check_instant_failure` has tripped `self._abort_reason`
        (`budget_left()` is False at that point; the caller's job is to
        notice and stop, not to keep retrying).

        This is exactly the retry shape `_step_planning` used inline before
        task 120 split the "get one resolved attempt" concern (this method)
        from the "is the attempt's payload a valid plan proposal" concern
        (`plan_ingestion.enforce_plan_ingestion`, now the caller).

        Task 004 (requirement A, #72): the SAME carve-out extends to an
        attempt the ENGINE itself cut short
        (`_iteration_cut_short_by_engine` -- SIGINT, the iteration
        timeout, the startup watchdog, or an abnormal exit) -- exactly the
        review-interrupted shape task 003 fixed for `_step_review`, but
        `_instant_failure_signature` deliberately returns `None` for that
        shape ("not instant / not this shape", loop.py's own comment on
        it) so it was falling through UNCAUGHT: a planning attempt the
        engine interrupted was handed to `respond()` and from there to
        `plan_ingestion.enforce_plan_ingestion` as an ordinary attempt,
        burning one of its `contract_retry_max` retries on something the
        model never got a fair shot at -- enough consecutive interrupts
        exhausts that budget and costs the WHOLE approach via the
        `empty_plan` edge, for a reason that was never the plan's own
        fault. Retried in place here instead, for as long as budget
        remains, before ever reaching `respond()`.
        """
        while True:
            await self._gate()
            # Task 016 (requirement I): planning has no task in hand -- its
            # ladder's designated tier is `tiers.DEFAULT_TIER`, unaffected
            # by any task's tier in the plan.
            presult = await self.run_iteration("planning", extra=extra,
                                               tier=tiers.DEFAULT_TIER)
            if self._check_instant_failure(presult, self.iterations_used):
                return None  # abort_reason set; budget_left() now False
            if self._instant_failure_streak and self.budget_left():
                continue
            if (self._iteration_cut_short_by_engine(presult)
                    and self.budget_left()):
                continue
            return presult

    async def _step_planning(self, skip_planning: bool) -> tuple:
        """The `planning` state. Guards: `instant_failure_streak` (self-loop,
        handled by `_one_planning_attempt`'s inner retry `while True`
        without ever leaving this method -- an in-place retry is not a
        state CHANGE), `empty_plan` (-> replan) and
        `planning_produced_tasks` (-> worker). Returns `("done", <state>)`
        on an off-budget grace-reviewed success, `("tail",)` to fall
        straight to the terminal-failure/-abort tail (budget ran out with
        no grace review, or an instant-failure streak just tripped
        `self._abort_reason`), or `("advance", target, guard)`.

        Task 120 (requirement C, #60 pillar 1): the planning hat no longer
        writes `tasks.json` itself (fact 9) -- it drafts a file-borne
        `plan-proposal.json` in the run's own scratch area
        (`self.run.root`, the same directory the prompt's "Job context"
        names as "Run state directory") and reports only a
        pointer-plus-summary (`transitions.HAT_SCHEMAS["planning"]`) as its
        final line. `plan_ingestion.enforce_plan_ingestion` -- already
        implemented and unit-tested in isolation (task 011), just not
        wired into a real iteration loop until now -- owns the whole
        pointer-schema / proposal-shape / semantic-lint / ingestion
        pipeline and the N-then-agent-fault re-prompt policy around it,
        reusing `self.cfg.contract_retry_max` (the same knob the router
        and every other contract already share). Its `respond` callback
        below re-prompts the SAME approach's planning hat with the
        previous attempt's specific errors folded into `extra` -- the
        model genuinely sees what was wrong, exactly as any other contract
        re-prompt does.

        `enforce_plan_ingestion`'s own retry loop is synchronous (a plain
        `respond(attempt, prior_errors) -> str | None` callable with no
        `await` in its signature -- task 010's shape, reused verbatim
        rather than re-derived for the file-borne case), so it runs off
        the event loop via `asyncio.to_thread`; `respond` bridges back onto
        THIS coroutine's own event loop with `run_coroutine_threadsafe`
        rather than nesting a second event loop (which asyncio refuses).
        """
        approach = self._approach
        self.run.update_status(approach=approach)
        if not self.budget_left():
            # Task 002: covers the resume edge case where a prior process
            # already completed every task for this approach but the
            # process died/ran out of budget before any review ran at all.
            if await self._maybe_grace_review(approach):
                return ("done", "succeeded")
            return ("tail",)
        if not skip_planning:
            self.run.emit("phase", phase="planning", approach=approach)

            # Task 018 (requirement K, #101): consumed here, once, and
            # cleared immediately -- `_step_plan_review` is the only
            # writer, and this is the very next planning attempt after it
            # wrote it, so the model actually sees what the peer objected
            # to on the revision it asked for.
            pending_objection = self._pending_plan_review_objection
            self._pending_plan_review_objection = None

            event_loop = asyncio.get_running_loop()

            def respond(attempt: int, prior_errors):
                # Once budget/abort has already been decided (by an
                # earlier attempt's instant-failure streak), every further
                # `enforce_plan_ingestion` attempt is a no-op "absent"
                # response rather than another real subprocess spawn --
                # the outer check below (`if not self.budget_left()`) is
                # what actually stops the run; this just stops burning
                # iterations once that outcome is already locked in.
                if self._abort_reason is not None or not self.budget_left():
                    return None
                extra = ""
                if pending_objection:
                    extra += (
                        "\n## Peer plan review objected to your previous "
                        "plan\n\nRevise the plan to address this objection "
                        "before resubmitting a proposal:\n"
                        + pending_objection + "\n")
                if prior_errors:
                    extra += (
                        "\n## Your previous plan-proposal.json was "
                        "rejected\n\nFix every problem below and resubmit "
                        "a corrected proposal (a fresh `proposal_path` "
                        "pointer, same or a new file):\n"
                        + "\n".join(f"- {e}" for e in prior_errors))
                presult = asyncio.run_coroutine_threadsafe(
                    self._one_planning_attempt(extra), event_loop).result()
                if presult is None:
                    return None
                return last_nonblank_line(presult.final_text)

            ingestion = await asyncio.to_thread(
                plan_ingestion.enforce_plan_ingestion,
                respond,
                max_retries=self.cfg.contract_retry_max,
                read_proposal=plan_ingestion.file_reader(self.run.root),
                state=self.run,
            )
            if not self.budget_left():
                return ("tail",)
            if not ingestion.ok:
                self.run.emit(
                    "log", level="error",
                    message=("planning produced no usable plan proposal: "
                              + "; ".join(ingestion.errors)))
                # Task 012 (requirement C.2, #124): `ingestion.errors` is
                # already a stable deduplicated list -- a natural blocker
                # signature -- written to the SAME `blockerSignatures` map
                # (`_record_stagnation_signature`) worker stagnation writes
                # its own signature to (task 026, #61), keyed by the SAME
                # approach number. `_step_replan`'s existing `repeated_
                # blocker_signature` guard reads that map generically, so
                # two consecutive approaches failing ingestion on the same
                # normalized errors end the run there without any change to
                # that guard -- exactly the reuse the PRD asks for, not a
                # second lint-specific mechanism.
                self._record_stagnation_signature(
                    approach, self._ingestion_blocker_signature(ingestion.errors),
                    origin="ingestion")
                return ("advance", "replan", "empty_plan")
            self.run.write_tasks(ingestion.tasks_doc)
            # Task 107 (#60 pillar 3): pin the planner's `standing_rules`
            # contract field, once, as this run's harness-owned copy --
            # `_reinject_standing_rules` (every iteration end, any hat)
            # reads it back and splices it into notes.md, so nothing here
            # writes notes.md directly. A replan that supplies a fresh
            # block updates the pin the same way; a proposal that omits
            # the field (or an approach that never replans) leaves
            # whatever was pinned before untouched.
            if ingestion.standing_rules.strip():
                self.run.write_standing_rules(ingestion.standing_rules)

        # Task 008: establish (or backfill, for a run resumed from before
        # this feature existed) a criteriaFingerprint baseline for every
        # task before the worker loop's first before/after diff --
        # otherwise the fingerprint field appearing for the first time
        # inside that diff would look like spurious task progress. Runs
        # exactly once per approach, whether planning ran or was skipped
        # (a resumed process's first visit to `worker`).
        self._ensure_criteria_baseline()
        # Task 018 (requirement K, #101): the opt-in gate, off by default.
        # `transitions.PEER_PLAN_REVIEW_DELTA` is what actually declares
        # this alternate edge legal -- this `if` is just which of the two
        # ALREADY-legal targets this run takes, decided from the SAME
        # config flag `transitions.table_for`'s caller (`TransitionExecutor`
        # construction, `__init__`) was built with, so the two can never
        # disagree about which table variant is in force.
        if self.cfg.peer_plan_review:
            return ("advance", "plan_review",
                    "planning_produced_tasks_peer_review_enabled")
        return ("advance", "worker", "planning_produced_tasks")

    async def _step_plan_review(self) -> tuple:
        """The `plan_review` state (task 018, requirement K, #101) --
        reachable only when `self.cfg.peer_plan_review` is on. Budget-
        charged like planning/worker/review, NOT given `steer`'s uncharged
        special-casing: this hat does real reasoning work over the plan
        `planning` just ingested, unlike `steer`'s bookkeeping-only role
        (the decision this task's own commit message and
        `artifacts/reports/task-018-requirement-k-budget-decision.md`
        record).

        Parses the hat's `plan_review` contract payload
        (`transitions.HAT_SCHEMAS["plan_review"]`) exactly the way
        `_review_verdict` parses review's own: an absent/malformed payload,
        a schema violation, or an `objected` verdict with no `reason`
        (`transitions.plan_review_report_errors`) is never a run-ending
        contract violation -- it is an UNDECIDABLE reply, routed as
        described below.

        `approved` (real, per above) -> `worker` (`plan_review_approved`).
        An `objected` verdict with revisions still available for THIS
        approach (`self.cfg.peer_plan_review_max_revisions`, an explicit
        NUMBER, checked against status.json's persisted
        `planReviewRevisions[str(approach)]` count -- disk, not an
        in-memory-only counter, so it survives a resumed process exactly
        the way `_record_stagnation_signature`'s `blockerSignatures` map
        already does) -> `planning` (`plan_review_objected_revision_
        available`), stashing the objection's reason on
        `self._pending_plan_review_objection` for the very next planning
        attempt to fold into its prompt. Once the bound is spent, the SAME
        objected verdict instead falls through to `worker` anyway
        (`plan_review_revision_bound_reached`) -- fail OPEN on a bounded
        gate, never a deadlocked or faulted run.

        Task 019 (requirement M) answered the undecidable case -- an
        absent/malformed/schema-invalid reply, or an `objected` verdict
        with no reason (`transitions.plan_review_report_errors`) -- by
        asking the harness's own constrained question, `router.decide()`
        through `_route_with_router`, over `plan_review`'s REAL edges.
        That made `plan_review` the first *declared* router-eligible
        state -- a table entry with a genuine multi-edge choice on a real
        reply -- but it also meant the one production path that ever
        reached a SECOND model for an uninformed decision was this one:
        the peer's reply was garbage, not a vote for either edge, and
        disk state (`revision_available`) already says whether `planning`
        is even a legal move -- there was no genuine choice left to ask
        about.

        Task 016 (requirement D.2, #117 half B) replaces that call with
        the deterministic policy the disk state already supports:
        `payload is None and revision_available` -> spend the revision
        directly, exactly the same move an `objected`-with-no-reason
        verdict already takes just below. The revision re-prompt now
        carries the peer's raw reply and its parse errors (task 019's
        boilerplate "the router routed the plan back" is gone) so the
        next planning attempt learns something concrete about what the
        peer actually sent, not just that the harness gave up reading it.
        The `plan_review_revision_bound_reached` fallback still bounds the
        loop exactly as before: once the revision bound is spent,
        `planning` is not a move this gate may make regardless of what the
        reply says, decidable or not.

        `router.decide()` and its fixture tests (`tests/test_router.py`)
        stay -- the zero-call record here is the deterministic-first
        contract holding, not the machinery going unused for good; a
        genuinely ambiguous state still has somewhere real to call.
        `_route_with_router` is untouched too -- `plan_review` simply
        stops being one of its callers.
        """
        approach = self._approach
        if not self.budget_left():
            return ("tail",)
        self.run.emit("phase", phase="plan_review", approach=approach)
        result = await self.run_iteration("plan_review")
        raw_reply = result.final_text or ""
        parse_errors: list[str] = []
        try:
            parsed = json.loads(last_nonblank_line(raw_reply))
        except (json.JSONDecodeError, TypeError, AttributeError) as exc:
            parsed = None
            parse_errors = [
                f"could not parse a JSON object from the reply: {exc}"]
        payload = None
        if isinstance(parsed, dict):
            contract_errors = (
                contracts.validate_payload("plan_review", parsed)
                or transitions.plan_review_report_errors(parsed))
            if contract_errors:
                parse_errors = list(contract_errors)
            else:
                payload = parsed
        elif parsed is not None:
            parse_errors = [
                f"the reply parsed as {type(parsed).__name__}, not a JSON "
                "object"]
        if payload is not None and payload.get("verdict") == "approved":
            return ("advance", "worker", "plan_review_approved")

        reason = (payload or {}).get("reason") if payload else None
        revisions = dict(
            self.run.read_status().get("planReviewRevisions") or {})
        used = int(revisions.get(str(approach), 0))
        revision_available = used < self.cfg.peer_plan_review_max_revisions

        def spend_revision(objection: str) -> tuple:
            revisions[str(approach)] = used + 1
            self.run.update_status(planReviewRevisions=revisions)
            self._pending_plan_review_objection = objection
            return ("advance", "planning",
                    "plan_review_objected_revision_available")

        if payload is None and revision_available:
            # Task 016 (requirement D.2, #117 half B): the peer named no
            # usable verdict at all -- no vote for either edge to weigh --
            # so the policy is the deterministic one disk state already
            # decides, never a router call (see the docstring above).
            errors_block = ("\n".join(f"- {e}" for e in parse_errors)
                            or "- (no parse errors recorded)")
            objection = (
                "the plan review hat's reply named no usable verdict; "
                "re-state the plan so a peer can judge it. The peer's raw "
                f"reply was:\n{raw_reply.strip()}\n\nParse errors:\n"
                f"{errors_block}")
            return spend_revision(objection)

        if revision_available:
            return spend_revision(
                reason or "plan review objected with no reason given")
        return ("advance", "worker", "plan_review_revision_bound_reached")

    async def _step_verdict_gate(self) -> tuple:
        """The `verdict_gate` state (task 020, requirement L, #101) --
        reachable only when `self.cfg.peer_verdict_gate` is on, and only
        once `review` itself has already emitted `verified` with no other
        `_terminal_success_blockers()` outstanding. Budget-charged like
        `plan_review` (real reasoning work over the finished approach, not
        bookkeeping -- the decision recorded in this task's own commit
        message and `artifacts/reports/task-020-requirement-l-rejection-
        semantics.md`).

        Genuinely BLIND (requirement L's own teeth): `run_iteration(
        "verdict_gate")` is called with NO `extra` argument at all, unlike
        `_run_review_iteration`'s `extra=self._review_extra_context()` --
        review's own reasoning/verdict/flagged-criteria context is never
        assembled for this call, and `build_prompt` itself carries nothing
        review-specific for any OTHER phase (job context, steering, the
        recent-steering echo -- none of it names review's verdict).
        `prompts/verdict_gate.md` states this explicitly, and
        `tests/test_peer_verdict_gate.py`'s blind-prompt test asserts a
        planted review verdict/reason string is absent from this exact
        call's rendered prompt.

        Parses the hat's `verdict_gate` contract payload
        (`transitions.HAT_SCHEMAS["verdict_gate"]`) exactly the way
        `_review_verdict` parses review's own: an absent/malformed payload,
        a schema violation, or a `rejected` verdict with no `reason`
        (`transitions.verdict_gate_report_errors`) is never a run-ending
        contract violation -- it defaults to the SAME safe choice an
        unparseable review reply already defaults to (not verified, i.e.
        rejected), never through `engine.router`: both of this hat's live
        edges are always decidable from its own `verdict` field alone, so
        there is no genuine choice left for the router to resolve (unlike
        `plan_review`'s undecidable-reply case, task 019, requirement M).

        `verified` -> `finishing` (`verdict_gate_approved`), the run's
        real success edge now sitting here instead of directly on
        `review`'s own `verified` edge. A `rejected` verdict (real, or the
        safe default above) folds the peer's own findings into a FRESH
        approach -- `_archive_approach`/`_write_composite_prd`, the exact
        machinery `review`'s own rejection already uses, so the peer's
        `verdict-gate-findings.md`/`.json` (task 019, requirement D.4,
        #125: its OWN fixed path, distinct from review's, written by the
        hat itself per `prompts/verdict_gate.md`) is what the next
        approach's composite PRD folds in, with authorship attributed --
        `replan` when an approach is still available (`verdict_gate_
        rejected_approach_left`), `finishing`
        (fail OPEN, never a deadlocked run) once the budget is spent
        (`verdict_gate_rejected_no_approach_left`). This is the DECIDED
        rejection semantics (a NEW approach, never a targeted worker
        fix-up): see this task's commit message and
        `artifacts/reports/task-020-requirement-l-rejection-semantics.md`
        for the reasoning, including who re-runs `review` -- the fresh
        approach's own ordinary `worker -> review` pass, automatically,
        with no bespoke re-invocation mechanism needed.
        """
        approach = self._approach
        if not self.budget_left():
            return ("tail",)
        self.run.emit("phase", phase="verdict_gate", approach=approach)
        result = await self.run_iteration(
            "verdict_gate", tier=state.run_level_verify_tier())
        approved, payload = await self._verdict_gate_verdict(result)
        self._record_gate_approach_verdict(approach, approved, payload)
        self._record_verdict_gate_peek(approach)
        if approved:
            self._advance("verdict_gate", "finishing", "verdict_gate_approved")
            self.run.emit("signal", signal="VERIFIED")
            self._finish("succeeded", self._review_success_verdict_label(),
                        **self._unconsumed_steering_patch())
            return ("done", "succeeded")
        reason = (payload or {}).get("reason") if payload else None
        self.run.emit(
            "log",
            message=(f"peer verdict gate rejected approach {approach}"
                     + (f": {reason}" if reason else
                        " (no usable verdict_gate payload)")))
        self._archive_approach(approach)
        self._write_composite_prd(approach)
        if approach < self.cfg.max_approaches:
            self._advance("verdict_gate", "replan",
                         "verdict_gate_rejected_approach_left")
            return ("advance", "replan", "verdict_gate_rejected_approach_left")
        self._advance("verdict_gate", "finishing",
                     "verdict_gate_rejected_no_approach_left")
        return ("tail",)

    async def _verdict_gate_verdict(self, result) -> tuple[bool, dict | None]:
        """Task 020 (requirement L, #101): the ONE parser of a
        `verdict_gate` hat reply, consulted by BOTH sites that can let the
        peer gate bless a run -- the ordinary `_step_verdict_gate` state
        and the off-budget `_grace_verdict_gate` call `_maybe_grace_review`
        makes (the same single-predicate discipline task 013 imposed on
        `_terminal_success_blockers`, for the same reason: two hand-rolled
        copies of a gate's parsing rules drift).

        Task 003 (requirement A.3, #119): the uncharged re-ask ladder
        every other contract site already has
        (`contract_enforcement.enforce_contract`) now lives INSIDE this
        parser itself, rather than a separate `_verdict_gate_contract_
        ladder` method a caller would have to remember to invoke first
        (the shape `_review_contract_ladder`/`_verify_contract_ladder`
        use). Pushing it down one level means BOTH call sites inherit the
        ladder the moment they `await` this method -- exactly the way both
        already inherit its safe default -- with no second copy of "call
        the ladder, then parse" for a future third call site to forget.
        This is why the method is now `async` (it was sync before this
        task): both call sites already `await`ed the coroutine that
        produced `result`, so making this one `await`able too is a
        one-line change at each site, not a new coroutine-plumbing
        problem.

        Skips the ladder entirely, going straight to today's parse, for a
        reply that is ALREADY a schema-valid dict clean of
        `verdict_gate_report_errors` -- a genuine `verified` verdict, or a
        `rejected` one with a `reason` -- there is nothing to repair, same
        exemption `_review_contract_ladder` and `_verify_contract_ladder`
        both apply. An absent/unparseable reply, a schema violation, or a
        `rejected` verdict with no `reason` gets one uncharged first
        re-ask that reuses THIS already-completed iteration's own final
        line (no extra hat call, no double-charge -- mirrors `_verify_
        contract_ladder`'s `first_attempt_text` reuse), then up to
        `cfg.contract_retry_max` further uncharged `_ask_contract_fix(
        "verdict_gate", ...)` re-asks. `result is None` (defensive; no
        current caller passes it) short-circuits to the safe default
        before any of that, since there is no final line to parse or
        repair.

        On ladder exhaustion this returns the exact SAME safe default it
        always has -- `approved=False`, `payload=None` -- never a
        run-ending contract violation and never a rewritten default: the
        ladder only ever gives a genuinely repaired payload a chance to be
        READ, it does not manufacture one.

        Returns `(approved, payload)`. `approved` is True only for a
        well-formed payload whose `verdict` is `verified`; an absent,
        unparseable or schema-violating reply, or a `rejected` verdict with
        no `reason` (`transitions.verdict_gate_report_errors`), is never a
        run-ending contract violation -- it collapses to the SAME safe
        default an unparseable `review` reply already gets (not verified),
        with `payload` None so the caller's log says "no usable
        verdict_gate payload" rather than inventing a reason.
        """
        if result is None:
            return False, None
        try:
            parsed = json.loads(last_nonblank_line(result.final_text))
        except (json.JSONDecodeError, TypeError, AttributeError):
            parsed = None
        if isinstance(parsed, dict) and not contracts.validate_payload("verdict_gate", parsed):
            if not transitions.verdict_gate_report_errors(parsed):
                approved = parsed.get("verdict") == "verified"
                return approved, parsed

        first_attempt_text = last_nonblank_line(result.final_text)
        event_loop = asyncio.get_running_loop()

        def respond(attempt: int, prior_errors: list[str] | None) -> str | None:
            if attempt == 0:
                return first_attempt_text
            raw, _usage = asyncio.run_coroutine_threadsafe(
                self._ask_contract_fix("verdict_gate", prior_errors or [],
                                       final_text=result.final_text),
                event_loop).result()
            return last_nonblank_line(raw)

        enforcement = await asyncio.to_thread(
            contract_enforcement.enforce_contract,
            "verdict_gate", respond,
            max_retries=self.cfg.contract_retry_max,
            state=self.run,
            extra_validate=transitions.verdict_gate_report_errors,
        )
        if not enforcement.ok:
            return False, None
        payload = enforcement.payload
        approved = payload.get("verdict") == "verified"
        return approved, payload

    def _record_gate_approach_verdict(self, approach: int, approved: bool,
                                      payload: dict | None) -> None:
        """Task 035 (requirement B.1): the peer verdict gate's half of
        `_record_approach_verdict` -- called from BOTH sites that can settle a
        gate verdict (`_step_verdict_gate`, `_grace_verdict_gate`), the same
        two `_record_verdict_gate_peek` is called from, strictly after
        `_verdict_gate_verdict` has returned.

        The gate's own structured sidecar is read here only as a SOURCE of
        blocking/residual detail, never as a precondition: an absent or
        unreadable `verdict_gate_findings_json_file` (the ordinary case for an
        approving gate, which writes no findings at all) still records the
        verdict, with explicit empty arrays and `findingsChannel: false`.
        """
        path = self.run.verdict_gate_findings_json_file
        items: list[dict] | None = None
        if path.exists():
            try:
                items = findings.read_findings_json(path)
            except ValueError:
                items = None
        self._record_approach_verdict(
            approach, "verdictGate",
            verdict="verified" if approved else "rejected",
            reason=(payload or {}).get("reason"), findings_list=items)

    def _record_verdict_gate_peek(self, approach: int) -> list[dict]:
        """Task 020 (D.5, #125 A): a post-hoc, honor-system check over the
        `verdict_gate` iteration that JUST FINISHED -- called from both of
        the peer gate's two call sites (`_step_verdict_gate`,
        `_grace_verdict_gate`) strictly AFTER `_verdict_gate_verdict` has
        already decided `approved`/`payload`, so nothing this method finds
        can reach back into that decision (see `state.
        detect_verdict_gate_peek`'s own docstring for why this ordering is
        the whole point, not incidental).

        `self.run.max_iteration_number()` names the iteration this call
        just wrote (its `meta.json` already carries `endedAt` by the time
        `run_iteration` returns) -- the SAME iteration `_verdict_gate_
        verdict`'s `result` came from, never a stale or future one, since
        the engine runs one iteration at a time.

        A clean iteration (the overwhelming, intended case) records
        nothing at all -- no event, no status write -- so an operator who
        never sees `verdictGatePeeks` on `GET /status` knows the honor
        system held for every gate call so far, not merely that nobody
        looked. A flagged one gets exactly one `verdict_gate_peek` event
        (`events.jsonl`) and one entry appended to the `verdictGatePeeks`
        list on the run's status document (`GET /status` surfaces it
        automatically, since that endpoint just augments `read_status()`).
        """
        number = self.run.max_iteration_number()
        flagged = state.detect_verdict_gate_peek(self.run.root, number)
        if not flagged:
            return flagged
        self.run.emit("verdict_gate_peek", approach=approach, iteration=number,
                      calls=flagged)
        existing = self.run.read_status().get("verdictGatePeeks") or []
        self.run.update_status(
            verdictGatePeeks=existing + [{"approach": approach,
                                          "iteration": number,
                                          "calls": flagged}])
        log.warning(
            "verdict_gate iteration %d (approach %d) read past its "
            "blindness contract: %s", number, approach, flagged)
        return flagged

    def _step_replan(self) -> tuple:
        """The `replan` state: the approach boundary as a transition with a
        guard (requirement B). `budget_and_approach_left` -> `planning`
        (bumping `self._approach`); `budget_or_approach_exhausted` ->
        `finishing`, which `_run_job_core` has no dispatch branch for on
        purpose -- reaching it IS the tail (see module docstring on why
        `finishing`/the terminal outcome are `run_job()`'s business, not
        this dispatch loop's).
        """
        # Task 026 (#61): a repeated STRUCTURAL blocker -- this approach's
        # own stagnation signature equalling its predecessor's -- ends the
        # run right here instead of consuming (and losing) another approach
        # to the identical wall. Guarded on an actual signature existing for
        # BOTH `self._approach` and `self._approach - 1`: a replan entered
        # for any other reason (review rejection, empty plan, ...) never
        # wrote one for `self._approach`, so this guard is silently absent
        # there by construction, not by a special case for those paths.
        status = self.run.read_status()
        signatures = status.get("blockerSignatures") or {}
        this_sig = signatures.get(str(self._approach))
        prev_sig = signatures.get(str(self._approach - 1))
        if this_sig is not None and this_sig == prev_sig:
            self._terminal_reason_note = (
                f"approach {self._approach} stagnated on the same "
                f"structural blocker as approach {self._approach - 1} "
                f"(#61): {this_sig}")
            # Task 012 (requirement C.2, #124): only an ingestion-origin
            # repeat -- BOTH approaches' signatures written by the
            # `empty_plan` boundary, never an ordinary worker-stagnation
            # (#61) repeat -- marks the run degraded. Checked against the
            # SAME `blockerSignatureOrigins` map `_record_stagnation_
            # signature` writes, keyed the same way as `blockerSignatures`
            # itself, so a mixed pair (one ingestion, one stagnation) is
            # never mistaken for either kind of pure repeat.
            origins = status.get("blockerSignatureOrigins") or {}
            if (origins.get(str(self._approach)) == "ingestion"
                    and origins.get(str(self._approach - 1)) == "ingestion"):
                self.run.update_status(health="degraded")
            self._advance("replan", "finishing", "repeated_blocker_signature")
            return ("tail",)
        if self._approach < self.cfg.max_approaches:
            self._approach += 1
            self._advance("replan", "planning", "budget_and_approach_left")
            return ("advance", "planning", "budget_and_approach_left")
        self._advance("replan", "finishing", "budget_or_approach_exhausted")
        return ("tail",)

    async def _step_worker(self) -> tuple:
        """The `worker` state, including the vigilant `verify` sub-cycle.

        Guards: `no_verdict_yet` (self-loop, stays inside this method's own
        `while` exactly like the pre-cutover code -- no state CHANGE
        happens on every ordinary iteration, so there is nothing for the
        dispatch loop above to revisit); `completed_task_pending_verification`
        / `verify_failed_retry_same_task` / `verify_passed_next_task` (the
        worker <-> verify round trip, validated through `self._advance()`
        but -- like the self-loop -- never surfaced to the dispatch loop,
        because the table declares verify's only successor as `worker`);
        `verdict_ready`, `blocked_with_failures` (-> review); and
        `stagnation_exhausted` (-> replan -- ledger guard 10 fails the
        approach directly, it does NOT get a review verdict; task 008's
        first draft of the table had this row wrong, corrected by this
        cutover against the runtime behaviour it now executes). Task 007
        (requirement D, #99) adds `doomed_plan_unverifiable` (-> review,
        same target as `blocked_with_failures` but a DIFFERENT guard and a
        different cause: the doomed set covers the entire actionable
        remainder, checked and routed before this pass's worker iteration
        is ever dispatched -- see `_doomed_plan_fault`'s own docstring).

        Task 025 (requirement H, #63) adds `unsatisfiable_targeted_replan`
        (self-loop, applied and audited exactly like the verify round trip
        above, the moment a worker's `verdict: "unsatisfiable"` report
        lands and other tasks remain actionable) and
        `unsatisfiable_and_nothing_left` (-> review, same reasoning as
        `blocked_with_failures` when nothing else is left to try).
        """
        approach = self._approach
        stagnant = 0
        verdict_ready = False
        guard_ready: str | None = None
        while self.budget_left():
            await self._gate()
            # Task 012 (requirement I, #98): unsettled operator steering is
            # ABSOLUTE TOP PRIORITY -- above a validation-failed retry
            # (guard 1 of this same while loop, computed a few lines below
            # via `scheduler.select_next_task`) and above any pending task
            # (guard 3). Checked FIRST, every pass, before
            # `scheduler.select_next_task` is ever called and before this
            # pass's would-be task claim is written -- so a pending steer
            # never loses a race to a task pick, and the rule holds
            # deterministically without consulting `engine.router` at all
            # (this is a hand-written `_step_*` guard, exactly like
            # `no_verdict_yet`/`verdict_ready` below, never one of
            # `_step_generic_hat`'s multi-edge states). `self.run.
            # pending_steering()` already returns every unsettled file in
            # arrival order (`RunDir.add_steering`'s sequence-numbered
            # names, sorted) -- the steer hat's own iteration (dispatched
            # by `_step_generic_hat` for the `steer` state declared in
            # `transitions.TABLE`) is handed that SAME list and settles all
            # of them in the one iteration this edge routes to, per the
            # PRD's "all pending steers in one iteration, in arrival
            # order." `ralphctl steer --now` (`POST /interrupt`) enqueues
            # its message via `RunDir.add_steering` before interrupting the
            # running iteration (`api.py`'s `interrupt` handler) -- so by
            # the time this pass re-enters `_step_worker`, `pending_
            # steering()` is non-empty and this same check forces the next
            # transition to `steer`, superseding the pre-task-012 behaviour
            # of simply falling back into `worker`'s own self-loop.
            if self.run.pending_steering():
                self._advance("worker", "steer", "steering_pending")
                return ("advance", "steer", "steering_pending")
            tasks_before = self.run.read_tasks()
            # Task 116 (#60 pillar 3, fact 9): the HARNESS -- not the agent
            # -- decides which task a worker iteration works on, through
            # the pure scheduler (task 115) applied to the plan as it
            # stands right now, and persists any pending/validation-failed
            # -> in-progress claim itself, strictly BEFORE the subprocess
            # below is spawned (`tests/test_worker_harness_owned_status.py`
            # asserts the on-disk status is already in-progress at the
            # moment `run_iteration` is invoked). An already-in-progress
            # pick (a resumed claim) is left untouched -- writing it again
            # would be a no-op read followed by an identical write, and
            # `before`'s snapshot below is taken AFTER this write so a
            # resumed run's very first pass through this loop does not
            # read as "progress" for the stagnation counter just because
            # the claim moved.
            selected = scheduler.select_next_task(
                tasks_before.get("tasks", []))
            assigned_id = selected.get("id") if isinstance(selected, dict) else None
            if selected is None:
                # Task 123 (#60): the harness -- not the agent, and not
                # `tests/stub-pi/pi`'s own emulation -- is the one place
                # that runs the dependency graph now (task 116 moved
                # `select_next_task` here), so it is also the one place
                # that notices a task can NEVER become unblocked (a
                # `dependsOn` naming a `failed`/`skipped` ancestor) and
                # says so, once, rather than asking worker.md's prompt or
                # the stub to notice it independently a second time. Only
                # written the first time this exact blocked set is seen
                # (by id) so a plan that stays blocked for several
                # iterations in a row (e.g. while the stagnation guard
                # counts down) does not grow notes.md once per iteration.
                blocked_by = state.task_blocked_by(tasks_before.get("tasks", []))
                # Task 007 (requirement D, #99): the doomed set can cover
                # the ENTIRE actionable remainder -- no worker iteration,
                # ever, could move this plan again -- rather than merely a
                # subset that still leaves other work reachable. That is a
                # DIFFERENT fact from "nothing is pickable THIS pass" (the
                # ordinary reason `selected` is None here, e.g. every
                # pending task's dependency is merely not finished YET) and
                # from "something failed but other work remains" (guard 9's
                # `blocked_with_failures`, `_unresolved_failures` below,
                # which is dependency-BLIND and reads a doomed `pending`
                # task as still actionable). Checked, and routed, BEFORE
                # this pass's worker iteration is ever dispatched -- unlike
                # `blocked_with_failures`, which still gives the worker one
                # real iteration to carve a residual task or self-skip, a
                # doomed plan has no such escape for ANY worker reply to
                # take, so spending even one iteration confirming that changes
                # nothing.
                doom_fault = self._doomed_plan_fault(tasks_before, blocked_by)
                if doom_fault is not None:
                    self.run.emit(
                        "log", level="error",
                        message=(
                            "the doomed set covers the entire actionable "
                            "remainder of this plan -- no worker iteration "
                            "could ever move it again; routing to review "
                            "for a verdict instead of three silent "
                            "no-progress iterations and a stagnation "
                            "verdict (task 007, requirement D, #99)"),
                        **doom_fault)
                    self._advance("worker", "review", "doomed_plan_unverifiable")
                    verdict_ready = True
                    guard_ready = "doomed_plan_unverifiable"
                    break
                if blocked_by:
                    new_ids = sorted(set(blocked_by) - self._blocked_notes_written)
                    if new_ids:
                        lines = [
                            f"task {tid} blocked: dependency "
                            f"{','.join(blocked_by[tid])} failed/skipped, "
                            "not completed"
                            for tid in new_ids]
                        with open(self.run.notes_file, "a") as f:
                            f.write("\n" + "\n".join(lines) + "\n")
                        self._blocked_notes_written.update(new_ids)
            claimed_from = None
            claimed_from_tier: str | None = None
            if isinstance(selected, dict) and selected.get("status") in (
                    "pending", "validation-failed"):
                old_status = selected.get("status")
                claimed_from = old_status
                for t in tasks_before.get("tasks", []):
                    if t.get("id") == assigned_id:
                        t["status"] = "in-progress"
                        if old_status == "validation-failed":
                            # Task 014 (requirement G): a validation-failed
                            # RE-ENTRY bumps the task's effective tier one
                            # step, capped at `hard` -- the harness's own
                            # response to the retry, never a hat's. Record
                            # the pre-escalation tier so an infra-fault
                            # rollback just below can give the bump back
                            # too, the same way it gives the claim back:
                            # nothing was actually attempted at the new
                            # rung, so nothing should look escalated.
                            claimed_from_tier = state.task_effective_tier(t)
                            state.escalate_task_effective_tier(t)
                        else:
                            state.claim_task_effective_tier(t)
                        break
                self.run.write_tasks(tasks_before)
                self._emit_task_status(assigned_id, old_status, "in-progress")
            before = json.dumps(tasks_before, sort_keys=True)
            # Task 021 (requirement N, #102): the ids already live in the
            # plan BEFORE this iteration ran -- fixed here, once, so the
            # `after` diff below (`state.tasks_doc_excluding_new_entries`)
            # can tell "a brand-new worker-origin task this pass created"
            # (excluded from the diff -- never counts as progress) from "an
            # EXISTING worker-origin task whose status just changed"
            # (included -- genuine progress, whether that task was created
            # this pass's own predecessor or any earlier one).
            before_task_ids = {
                t.get("id") for t in tasks_before.get("tasks", [])
                if isinstance(t, dict)
            }
            extra = self._worker_assignment_extra(selected)
            # Task 016 (requirement I/J): the in-hand task's EFFECTIVE tier
            # (post-escalation) drives this dispatch's model; a pass with
            # nothing actionable to assign (`selected` is not a task dict)
            # has no task tier to read and degrades to `tiers.DEFAULT_TIER`,
            # same as any other non-task dispatch.
            worker_tier = (state.task_effective_tier(selected)
                          if isinstance(selected, dict) else tiers.DEFAULT_TIER)
            result = await self._run_worker_iteration(extra, worker_tier)
            worker_iter_n = self.iterations_used  # captured after run_iteration increments it
            tasks_after = self.run.read_tasks()
            self._warn_if_batched(tasks_before, tasks_after)

            # Task 123 (#60): the pre-spawn `in-progress` claim just above
            # is the harness's OWN bet that this iteration will actually
            # work the task -- when the iteration never reached the model
            # at all (an infra fault: broken endpoint, no-traffic timeout,
            # or an instant zero-work startup failure -- all classify
            # `"infra"` per `faults.classify_fault`), the bet did not pay
            # off and the claim must be given back so the NEXT worker
            # iteration (this one, on retry, or a fresh approach) can pick
            # the same task up from `pending`/`validation-failed` again
            # instead of finding it stuck `in-progress` with nothing behind
            # that status. A genuine WORK fault (the agent reached the
            # model and failed doing the task) is left `in-progress` --
            # that is real, if unsuccessful, work attempted on it, and the
            # existing validation-failed/stagnation machinery already
            # handles that case. Only a claim made THIS pass is rolled
            # back (`claimed_from` is None for an already-in-progress pick
            # resumed from an earlier pass, which is left exactly as it
            # was found).
            if claimed_from is not None and self._last_fault_class == "infra":
                for t in tasks_after.get("tasks", []):
                    if t.get("id") == assigned_id and t.get("status") == "in-progress":
                        t["status"] = claimed_from
                        if claimed_from_tier is not None:
                            # Give the tier bump back too, same reasoning
                            # as the status rollback just above: an infra
                            # fault attempted nothing at the new rung.
                            t[state.TASK_EFFECTIVE_TIER_FIELD] = claimed_from_tier
                        self.run.write_tasks(tasks_after)
                        self._emit_task_status(assigned_id, "in-progress", claimed_from)
                        break

            # Task 025 (requirement H, #63) / task 116: a worker's FINAL
            # line may be a JSON `verdict` report -- "unsatisfiable" (a
            # task's criteria cannot be satisfied as written), "complete"
            # (the ASSIGNED task above is done -- task 116 folds the old
            # "agent edits tasks.json directly" channel, fact 9, into this
            # same reported-contract shape `_apply_reported_task_status`
            # already uses for every other hat) or "no_verdict" (still
            # working, nothing to apply). Parsed opportunistically: a
            # non-JSON last line (plain chatter, or the separate literal
            # `<promise>COMPLETE</promise>` whole-plan marker handled
            # further down, unchanged) or a JSON object with no `verdict`
            # key at all leaves the rest of this method's existing
            # behaviour completely untouched.
            worker_final_text = last_nonblank_line(result.final_text)
            try:
                payload = json.loads(worker_final_text)
            except (json.JSONDecodeError, TypeError):
                payload = None
            verdict = payload.get("verdict") if isinstance(payload, dict) else None
            proposed_split_tasks = (
                payload.get("proposed_tasks") if isinstance(payload, dict) else None)
            applied_this_iteration = False
            # Task 005 PART 1 (requirement A.1/A.2, #114, closes task 004's
            # residual gap -- operator steer 001): route EVERY worker
            # payload carrying a `petition` field through the declaration
            # schema + `transitions.worker_petition_report_errors` + the
            # SAME re-ask ladder every other worker semantic channel in
            # this method already uses -- INDEPENDENTLY of `verdict`'s own
            # value (a petition may ride alongside `"no_verdict"` just as
            # well as `"unsatisfiable"`/`"complete"`/`"plan_complete"`),
            # so a malformed declaration is re-asked and never filed, a
            # duplicate against an obligation already filed is refused and
            # never filed, and a valid one is filed EXACTLY ONCE, through
            # `petitions.file_worker_petition`, AFTER this ladder's own
            # validation succeeds -- never before. Runs BEFORE the
            # verdict-specific branches below so a petition riding beside
            # `"unsatisfiable"`/`"complete"`/`"plan_complete"` is filed (or
            # refused) before that verdict's own ladder ever starts.
            if isinstance(payload, dict) and "petition" in payload:
                event_loop = asyncio.get_running_loop()

                def respond(attempt: int, prior_errors: list[str] | None) -> str | None:
                    if attempt == 0:
                        return worker_final_text
                    raw, _usage = asyncio.run_coroutine_threadsafe(
                        self._ask_contract_fix("worker", prior_errors or [],
                            final_text=result.final_text),
                        event_loop).result()
                    return last_nonblank_line(raw)

                petition_enforcement = await asyncio.to_thread(
                    contract_enforcement.enforce_contract,
                    "worker", respond,
                    max_retries=self.cfg.contract_retry_max,
                    state=self.run,
                    extra_validate=lambda p: self._apply_worker_petition_declaration(p),
                )
                if petition_enforcement.ok:
                    stagnant = 0
                    if petition_enforcement.payload is not None:
                        payload = petition_enforcement.payload
                        verdict = payload.get("verdict")
                        worker_final_text = json.dumps(payload)
                # Ladder exhausted: falls through to the SAME ordinary
                # stagnation bookkeeping every other exhausted site in this
                # method already takes -- no `_advance` call, no
                # `self._abort_reason`, no early `return`. `enforce_
                # contract` already emitted the `contract.violation`/
                # `contract.agent_fault` events for the record.
            # Task 011 (requirement C.1, #123): the worker-side half of
            # task 009's sanctioned `failed -> skipped` escape. Task 010
            # rewrote worker.md to report that move through the
            # `task_status` contract channel ("carry {"task_status":
            # "skipped", "task_id": "<id>", "justification": "<text>"}
            # alongside your ordinary verdict in the same JSON reply")
            # instead of writing `tasks.json` by hand -- but until this
            # site existed `_step_worker` had NO application path for such
            # a report, so the escape the shipped prompt instructs was
            # unreachable in production and a `failed` task with a
            # dependent could still only be resolved by an operator
            # editing the record. Routed through exactly the same
            # `contract_enforcement.enforce_contract` ladder + `extra_
            # validate` semantic hook `_step_generic_hat`'s own
            # `task_status_extra_validate` (task 004, requirement F, #92
            # site 2) already uses for every other hat, and applied by the
            # SAME single applier (`_apply_reported_task_status` -> the
            # declared table -> `RunState.write_tasks`) -- never a second
            # mechanism, and never a direct write. Placed BEFORE the
            # verdict-specific branches and independent of `verdict`'s own
            # value, exactly like the `petition` side channel just above,
            # so the escape rides alongside `"no_verdict"` (the usual
            # case: the worker resolves a DIFFERENT, already-failed task
            # while its own assigned task continues) as readily as any
            # other verdict.
            if isinstance(payload, dict) and "task_status" in payload:
                event_loop = asyncio.get_running_loop()

                def respond(attempt: int, prior_errors: list[str] | None) -> str | None:
                    if attempt == 0:
                        return worker_final_text
                    raw, _usage = asyncio.run_coroutine_threadsafe(
                        self._ask_contract_fix("worker", prior_errors or [],
                            final_text=result.final_text),
                        event_loop).result()
                    return last_nonblank_line(raw)

                status_enforcement = await asyncio.to_thread(
                    contract_enforcement.enforce_contract,
                    "worker", respond,
                    max_retries=self.cfg.contract_retry_max,
                    state=self.run,
                    extra_validate=lambda p: self._apply_worker_task_status_report(
                        p, assigned_id),
                )
                if status_enforcement.ok:
                    if status_enforcement.payload is not None:
                        payload = status_enforcement.payload
                        verdict = payload.get("verdict")
                        worker_final_text = json.dumps(payload)
                        proposed_split_tasks = (
                            payload.get("proposed_tasks")
                            if isinstance(payload, dict) else None)
                    # The applier may have just written tasks.json; every
                    # branch below (and this pass's own stagnation diff)
                    # must see that write rather than the pre-report
                    # snapshot -- so an applied escape registers as the
                    # genuine plan progress it is, exactly like an applied
                    # `"complete"`/`"unsatisfiable"` report does.
                    tasks_after = self.run.read_tasks()
                # Ladder exhausted: same fall-through as the petition site
                # above -- nothing was applied, no `_advance`, no
                # `_abort_reason`, no early `return`; `enforce_contract`
                # already emitted the `contract.violation`/`contract.
                # agent_fault` events for the record.
            if verdict is not None:
              # Task 008 (requirement B.2, #128): the generic schema site
              # (the `elif schema_errors:` branch below) used to have no
              # way to re-dispatch a successfully-repaired payload into
              # the branch that actually owns its verdict -- "fact
              # 16/#65" in that branch's own pre-existing comment.
              # `reentries` bounds that re-dispatch to AT MOST ONE extra
              # pass through this inner loop per worker iteration --
              # never a second retry LADDER (each branch below still
              # owns its own `enforce_contract` ladder exactly as
              # before; this only lets the OUTCOME of the schema-site
              # ladder be re-routed once). `dispatch_outcome` carries
              # what the matched branch decided the ENCLOSING `while
              # self.budget_left():` loop should do -- `None` (fall
              # through to this pass's ordinary stagnation bookkeeping
              # below, unchanged), `"continue"`, or `"break"` -- applied
              # via a real `continue`/`break` placed OUTSIDE this inner
              # loop (directly in the outer loop's own body, a few
              # lines below) so it targets the OUTER loop, never this
              # one. Every branch below falls through to this loop's own
              # unconditional trailing `break` except the reentry path
              # itself, which `continue`s this inner loop instead.
              reentries = 0
              dispatch_outcome = None
              while True:
                schema_errors = contracts.validate_payload("worker", payload)
                # Task 021 (requirement N, #102): `proposed_tasks` is an
                # optional side channel, independent of which verdict this
                # reply also carries -- applied here, once, BEFORE the
                # verdict-specific branches below (each of which may re-read
                # `tasks_after` on its own), so a subsequent re-read in any
                # branch already sees whatever split this call accepted.
                # Only attempted when the whole payload is schema-valid: a
                # malformed `proposed_tasks` (wrong type) already falls into
                # the generic `elif schema_errors:` re-ask ladder below, same
                # as any other schema violation on this payload.
                if not schema_errors and proposed_split_tasks:
                    self._apply_worker_proposed_tasks(
                        proposed_split_tasks, assigned_id, approach)
                    tasks_after = self.run.read_tasks()
                if verdict == "unsatisfiable":
                    # v0.13 task 005 (#92 site 3): a schema violation or an
                    # illegal `_apply_unsatisfiable_report` (bad `task_id`,
                    # missing/blank `reason`) is no longer an immediate
                    # abort -- it is carried back to the SAME worker
                    # through `contract_enforcement.enforce_contract`'s
                    # uncharged `_ask_contract_fix` re-ask ladder, exactly
                    # like `_step_generic_hat`'s sites 1/2 (tasks 003/004).
                    # Attempt 0 reuses THIS already-completed work
                    # iteration's own final line -- no extra hat call, no
                    # double-charge.
                    event_loop = asyncio.get_running_loop()

                    def respond(attempt: int, prior_errors: list[str] | None) -> str | None:
                        if attempt == 0:
                            return worker_final_text
                        raw, _usage = asyncio.run_coroutine_threadsafe(
                            self._ask_contract_fix("worker", prior_errors or [],
                                final_text=result.final_text),
                            event_loop).result()
                        return last_nonblank_line(raw)

                    enforcement = await asyncio.to_thread(
                        contract_enforcement.enforce_contract,
                        "worker", respond,
                        max_retries=self.cfg.contract_retry_max,
                        state=self.run,
                        extra_validate=lambda p: self._apply_unsatisfiable_report(p),
                    )
                    if enforcement.ok:
                        self._record_worker_iteration_verdict(
                            worker_iter_n, "unsatisfiable", assigned_id)
                        tasks_after = self.run.read_tasks()
                        stagnant = 0
                        if any(task_is_actionable(t)
                               for t in tasks_after.get("tasks", [])):
                            self._advance("worker", "worker",
                                         "unsatisfiable_targeted_replan")
                            dispatch_outcome = "continue"
                        else:
                            verdict_ready = True
                            guard_ready = "unsatisfiable_and_nothing_left"
                            dispatch_outcome = "break"
                    # Task 005 (requirement C, qualified as task 003 settled
                    # it -- but here the ladder's exhaustion is NEVER read
                    # as the reason this run ends, budget or not, because
                    # this method's own `while self.budget_left():` loop
                    # already owns that decision independently a few lines
                    # below, exactly as it already does for an ordinary
                    # `"no_verdict"` reply): an exhausted ladder here takes
                    # the declared `T("worker", "no_verdict_yet")` self-loop
                    # -- no `_advance` call (undeclared/no-op, same as the
                    # ordinary no-verdict path just below), no
                    # `self._abort_reason`, no early `return`. It simply
                    # falls through to this pass's ordinary stagnation
                    # bookkeeping below, exactly as a genuine `"no_verdict"`
                    # reply would -- `enforce_contract` already emitted the
                    # `contract.violation`/`contract.agent_fault` events for
                    # the record.
                elif verdict == "complete":
                    # v0.13 task 006 (#92 site 4): same shape as site 3
                    # (task 005, unsatisfiable) -- a schema violation or an
                    # illegal `_apply_worker_completion_report` (task_id
                    # naming a DIFFERENT task than the one the harness
                    # assigned, or no task assigned this iteration at all)
                    # is no longer an immediate abort; it is carried back
                    # to the SAME worker through `contract_enforcement.
                    # enforce_contract`'s uncharged `_ask_contract_fix`
                    # re-ask ladder, exactly like sites 1-3. Attempt 0
                    # reuses THIS already-completed work iteration's own
                    # final line -- no extra hat call, no double-charge.
                    event_loop = asyncio.get_running_loop()

                    def respond(attempt: int, prior_errors: list[str] | None) -> str | None:
                        if attempt == 0:
                            return worker_final_text
                        raw, _usage = asyncio.run_coroutine_threadsafe(
                            self._ask_contract_fix("worker", prior_errors or [],
                                final_text=result.final_text),
                            event_loop).result()
                        return last_nonblank_line(raw)

                    enforcement = await asyncio.to_thread(
                        contract_enforcement.enforce_contract,
                        "worker", respond,
                        max_retries=self.cfg.contract_retry_max,
                        state=self.run,
                        extra_validate=lambda p: (
                            transitions.worker_completion_report_errors(p)
                            or self._apply_worker_completion_report(
                                p, assigned_id)),
                    )
                    if enforcement.ok:
                        self._record_worker_iteration_verdict(
                            worker_iter_n, "complete", assigned_id)
                        tasks_after = self.run.read_tasks()
                        stagnant = 0
                        applied_this_iteration = True
                    # Re-derived for `complete` (the open question tasks
                    # 005/006's map left unresolved): `applied_this_
                    # iteration` simply stays False on exhaustion --
                    # nothing was actually applied to tasks.json, so the
                    # grace-review/`_outstanding_for_completion` block
                    # below (gated on `applied_this_iteration`) is
                    # correctly skipped, same as it already is for a
                    # `"no_verdict"` reply. No `_advance` call, no
                    # `self._abort_reason`, no early `return` -- this pass
                    # falls through to the SAME ordinary stagnation
                    # bookkeeping further down that a genuine
                    # `"no_verdict"` reply already takes, and this
                    # method's own enclosing `while self.budget_left():`
                    # loop still owns what happens once budget runs out,
                    # exactly as task 005 reasoned for site 3.
                    # `enforce_contract` already emitted the
                    # `contract.violation`/`contract.agent_fault` events
                    # for the record.
                elif verdict == "plan_complete":
                    # Task 015 (requirement O, #100): the LAST untyped
                    # claim -- "nothing was assigned to me and every task
                    # is already terminal", formerly only sayable through
                    # the literal `<promise>COMPLETE</promise>` marker --
                    # is now a contract value, refused on exactly the same
                    # evidence `honoured_complete` used to cross-check the
                    # marker against (`_apply_worker_plan_complete_report`
                    # below): no task assigned THIS iteration (a
                    # structural check -- a worker that was handed real
                    # work has no business claiming the WHOLE plan is
                    # done in the same breath) and nothing outstanding in
                    # the freshly re-read plan (the semantic check,
                    # `_outstanding_for_completion`). Same ladder shape as
                    # "unsatisfiable"/"complete" above: a schema violation
                    # or an illegal claim is carried back to the SAME
                    # worker through `contract_enforcement.enforce_
                    # contract`'s uncharged `_ask_contract_fix` re-ask
                    # ladder rather than read as silence. Attempt 0 reuses
                    # THIS already-completed work iteration's own final
                    # line -- no extra hat call, no double-charge.
                    event_loop = asyncio.get_running_loop()

                    def respond(attempt: int, prior_errors: list[str] | None) -> str | None:
                        if attempt == 0:
                            return worker_final_text
                        raw, _usage = asyncio.run_coroutine_threadsafe(
                            self._ask_contract_fix("worker", prior_errors or [],
                                final_text=result.final_text),
                            event_loop).result()
                        return last_nonblank_line(raw)

                    enforcement = await asyncio.to_thread(
                        contract_enforcement.enforce_contract,
                        "worker", respond,
                        max_retries=self.cfg.contract_retry_max,
                        state=self.run,
                        extra_validate=lambda p: self._apply_worker_plan_complete_report(
                            p, assigned_id, tasks_after),
                    )
                    if enforcement.ok:
                        # Nothing was, or could be, applied to tasks.json
                        # by this verdict (unlike "complete"/"unsatisfiable"
                        # above) -- the claim IS the whole-plan conclusion,
                        # not a step towards one, so this ends the worker
                        # loop directly rather than falling through to the
                        # ordinary stagnation bookkeeping below (matching
                        # the retired marker's own `honoured_complete`
                        # break, just moved onto the contract-verified
                        # path instead of a raw string match).
                        self._record_worker_iteration_verdict(
                            worker_iter_n, "plan_complete", None)
                        verdict_ready = True
                        guard_ready = "verdict_ready"
                        self.run.emit("signal", signal="COMPLETE")
                        dispatch_outcome = "break"
                    # Ladder exhausted: falls through to the SAME ordinary
                    # stagnation bookkeeping every other exhausted site
                    # already takes -- no `_advance` call, no early
                    # `return`, `self.budget_left()` still owns when this
                    # run actually ends. `enforce_contract` already
                    # emitted the `contract.violation`/`contract.
                    # agent_fault` events for the record.
                elif schema_errors:
                    # v0.13 task 007 (#92 site 5, the LAST cutover): a
                    # `verdict` the schema itself rejects -- not
                    # "unsatisfiable" or "complete" above, and not the
                    # valid "no_verdict" either (an unknown enum value,
                    # an unexpected extra field, ...) -- is no longer an
                    # immediate abort; it goes through the exact same
                    # `contract_enforcement.enforce_contract` re-ask
                    # ladder as sites 1-4. Attempt 0 reuses THIS already-
                    # completed work iteration's own final line -- no
                    # extra hat call, no double-charge. No
                    # `extra_validate` here: this branch's only failure
                    # mode IS the schema check `enforce_contract`'s own
                    # `parse_response` already performs -- unlike
                    # "unsatisfiable"/"complete" there is no semantic
                    # task-application layered on top of it, so there is
                    # nothing further for this branch to apply even on a
                    # successful repair: a corrected reply is treated
                    # exactly like an ordinary "no_verdict" reply (nothing
                    # to apply this iteration) rather than re-dispatched
                    # into the unsatisfiable/complete branches above,
                    # which this generic site has no way to re-enter
                    # without a second retry loop (fact 16/#65).
                    event_loop = asyncio.get_running_loop()

                    def respond(attempt: int, prior_errors: list[str] | None) -> str | None:
                        if attempt == 0:
                            return worker_final_text
                        raw, _usage = asyncio.run_coroutine_threadsafe(
                            self._ask_contract_fix("worker", prior_errors or [],
                                final_text=result.final_text),
                            event_loop).result()
                        return last_nonblank_line(raw)

                    enforcement = await asyncio.to_thread(
                        contract_enforcement.enforce_contract,
                        "worker", respond,
                        max_retries=self.cfg.contract_retry_max,
                        state=self.run,
                    )
                    # Task 008 (requirement B.2, #128): a REPAIR here --
                    # `enforcement.ok` and this is still the first pass
                    # (`reentries < 1`) -- is re-dispatched into the SAME
                    # if/elif chain above, once, with the repaired
                    # payload/verdict in hand, so a repaired `"complete"`/
                    # `"unsatisfiable"`/`"plan_complete"` is applied by the
                    # branch that actually owns it (each of those branches
                    # keeps its own cross-checks -- this does not bypass
                    # any of them). `proposed_split_tasks` is re-derived
                    # from the repaired payload too, so a split proposal
                    # carried on the CORRECTED reply is still honoured
                    # (it could not have been on the first pass -- that
                    # pass's `payload` was schema-invalid, so the
                    # `not schema_errors and proposed_split_tasks` guard
                    # above never fired for it). Bounded to exactly one
                    # re-dispatch: `reentries` is never incremented a
                    # second time, so a repaired payload that is ITSELF
                    # schema-invalid (defensive only -- `enforce_contract`
                    # already validated the schema before setting `.ok`)
                    # cannot loop. Ladder EXHAUSTION here (`not
                    # enforcement.ok`) takes the unconditional trailing
                    # `break` below instead -- no `_advance` call, no
                    # `self._abort_reason`, no early `return` -- falling
                    # through to the SAME ordinary stagnation bookkeeping
                    # every other exhausted site already takes, exactly as
                    # tasks 005/006 reasoned for sites 3/4's own
                    # exhaustion path. `enforce_contract` already emitted
                    # the `contract.violation`/`contract.agent_fault`
                    # events for the record either way.
                    if (enforcement.ok and reentries < 1
                            and enforcement.payload is not None):
                        reentries += 1
                        payload = enforcement.payload
                        verdict = payload.get("verdict")
                        worker_final_text = json.dumps(payload)
                        proposed_split_tasks = (
                            payload.get("proposed_tasks")
                            if isinstance(payload, dict) else None)
                        continue
                # verdict == "no_verdict": nothing to apply this iteration.
                break
              self._last_worker_dispatch_reentries = reentries
              if dispatch_outcome == "continue":
                  continue
              if dispatch_outcome == "break":
                  break

            if self.cfg.vigilant:
                # Task 020 (requirement F bullet 4): the vigilant claim
                # state IS `awaiting-validation` on disk now -- the harness
                # relabels a worker's raw `completed` write the moment this
                # iteration's subprocess exits, strictly before anything
                # else below reads tasks.json again, so "completed" never
                # rests as vigilant's terminal even for one extra read.
                # This is what used to need the separate `vigilant-
                # verified.json` sidecar (task 052/#29): a crash between a
                # worker iteration's completion write and the corresponding
                # verify iteration used to leave both processes' first
                # tasks.json snapshot reading identically "completed", so a
                # same-process before/after diff couldn't tell "needs
                # verification" from "already verified" across the crash
                # boundary -- only a second, disk-persisted record could.
                # Now the record IS the task itself: a crash leaves the
                # task exactly at `awaiting-validation` (never advanced to
                # `validated`, which only `_verify_task`'s own passing
                # write can do), so guard 4's property -- resume finds it
                # and verifies it -- holds from the task's own `status`
                # field, asserted from disk state, with no sidecar and no
                # before/after diff at all.
                tasks_after = self._relabel_vigilant_claims(tasks_after)
                pending_verify = [
                    t for t in tasks_after.get("tasks", [])
                    if t.get("status") == "awaiting-validation"
                ]
                if pending_verify:
                    # Task 005 PART 2 (requirement A.2, #114): the bounded
                    # mid-run adjudication episode, when one is due, runs
                    # HERE -- strictly before this pass's own
                    # `_verify_task` calls, i.e. before every one of these
                    # tasks' NEXT validation attempt.
                    await self._maybe_run_midrun_adjudication_episode()
                    self._advance("worker", "verify",
                                 "completed_task_pending_verification")
                    any_failed = False
                    for task in pending_verify:
                        if self.budget_left():
                            await self._gate()
                            ok = await self._verify_task(task)
                            any_failed = any_failed or not ok
                    self._advance(
                        "verify", "worker",
                        "verify_failed_retry_same_task" if any_failed
                        else "verify_passed_next_task")
                # Re-read after any verification-driven status updates
                tasks_after = self.run.read_tasks()

            # Task 008: a worker that rewrites a task's successCriteria
            # after that task has already failed verification at least
            # once is quietly moving the bar instead of doing the work --
            # flag it persistently so review (task 009) can demand
            # independent re-verification of the new text.
            self._check_criteria_edits(tasks_after)

            if applied_this_iteration:
                # Task 116: a "complete" verdict just applied is definite
                # progress -- `stagnant` is already reset above -- and
                # whether the WHOLE run is now done is the harness's own
                # question to answer from the record it just wrote, not
                # something the agent has to notice or re-assert with a
                # separate marker (contrast the `verdict: "plan_complete"`
                # branch above, task 015/#100, which exists for exactly
                # the OTHER case -- nothing was assigned this iteration at
                # all, so there is no per-task application here to fold
                # the check into). `_outstanding_for_completion` is the
                # same check that branch cross-checks its own claim
                # against; here it is the sole decision, made directly
                # from state.
                if not self._outstanding_for_completion(tasks_after):
                    verdict_ready = True
                    guard_ready = "verdict_ready"
                    self.run.emit("signal", signal="COMPLETE")
                    break
                continue

            if self._check_instant_failure(result, self.iterations_used):
                break
            if self._instant_failure_streak:
                # An instant startup/infra failure that hasn't yet hit the
                # abort threshold: must not count as either progress or
                # no-progress evidence (task 059) -- skip stagnation
                # bookkeeping entirely this pass.
                continue
            if self._last_fault_class in ("infra", "signal"):
                # Task 117 (steering 005): an iteration whose own fault class
                # is `"infra"` or `"signal"` reached this line either because
                # the retry wrapper gave up on it (outage budget spent or
                # `infra_retry_max` reached -- see
                # `_run_iteration_with_infra_retry`) or because it never was
                # retried in the first place (`"signal"` is never retried).
                # Neither is evidence about whether THIS PLAN is stuck: the
                # agent never got a fair shot at the task (infra) or was cut
                # off before it could fail on its own (signal). Skipping
                # stagnation bookkeeping entirely this pass -- exactly like
                # the `_instant_failure_streak` carve-out just above, for the
                # same underlying reason -- means the counter is neither
                # incremented NOR reset: it holds exactly where it was,
                # because "nothing was learned this pass" is not the same
                # claim as "progress was made this pass". The regression this
                # fixes: three genuine provider stream-aborts (94/100/101 of
                # run selfdev-v09-fsm-loop) were misclassified `"work"` by a
                # since-retired duration bound and failed an approach through
                # this exact guard while nothing about the plan was stuck.
                continue

            after = json.dumps(
                state.tasks_doc_excluding_new_entries(
                    tasks_after, before_task_ids, origin=state.TASK_ORIGIN_WORKER),
                sort_keys=True)
            # Task 021 (requirement N, #102): the view above excludes any
            # task `_apply_worker_proposed_tasks` inserted THIS pass from
            # the diff `stagnant` below is computed over -- proposing a
            # split is a `tasks.json` change like any other, and without
            # this exclusion it would register as progress every single
            # pass, resetting `stagnant` to 0 forever and giving a worker
            # that never does anything BUT propose splits an unlimited
            # escape from the 3-iteration stagnation guard
            # (`tests/test_worker_split_proposal.py` pins exactly this).
            # Task 015 (requirement O, #100) retired the literal
            # `<promise>COMPLETE</promise>` marker as an authoritative
            # decision here: `IterationResult.saw_complete` is now purely
            # an operator-facing diagnostic (recorded in `meta.json`/
            # `iteration.end`, same demotion `saw_verified` already had --
            # `_review_verdict`'s docstring), never consulted below. The
            # whole-plan conclusion the retired marker used to make ("the
            # plan is done, refused unless `_outstanding_for_completion`
            # agrees") is now made exclusively by the `verdict:
            # "plan_complete"` contract branch above -- which `break`s
            # out of this loop directly on success, so code below this
            # point never has a completion claim to cross-check at all.
            # Task 117 (steering 005): the `self._last_fault_class in
            # ("infra", "signal")` gate a few lines above already excludes
            # those iterations from reaching this line at all (`continue`),
            # so this is unchanged from before that task -- it never
            # needed to know about fault class itself, only about whether
            # tasks.json moved.
            stagnant = stagnant + 1 if (before == after
                                        and not result.interrupted) else 0
            blocked = self._unresolved_failures(tasks_after)
            if stagnant and blocked:
                # Task 024 (#33): the plan holds no task a worker iteration
                # could still pick up, and at least one is `failed` -- so
                # the worker CANNOT signal COMPLETE (worker.md recognises
                # only completed/skipped) and it just spent a whole
                # iteration without changing tasks.json. Before this, the
                # run sat here until the stagnation guard failed the
                # approach and the next one replanned the whole wave
                # against an already-finished repo; the only escape was an
                # operator editing the record by hand (the `043d`
                # incident). Route to review instead: entry to review is
                # the engine's decision, and a verdict on work-with-a-hole
                # is worth strictly more than a replan. It is still only
                # entry -- the reviewer decides whether the run succeeded.
                #
                # Why gated on `stagnant` rather than firing the moment a
                # task fails: the worker's two legitimate resolutions
                # (carve the residual gap into a NEW task, or relabel
                # `skipped` with an honest justification -- worker.md's
                # completion-signal section) both need one iteration to
                # apply, and either one makes tasks.json change. So the
                # worker always gets that iteration; only a worker that
                # demonstrably did nothing with it hands the run to review.
                names = ", ".join(
                    f"{t['id']} ({task_failure_kind(t)})" for t in blocked)
                self.run.emit(
                    "log", level="warning",
                    message=(
                        f"no task left for a worker iteration to act on "
                        f"and {len(blocked)} failed: {names}; routing to "
                        "review for a verdict rather than stagnating "
                        "(task 024, #33)"))
                verdict_ready = True
                guard_ready = "blocked_with_failures"
                break
            if stagnant >= 3:
                # Task 025 (requirement H, #63): name what is left outstanding
                # ON THE EVENT PAYLOAD, not just folded into a message string
                # a reader has to parse -- the pre-existing message alone did
                # not say which tasks were still open, "which is why the
                # operator could not see the cause in the log" (PRD
                # requirement H).
                outstanding_tasks = [
                    {"id": t.get("id"), "status": t.get("status", "pending")}
                    for t in tasks_after.get("tasks", []) if isinstance(t, dict)
                ]
                names = ", ".join(
                    f"{o['id']} ({o['status']})" for o in outstanding_tasks)
                self.run.emit(
                    "log", level="error",
                    message=("3 iterations with no task progress; failing "
                              "approach"
                              + (f" (outstanding: {names})" if names else "")),
                    outstandingTasks=outstanding_tasks)
                # Task 026 (#61): persist THIS approach's blocker signature
                # before the approach boundary -- `_step_replan` (the guard
                # predicate the PRD asks for) reads it back, keyed by
                # approach number, to tell a repeated structural wall from
                # a distinct one on the next approach. Written here rather
                # than in `_step_replan` because only this call site has
                # the failing iteration's own `result` in hand.
                self._record_stagnation_signature(
                    approach, self._blocker_signature(result))
                break

        if not self.budget_left():
            # Task 002: a job whose worker loop ran out of budget with
            # every task already completed -- whether or not the worker
            # happened to signal COMPLETE on that final iteration -- gets
            # exactly one off-budget grace review rather than going
            # terminal failed/unverified with all work done and no
            # reviewer ever having looked at it. Checked BEFORE
            # `verdict_ready` below on purpose, matching the pre-cutover
            # ordering exactly: an honoured COMPLETE that lands in the same
            # instant the budget runs out still goes through the grace
            # path, not straight to a real review.
            if await self._maybe_grace_review(approach):
                return ("done", "succeeded")
            return ("tail",)
        if not verdict_ready:
            self._archive_approach(approach)
            return ("advance", "replan", "stagnation_exhausted")
        return ("advance", "review", guard_ready)

    def _blocker_signature(self, result: IterationResult) -> str | None:
        """A stable signature for a stagnating worker's blocker (#61),
        borrowing `_instant_failure_signature`'s normalisation shape (case-
        folded, digits stripped, whitespace collapsed, truncated) so the
        SAME structural wall reported in slightly different wording across
        attempts still compares equal, while a genuinely different blocker
        does not. Derived from the final stagnant worker iteration's own
        text (`final_text`, falling back to `error_message`) because that
        is the only evidence at the stagnation boundary that has a chance
        of surviving a replan -- tasks.json's own ids and titles do not (a
        fresh plan invents new ones).

        None when there is no usable text at all -- an absent signature
        never matches anything (including another absent one), so a
        content-free stagnation never triggers the `repeated_blocker_
        signature` guard by coincidence.
        """
        text = (result.final_text or result.error_message or "").strip()
        if not text:
            return None
        return self._normalize_blocker_signature_text(text)

    @staticmethod
    def _normalize_blocker_signature_text(text: str) -> str:
        """The normalisation shared by every blocker-signature site --
        case-folded, digits stripped, whitespace collapsed, truncated --
        factored out of `_blocker_signature` (task 026, #61) so the ingestion
        signature (task 012, requirement C.2, #124) compares on the exact
        same rule instead of a hand-copied second implementation drifting
        from it later.
        """
        return re.sub(r"\s+", " ", re.sub(r"[0-9]+", "N", text.lower()))[:500]

    def _ingestion_blocker_signature(self, errors: list[str]) -> str | None:
        """A stable signature for a plan-ingestion exhaustion (task 012,
        requirement C.2, #124), mirroring `_blocker_signature`'s shape for
        worker stagnation: `ingestion.errors` is already a stable
        deduplicated list (plan_ingestion's own contract), so joining and
        normalising it the same way makes the SAME lint failure across two
        approaches compare equal while genuinely distinct errors do not.
        None when there is no usable text at all, for the same reason
        `_blocker_signature` returns None on empty text: an absent signature
        never matches anything, including another absent one.
        """
        text = "; ".join(errors).strip()
        if not text:
            return None
        return self._normalize_blocker_signature_text(text)

    def _record_stagnation_signature(self, approach: int,
                                     signature: str | None,
                                     origin: str = "stagnation") -> None:
        """Persist `signature` for `approach` on status.json's
        `blockerSignatures` map -- disk, not memory, so the signature
        survives both the approach boundary and a process restart (#61:
        "today nothing does"). `_step_replan`'s guard reads this same map
        back to compare `self._approach` against `self._approach - 1`.

        A None signature is simply not recorded: absence at `self._approach`
        already reads as "never stagnated with usable text", which is
        exactly the fact a missing entry should carry -- no separate null
        marker needed.

        `origin` (task 012, requirement C.2, #124) is recorded in the
        parallel `blockerSignatureOrigins` map so `_step_replan` can tell
        an ingestion-exhaustion repeat from an ordinary #61 worker-
        stagnation repeat -- the two share the same map and guard by
        design (the reuse the PRD asks for), but only a repeat where BOTH
        approaches' signatures originated from ingestion failure should
        ever mark the run `health="degraded"`; an ordinary stagnation
        repeat keeps `health="ok"` exactly as it did before this task.
        """
        if signature is None:
            return
        status = self.run.read_status()
        signatures = dict(status.get("blockerSignatures") or {})
        signatures[str(approach)] = signature
        origins = dict(status.get("blockerSignatureOrigins") or {})
        origins[str(approach)] = origin
        self.run.update_status(blockerSignatures=signatures,
                               blockerSignatureOrigins=origins)

    def _review_findings_channel(self) -> tuple[list[dict] | None, list[str]]:
        """Task 002 (requirement A.2, #110): read the structured findings
        sidecar (`engine/findings.py`, task 001) for the review just run and
        report BOTH halves of its state, because they mean opposite things:

        * `(None, [])` -- there is no findings channel for this run AT ALL
          (`review.md` has not been updated to always produce one; that is
          task 004's scope). Nothing exists to check the payload's claimed
          verdict against, so every caller falls back to trusting that
          claim, exactly as review always has.
        * `(list, [])` -- a channel exists and every finding in it
          re-validated through the one schema engine.
        * `(None, [errors])` -- a channel EXISTS on disk but does not
          re-validate (`findings.read_findings_json` raised: truncated,
          hand-edited, or -- the case a validation attempt on this task
          actually caught -- partially malformed, one good `severity:
          "blocking"` finding beside one finding missing `evidence`).

        That third case must never be collapsed into the first. Treating an
        unreadable-but-present channel as "no channel" fails OPEN: the
        blocking finding sitting in the file is invisible, and a `verdict:
        "verified"` payload contradicting it is accepted instead of refused.
        So the errors are returned as CONTRACT errors -- `_review_verdict`
        folds them into `contract_errors` (a payload can never earn
        `verified` while its own findings channel is unreadable) and
        `_review_contract_ladder` re-asks over them through
        `contract_enforcement.enforce_contract`, re-reading the channel on
        every attempt so the hat can genuinely repair the sidecar. Still
        never a run-ending crash: an unrepaired channel ends as an ordinary
        refused verdict, never a rewritten one.

        Task 011 (requirement C.4, #112): before returning a present,
        valid list, downgrade every `severity: "blocking"` finding whose
        `clause` matches a run-scope CONFIRMED petition adjudication
        (`engine.petitions.downgrade_confirmed_findings`) to `"residual"`
        -- "a CONFIRMED petition removes its obligation from the blocking
        set". This is the ONE place that downgrade happens, so every
        caller (`_review_verdict`, `_review_contract_ladder`,
        `_review_incurable_rejection`) sees the same post-downgrade list
        without re-deriving the rule -- exactly the discipline `compute_
        verdict`/`all_blocking_findings_incurable` already document for
        themselves. A tampered adjudication record (`engine.petitions.
        confirmed_obligations`'s second return value) is treated as an
        EMPTY confirmed set here -- fail towards the all-blocking default,
        never towards trusting a record that might have been edited to
        manufacture a false exemption; `_petition_adjudications_tamper_
        context` is what surfaces that fact to review itself.

        Task 012 (requirement C.1, #112): after the downgrade, APPEND one
        engine-detected `severity: "blocking"` finding per obligation with
        more than one filed petition under this run's workspace
        (`engine.petitions.duplicate_petition_findings`) -- a duplicate
        filing, or a re-filed petition whose earlier filing was already
        REFUTED (`docs/petition-format.md`). This is unconditional: the
        engine adds it whether or not review's own findings.json mentioned
        it, so the fact is reported on the engine/prompt surface rather
        than resting on a run's own prose ever having noticed it. Only
        reached when a base channel exists (the `path.exists()` guard
        above) -- by task 004 review.md always produces one, so this is
        not a live gap in practice.

        Task 024 (requirement E.3, #116 section 2): APPENDs, the same
        unconditional way, one more engine-detected `severity: "blocking"`
        finding per task carrying a truthy `criteriaEditedAfterValidation
        Failure` marker (`findings.flagged_criteria_findings`) -- the
        engine-enforced half of the criteria-tamper authority boundary
        task 008/009 only surfaced to review as a prompt instruction --
        PERSISTS it into the sidecar (`_record_flagged_criteria_findings`,
        so the blocking finding is a record on disk and not just this
        return value), and reports it EVEN IN the no-channel-at-all first
        case above: a missing review sidecar must not be a way to get a
        `verified` claim over a criteria rewrite trusted.
        """
        path = self.run.findings_json_file
        # Task 024 (requirement E.3, #116 section 2): the engine-forced
        # blocking finding for a criteria-tamper marker is derived BEFORE
        # the no-channel early return below, because an absent channel
        # must not be an escape hatch from it: with review's own sidecar
        # missing, `(None, [])` would make every caller trust a `verdict:
        # "verified"` claim over a task whose criteria a worker or planner
        # rewrote. So when any task is flagged, this method reports a
        # findings LIST (the engine's own finding) even with no review
        # channel on disk at all -- failing towards refusal, the same
        # direction the present-but-unreadable case already fails.
        flagged = findings.flagged_criteria_findings(
            self.run.read_tasks().get("tasks", []))
        if flagged:
            self._record_flagged_criteria_findings(flagged)
        if not path.exists():
            return (flagged, []) if flagged else (None, [])
        try:
            findings_list = findings.read_findings_json(path)
        except ValueError as exc:
            return None, [
                "findings: a structured findings channel is present but does "
                f"not validate, so the verdict cannot be checked against it: {exc}"]
        confirmed, tampered = petitions.confirmed_obligations(self.run)
        if tampered:
            confirmed = set()
        downgraded = petitions.downgrade_confirmed_findings(findings_list, confirmed)
        # Task 024: append the flagged-criteria findings derived above --
        # unconditionally, exactly like the duplicate-petition append just
        # below -- so a review payload's own `verdict: "verified"` claim
        # over a worker/planner criteria rewrite is refused by the same
        # contradiction check, never merely surfaced as a prompt
        # instruction it can ignore. Deduplicated by `clause` against the
        # channel already on disk, because `_record_flagged_criteria_
        # findings` PERSISTS these into that same sidecar: without this
        # filter, every later read of the channel would report the same
        # engine finding twice.
        seen = {f.get("clause") for f in downgraded}
        return (downgraded + petitions.duplicate_petition_findings(self.workspace)
                + [f for f in flagged if f.get("clause") not in seen]), []

    def _record_flagged_criteria_findings(self, flagged: list[dict]) -> None:
        """Task 024 (requirement E.3, #116 section 2): PERSIST the
        engine-forced criteria-tamper findings into review's own structured
        findings sidecar (`RunDir.findings_json_file`) and witness each
        newly recorded one with its own `review.criteria_tamper_finding`
        event.

        Without this, the engine's finding would exist only inside one
        in-memory return value: the run would refuse the contradicting
        `verified` claim but leave NO record of why on disk, and an
        operator reading the findings channel afterwards would see an
        empty list. Recording it makes the blocking finding part of the
        same channel every other finding is read from.

        Idempotent, and deliberately narrow:
        * an entry whose `clause` is already present in the channel is
          neither re-appended nor re-announced, so the re-ask ladder's
          repeated reads (and repeated review passes) cannot grow the file
          or spam the event log;
        * a channel that exists but does not validate is left completely
          untouched -- `_review_findings_channel`'s own third case already
          refuses that payload, and overwriting a malformed channel would
          destroy the very evidence a re-ask has to repair;
        * a channel that does not exist yet is created holding exactly
          these findings.
        """
        if not flagged:
            return
        path = self.run.findings_json_file
        existing: list[dict] = []
        if path.exists():
            try:
                existing = findings.read_findings_json(path)
            except ValueError:
                return
        seen = {f.get("clause") for f in existing}
        fresh = [f for f in flagged if f.get("clause") not in seen]
        if not fresh:
            return
        findings.write_findings_json(path, existing + fresh)
        for f in fresh:
            self.run.emit(
                "review.criteria_tamper_finding", severity=f.get("severity"),
                clause=f.get("clause"), evidence=f.get("evidence"),
                curable=f.get("curable"))

    def _review_findings(self) -> list[dict] | None:
        """The valid findings list for the review just run, or `None` when
        there is none. Thin caller of `_review_findings_channel` above for
        the sites that only need the list; anything that decides whether a
        verdict may be TRUSTED must use `_review_findings_channel` instead,
        so that a present-but-invalid channel is refused rather than read as
        an absent one (see that method's docstring).
        """
        return self._review_findings_channel()[0]

    def _record_approach_verdict(self, approach: int, channel: str, *,
                                 verdict: str, reason: str | None,
                                 findings_list: list[dict] | None) -> dict:
        """Task 035 (requirement B.1, cure for task 006): PERSIST one judge's
        settled verdict for `approach` into the engine's OWN per-approach
        metadata (`state.record_approach_verdict` ->
        `RunDir.approach_metadata_file`) at the moment that judge's verdict is
        decided -- not at `finishing`, and not conditional on the judge's
        optional structured findings sidecar existing.

        Why the engine keeps its own copy at all: the two sidecars
        (`findings_json_file`, `verdict_gate_findings_json_file`) are the
        JUDGE's deliverable. They can be absent (a hat that wrote only prose),
        present-but-unreadable (`_review_findings_channel`'s third case) or
        wiped by a later approach's own review, and an archived approach only
        has one if that approach's judge happened to write one. Reading the
        record back off them alone therefore loses the one fact the run record
        must never lose -- WHAT EACH JUDGE DECIDED. `findings_list` is passed
        through as whatever the channel gave this verdict (`None` for "no
        usable channel"), and `state.record_approach_verdict` writes explicit
        `blocking`/`residual` arrays plus `findingsChannel` either way.

        Also emits one `approach.verdict_recorded` event per settled verdict,
        so the fact is witnessed on the event stream and not only in a file a
        later phase reads.
        """
        entry = self.run.record_approach_verdict(
            approach, channel, verdict=verdict, findings=findings_list,
            reason=reason)
        self.run.emit("approach.verdict_recorded", approach=approach,
                      channel=channel, verdict=verdict,
                      findingsChannel=entry["findingsChannel"],
                      blocking=len(entry["blocking"]),
                      residual=len(entry["residual"]))
        return entry

    def _review_success_verdict_label(self) -> str:
        """Task 012 (requirement D.3, #56 successor): the exact string a
        terminal-success write records as `status.json`'s `verdict` for
        review's own VERIFIED edge -- `"verified-with-residuals"` when the
        findings channel this verdict was computed from carries at least
        one `severity: "residual"` finding (`findings.has_residual_
        findings`), `"verified"` otherwise (no channel at all, or a
        channel with no residual entries).

        Called from every site that is about to write `_finish("succeeded",
        ...)` off the back of a review verdict that resolved VERIFIED --
        `_step_review`'s own direct success, the ordinary (non-grace) peer
        verdict-gate approval in `_step_verdict_gate`, and the off-budget
        grace review's success in `_maybe_grace_review` -- each re-reads
        the channel independently via `_review_findings_channel` rather
        than threading a value through `_review_verdict`'s own return
        shape, exactly the same independence `_review_incurable_rejection`/
        `_review_contract_ladder` already keep from each other: the
        channel is immutable disk state by the time a verdict has already
        settled VERIFIED, so re-reading it here costs nothing and never
        depends on which caller's copy of the findings list is passed
        around.

        Never itself decides curability/blocking-ness -- only whether a
        genuinely VERIFIED outcome still has residual findings worth
        naming in the record a human or script reads first, so a clean
        approach still gets the plain `"verified"` word it always has.
        """
        findings_list, _errors = self._review_findings_channel()
        if findings_list and findings.has_residual_findings(findings_list):
            return "verified-with-residuals"
        return "verified"

    def _review_verdict(
        self, result: IterationResult, mid_run: bool = False
    ) -> tuple[bool, dict | None, list[str]]:
        """Task 119 (#60 pillar 3, fact 9): parse the review hat's final
        line as a `review` contract payload (`{"verdict": "verified"|
        "rejected", "reason": ..., "findings_path": ...}`,
        `transitions.HAT_SCHEMAS["review"]`) -- `review.md` no longer ends
        its reply with the bare `<promise>VERIFIED</promise>` sentinel
        (`IterationResult.saw_verified`, still matched by `engine/runner.py`
        for the operator-facing "mentioned but did not end with it"
        diagnostic only, task 046/#55); this contract payload is the SOLE
        channel this method reads to decide the verdict.

        An absent/malformed payload, a schema violation, or a `rejected`
        verdict with no `reason` (`transitions.review_report_errors`) is
        treated as an ordinary non-verified verdict -- exactly the safe
        default the sentinel scan always had (no marker seen == not
        verified) -- never a run-ending contract violation: review's own
        independent judgement, not the harness, is what decides whether an
        approach is archived and replanned or the job goes terminal, so a
        malformed reply simply fails to earn VERIFIED rather than crashing
        the run.

        Task 002 (requirement A.2, #110) adds two things ON TOP of the
        above, both engine-side, neither a re-derivation of the schema
        check fact 16/#65 already guards: `transitions.review_verdict_
        findings_errors` folds a `verified` claim that contradicts the
        review's own structured findings (at least one `severity:
        "blocking"`) into `contract_errors` exactly like any other schema
        violation -- `_review_contract_ladder` (called by every site that
        drives a review iteration, strictly BEFORE this method ever sees
        the result) is what gives that specific contradiction a re-ask
        chance; by the time control reaches here the ladder has already
        exhausted its retries, so a contradiction still present at this
        point is refused (`verified` stays `False`) exactly like any other
        unresolved contract violation, never silently rewritten. Second: a
        A findings channel that is PRESENT but does not re-validate is
        folded into `contract_errors` the same way (`_review_findings_
        channel`): a verdict can never be trusted against a channel the
        engine cannot read, so it is refused (and re-asked by the ladder
        first), never accepted as though no channel existed. Second: a
        `rejected` verdict whose findings are ALL residual (`findings.
        compute_verdict` returns `"verified"`) is the engine's own honest
        recomputation (DoD predicate 3) -- not a contradiction to refuse,
        an overcautious claim to correct -- so `verified` flips to `True`
        here, and the residuals it was computed from are `emit()`ed to
        `events.jsonl` (surfaced, never dropped) in the same step.

        Task 011 (requirement C.3, #112): as a side effect, whatever the
        payload's own `petition_adjudications` field names CONFIRMED is
        persisted at run scope right here (`engine.petitions.record_
        confirmed_adjudications`) -- BEFORE `contract_errors` is even
        computed, deliberately: a malformed payload elsewhere (a bad
        `findings_path`, a missing `reason`) must not swallow a perfectly
        valid CONFIRMED adjudication sitting in the SAME payload. Recording
        is idempotent and additive (never erases an earlier CONFIRMED
        entry), so calling it on every settled review iteration -- verified
        or not -- is safe.

        Task 010 (requirement C.2, #114): the SAME field's REFUTED entries
        are applied here too, but never persisted at run scope the way
        CONFIRMED is (`docs/petition-format.md`: REFUTED is not something
        a later approach inherits) -- instead `engine.petitions.apply_
        refuted_petition_adjudications` mutates the CURRENT `tasks.json`
        directly, moving any matching `petitioned` task to `failed` with
        the refutation's remedy recorded, and the change is written back
        immediately so `_outstanding_for_completion`/`plan_complete` and
        every other reader see it from this point on.

        Task 014 (requirement F.1, #144): `mid_run` (default `False`,
        every pre-existing caller unaffected) is `True` only for
        `_maybe_run_midrun_adjudication_episode`'s own bounded, off-budget
        episode -- it swaps the schema this method validates `payload`
        against from `HAT_SCHEMAS["review"]` (verdict required,
        `verified`/`rejected` only) to `transitions.MIDRUN_REVIEW_SCHEMA`
        (verdict optional, plus the legal value `"adjudicated"`), so a
        reply with nothing terminal to claim is never read as a contract
        violation. The ordinary, run-ending review path's own schema is
        untouched by this parameter -- the safe default at ladder
        exhaustion is unchanged.
        """
        try:
            parsed = json.loads(last_nonblank_line(result.final_text))
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            petitions.record_confirmed_adjudications(
                self.run, parsed.get("petition_adjudications"))
            self._apply_refuted_petition_adjudications(
                parsed.get("petition_adjudications"))
            self._apply_criteria_amendments(
                parsed.get("petition_adjudications"))
        findings_list, channel_errors = self._review_findings_channel()
        if isinstance(parsed, dict):
            payload = parsed
            schema_errors = (
                contracts.validate_against(
                    transitions.MIDRUN_REVIEW_SCHEMA, payload)
                if mid_run else contracts.validate_payload("review", payload))
            contract_errors = (
                schema_errors
                or transitions.review_report_errors(payload)
                or channel_errors
                or transitions.review_verdict_findings_errors(
                    payload, findings_list))
        else:
            payload = None
            contract_errors = [
                "response: no parseable 'review' contract payload "
                "(absent or malformed output)"]
        verified = not contract_errors and payload.get("verdict") == "verified"
        if (not contract_errors and payload is not None
                and payload.get("verdict") == "rejected"
                and findings_list is not None
                and findings.compute_verdict(findings_list) == "verified"):
            residual = [f for f in findings_list if f.get("severity") == "residual"]
            self.run.emit(
                "review.verdict_recomputed", approach=self._approach,
                claimed_verdict="rejected", computed_verdict="verified",
                residual_count=len(residual), residual_findings=residual)
            verified = True
        # Task 035 (requirement B.1): the settled review verdict goes into the
        # engine's own approach metadata right here, the moment the judge's
        # verdict is decided -- `mid_run` excluded, because a mid-run
        # adjudication episode (task 014, requirement F.1) scores no approach
        # verdict at all and its legal `"adjudicated"` reply must not be
        # recorded as a rejection of the approach it ran inside.
        if not mid_run:
            self._record_approach_verdict(
                self._approach, "review",
                verdict="verified" if verified else "rejected",
                reason=(payload or {}).get("reason"),
                findings_list=findings_list)
        return verified, payload, contract_errors

    async def _review_contract_ladder(
        self, result: IterationResult | None, mid_run: bool = False
    ) -> IterationResult | None:
        """Task 002 (requirement A.2, #110): give a review payload that
        CONTRADICTS its own structured findings (`verdict: "verified"`
        alongside at least one `severity: "blocking"` finding --
        `transitions.review_verdict_findings_errors`) the SAME re-ask
        ladder every other contract site already has
        (`contract_enforcement.enforce_contract`) -- mirroring
        `_reflect_contract_ladder`'s shape -- instead of `_review_verdict`
        silently treating the claim as non-verified with no chance for the
        hat to correct it. Called once per settled review iteration,
        strictly before `_review_verdict` ever reads the result --
        `_run_review_iteration`'s return and `_maybe_grace_review`'s own
        `run_iteration` call are the two sites.

        Task 002 (requirement A.2, #119) widens the entry condition
        again, on top of the above: a review reply with NO parseable
        contract payload at all (absent output, malformed JSON, or valid
        JSON that is not even a dict) and a reply that IS a dict but fails
        `review`'s own schema now also enter this same ladder, exactly
        like every other hat's ordinary parse/schema failures already do
        via `contract_enforcement.parse_response` -- there is nothing
        review-specific to add for those two shapes, `enforce_contract`
        already re-prompts on them unconditionally once given the chance.
        Before this task those two shapes fell straight through to
        `_review_verdict`'s non-verified default with no re-ask at all,
        the same silent-miss gap task 001 (#119) already closed for
        verify's own ladder entry (`_verify_contract_ladder`) -- this is
        that same fix, mirrored onto review. A prose-only final line is
        the concrete case this exists for: it now gets a genuine chance to
        repair itself into a valid payload on re-ask, rather than being
        read as an unspoken rejection that archives the approach and
        writes a composite PRD for a successor that need not exist.

        Still skips the ladder entirely (returns `result` unchanged) for a
        reply that is ALREADY a schema-valid dict, `review_report_errors`-
        clean, and raises no findings-channel contradiction -- a genuine
        `rejected` verdict with a reason, or a `verified` verdict that
        does not contradict a present findings list, is not a mistake to
        re-ask over. A `rejected` verdict with no reason (`review_report_
        errors`) also still falls straight through unchanged -- that
        exemption is untouched by this task. Never re-asks over a
        `rejected` verdict whose findings are all residual either -- that
        is `_review_verdict`'s own honest recomputation
        (`review_verdict_findings_errors`, unchanged by this diff), not a
        hat mistake to refuse.

        One case beyond the plain contradiction rides the same ladder: a
        findings channel PRESENT on disk that does not re-validate
        (`_review_findings_channel`'s third case). It cannot be read as "no
        channel" without failing open on whatever blocking findings it
        holds, so it is re-asked here too -- and `extra_validate` below
        re-reads the channel on every attempt, so a hat that rewrites the
        sidecar correctly recovers on its own.

        On exhaustion (every re-ask attempt still fails), `result` comes
        back byte-for-byte unchanged -- exactly like `_verify_contract_
        ladder`'s own exhaustion path -- so `_review_verdict` still reads
        the original broken payload and produces today's ordinary
        non-verified outcome: the approach is still archived and a
        composite PRD is still written for a genuinely-exhausted case,
        unchanged by this task.

        Task 014 (requirement F.1, #144): `mid_run` (default `False`,
        every pre-existing caller unaffected) is `True` only for
        `_maybe_run_midrun_adjudication_episode`'s own bounded, off-budget
        episode. It swaps the schema this method's own early-exit check
        validates the FIRST attempt against from `HAT_SCHEMAS["review"]`
        to `transitions.MIDRUN_REVIEW_SCHEMA` (verdict optional, plus the
        legal value `"adjudicated"`) -- so a compliant mid-run reply
        (the overwhelming common case: the episode's own reply on its
        first attempt) is accepted right here and never even reaches
        `enforce_contract`'s retry ladder, let alone burns an attempt or
        emits `contract.agent_fault` over a `verdict` value the ordinary
        review contract would reject but this episode's own contract
        explicitly permits. The ordinary run-ending review path (`mid_run`
        left at its default) validates against the SAME strict schema it
        always has -- the safe default at ladder exhaustion is unchanged.
        """
        if result is None or self._review_interrupted(result):
            return result
        findings_list, channel_errors = self._review_findings_channel()
        try:
            parsed = json.loads(last_nonblank_line(result.final_text))
        except (json.JSONDecodeError, TypeError):
            parsed = None
        schema_errors = [] if not isinstance(parsed, dict) else (
            contracts.validate_against(transitions.MIDRUN_REVIEW_SCHEMA, parsed)
            if mid_run else contracts.validate_payload("review", parsed))
        if isinstance(parsed, dict) and not schema_errors:
            if transitions.review_report_errors(parsed):
                return result
            if not (channel_errors or transitions.review_verdict_findings_errors(
                    parsed, findings_list)):
                return result

        def extra_validate(payload: dict) -> list[str]:
            # Re-read the channel on every attempt: the hat's re-ask answer
            # may have repaired the sidecar, and a stale in-memory copy
            # would either refuse a now-consistent payload or accept a
            # newly-broken one.
            fresh, fresh_channel_errors = self._review_findings_channel()
            return (transitions.review_report_errors(payload)
                    or fresh_channel_errors
                    or transitions.review_verdict_findings_errors(
                        payload, fresh))

        first_attempt_text = last_nonblank_line(result.final_text)
        event_loop = asyncio.get_running_loop()

        def respond(attempt: int, prior_errors: list[str] | None) -> str | None:
            if attempt == 0:
                return first_attempt_text
            raw, _usage = asyncio.run_coroutine_threadsafe(
                self._ask_contract_fix("review", prior_errors or [],
                                       final_text=result.final_text),
                event_loop).result()
            return last_nonblank_line(raw)

        enforcement = await asyncio.to_thread(
            contract_enforcement.enforce_contract,
            "review", respond,
            max_retries=self.cfg.contract_retry_max,
            state=self.run,
            extra_validate=extra_validate,
            schema=transitions.MIDRUN_REVIEW_SCHEMA if mid_run else None,
        )
        if enforcement.ok:
            return dataclasses.replace(
                result, final_text=json.dumps(enforcement.payload))
        return result

    @staticmethod
    def _iteration_cut_short_by_engine(result: IterationResult) -> bool:
        """Task 004 (requirement A, #72): True iff `result` shows an
        iteration was cut short by the ENGINE -- SIGINT (`interrupted`),
        the full `iteration_timeout_s` or the startup-window watchdog
        (`timed_out`/`no_traffic_timeout`), or an abnormal process exit
        (`exit_code` neither 0 nor None -- the same "never even reached a
        verdict" shape `_verify_no_verdict` and the reflect path's own
        failure check already use) -- rather than an ordinary iteration
        that ran to completion and simply replied with something its
        caller cannot parse.

        Extracted from `_review_interrupted` (task 003, #72's original
        fix for `_step_review`) so the SAME discrimination -- an engine-
        side cutoff is not a judged outcome -- is available, byte-for-byte
        identical, to every other hat-driving path task 004's audit found
        making the same mistake: `_step_generic_hat`'s hat retry and
        `_one_planning_attempt`'s planning retry. `_review_interrupted`
        below is now a thin, name-preserving wrapper (its own callers, and
        the mutation test that monkeypatches it by name, are unaffected).
        """
        return bool(result.interrupted or result.timed_out
                    or result.no_traffic_timeout
                    or result.exit_code not in (0, None))

    @staticmethod
    def _review_interrupted(result: IterationResult) -> bool:
        """Requirement A (#72): True iff `result` shows the review
        iteration itself was cut short by the ENGINE -- see
        `_iteration_cut_short_by_engine`, which this delegates to
        entirely (same predicate, shared now with `_step_generic_hat` and
        `_one_planning_attempt`, task 004) -- rather than an ordinary
        iteration that ran to completion and simply replied with
        something `_review_verdict` cannot parse.

        This is the ONLY thing that may route a review iteration onto the
        `review_interrupted` / `review_unjudged_at_budget_end` edges
        instead of an ordinary rejected verdict. An absent or malformed
        payload from an iteration that DID run to completion must still
        fall through to `_review_verdict`'s existing "not verified"
        default -- that contract-payload semantics are unchanged by this
        method's existence (a test pins it).
        """
        return LoopSupervisor._iteration_cut_short_by_engine(result)

    async def _run_review_iteration(self, approach: int) -> IterationResult | None:
        """One review iteration, self-looping in place on the
        `review_interrupted` guard (requirement A, #72) while the engine
        itself keeps cutting the attempt short and there is still budget
        and wall clock left to ask again -- the same self-loop shape as
        `_one_planning_attempt`'s inner retry and `worker`'s
        `no_verdict_yet`: an in-place retry is not a state CHANGE, so
        nothing here is visible to `_run_job_core`'s dispatch loop, and the
        approach counter is never touched.

        Returns the settled `IterationResult` the moment one attempt
        actually runs to completion -- whatever it then says, however
        malformed; that is `_review_verdict`'s business, not this
        method's -- or `None` once budget/wall clock runs out while still
        interrupted, in which case the caller must land on
        `review_unjudged_at_budget_end`, never on an ordinary rejected
        verdict: no approach archive, no `review rejected` event, no
        composite PRD.
        """
        while True:
            # Task 016 (requirement I/decision 8): the run-ending review
            # always provisions at verify's top tier, regardless of any
            # task's tier -- `state.run_level_verify_tier()` takes no task
            # argument on purpose.
            result = await self.run_iteration(
                "review", extra=self._review_extra_context(),
                tier=state.run_level_verify_tier())
            if not self._review_interrupted(result):
                return await self._review_contract_ladder(result)
            self._advance("review", "review", "review_interrupted")
            if not self.budget_left():
                return None
            await self._gate()

    def _review_unjudged_at_budget_end(self, approach: int) -> tuple:
        """Requirement A (#72): budget/wall clock ran out while review
        itself kept getting interrupted -- advance straight to `finishing`
        on the `review_unjudged_at_budget_end` edge, never the ordinary
        `rejected_*` edges: no verdict was ever actually scored, so the
        approach must not be archived, no `review rejected approach N`
        log line is emitted, and no composite PRD is written for a
        successor approach that will never run.

        Requirement B (#72): this is ALSO the one call site that stashes
        `_deadline_beat_verdict_patch()`'s result for the shared terminal
        write (`_run_job_core`'s tail exit) to include -- see that
        method's docstring for the marker's shape and, more importantly,
        for why it must be computed HERE and nowhere more generic: this is
        the one place in the whole dispatch loop that positively knows
        review never rendered a judgement THIS approach, as opposed to
        having rendered one that was rejected (which leaves an
        indistinguishable trace on disk -- same completed tasks, same past
        COMPLETE signal -- once `_archive_approach` has copied today's
        tasks.json into `approaches/NN/` without resetting it).
        """
        self._advance("review", "finishing", "review_unjudged_at_budget_end")
        self._terminal_reason_note = (
            f"review (approach {approach}) was repeatedly interrupted with "
            "no budget or wall clock left to ask again; no verdict was "
            "ever scored, so the approach was not archived and no "
            "rejection was recorded (requirement A, #72)")
        # Task 016 (requirement F.3, #146): this is the ordinary-path call
        # site that positively knows the deadline, not a rejection, is what
        # ended review this approach -- `_finish_tail_unverified` reads the
        # flag to write `verdict: "unreviewed"` / `reason: "deadline
        # during review"` instead of the ordinary `unverified` outcome.
        # The event lets a later episode's `_resume_point()` find this
        # approach again on disk (`_unreviewed_awaiting_resume`) without
        # relying on status.json, which the very next episode's own
        # `running` write rebases before `_resume_point()` ever runs.
        self._review_deadline_unreviewed = True
        self.run.emit("review_deadline_unreviewed", approach=approach)
        self._deadline_beat_verdict_extra = self._deadline_beat_verdict_patch()
        return ("tail",)

    def _deadline_beat_verdict_patch(self) -> dict:
        """status.json patch (requirement B, #72) for the ONE terminal
        write that must not read as "the work failed" when it wasn't: a
        run where every task is done and the worker's own `COMPLETE` was
        already honoured, and the ONLY thing missing is a review verdict
        because the deadline took the last review attempt before it could
        speak.

        Chosen design, out of the three the PRD offers (rejected two
        recorded in `artifacts/reports/task-006-deadline-beat-verdict-
        design.md`): keep `state: failed` / `verdict: unverified` exactly
        as `_run_job_core`'s tail exit already writes them --
        `verdict: verified` stays grantable by the review hat alone, the
        operator's one non-negotiable ruling -- and add a dedicated,
        structured field instead of asking a human to parse the free-text
        `reason` string. Modelled on `_unconsumed_steering_patch`'s own
        precedent: name the thing that was stranded, loudly, in a field a
        script can check without a human reading prose.

        Empty (the common, correct-for-every-OTHER-tail-exit case) unless
        BOTH:
          * every task in the record is done -- `_outstanding_for_
            completion` empty, the identical check `_step_worker`'s own
            `complete`/`plan_complete` verdict branches use to decide
            whether a COMPLETE claim was earned; and
          * an actual `signal` event recording `signal: "COMPLETE"` exists
            in this run's `events.jsonl` -- so a plan that merely happens
            to read all-done at this exact snapshot (for some unrelated
            reason) is not mistaken for one whose COMPLETE was ever
            honoured.

        Deliberately NOT wired in unconditionally at the shared tail-exit
        `_finish()` call the way `_unconsumed_steering_patch()` is: those
        two conditions, on their own, are also true of a run whose LAST
        approach really was reviewed and genuinely REJECTED (`rejected_
        no_approach_left`) -- `_archive_approach` copies tasks.json into
        `approaches/NN/` without resetting anything on the live copy, and
        the COMPLETE signal that made the worker hand off to review in the
        first place is still sitting in the event log. Calling this
        method from that path would misreport a real rejection as "the
        clock beat the verdict". It is therefore called from exactly one
        place, `_review_unjudged_at_budget_end`, which is the only call
        site that positively knows review itself never rendered a verdict.
        """
        tasks_doc = self.run.read_tasks()
        if self._outstanding_for_completion(tasks_doc):
            return {}
        complete_events = [
            ev for ev in state.read_events(self.run.root, types={"signal"})
            if ev.get("signal") == "COMPLETE"
        ]
        if not complete_events:
            return {}
        return {
            "deadlineBeatVerdict": {
                "deadline": utc_from_epoch(self._deadline_epoch),
                "completeHonouredAt": complete_events[-1].get("ts"),
            }
        }

    async def _step_review(self) -> tuple:
        """The `review` state. Guards: `review_interrupted` (self-loop,
        stays inside `_run_review_iteration`'s own `while True` exactly
        like `planning`'s `instant_failure_streak` and `worker`'s
        `no_verdict_yet` -- requirement A, #72: the iteration itself was
        cut short by the engine, not an ordinary rejected verdict, so it
        must not cost the approach); `review_unjudged_at_budget_end`
        (-> finishing, i.e. `("tail",)`, when budget/wall clock ran out
        while still interrupted -- see `_review_unjudged_at_budget_end`);
        `steering_pending_at_verified` (-> worker, taken ONCE, whenever
        `_terminal_success_blockers()` names anything at all -- task 013,
        requirement I, #98, REPLACES the pre-013 in-place discard loop
        (`while verified and self.run.pending_steering():`, which
        dispatched a no-task `worker` iteration from inside this method
        until every steering FILE was consumed) with that single
        predicate and a return to the ordinary dispatch loop: unsettled
        steering is settled by the `steer` hat `_step_worker` routes to at
        absolute top priority (task 012), and a steering-ORIGIN TASK that
        is not terminal blocks terminal success as well -- `settled` is
        not `done`); `verified` (-> finishing, i.e.
        `("done", "succeeded")`, reached only once
        `_terminal_success_blockers()` is empty); `rejected_approach_left`
        (-> replan); `rejected_no_approach_left` (-> finishing, i.e.
        `("tail",)`).
        """
        approach = self._approach
        await self._gate()
        self.run.emit("phase", phase="review", approach=approach)
        result = await self._run_review_iteration(approach)
        if result is None:
            return self._review_unjudged_at_budget_end(approach)
        verified, _payload, _errors = self._review_verdict(result)
        if verified:
            # Task 013 (requirement I, #98): ONE exit gate, asked once.
            # It REPLACES the pre-013 mechanism -- `while verified and
            # self.run.pending_steering():`, which discarded the verdict
            # and dispatched a no-task `worker` iteration from inside this
            # method, looping in place until every steering FILE was
            # consumed -- with `_terminal_success_blockers()` plus a
            # return through the declared `steering_pending_at_verified`
            # edge to the ordinary dispatch loop. Two things change, both
            # strictly stronger than the loop they replace:
            #
            #   * unsettled steering is no longer consumed by a worker
            #     iteration THIS method dispatches by hand: control goes
            #     back to `_step_worker`, whose absolute-top-priority
            #     check (task 012) routes the very next pass to the
            #     `steer` hat -- the hat that exists to process operator
            #     messages, handed all of them in arrival order, marking
            #     them consumed only on a clean exit
            #     (`_run_iteration_once`, task 018/#34). The at-least-once
            #     property across a dying iteration is therefore exactly
            #     the one that always guarded them, unchanged; what the
            #     mid-review race (`tests/test_e2e.py::test_steering_
            #     during_review_iteration_never_silently_dropped`,
            #     `::test_verified_review_refused_while_steering_pending`)
            #     is protected by is no longer a hand-rolled loop here.
            #   * a steering-ORIGIN TASK that is not terminal
            #     (`state.is_steering_origin` / `state.task_is_terminal`)
            #     blocks terminal success too. `settled` is not `done` --
            #     the PRD's own diagnosis: "steer 003 was `consumed` at
            #     iteration 87 with three quarters of it undone."
            #
            # Returning instead of looping in place is what makes the
            # second half expressible at all: a FILE was consumable in
            # exactly one more actionable iteration, but finishing a TASK
            # takes an unbounded number of ordinary worker iterations, and
            # the worker's own scheduler must pick it up like any other
            # pending task. Review is asked again the ordinary way, once
            # the worker signals COMPLETE again.
            blockers = self._terminal_success_blockers()
            if blockers:
                self.run.emit(
                    "log", level="warning",
                    message=("VERIFIED verdict deferring: "
                             + "; ".join(blockers)
                             + " -- routing back to worker so the ordinary "
                               "lifecycle settles and finishes it before the "
                               "run can go terminal-succeeded"))
                self._advance("review", "worker", "steering_pending_at_verified")
                return ("advance", "worker", "steering_pending_at_verified")
            # Task 020 (requirement L, #101): the opt-in, genuinely BLIND
            # peer verdict gate. `transitions.PEER_VERDICT_GATE_DELTA` is
            # what actually declares this alternate edge legal -- this
            # `if` is just which of the two ALREADY-legal targets this run
            # takes, decided from the SAME config flag `transitions.
            # table_for`'s caller (`TransitionExecutor` construction,
            # `__init__`) was built with, so the two can never disagree
            # about which table variant is in force (the same shape
            # `_step_planning`'s `peer_plan_review` check already uses).
            if self.cfg.peer_verdict_gate:
                self._advance("review", "verdict_gate",
                             "verified_peer_verdict_gate_enabled")
                return ("advance", "verdict_gate",
                        "verified_peer_verdict_gate_enabled")
            self._advance("review", "finishing", "verified")
            self.run.emit("signal", signal="VERIFIED")
            self._finish("succeeded", self._review_success_verdict_label(),
                        **self._unconsumed_steering_patch())
            return ("done", "succeeded")
        self.run.emit("log", message=f"review rejected approach {approach}")
        # Task 006 (PRD requirement B.2, #111): a rejection whose BLOCKING
        # findings are ALL incurable takes `rejected_incurable` instead of
        # spending (and losing) a fresh approach on findings a fresh
        # approach cannot change -- checked BEFORE the ordinary archive/
        # replan below, but only once an approach is actually available
        # (requirement B.3: `rejected_no_approach_left` still wins once the
        # budget is spent -- see `_review_incurable_rejection` for why that
        # order is load-bearing, not merely convenient).
        if approach < self.cfg.max_approaches and self._review_incurable_rejection():
            self._pause_and_escalate_incurable_rejection(approach)
            self._advance("review", "review", "rejected_incurable")
            return ("advance", "review", "rejected_incurable")
        # Task 009 (PRD requirement C.1, #141): a rejection whose blocking
        # findings are ALL curable, with this approach's own cure pass
        # still unspent, is a cure pass -- not a replan. Checked AFTER the
        # incurable branch above (requirement C.2: an all-incurable
        # rejection keeps its pause-and-escalate precedence) and BEFORE
        # the ordinary archive/replan below, so a curable rejection never
        # burns the approach it could instead be cured inside of.
        #
        # Task 004 (PRD requirement C.1, finding 4): the target is the
        # dedicated `cure` step state (`transitions.TABLE`), not `worker`
        # directly -- `_step_cure` takes the run the rest of the way to
        # `worker`, immediately, in the same dispatch pass, so the worker
        # still runs the appended cure tasks exactly as before.
        if self._review_cure_pass_available(approach):
            self._take_cure_pass(approach)
            self._advance("review", "cure", "rejected_curable_pass_available")
            return ("advance", "cure", "rejected_curable_pass_available")
        # Task 011 (PRD requirement D.1, #142): on the FINAL approach, a
        # rejection whose blocking findings are ALL curable but whose one
        # cure pass for this approach is already spent (the branch just
        # above already took it while it was still available) must not
        # end the run `failed` on paperwork a fresh approach cannot even
        # be granted to fix -- checked AFTER the cure-pass branch (so a
        # still-available cure pass always wins, requirement D.1's own
        # "if a cure pass is available, take it") and BEFORE the ordinary
        # archive/replan-or-fail below, so the approach is never archived
        # on this branch (`resume` re-reviews the SAME approach).
        #
        # Task 004 (PRD requirement D.1, finding 4): the target is the
        # dedicated `paused` step state, not a `review -> review` self-
        # loop -- `_step_paused` parks the run on the same `self._pause`
        # `asyncio.Event` this self-loop always relied on, then lands back
        # on `review` over the SAME approach once resumed.
        if (approach == self.cfg.max_approaches
                and self._review_last_approach_curable_rejection(approach)):
            self._pause_and_escalate_last_approach_rejection(approach)
            self._advance("review", "paused", "rejected_last_approach")
            return ("advance", "paused", "rejected_last_approach")
        self._archive_approach(approach)
        self._write_composite_prd(approach)
        if approach < self.cfg.max_approaches:
            self._advance("review", "replan", "rejected_approach_left")
            return ("advance", "replan", "rejected_approach_left")
        self._advance("review", "finishing", "rejected_no_approach_left")
        return ("tail",)

    async def _step_cure(self) -> tuple:
        """The `cure` state (task 004, PRD requirement C.1, finding 4).

        A pure formalisation of "review took the cure-pass edge" as its
        own named position in the FSM, rather than an unrecorded side
        effect that happened to land straight back on `worker`. The
        actual append-cure-tasks/increment-counter/emit-event work
        (`_take_cure_pass`) already ran inside `_step_review` BEFORE this
        state was ever entered -- there is nothing left to decide here:
        `cure` has exactly one declared successor (`transitions.TABLE
        ["cure"]`), taken unconditionally and immediately, in the same
        dispatch pass, so "the worker runs the appended cure tasks" is
        exactly as immediate as it was before this state existed.
        """
        self._advance("cure", "worker", "cure_pass_appended")
        return ("advance", "worker", "cure_pass_appended")

    async def _step_paused(self) -> tuple:
        """The `paused` state (task 004, PRD requirement D.1, finding 4).

        The formal step-table home for "review paused itself on
        `rejected_last_approach`" -- `_pause_and_escalate_last_approach_
        rejection` already did the actual work (persisted the pause to
        status.json, emitted `operator_escalation`, notified) before
        `_step_review` advanced here. This state's own job is only to
        occupy the dispatch loop's `step` position while the run sits
        parked, and to resume straight back into `review` -- never
        `worker`, never a fresh `replan` -- the instant the existing
        `self._pause` `asyncio.Event` is released again
        (`ralphctl resume`/`POST /resume`), exactly the target a fresh
        process's own `_resume_point`/`_paused_awaiting_last_approach_
        ruling` case already lands on. `await self._gate()` is the SAME
        parking primitive every other step boundary already calls -- a
        no-op the instant `resume()` has already run, and an actual park
        otherwise.
        """
        await self._gate()
        self._advance("paused", "review", "paused_resumed")
        return ("advance", "review", "paused_resumed")

    def _cure_passes_for_approach(self, approach: int) -> int:
        """Task 009 (requirement C.1, #141): `curePasses[approach]` as
        recorded on status.json right now -- disk, not memory, the same
        `_record_stagnation_signature` shape, so a resumed process sees
        exactly the count a crashed one already persisted. Absence (never
        cured, or a run from before this task) reads as `0`, same as
        `_blocker_signature`'s own missing-entry default.
        """
        cure_passes = self.run.read_status().get("curePasses") or {}
        try:
            return int(cure_passes.get(str(approach), 0))
        except (TypeError, ValueError):
            return 0

    def _review_cure_pass_available(self, approach: int) -> bool:
        """Task 009 (requirement C.1, #141): does this rejection deserve
        the cure-pass edge (`rejected_curable_pass_available`) rather than
        the ordinary replan? True only when a structured findings channel
        exists (`_review_findings`, `None` falls back to the ordinary
        replan path exactly like `_review_incurable_rejection` does),
        the findings actually compute to `"rejected"` (`findings.compute_
        verdict`), EVERY blocking finding is curable (`findings.all_
        blocking_findings_curable` -- requires `curable is True` across
        the whole blocking set), AND this approach has not already spent
        its one cure pass (`curePasses[approach] == 0`).

        Task 039 (operator steer 002): a rejection that carries a
        criteria-tamper finding at all (`findings.any_criteria_tamper_
        finding` -- the engine's own finding for a worker/planner rewrite of
        a task's `successCriteria`, #116 section 2) is NEVER curable, whatever
        that finding's own `curable` flag says. The flag is `True` for a
        sound reason of its own (a *fresh* approach that never edits criteria
        again does not repeat the violation -- `findings.flagged_criteria_
        findings`' own docstring), but curing is not a fresh approach: it
        hands THIS approach, the one that already rewrote its own criteria,
        another worker pass over the very tasks whose criteria are in
        dispute. So a tampered approach is archived and replanned exactly as
        it was before this wave's cure-pass edge existed (task 009), which is
        also what `tests/test_e2e.py::test_criteria_edited_after_validation_
        failure_is_refused` pins end to end: the archived approach holds its
        planned tasks and no engine-appended cure task.
        """
        findings_list = self._review_findings()
        if findings_list is None:
            return False
        if findings.compute_verdict(findings_list) != "rejected":
            return False
        if findings.any_criteria_tamper_finding(findings_list):
            return False
        if not findings.all_blocking_findings_curable(findings_list):
            return False
        return self._cure_passes_for_approach(approach) == 0

    def _take_cure_pass(self, approach: int) -> None:
        """Task 009 (requirement C.1, #141): the consequence of taking the
        cure-pass edge -- append one cure task per blocking finding to
        THIS approach's `tasks.json` (`state.insert_cure_tasks`),
        increment and persist `curePasses[approach]` on status.json (so
        it survives resume, the same disk-not-memory shape `_record_
        stagnation_signature` already uses), and emit `approach.cure_
        pass` naming the approach, the new count and the inserted task
        ids. The caller (`_step_review`) is what actually routes to
        `worker` -- this method only performs the append/counter/event
        side effects, mirroring how `_pause_and_escalate_incurable_
        rejection` only performs ITS side effects and leaves the edge
        itself to `_step_review`.
        """
        findings_list = self._review_findings() or []
        blocking = [f for f in findings_list if f.get("severity") == "blocking"]
        inserted = state.insert_cure_tasks(self.run, approach, blocking)
        status = self.run.read_status()
        cure_passes = dict(status.get("curePasses") or {})
        new_count = self._cure_passes_for_approach(approach) + 1
        cure_passes[str(approach)] = new_count
        self.run.update_status(curePasses=cure_passes)
        self.run.emit("approach.cure_pass", approach=approach,
                      curePasses=new_count, taskIds=inserted,
                      findings=blocking)

    def _review_last_approach_curable_rejection(self, approach: int) -> bool:
        """Task 011 (PRD requirement D.1, #142): does this rejection,
        already established to be on the FINAL approach
        (`approach == self.cfg.max_approaches`, checked by the caller), and
        already established to have exhausted this approach's own cure
        pass (`_review_cure_pass_available` already returned `False`, also
        checked by the caller, ahead of this call), deserve the `rejected_
        last_approach` pause-and-escalate edge rather than falling through
        to the ordinary archive/`rejected_no_approach_left` branch? True
        only when a structured findings channel exists (`_review_
        findings`, `None` falls back to the ordinary path exactly like
        `_review_incurable_rejection`/`_review_cure_pass_available` already
        do), the findings actually compute to `"rejected"` (`findings.
        compute_verdict`), AND every blocking finding in it is curable
        (`findings.all_blocking_findings_curable` -- requirement D.2's own
        complement: ANY `curable: false` blocking finding here means this
        returns `False` and the caller falls through to `rejected_no_
        approach_left` -> `failed` instead, exactly as it does today).
        """
        findings_list = self._review_findings()
        if findings_list is None:
            return False
        if findings.compute_verdict(findings_list) != "rejected":
            return False
        return findings.all_blocking_findings_curable(findings_list)

    def _last_approach_rejection_escalation_text(
        self, approach: int, blocking: list[dict]
    ) -> str:
        """The operator-facing escalation body for a `rejected_last_
        approach` pause -- the same shape as `_incurable_rejection_
        escalation_text` (each blocking finding plus the exact `ralphctl
        amend` invocation that would resolve it), worded for the case this
        edge actually covers: every finding IS curable, but there is no
        approach budget left to spend a fresh worker pass on them, so the
        run pauses for an operator ruling instead of failing on paperwork.
        """
        lines = [
            f"Run {self.cfg.run_id} approach {approach}: review rejected "
            "this, the LAST available approach, and every blocking finding "
            "is curable -- but this approach's own cure pass is already "
            "spent and there is no fresh approach left to grant. The run "
            "has been paused (existing pause machinery) rather than ended "
            "`failed` on paperwork; it will not proceed until an operator "
            "ruling is recorded and the run is resumed "
            f"(`ralphctl resume {self.cfg.run_id}`).",
            "",
        ]
        for index, finding in enumerate(blocking, start=1):
            lines.append(f"Finding {index} (blocking, curable):")
            lines.append(f"  clause: {finding.get('clause', '')}")
            lines.append(f"  evidence: {finding.get('evidence', '')}")
            lines.append(f"  resolve with: {self._amend_invocation_for_finding(finding)}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _pause_and_escalate_last_approach_rejection(self, approach: int) -> None:
        """Task 011 (PRD requirement D.1, #142): pause the run through the
        EXISTING pause machinery, generalising `_pause_and_escalate_
        incurable_rejection` to this edge's own reason -- same seam
        (`self.pause()`, `self.notifier`, an `operator_escalation` event
        always emitted to `events.jsonl` regardless of whether a notifier
        is wired in), just a different `reason` and escalation body.
        """
        findings_list = self._review_findings() or []
        blocking = [f for f in findings_list if f.get("severity") == "blocking"]
        text = self._last_approach_rejection_escalation_text(approach, blocking)
        self.run.emit("operator_escalation", approach=approach,
                      reason="rejected_last_approach", findings=blocking,
                      escalation=text)
        # Task 036 (requirement D.1/D.2, #142): the reason argument is what
        # makes this pause observable from the host -- status.json carries
        # `state: "paused"` / `pauseReason: "rejected_last_approach"` from
        # this moment until an operator's `resume` clears it. The edge name
        # and the `pauseReason` are deliberately the same string: the
        # `operator_escalation` event above and the persisted status agree
        # without a second vocabulary to map between.
        self.pause(reason="rejected_last_approach")
        if self.notifier is not None:
            self.notifier(text)

    def _review_incurable_rejection(self) -> bool:
        """Task 006 (requirement B.2, #111): does the review just run
        deserve `rejected_incurable` rather than the ordinary replan edge?
        True only when a structured findings channel exists for this
        review (`_review_findings`, `None` when review.md has not been
        updated to always produce one -- nothing to check curability
        against, so this falls back to `False`, the ordinary replan path,
        exactly as `_review_verdict` falls back to trusting the payload's
        own claim when no channel exists), the findings actually compute
        to `"rejected"` (`findings.compute_verdict` -- a channel present
        but entirely residual is not a rejection at all, so curability is
        never asked), AND every blocking finding in it is incurable
        (`findings.all_blocking_findings_incurable`, PRD requirement B.4:
        asserted by review alone, no run-side input).
        """
        findings_list = self._review_findings()
        if findings_list is None:
            return False
        if findings.compute_verdict(findings_list) != "rejected":
            return False
        return findings.all_blocking_findings_incurable(findings_list)

    def _amend_invocation_for_finding(self, finding: dict) -> str:
        """The exact `ralphctl amend` invocation an operator would run to
        waive the obligation `finding` says is violated (PRD requirement
        B.2: "the exact `ralphctl amend` invocations that would resolve
        each one"). `FINDING_SCHEMA` (`engine/findings.py`) carries no task
        linkage at all (`additionalProperties: False`, only `severity`/
        `curable`/`clause`/`evidence`) -- deliberately: requirement B.4
        forbids curability from taking any run-side input, and a task id
        supplied by the run would be exactly that. So the identifier this
        invocation amends is a slug of the finding's own `clause` --
        `write_amendment` (`engine/state.py`) never validates its `task_id`
        argument against `tasks.json` at all (it is host-only and cannot
        read the run dir), so a clause-derived slug is a fully valid,
        already-instantiated ruling target, not a placeholder the operator
        has to fill in themselves. The ruling text is the finding's own
        `clause`/`evidence`, so the file `ralphctl amend` writes records
        exactly what this escalation showed the operator, verbatim.
        """
        clause = str(finding.get("clause") or "finding")
        evidence = str(finding.get("evidence") or "")
        slug = re.sub(r"[^a-z0-9]+", "-", clause.lower()).strip("-") or "finding"
        reason = f"{clause}: {evidence}" if evidence else clause
        return (f"ralphctl amend {self.cfg.run_id} {slug} --waive "
               f"{shlex.quote(reason)}")

    def _incurable_rejection_escalation_text(
        self, approach: int, blocking: list[dict]
    ) -> str:
        """The operator-facing escalation body for a `rejected_incurable`
        pause: each blocking finding (clause, evidence, curable) plus the
        exact `ralphctl amend` invocation that would resolve it (requirement
        B.2's own wording), in finding order so a reader can match one to
        the other by position.
        """
        lines = [
            f"Run {self.cfg.run_id} approach {approach}: review rejected "
            "this approach and every blocking finding is incurable -- a "
            "fresh approach would not change any of them. The run has "
            "been paused (existing pause machinery); it will not proceed "
            "until an operator ruling is recorded and the run is resumed "
            f"(`ralphctl resume {self.cfg.run_id}`).",
            "",
        ]
        for index, finding in enumerate(blocking, start=1):
            lines.append(f"Finding {index} (blocking, incurable):")
            lines.append(f"  clause: {finding.get('clause', '')}")
            lines.append(f"  evidence: {finding.get('evidence', '')}")
            lines.append(f"  resolve with: {self._amend_invocation_for_finding(finding)}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _pause_and_escalate_incurable_rejection(self, approach: int) -> None:
        """Task 006 (requirement B.2, #111): pause the run through the
        EXISTING pause machinery (`self.pause()` -- the same `self._pause`
        `asyncio.Event` `POST /pause` clears, no second implementation) and
        produce the operator escalation. `self.notifier`, when set, is
        handed the escalation TEXT (a stub in tests; a real notifier --
        e.g. the `telegram-notify` skill's HTTP push -- in production);
        either way the escalation is also `emit()`ed to `events.jsonl` so
        it is never ONLY reachable through whatever notifier is wired in.
        """
        findings_list = self._review_findings() or []
        blocking = [f for f in findings_list if f.get("severity") == "blocking"]
        text = self._incurable_rejection_escalation_text(approach, blocking)
        self.run.emit("operator_escalation", approach=approach,
                      reason="rejected_incurable", findings=blocking,
                      escalation=text)
        self.pause()
        if self.notifier is not None:
            self.notifier(text)

    def _terminal_success_blockers(self) -> list[str]:
        """Task 013 (requirement I, #98): the ONE predicate both exit-gate
        sites (`_step_review`'s VERIFIED check and `_maybe_grace_review`)
        consult before a VERIFIED verdict is allowed to make the run
        terminal-succeeded -- one human-readable reason per blocker, empty
        list when the run may finish. Defined once so the two sites cannot
        drift apart the way they did while each hand-rolled its own
        `self.run.pending_steering()` test.

        Two blockers, in the PRD's own vocabulary:

        * NOT SETTLED -- a steering file no iteration has processed yet
          (`pending_steering()`, i.e. absent from
          `steering/.consumed.json`). The run must not go terminal with an
          operator message nothing ever acted on; `_step_review` hands
          control back to the dispatch loop, which routes to the `steer`
          hat at absolute top priority (task 012), and the file is marked
          consumed only when that iteration exits cleanly -- the
          at-least-once ordering of task 018/#34, kept deliberately.
        * NOT DONE -- a steering-origin task (`state.is_steering_origin`)
          still non-terminal (`state.task_is_terminal`). `settled` says
          only that the hat processed the message and applied its changes;
          `done` is the ordinary lifecycle's verdict on the tasks it
          created, and conflating the two is the whole defect requirement
          I names.
        """
        blockers: list[str] = []
        pending = self.run.pending_steering()
        if pending:
            names = ", ".join(p.name for p in pending)
            blockers.append(f"operator steering ({names}) is not settled yet")
        outstanding = self._steering_origin_outstanding()
        if outstanding:
            ids = ", ".join(str(t.get("id", "?")) for t in outstanding)
            blockers.append(f"steering-origin task(s) ({ids}) are not terminal "
                            "(`settled` is not `done`)")
        return blockers

    def _steering_origin_outstanding(self) -> list[dict]:
        """Task 013 (requirement I, #98): steering-origin tasks
        (`state.is_steering_origin`) not yet terminal (`state.task_is_
        terminal`), read fresh from disk -- the "not done" half of
        `_terminal_success_blockers()`, which is what REPLACED the pre-013
        `self.run.pending_steering()` discard loop at the two exit-gate
        sites (`_step_review`'s VERIFIED check, `_maybe_grace_review`).
        Task 012's absolute-top-priority routing settles every steering
        FILE in the `steer` hat, but a settled message can still leave the
        TASK it produced undone -- `settled` is not `done`.
        `state.steering_origin_outstanding` does the actual filtering; this
        is just the `self.run.read_tasks()` plumbing around it, matching
        every other `tasks_doc`-reading call site in this class.
        """
        return state.steering_origin_outstanding(self.run.read_tasks())

    def _unconsumed_steering_patch(self) -> dict:
        """status.json patch (task 006) applied at every terminal-state write:
        names any steering files still unconsumed when the run went
        terminal, so a run that ends failed/aborted/succeeded with steering
        stranded (e.g. budget exhausted right after a rejected review that
        left a just-landed steering file pending, with no further
        actionable iteration to consume it) is loudly discoverable from
        status.json alone -- not just from combing steering/.consumed.json
        by hand. Empty list when nothing is stranded (the common case)."""
        return {"unconsumedSteering": [p.name for p in self.run.pending_steering()]}

    def _doomed_plan_fault(self, tasks_doc: dict,
                           blocked_by: dict[str, list[str]]) -> dict | None:
        """Task 007 (requirement D, #99): the fault payload iff `blocked_by`
        (`state.task_blocked_by`'s doomed-set derivation, computed by the
        caller from the SAME `tasks_doc`) covers the ENTIRE actionable
        remainder -- every task `task_is_actionable()` still calls workable
        is doomed -- else `None`.

        This is the two-function disagreement the PRD names directly:
        `scheduler.select_next_task()` is graph-aware (a doomed `pending`
        task is never a candidate), but `task_is_actionable()` is per-task
        and dependency-blind (`status in ("pending", "in-progress",
        "validation-failed")`, nothing more) -- so a doomed task still reads
        as actionable to `_unresolved_failures()` (guard 9) below, and the
        run starves through three no-progress iterations into
        `stagnation_exhausted` (guard 10) with the cause misattributed as a
        stuck approach rather than an unreachable plan. `None` whenever at
        least one actionable task is NOT doomed -- a doomed SUBSET with real
        work left is unaffected by this method: the existing per-task
        blocked-notes/stagnation machinery still governs exactly as it did
        before this task.

        Chosen consequence (task 007, recorded here and in the commit
        message and in `transitions.py`'s `doomed_plan_unverifiable` table
        entry): the caller routes straight to `review` for a verdict,
        reusing the #33 `blocked_with_failures` mechanism, rather than a
        dependency-graph replan or a terminal degraded state -- a doomed
        plan is exactly the shape `blocked_with_failures` already hands to
        a reviewer (work-with-a-hole is worth a verdict), and inventing a
        second consequence for a dependency-shaped hole would be a second
        answer to a question guard 9 already answers.

        The payload: `doomedTaskIds` (the full doomed set, `blocked_by`'s own
        keys -- not only the actionable subset, since a doomed
        `awaiting-validation` task is worth naming too), `deadAncestorIds`
        (every `failed`/`skipped` task reachable by walking `dependsOn` back
        from any doomed task -- the actual dead root cause, as opposed to
        `blocked_by`'s own per-task "direct doomed dependency" lists, which
        stop at the first doomed hop and may themselves be merely doomed,
        not dead), and `planUnverifiable: True` -- the plan cannot produce a
        genuine COMPLETE from here, stated directly rather than left for a
        reader to infer from the other two fields.
        """
        if not blocked_by:
            return None
        tasks = tasks_doc.get("tasks") or []
        actionable_ids = {
            str(t["id"]) for t in tasks
            if isinstance(t, dict) and t.get("id") not in (None, "")
            and task_is_actionable(t)}
        if not actionable_ids:
            return None
        doomed_ids = set(blocked_by.keys())
        if not actionable_ids <= doomed_ids:
            return None
        depends_on: dict[str, list[str]] = {}
        status_by_id: dict[str, str] = {}
        for t in tasks:
            if not isinstance(t, dict) or t.get("id") in (None, ""):
                continue
            tid = str(t["id"])
            status_by_id[tid] = t.get("status", "pending")
            raw = t.get("dependsOn")
            depends_on[tid] = [str(d) for d in raw] if isinstance(raw, list) else []
        dead_ancestors: set[str] = set()
        seen: set[str] = set(doomed_ids)
        stack = list(doomed_ids)
        while stack:
            tid = stack.pop()
            for dep in depends_on.get(tid, []):
                if dep in seen:
                    continue
                seen.add(dep)
                if status_by_id.get(dep) in state.TASK_DEPENDENCY_DEAD_STATUSES:
                    dead_ancestors.add(dep)
                else:
                    stack.append(dep)
        return {
            "doomedTaskIds": sorted(doomed_ids),
            "deadAncestorIds": sorted(dead_ancestors),
            "planUnverifiable": True,
        }

    def _unresolved_failures(self, tasks_doc: dict) -> list[dict]:
        """The `failed` tasks of a plan that has nothing actionable left in it.

        Empty (the normal case) whenever any task is still pending,
        in-progress or validation-failed -- a worker iteration can act on those,
        so the run is not blocked -- and empty for a plan that ended with no
        `failed` task at all, where the worker's own COMPLETE signal is what
        gates review (SPEC R5). Both kinds of `failed` count: a task the engine
        marked `validation-exhausted` and one an agent marked
        `requirement-unmet` block the worker's COMPLETE identically, and the
        reviewer is the right judge of either. An empty/unreadable plan is never
        blocked (`task_is_actionable` reads a status-less task as actionable).
        """
        tasks = tasks_doc.get("tasks") or []
        if not tasks or any(task_is_actionable(t) for t in tasks):
            return []
        return [t for t in tasks if isinstance(t, dict) and task_failure_kind(t)]

    def _outstanding_for_completion(self, tasks_doc: dict) -> list[dict]:
        """The tasks that make a COMPLETE signal unearned, in plan order.

        Task 046 (#55). Terminal for completion is `completed` or `skipped`
        (`state.TASK_COMPLETION_STATUSES`) -- exactly what worker.md's
        completion signal recognises -- so a `pending`, `in-progress`,
        `validation-failed` or `failed` task all show up here. A `failed` one
        deliberately does: the honest resolutions are carving the residual gap
        into a new task or relabelling it `skipped` (worker.md), and until one
        of them happens `_unresolved_failures()` routes the run to review for a
        verdict rather than letting a self-declared completion stand.

        An empty or unreadable plan returns [] -- the signal is honoured and
        review, which re-derives everything from the PRD, is the gate. Refusing
        here instead would strand such a run in the stagnation guard with no
        reviewer ever looking at it, which is the failure shape task 002's
        grace review and task 024's route-to-review both exist to avoid.

        Task 010 (requirement C.2, #114): a `petitioned` task -- outside
        `state.TASK_COMPLETION_STATUSES` by design, so `task_blocks_
        completion` alone would block here forever, exactly like an
        un-adjudicated one must -- is EXCEPTED from that block, and only
        that, when its own engine-filed obligation (`petitions.
        task_petition_obligation`) has a run-scope CONFIRMED adjudication
        (`petitions.confirmed_obligations`/`petitioned_task_accepted`):
        'the task counts as accepted-with-residual; the plan_complete
        contract check accepts it' (PRD C.2). A tampered adjudication
        record is treated as an EMPTY confirmed set (fail towards still
        blocking, never towards trusting a record that might have been
        edited to manufacture a false exemption) -- same posture
        `_review_findings_channel` already takes for the identical
        record. An un-adjudicated `petitioned` task, or one whose
        petition was REFUTED (which moves it to `failed` instead --
        `petitions.apply_refuted_petition_adjudications`, called from
        `_review_verdict` -- so it never reaches this branch as
        `petitioned` again), still blocks exactly like `task_blocks_
        completion` already says.
        """
        tasks = tasks_doc.get("tasks") or []
        confirmed: set[str] | None = None
        outstanding = []
        for t in tasks:
            if isinstance(t, dict) and t.get("status") == "petitioned":
                if confirmed is None:
                    confirmed, tampered = petitions.confirmed_obligations(self.run)
                    if tampered:
                        confirmed = set()
                if petitions.petitioned_task_accepted(t, confirmed):
                    continue
                outstanding.append(t)
                continue
            if task_blocks_completion(t):
                outstanding.append(t)
        return outstanding

    def _apply_refuted_petition_adjudications(self, adjudications) -> list[str]:
        """Task 010 (requirement C.2, #114): the write-back half of
        `engine.petitions.apply_refuted_petition_adjudications` -- read
        the CURRENT `tasks.json`, apply any REFUTED entries in
        `adjudications` to it (mutating any matching `petitioned` task to
        `failed`, remedy recorded), and persist + announce the change
        exactly like any other engine-driven task-status move
        (`_emit_task_changes`, the same seam `_verify_task`'s own
        exhaustion routing uses). A no-op call touches neither the file
        nor the emitted-changes machinery -- `write_tasks` is skipped
        entirely when nothing changed, so a review pass with no REFUTED
        entries never perturbs `tasks.json`'s mtime.

        Returns the ids of every task this call moved to `failed`, same
        as the petitions-module function it wraps.

        Task 007 (requirement B, #114): ALSO calls `petitions.
        record_refuted_adjudications` -- unconditionally, before the
        tasks.json branch above, so the remedy is witnessed in
        `events.jsonl` even for the common case this method's own
        tasks.json mutation never touches: a REFUTED entry whose
        obligation names no `petitioned` task at all (the mid-run
        episode's usual shape, since a worker's own declaration never
        moves the task off its ordinary status). A no-op call (nothing
        REFUTED) records nothing, exactly like the tasks.json half.
        """
        if not adjudications:
            return []
        petitions.record_refuted_adjudications(self.run, adjudications)
        tasks_doc = self.run.read_tasks()
        changed = petitions.apply_refuted_petition_adjudications(
            tasks_doc, adjudications)
        if changed:
            self.run.write_tasks(tasks_doc)
            self._emit_task_changes()
        return changed

    def _apply_criteria_amendments(self, adjudications) -> list[str]:
        """Task 006 (requirement B.1, #114): the write-back half of
        `engine.petitions.criteria_amendment_candidates` -- called from
        `_review_verdict` for EVERY settled review/mid-run-adjudication
        payload, verified or not, exactly like `_apply_refuted_petition_
        adjudications` beside it.

        Reads the petition TYPE from `petitions.open_petitions_in_
        workspace(self.workspace)` -- deliberately NOT `petitions.
        open_unconfirmed_petitions`/`_petitions_review_context`'s own
        run-scope-filtered view: this call happens in the SAME method
        that just persisted this exact obligation as CONFIRMED
        (`record_confirmed_adjudications`, immediately above), so the
        run-scope-filtered view would already exclude the very petition
        whose type this needs to read, for no reason but call order. The
        unfiltered, on-disk view is exactly the fact this decision needs:
        the filed `Type` field never changes once written, regardless of
        run-scope adjudication state.

        For each candidate `engine.petitions.criteria_amendment_
        candidates` returns, mutates the CURRENT `tasks.json`'s matching
        task IN PLACE -- `successCriteria` replaced with the judge's
        `replacement_criteria` text, `criteriaFingerprint` recomputed
        (`_criteria_fingerprint`, the SAME staticmethod `_ensure_criteria_
        baseline`/`_check_criteria_edits` already use, so a later
        `_check_criteria_edits` pass never mistakes this engine-applied
        rewrite for a worker-authored one), `validationAttempts` reset to
        0 for the re-scoped criteria -- and nothing else on the task
        record touched: status, id, title, dependsOn, tier and every
        other field are left exactly as they were. Persists the change
        and announces EACH amendment with its own `task.criteria_amended`
        event (`oldCriteria`, `newCriteria`, `adjudication` -- the
        authorizing CONFIRMED entry, dict-copied so a later mutation of
        the live payload object can never retroactively alter the
        announced record), the same 'engine emits, never silently
        mutates' discipline `_emit_task_changes` already keeps for an
        ordinary status move.

        Returns the ids of every task amended, in `adjudications` order;
        a no-op call (nothing valid to amend and nothing to refuse)
        touches neither the file nor the event log, exactly like its
        REFUTED sibling.

        Task 008 (requirement B.2, #114): before anything is applied,
        every CONFIRMED entry whose `task_id` names something that is
        NOT a real task in the CURRENT plan (`petitions.
        criteria_amendment_refusals`) -- the shape an attempted amendment
        of the PRD text, a Definition-of-done item, or any path under
        `/config` would take, since none of those is ever a key in the
        plan's own task table -- gets its own `task.criteria_amendment_
        refused` event, naming the offending `target` verbatim. This
        fires even when `candidates` ends up empty, so a run that
        attempts nothing BUT an out-of-table amendment still leaves a
        record of the refusal, not silence.
        """
        if not adjudications:
            return []
        tasks_doc = self.run.read_tasks()
        all_tasks = tasks_doc.get("tasks") or []
        candidates = petitions.criteria_amendment_candidates(
            adjudications,
            petitions.open_petitions_in_workspace(self.workspace),
            all_tasks)
        refusals = petitions.criteria_amendment_refusals(adjudications, all_tasks)
        for r in refusals:
            self.run.emit(
                "task.criteria_amendment_refused", target=r["target"],
                obligation=r.get("obligation"), adjudication=r["adjudication"])
        if not candidates:
            return []
        changed = []
        for cand in candidates:
            task = cand["task"]
            old = cand["old_criteria"]
            new = cand["new_criteria"]
            task["successCriteria"] = new
            task["criteriaFingerprint"] = self._criteria_fingerprint(new)
            # Task 024 (requirement E.3, #116 section 2): move the verbatim
            # criteria-of-record forward together with the hash, in the
            # SAME atomic step -- otherwise `_check_criteria_edits` would
            # see a legitimate judge-authored amendment as a rewrite away
            # from the old recorded text and restore it, undoing the one
            # legal amendment path.
            task["criteriaOfRecord"] = new
            task["validationAttempts"] = 0
            changed.append(task.get("id"))
            self.run.emit(
                "task.criteria_amended", taskId=task.get("id"),
                oldCriteria=old, newCriteria=new,
                adjudication=cand["adjudication"])
        self.run.write_tasks(tasks_doc)
        self._emit_task_changes()
        return changed

    def _all_tasks_completed(self) -> bool:
        """True iff tasks.json exists, is non-empty, and every task's
        `status` is a vigilant-or-not TERMINAL-completion status
        (`state.TASK_COMPLETION_STATUSES` -- `completed`, `validated` or
        `skipped`), not just the literal `completed`. Task 002's grace-review
        gate: an empty or missing tasks list (e.g. planning never ran/
        produced nothing) must never be treated as "all done". Task 018
        (#77/#78): without this, a vigilant run whose plan is entirely
        `validated` could never reach this gate at all -- `completed` is a
        status only the non-vigilant table ever assigns."""
        tasks = self.run.read_tasks().get("tasks") or []
        return bool(tasks) and all(
            t.get("status") in state.TASK_COMPLETION_STATUSES for t in tasks)

    async def _maybe_grace_review(self, approach: int) -> bool:
        """Task 002: invariant -- a job whose tasks are ALL completed by the
        time the iteration budget exhausts should still get a review
        verdict if at all possible, rather than going terminal
        failed/unverified with e.g. 7/7 tasks done and no reviewer ever
        having looked at it (the live incident this closes: the operator
        had to `ralphctl resume +3` just to get a review slot).

        Design choice (stated here and in docs/architecture.md): rather
        than reserving the final budget slot ahead of time (which would
        require predicting exhaustion before it happens), grant a single
        OFF-BUDGET review iteration at the moment budget is discovered to
        be exhausted, if and only if every task is already completed and
        this approach hasn't already had one. This is simpler to reason
        about (no speculative slot-reservation bookkeeping earlier in the
        loop) and bounded by construction: `_grace_review_granted` records
        one entry per approach, so this can never run twice for the same
        approach, and it never loops back into the worker -- exactly one
        review, then the job's fate is decided.

        Returns True iff the grace review came back VERIFIED and
        status.json has already been written terminal-succeeded (caller
        should return "succeeded" immediately). Returns False otherwise,
        having set `self._terminal_reason_note` to explain what happened
        (grace review didn't run because tasks weren't all complete, or it
        ran but did not verify) for the normal failed/aborted terminal
        write that follows in the caller.
        """
        if approach in self._grace_review_granted:
            return False
        if not self._all_tasks_completed():
            return False
        self._grace_review_granted.add(approach)
        self.run.emit("log", message=(
            "iteration budget exhausted with all tasks completed; granting "
            "a single off-budget grace review before ending the job "
            "(task 002)"))
        await self._gate()
        self.run.emit("phase", phase="review", approach=approach)
        # Task 016 (requirement I/decision 8): the grace review is still
        # the run-ending review -- verify's top tier, unconditionally.
        result = await self.run_iteration(
            "review", extra=self._review_extra_context(),
            tier=state.run_level_verify_tier())
        # Off-budget: this attempt must never count against the job's
        # iteration budget (same mechanism, and since task 023/#32 the same
        # persistence, as the infra-retry refund).
        self._refund_iteration("grace")
        # Task 016 (requirement F.3, #146): the grace attempt is a single
        # off-budget shot -- there is no budget left to self-loop on an
        # engine-cut-short attempt the way `_run_review_iteration`'s
        # ordinary path does, so an interrupted/timed-out grace review
        # must land on the SAME `unreviewed` outcome as the ordinary path's
        # own budget-exhaustion case, never fall through to the rejected-
        # verdict ladder below (which would misread the engine's own
        # cutoff as review's judgement, exactly the mistake requirement A,
        # #72 already fixed for the ordinary path).
        if self._review_interrupted(result):
            self._review_deadline_unreviewed = True
            self._terminal_reason_note = (
                f"the off-budget grace review (approach {approach}) was "
                "itself interrupted by the engine with no budget left to "
                "ask again; no verdict was ever scored (requirement F.3, "
                "#146)")
            self.run.emit("review_deadline_unreviewed", approach=approach)
            self.run.emit(
                "log",
                message=(f"grace review (approach {approach}) was "
                         "interrupted by the engine; ending unreviewed, "
                         "not rejected (requirement F.3, #146)"))
            return False
        result = await self._review_contract_ladder(result)
        verified, _payload, _errors = self._review_verdict(result)
        # Task 013 (requirement I, #98): the SAME single exit-gate
        # predicate `_step_review` asks (`_terminal_success_blockers`),
        # REPLACING this site's own hand-rolled `self.run.pending_
        # steering()` test -- so "not settled" and "not done" are decided
        # in one place for both gates, and a VERIFIED grace review can no
        # more strand an unsettled operator message than it can bless a
        # non-terminal steering-origin task. (`_all_tasks_completed()`,
        # this method's caller-side guard, already requires every task --
        # steering-origin or not -- to be in `TASK_COMPLETION_STATUSES`
        # before a grace review is granted at all, so the task half is
        # empty by construction on every path that reaches here; kept
        # explicit rather than assumed so a future change to that guard
        # cannot silently reopen the gap this closes.)
        blockers = self._terminal_success_blockers()
        if verified and not blockers:
            # Task 020 rework (requirement L, #101): the peer verdict gate
            # guards EVERY terminal-success path, not only the ordinary
            # `review -> verdict_gate` edge. This off-budget grace review
            # is the second (and only other) site that can write
            # `succeeded`/`verified`, so when the gate is on it gets its
            # own off-budget, equally blind peer call before the run may
            # finish -- otherwise a run that exhausts its budget on the
            # very iteration its last task completes reaches terminal
            # success with the gate never having run, which is exactly the
            # hole an opt-in "nothing succeeds unless a second, independent
            # peer agrees" gate exists to close. Decision (recorded in
            # `artifacts/reports/task-020-requirement-l-rejection-
            # semantics.md`): an off-budget grace REVIEW gets an
            # off-budget grace VERDICT GATE -- charging it would be
            # incoherent (the budget is already spent, which is why we are
            # here at all), and skipping it would make the gate's
            # guarantee conditional on how the budget ran out.
            if self.cfg.peer_verdict_gate and \
                    not await self._grace_verdict_gate(approach):
                self._terminal_reason_note = (
                    "budget exhausted with all tasks completed; the grace "
                    "review VERIFIED but the off-budget blind peer verdict "
                    "gate did not")
                return False
            self.run.emit("signal", signal="VERIFIED")
            self._finish("succeeded", self._review_success_verdict_label(),
                        graceReview=True,
                        reason=("budget exhausted with all tasks completed; "
                                "the grace review ran and VERIFIED"),
                        **self._unconsumed_steering_patch())
            return True
        if verified:
            self._terminal_reason_note = (
                "budget exhausted with all tasks completed; the grace "
                "review VERIFIED but the run cannot go terminal-succeeded: "
                + "; ".join(blockers))
        else:
            self._terminal_reason_note = (
                "budget exhausted with all tasks completed; a grace "
                "review ran but did not verify")
        self.run.emit("log", message=f"grace review did not verify approach {approach}")
        return False

    async def _grace_verdict_gate(self, approach: int) -> bool:
        """Task 020 rework (requirement L, #101): the off-budget peer
        verdict-gate call that guards the OTHER terminal-success site --
        `_maybe_grace_review`'s VERIFIED grace review (task 002) -- so the
        opt-in gate is a property of the run reaching terminal success, not
        a property of one particular edge into `finishing`.

        Bounded by construction exactly as the grace review itself is: it
        is reached only from inside the `_grace_review_granted`-guarded
        block, so at most one call per approach, and it never loops back
        into the worker.

        Off-budget (`_refund_iteration("grace")`, the same mechanism and
        persistence the grace review's own refund uses): the ordinary
        `_step_verdict_gate` charges its iteration like `plan_review`, but
        this path exists precisely because the budget is already exhausted,
        so charging it would either be a no-op or push the run further
        negative for a gate the operator asked for.

        Genuinely BLIND on the same terms as `_step_verdict_gate`:
        `run_iteration("verdict_gate", ...)` with NO `extra` argument --
        review's verdict and `reason` (including THIS grace review's,
        which `_maybe_grace_review` has in hand and deliberately does not
        pass) never reach the peer's prompt.

        Returns True iff the peer independently verified; False for a
        rejection or any unusable reply (`_verdict_gate_verdict`'s safe
        default), leaving the caller to write the ordinary
        failed/unverified terminal state -- fail OPEN in the sense the
        decision note means (never a deadlock, never a stuck run), never a
        silently granted `verified`.
        """
        await self._gate()
        self.run.emit("log", message=(
            "grace review VERIFIED with the peer verdict gate enabled; "
            "granting a single off-budget BLIND peer verdict-gate "
            "iteration before the run may go terminal-succeeded (task 020)"))
        self.run.emit("phase", phase="verdict_gate", approach=approach)
        result = await self.run_iteration(
            "verdict_gate", tier=state.run_level_verify_tier())
        self._refund_iteration("grace")
        approved, payload = await self._verdict_gate_verdict(result)
        self._record_gate_approach_verdict(approach, approved, payload)
        self._record_verdict_gate_peek(approach)
        if approved:
            return True
        reason = (payload or {}).get("reason")
        self.run.emit(
            "log", level="warning",
            message=(f"off-budget peer verdict gate rejected approach {approach}"
                     + (f": {reason}" if reason else
                        " (no usable verdict_gate payload)")))
        return False

    # Bounded retries for a verify iteration that errors out mid-stream
    # (agent/provider failure such as a Bedrock 502, message stopReason ==
    # "error") before ever emitting a verdict sentinel. Infrastructure
    # faults must never be scored as work failures (task 050) -- retry
    # verification instead of touching the task's status/validationAttempts.
    #
    # Task 009 (#5): since `verify` runs through the infra-retry wrapper,
    # this budget is only reached by an error the wrapper did NOT handle --
    # an instant (sub-INSTANT_FAILURE_MAX_DURATION_S) in-band error, or a
    # non-infra one. An infra fault the wrapper retried never lands here at
    # all, so an outage cannot consume these retries; and when the wrapper
    # gives up it sets _abort_reason, so budget_left() ends the loop below
    # instead of double-counting the same outage against this budget.
    MAX_VERIFY_ERROR_RETRIES = 3

    # Task 059: an iteration whose agent process exits nonzero within a few
    # seconds having produced no observable work signal at all (no
    # assistant text, no usage) is almost certainly a provider/auth/infra
    # startup fault (e.g. missing or broken LLM credentials) rather than a
    # genuine attempted-but-failed work iteration. A run of these must
    # never be scored by -- or allowed to advance -- the "no progress"
    # stagnation guard, which exists to detect a stuck APPROACH, not a
    # broken environment; consuming approaches/attempts for them would burn
    # the whole job's budget in seconds without a single real attempt ever
    # having been made (the live incident behind this task: 11 consecutive
    # ~0.6s nonzero-exit iterations burned all 3 approaches in 7 seconds).
    #
    # Task 010 (#5): such a failure is an infra fault too, so it is now
    # *retried* by _run_iteration_with_infra_retry() first -- what stays
    # here is the fail-fast half: a run of MAX_CONSECUTIVE_INSTANT_FAILURES
    # attempts that all fail this way with the same signature stops the job
    # in seconds instead of waiting out the multi-hour outage budget on an
    # environment that cannot work (see _instant_failure_signature).
    INSTANT_FAILURE_MAX_DURATION_S = 5.0
    MAX_CONSECUTIVE_INSTANT_FAILURES = 3

    def _instant_failure_signature(self, result: IterationResult) -> str | None:
        """A stable signature for an *instant, zero-work* failure, or None
        when `result` isn't that shape at all.

        Task 010 (#5): the signature is what tells a broken environment
        apart from a transient infra fault now that both are retried.
        Transient faults vary and take time -- a gateway 502 arrives after
        a connect, a DNS glitch resolves itself, the error text moves
        around -- whereas a broken credential (or a missing agent binary)
        fails identically in 0.6s, every single time. So attempts are only
        counted towards the fail-fast streak while they keep producing the
        *same* signature: the exit code plus the error text with digits
        normalised away (timestamps, ports, request ids).

        An exit-0 attempt counts too when it recorded an in-band error
        (task 001/005's shape): pi can report a fatal startup/provider
        error as an assistant error message and still shut down cleanly.
        """
        if result.interrupted or result.timed_out or result.no_traffic_timeout:
            return None  # not instant / not this shape
        # "No observable work" means no assistant text and no *tokens*: pi
        # zero-fills a usage block on every message_end, so an in-band
        # error's usage dict is non-empty while nothing was ever billed.
        usage = result.usage or {}
        tokens = sum(int(usage.get(k) or 0) for k in
                     ("input", "output", "cacheRead", "cacheWrite", "totalTokens"))
        if result.final_text or tokens:
            return None  # observable work: the agent reached the model
        if result.duration_s is None or \
                result.duration_s >= self.INSTANT_FAILURE_MAX_DURATION_S:
            return None
        error = (result.error_message or "").strip()
        if result.exit_code in (0, None) and not error:
            return None  # not a failure at all
        return f"exit={result.exit_code}|{re.sub(r'[0-9]+', 'N', error.lower())[:200]}"

    def _check_instant_failure(self, result: IterationResult, n: int) -> bool:
        """Update the running consecutive-instant-failure streak for
        `result` (any phase). Returns True once
        MAX_CONSECUTIVE_INSTANT_FAILURES has just been reached, in which
        case self._abort_reason has been set with a clear diagnostic --
        budget_left() is now False and every caller's existing "ran out of
        budget" exit path takes over from here, so the job fails fast with
        state=aborted (not state=failed via the no-progress path) and a
        reason naming the likely cause. A result that isn't an instant
        zero-work failure resets the streak to 0, and so does one whose
        error signature differs from the streak's
        (_instant_failure_signature); self._instant_failure_streak is also
        readable by callers that need to know whether *this* result was an
        instant failure below the abort threshold, to exclude it from their
        own progress bookkeeping without aborting yet.

        Task 010 (#5): the infra-retry wrapper scores every instant attempt
        through here as it happens (instant infra faults are retried now),
        then hands the resolved result back to its caller -- which scores
        it again at the planning/worker call sites. The verdict is
        therefore memoised per result object: scoring one attempt twice
        would inflate the streak and duplicate its log line.
        """
        if result is self._instant_scored_result:
            return self._instant_scored_tripped
        self._instant_scored_result = result
        self._instant_scored_tripped = False
        signature = self._instant_failure_signature(result)
        if signature is None:
            self._instant_failure_streak = 0
            self._instant_failure_sig = None
            return False
        if signature != self._instant_failure_sig:
            # A *different* instant fault: two unrelated transient causes
            # must not add up to a broken-environment verdict.
            self._instant_failure_streak = 0
            self._instant_failure_sig = signature
        self._instant_failure_streak += 1
        self.run.emit(
            "log", level="error",
            message=(f"iteration {n} agent process exited instantly "
                      f"(exit={result.exit_code}, {result.duration_s:.1f}s) "
                      "with no observable work signal -- likely missing or "
                      "broken LLM credentials, or an agent-startup fault "
                      f"({self._instant_failure_streak}/"
                      f"{self.MAX_CONSECUTIVE_INSTANT_FAILURES} consecutive)"))
        if self._instant_failure_streak < self.MAX_CONSECUTIVE_INSTANT_FAILURES:
            return False
        diag = (f"{self._instant_failure_streak} consecutive iterations had "
                "the agent process exit instantly with no observable work "
                "(likely missing or broken LLM credentials, or an "
                "agent-startup fault) -- failing fast instead of burning "
                "through approaches via the no-progress escalation guard")
        self.run.emit("log", level="error", message=diag)
        self._abort_reason = diag
        self._instant_scored_tripped = True
        return True

    @staticmethod
    def _criteria_fingerprint(text: str) -> str:
        return hashlib.sha256((text or "").encode("utf-8")).hexdigest()

    def _ensure_criteria_baseline(self) -> None:
        """Task 008: give every task a criteriaFingerprint (sha256 of its
        successCriteria text) if it doesn't already have one -- true for a
        freshly planned task, and also backfills a run resumed from before
        this feature existed. Never flags anything: there is nothing yet to
        compare a first-sight fingerprint against.

        Task 024 (requirement E.3, #116 section 2): records the same
        baseline text VERBATIM beside the hash, as `criteriaOfRecord` --
        the authoritative copy `_check_criteria_edits` RESTORES a task's
        `successCriteria` from when it catches a worker- or
        planner-authored rewrite. A hash alone can only ever detect such
        a rewrite, never undo it, which is exactly why task 008's version
        of this could do no more than flag the edit and track the new
        text forward. Backfilled for a task that already carries a
        fingerprint only when that fingerprint still matches the task's
        CURRENT text -- if it does not, an unauthorized rewrite is
        already on disk and the pre-rewrite text is unrecoverable, so
        nothing is recorded and `_check_criteria_edits`'s documented
        legacy branch (flag, track forward, no restore) handles it.
        """
        data = self.run.read_tasks()
        changed = False
        for t in data.get("tasks", []):
            current = self._criteria_fingerprint(t.get("successCriteria", ""))
            if "criteriaFingerprint" not in t:
                t["criteriaFingerprint"] = current
                t["criteriaOfRecord"] = t.get("successCriteria", "")
                changed = True
            elif ("criteriaOfRecord" not in t
                    and t.get("criteriaFingerprint") == current):
                t["criteriaOfRecord"] = t.get("successCriteria", "")
                changed = True
        if changed:
            self.run.write_tasks(data)

    def _check_criteria_edits(self, tasks_data: dict) -> None:
        """Task 008: compare each task's current successCriteria against its
        stored criteriaFingerprint. A task seen for the first time (no
        stored fingerprint, e.g. a task discovered mid-run) just gets a
        baseline recorded -- never flagged.

        Task 024 (requirement E.3, #116 section 2): a change is now
        flagged UNCONDITIONALLY -- including while validationAttempts is
        still 0 -- closing the gap where a worker/planner rewrite made
        before any validation failure was silently accepted as a
        legitimate new baseline with no record at all (task 008's
        original, now-superseded negative case). The persistent
        criteriaEditedAfterValidationFailure marker, once set, is never
        cleared (a worker/planner doing this even once is the signal task
        009's review re-verification exists to catch, and task 024's own
        engine-forced blocking finding -- `findings.
        flagged_criteria_findings`, wired into `_review_findings_channel`
        -- exists to enforce, however the criteria keep evolving
        afterwards). The ONE legal way to change a task's own
        successCriteria remains the judge-authored amendment path
        (`_apply_criteria_amendments`), which recomputes this SAME
        fingerprint together with the replacement text in one atomic
        step, so a legitimate amendment is never mistaken for one of
        these unauthorized rewrites.

        The refusal is not merely a marker: when the task carries a
        `criteriaOfRecord` (the verbatim authoritative text recorded
        beside the hash by `_ensure_criteria_baseline` / this method's own
        first-sight branch / `_apply_criteria_amendments`), the rewrite
        DOES NOT STAND -- `successCriteria` is restored to that text, the
        stored fingerprint is left exactly as it was, and the refusal is
        witnessed with its own `task.criteria_edit_refused` event carrying
        both the refused and the restored text. The one case that cannot
        be undone is a task whose recorded text is missing or no longer
        matches its stored hash (a run resumed from before task 024, where
        the pre-rewrite text was never recorded and a sha256 cannot be
        inverted): there the pre-existing task 008 behaviour stands --
        flag, track the new text forward -- and the engine-forced blocking
        finding below is the whole of the enforcement."""
        changed = False
        for t in tasks_data.get("tasks", []):
            current = self._criteria_fingerprint(t.get("successCriteria", ""))
            stored = t.get("criteriaFingerprint")
            if stored is None:
                t["criteriaFingerprint"] = current
                t.setdefault("criteriaOfRecord", t.get("successCriteria", ""))
                changed = True
                continue
            if stored != current:
                if not t.get("criteriaEditedAfterValidationFailure"):
                    t["criteriaEditedAfterValidationFailure"] = True
                of_record = t.get("criteriaOfRecord")
                restorable = (isinstance(of_record, str)
                              and self._criteria_fingerprint(of_record) == stored)
                if restorable:
                    refused = t.get("successCriteria", "")
                    t["successCriteria"] = of_record
                    self.run.emit(
                        "task.criteria_edit_refused", taskId=t.get("id"),
                        refusedCriteria=refused, restoredCriteria=of_record,
                        reason=("a worker- or planner-authored successCriteria "
                                "rewrite has no authority: the ONE legal path "
                                "is a judge-authored amendment on a CONFIRMED "
                                "petition (task 006, #114)"))
                else:
                    t["criteriaFingerprint"] = current
                changed = True
        if changed:
            self.run.write_tasks(tasks_data)

    def _flagged_criteria_review_context(self) -> str:
        """Task 009: render the explicit list of tasks flagged
        criteriaEditedAfterValidationFailure (task 008) as extra review-
        prompt context, instructing the reviewer to independently re-verify
        each such task's CURRENT successCriteria text against the PRD and
        state a conclusion per task. This exists because _verify_task's own
        validationAttempts >= 3 skip would otherwise let a worker dodge
        every future automated check simply by rewriting the bar after a
        failure -- the review phase is the one place left that still sees
        every flagged task, every time, regardless of validationAttempts.
        Returns '' when no task is flagged (the common case), so review
        prompts are unaffected until this ever actually triggers.
        """
        data = self.run.read_tasks()
        flagged = [t for t in data.get("tasks", [])
                   if t.get("criteriaEditedAfterValidationFailure")]
        if not flagged:
            return ""
        lines = [
            "\n## Criteria edited after a validation failure (task 009)\n",
            ("The following task(s) had their `successCriteria` text rewritten "
             "AFTER at least one validation failure, before being re-marked "
             "`completed`. A worker doing this may have quietly moved the bar "
             "instead of doing the work. Before this review can report "
             '`{"verdict": "verified"}`, you MUST independently re-verify '
             "EACH task listed below against its CURRENT successCriteria text "
             "as written now -- not the original text, not what the worker's "
             "notes claim -- and state an explicit pass/fail conclusion for "
             "that task id. Do not let validationAttempts count (even if >= 3) "
             "substitute for this check; that automated skip is exactly what "
             "this manual re-verification exists to cover. Task 008 "
             "(requirement B.2, #114): only a judge-authored amendment on a "
             "CONFIRMED petition, through the existing petition ladder, may "
             "ever rewrite a task's own successCriteria -- a worker or "
             "planner rewriting this text directly, as observed here, has "
             "NO authority to do so. You MUST include this as a BLOCKING "
             'finding (`severity: "blocking"`) in your verdict\'s findings '
             "channel for each task listed below unless your own independent "
             "re-verification, against the rewritten text, still finds the "
             "work genuinely done -- an unauthorized rewrite is refused by "
             "this review, exactly as a tampered adjudication record "
             "(task 011, requirement C) is.\n"),
        ]
        for t in flagged:
            lines.append(
                f"\n- **{t['id']}** ({t.get('title', '')}): current "
                f"successCriteria: {t.get('successCriteria', '')}\n")
        return "".join(lines)

    def _apply_operator_status_rulings(self) -> None:
        """Task 005 (requirement E, #99): the load-time call site of the
        single engine-side task-status applier,
        `state.apply_amendment_status_rulings`. Called once per engine
        episode from `_run_job_core`, before `_resume_point()` -- see the
        comment there for why that position matters.

        Reads `CONFIG_DIR` directly, exactly as
        `_operator_amendment_review_context` does and for the same reason
        (the host's `:ro` bind mount is the only place this run cannot
        forge a ruling into; `overlay_or_config` would let it).

        The applier itself never raises for an ordinary refusal: it returns
        one record per `mode: status` entry, and the ones it did NOT apply
        carry the reason. A refusal must not be silent -- an operator who
        ruled `pending -> validated` needs to see that the engine declined
        rather than wonder why nothing moved -- so every non-applied ruling
        is surfaced here as a `log` event. `already at that status` is not
        reported: that ruling HAS taken effect, and this method runs on
        every episode of the run, so reporting it would repeat one line for
        the rest of the run's life. An applied ruling emits its own
        `amendment_status_applied` audit event from inside the applier
        (source=operator, surfaced like `budget_changed`), so nothing is
        emitted twice from here.

        A broken amendments mount costs nothing more than itself (same rule
        as `_verify_skills_placement`): the run continues unruled.
        """
        try:
            results = state.apply_amendment_status_rulings(
                self.run, CONFIG_DIR, self.cfg.vigilant)
        except (OSError, json.JSONDecodeError) as exc:
            self.run.emit("log", message=(
                "operator status rulings could not be applied: "
                f"{type(exc).__name__}: {exc}"))
            return
        for r in results:
            if r.get("applied") or r.get("reason") == "already at that status":
                continue
            self.run.emit("log", message=(
                f"operator status ruling {r.get('file')} REFUSED for task "
                f"{r.get('taskId')}: {r.get('reason')} "
                f"({r.get('previous')} -> {r.get('status')})"))

    def _operator_amendment_review_context(self) -> str:
        """Task 028 (#26): render every operator ruling recorded under
        `CONFIG_DIR/amendments/` (written HOST-SIDE by `ralphctl amend`,
        never through the API and never through the runtime overlay -- see
        `engine.state`'s amendment vocabulary block and
        artifacts/reports/task-027-operator-ruling-design.md) as extra
        review-prompt context, instructing review to treat each ruling as
        operator-authored and authoritative for the task it names: apply the
        waiver/replacement before deciding a verdict, and never demand
        justification for why the operator is entitled to grant it.

        Reads `CONFIG_DIR` directly -- deliberately NOT `overlay_or_config`.
        `OVERLAY_DIR` lives inside the container's own writable filesystem
        ($HOME/.ralphd/config-overlay), reachable by the worker phase's own
        shell with no API needed; preferring an overlay entry here would let
        the run forge the very ruling this channel exists to keep
        unforgeable. `CONFIG_DIR` itself is the host's `:ro` bind mount
        (`cli.main.config_root(run_id)` on the other side) -- nothing this
        process runs can write there.

        Returns '' when no ruling has been recorded (the common case), so
        review prompts are unaffected until an operator actually amends
        something.
        """
        # Task 005 (requirement E, #99): a `mode: status` ruling is NOT a
        # criteria ruling and must never be rendered as one -- it carries
        # the operator's reason for a status move, not replacement criteria
        # text, and telling review "the text below REPLACES that task's
        # successCriteria" for it is precisely the "a ruling must not be
        # able to look like a claim the criteria were met" failure this
        # requirement forbids. Those entries are applied to tasks.json by
        # `_apply_operator_status_rulings` and bind review not at all.
        entries = [e for e in state.amendment_entries(CONFIG_DIR)
                   if e.get("mode") != state.AMENDMENT_MODE_STATUS]
        if not entries:
            return ""
        lines = [
            "\n## Operator rulings (task 028, #26)\n",
            ("The following ruling(s) were written by the operator directly "
             "into this run's read-only-mounted /config directory via "
             "`ralphctl amend`, run on the HOST -- outside every container "
             "this run ever creates, none of which can write there (the "
             "mount is `:ro`). Treat each one as operator-authored and "
             "AUTHORITATIVE for the task it names: a `WAIVE` ruling means "
             "that task's successCriteria is satisfied as a matter of "
             "operator decision, full stop; a `REPLACE` ruling means the "
             "text below REPLACES that task's successCriteria for the "
             "purpose of this review. Do not demand independent "
             "justification for why the operator is entitled to grant "
             "either kind, and do not let a worker's own claim about a task "
             "substitute for one of these -- only a ruling actually found "
             "here counts.\n"),
        ]
        for e in entries:
            kind = "WAIVE" if e.get("waive") else "REPLACE"
            lines.append(
                f"\n- **task {e.get('taskId')}** ({kind}, recorded "
                f"{e.get('ts')}): {e.get('text')}\n")
        return "".join(lines)

    async def _maybe_run_midrun_adjudication_episode(self) -> None:
        """Task 005 PART 2 (requirement A.2, #114): before the engine ever
        calls `_verify_task` again for any task, run exactly ONE bounded,
        OFF-BUDGET adjudication iteration on the review-hat's OWN model
        (`run_iteration("review", ...)`, which resolves through
        `tiers.resolve_bundle`/`self.cfg.model_for("review")` -- NEVER the
        worker's model), scoped to every impossibility petition currently
        open-and-unconfirmed under this workspace
        (`petitions.open_unconfirmed_petitions`) -- if, and only if, at
        least one such obligation is both open AND not already given a
        bounded episode by an earlier pass of THIS same process
        (`self._petition_adjudication_seen`, updated at the end of this
        method regardless of the episode's own outcome -- see that set's
        own docstring in `__init__` for why re-asking forever would
        otherwise be the default: a petition's workspace filing never
        gains an `Adjudication` field by this module's own design).

        A no-op in the overwhelming common case (no petition open at
        all) -- this method is called unconditionally right before the
        `pending_verify` loop in `_step_worker`, so "before the affected
        task's next validation attempt" holds for every task about to be
        handed to `_verify_task` this pass, not just the one that
        happened to file the declaration.

        Reuses `_review_verdict` for the ENGINE side effect an ordinary
        review iteration already applies from the SAME `petition_
        adjudications` payload field (fact 16/#65: one path, never a
        second one) -- CONFIRMED entries persist at run scope
        (`petitions.record_confirmed_adjudications`, surviving
        `_archive_approach` exactly as an ordinary review's CONFIRMED call
        does -- that file is never one of the three paths that method
        copies away), REFUTED entries move any matching `petitioned` task
        straight to `failed` carrying the remedy
        (`petitions.apply_refuted_petition_adjudications`, via this
        class's own `_apply_refuted_petition_adjudications`). This
        episode's own `verdict` field (required by the `review` contract's
        schema, `verified`/`rejected`) is NEVER treated as a real review
        outcome here -- no `_advance`, no `_finish`, no terminal-success
        side effect of any kind; only the `petition_adjudications` field's
        side effect is what this episode exists for.

        Off-budget by the SAME mechanism and persistence the grace review
        already uses (`_refund_iteration("grace")`): a bonus iteration
        granted outside the ordinary iteration budget, exactly like the
        grace review is, so it never competes with (or is billed against)
        either the run's iteration budget or a task's own
        `validationAttempts` count.
        """
        open_petitions = petitions.open_unconfirmed_petitions(
            self.workspace, self.run)
        if not open_petitions:
            return
        obligations_now = {
            petitions._normalize_obligation(p["obligation"])
            for p in open_petitions}
        if obligations_now <= self._petition_adjudication_seen:
            return
        await self._gate()
        self.run.emit("phase", phase="review", approach=self._approach,
                      midRunAdjudication=True)
        result = await self.run_iteration(
            "review", extra=self._midrun_adjudication_extra(open_petitions),
            tier=state.run_level_verify_tier())
        self._refund_iteration("grace")
        self._petition_adjudication_seen |= obligations_now
        if result is None:
            return
        result = await self._review_contract_ladder(result, mid_run=True)
        self._review_verdict(result, mid_run=True)

    def _midrun_adjudication_extra(self, open_petitions: list[dict]) -> str:
        """The scoped prompt context for the mid-run adjudication episode
        (`_maybe_run_midrun_adjudication_episode`) -- every open-and-
        unconfirmed petition, and instructions that this reply's ONLY job
        is to adjudicate them through the `review` contract's EXISTING
        `petition_adjudications` field, in the SAME CONFIRMED/REFUTED
        vocabulary `docs/petition-format.md` and every ordinary
        run-ending review pass already use (`petitions.
        petitions_review_context`'s own instruction text, restated here
        for a call that is bounded and off-budget rather than run-ending).
        `verdict` is required by the schema but carries NO terminal effect
        for this call. Task 014 (requirement F.1, #144): `verdict:
        "adjudicated"` (or omitting `verdict` altogether) is a legal
        answer for THIS episode specifically -- `transitions.
        MIDRUN_REVIEW_SCHEMA` is what this call's own contract ladder
        (`_review_contract_ladder(..., mid_run=True)`) validates against,
        never the stricter `HAT_SCHEMAS["review"]` an ordinary run-ending
        review answers to.
        """
        lines = [
            "\n## Mid-run petition adjudication episode (task 005, "
            "requirement A.2, #114)\n\n"
            "This is a BOUNDED, OFF-BUDGET adjudication episode, not an "
            "ordinary run-ending review -- your `verdict` field has NO "
            "terminal effect here, and unlike an ordinary review you may "
            "answer `\"verdict\": \"adjudicated\"` (naming what this reply "
            "actually is) or omit `verdict` entirely; neither is a "
            "contract violation for THIS episode. Your ONLY job this "
            "iteration is to adjudicate every "
            "OPEN impossibility petition listed below, one entry per "
            "petition, in your verdict payload's `petition_adjudications` "
            "field: `CONFIRMED` (the obligation genuinely cannot be "
            "satisfied as written) or `REFUTED` (state CONCRETELY, in "
            "that entry's `remedy`, what would satisfy the obligation).\n"
            "\nTask 006 (requirement B.1, #114): if a petition's `Type` is "
            "`contradictory-clauses` or `self-referential` AND its "
            "obligation IS the affected task's own `successCriteria` text "
            "(never a PRD requirement, a Definition-of-done item, or "
            "another task's criteria), you MAY confirm it and re-scope "
            "that ONE task by adding `task_id` (that task's id) and "
            "`replacement_criteria` (the full, achievable replacement "
            "text) to that entry -- the engine applies the rewrite, the "
            "fingerprint recompute and the validation-attempt reset "
            "itself. Omit both fields on every other entry.\n",
        ]
        for p in open_petitions:
            lines.append(
                f"\n- **Obligation**: {p['obligation']}\n"
                f"  **Type**: {p['type']}\n"
                f"  **Evidence**: {p['evidence']}\n")
        return "".join(lines)

    def _petitions_review_context(self) -> str:
        """Task 010 (requirement C, #112): render every OPEN impossibility
        petition filed under this run's WORKSPACE (`artifacts/reports/
        <wave>-petitions.md`, `docs/petition-format.md`) as review-prompt
        context, the same treatment `_operator_amendment_review_context`
        gives an operator ruling -- a named block, joined at the same
        `_review_extra_context` seam.

        Reads `self.workspace`, never `self.run.root`: the petitions file
        is tracked evidence in the code tree a job operates on (committed
        alongside the wave's other `artifacts/reports/*.md`), not run
        state -- exactly the same workspace-vs-run-dir split
        `_operator_amendment_review_context`'s own docstring draws for
        `CONFIG_DIR` vs `OVERLAY_DIR`, applied here to a different pair of
        directories for a different reason (this channel is not about
        unforgeability -- a petition is worker-authored by design -- it is
        about which directory the evidence actually lives in).

        Also passes `self.run` through (task 001, requirement C.4,
        #112): `engine.petitions.petitions_review_context` uses it to
        drop any petition whose obligation the run-scope record
        (`petitions.confirmed_obligations`) already adjudicated CONFIRMED
        -- an obligation review already ruled on, at run scope, does not
        get a second 'MUST adjudicate' instruction on a later review pass
        just because its filing's own `Adjudication` field (a workspace,
        per-file fact) is still open. This is engine-side wiring, not a
        test-only convenience: the exclusion applies to every real review
        call through this one method, the same seam every other
        petitions context uses.

        Returns '' when no petition is open (the common case -- most waves
        file none at all), so review prompts are unaffected until one
        actually exists. All parsing/rendering logic lives in
        `engine.petitions`; this method is the one call site that hands it
        this run's workspace path (and now its run handle).
        """
        return petitions.petitions_review_context(self.workspace, self.run)

    def _petition_adjudications_tamper_context(self) -> str:
        """Task 011 (requirement C.3, #112): warn review, in-prompt, when
        the run-scope CONFIRMED-petition-adjudication record
        (`self.run.petition_adjudications_file`) no longer matches the
        digest `engine.petitions.record_confirmed_adjudications` -- its
        only writer -- always leaves it with. `engine.petitions.
        confirmed_obligations` is the single place that comparison is
        made; this method only renders it (`petitions.tamper_review_
        context`), the same split every other context method in this
        class keeps between 'decide' and 'render'. Returns '' in the
        overwhelming common case (no record yet, or an untouched one).
        """
        _confirmed, tampered = petitions.confirmed_obligations(self.run)
        return petitions.tamper_review_context(tampered)

    def _adjudicated_petitions_review_context(self) -> str:
        """Task 014 (requirement F.1, #144): render every obligation
        already CONFIRMED at run scope (`self.run.petition_adjudications_
        file`) as SETTLED HISTORY, not an open petition -- the review-
        prompt anchor that scopes `review.md`'s re-file prohibition to a
        *new* filing against an obligation that already has one, rather
        than to this entry's own continued appearance across every later
        review pass of the same run. Thin render call into `engine.
        petitions.adjudicated_petitions_review_context`; returns '' in
        the common case (nothing confirmed yet, or a tampered record --
        `read_confirmed_adjudications` already reads that as empty).
        """
        return petitions.adjudicated_petitions_review_context(self.run)

    def _duplicate_petitions_review_context(self) -> str:
        """Task 012 (requirement C.1, #112): tell review, in-prompt, about
        every obligation the engine has already found with more than one
        filed petition under this run's workspace -- so review sees the
        SAME fact its own findings channel is about to be forced to carry
        (`_review_findings_channel`'s appended `duplicate_petition_
        findings`), rather than only discovering the contradiction via a
        re-ask. Thin render call into `engine.petitions.
        duplicate_petitions_review_context`; returns '' in the common case
        (no duplicate/re-filed petition exists).
        """
        return petitions.duplicate_petitions_review_context(self.workspace)

    def _terminal_record_review_context(self) -> str:
        """Task 008 (requirement B.2, #140): tell review, in every review
        prompt, which parts of the run's history are the ENGINE's own
        record -- never review's job to reconstruct, and never a gap in a
        closing report worth a finding over. Mirrors, in the dynamic
        review-prompt context, the same sentence `review.md` itself now
        carries statically (task 008 also edits that file) so the
        guarantee reaches review whether or not the run's own prompt
        overlay still has the current `review.md` text.

        This is pure prose hand-off, unconditional (always returned, never
        gated on a run fact) -- unlike `_petitions_review_context` or
        `_operator_amendment_review_context`, there is no 'common case:
        empty' here; the guarantee applies to every review pass, so the
        text is always present in `_review_extra_context`'s output.
        """
        return (
            "\n## The engine owns the terminal record (task 008, "
            "requirement B.2, #140)\n\n"
            "Petition/adjudication accounting, steer and ruling history, "
            "approach history, terminal tallies and notification "
            "accounting are the engine's record -- written into "
            "`run-record.md`/`run-record.json` at `finishing` (task "
            "006/007, requirement B.1), never a closing report's job to "
            "reconstruct. A closing report is never faulted for omitting "
            "or predating them.\n\n"
            "A closing report's obligations are exactly: what shipped per "
            "requirement, its evidence (commit, test node ids), and the "
            "residuals it knows of, at the code sha it names.\n"
        )

    def _review_materiality_rubric_context(self) -> str:
        """Task 003/004 (requirement A.3/A.2, #110/#139, wiring half): hand
        review the contract's own materiality rubric -- inline OR adopted
        by reference (task 002's `findings.resolve_materiality_rubric`) --
        or, when the contract supplies neither, the engine's own default
        rubric (`findings.DEFAULT_RUBRIC`) plus `findings.NO_RUBRIC_NOTICE`,
        so review never has to guess which case it is in and the default
        stays engine-declared -- a real blocking/residual boundary, never a
        collapse-everything-to-blocking placeholder -- rather than merely
        assumed.

        Reads the same file `build_prompt` already names as "the PRD file"
        (`prd_path(self.run.root) or self.run.prd_file` -- composite when
        the engine has written one, the original otherwise; task 056/#1's
        single decision of which file *is* the run's PRD). A missing file
        (a run that has not written one yet, e.g. very early in planning)
        costs nothing more than itself: `findings.resolve_materiality_
        rubric` is called with `""`, which contains no rubric heading and
        no reference, so review still receives the engine's own default
        rubric rather than this method raising or silently omitting the
        context.

        Unlike `findings.materiality_rubric_context` (still used verbatim
        by other callers/tests), this method resolves references itself so
        it can (a) name the resolved source document path in the rendered
        block when the rubric text came from a reference rather than the
        contract's own inline heading, and (b) emit any `log_events` the
        resolver hands back (an unresolved or rubric-less reference) via
        `self.run.emit("log", **event)` -- the resolver has no `RunDir` of
        its own, so this call site is where that log line actually reaches
        the run's event log.
        """
        resolution = findings.resolve_materiality_rubric(
            self._contract_text(), self.workspace)
        for event in resolution.log_events:
            self.run.emit("log", **event)
        lines = ["\n## Materiality rubric (task 003, requirement A.3, #110)\n"]
        if resolution.rubric is None:
            lines.append(
                f"\n{findings.NO_RUBRIC_NOTICE}\n\n"
                f"{findings.DEFAULT_RUBRIC}\n")
        else:
            if resolution.source_path is not None:
                lines.append(
                    "\nThis run's own materiality rubric is adopted by "
                    f"reference from `{resolution.source_path}` (resolved "
                    "under the workspace root, quoted verbatim below). Use "
                    "it -- not any other source -- to decide each finding's "
                    "`severity`.\n")
            else:
                lines.append(
                    "\nThe contract below is this run's own materiality "
                    "rubric (quoted verbatim from the PRD/composite PRD). "
                    "Use it -- not any other source -- to decide each "
                    "finding's `severity`.\n")
            lines.append(f"\n{resolution.rubric}\n")
        return "".join(lines)

    def _contract_text(self) -> str:
        """THE run's contract text, read from the same file `build_prompt`
        names as "the PRD file" (`prd_path(self.run.root) or
        self.run.prd_file` -- the composite when the engine has written one,
        the original otherwise; task 056/#1's single decision of which file
        *is* the run's PRD). '' when there is no PRD file yet (very early in
        planning, or a stub run dir): every caller treats absence as "the
        contract says nothing", which is the default-safe direction for both
        readers -- the materiality rubric falls back to the engine default
        rubric (requirement A.2/A.3, #110/#139) and exhaustion falls back
        to the pre-wave `failed` routing (requirement C.3, #114).
        """
        prd = prd_path(self.run.root) or self.run.prd_file
        try:
            return prd.read_text()
        except OSError:
            return ""

    def _review_extra_context(self) -> str:
        """Everything appended to a review-phase prompt beyond the PRD and
        tasks.json themselves: flagged-criteria re-verification (task 009),
        the contract's materiality rubric or the engine default rubric
        (task 003/004, requirement A.3/A.2, #110/#139), the engine's
        always-present statement of which parts of the run's history are
        its own terminal record (task 008, requirement B.2, #140), any
        recorded operator ruling (task 028, #26), then every OPEN
        impossibility petition (task 010, requirement C, #112), then every
        ALREADY-CONFIRMED petition rendered as settled history (task 014,
        requirement F.1, #144), then any duplicate/re-filed petition
        already detected and turned into a blocking finding engine-side
        (task 012, requirement C.1, #112).
        All but the rubric and terminal-record contexts return '' in the
        common case, so review prompts are unaffected beyond those two
        fixed hand-offs until one of the other mechanisms actually fires.
        One helper, used at every call site that builds a review prompt,
        so a future context source has exactly one place to join in."""
        return (self._flagged_criteria_review_context() +
                self._review_materiality_rubric_context() +
                self._terminal_record_review_context() +
                self._operator_amendment_review_context() +
                self._petitions_review_context() +
                self._adjudicated_petitions_review_context() +
                self._duplicate_petitions_review_context() +
                self._petition_adjudications_tamper_context())

    @staticmethod
    def _verify_no_verdict(result: IterationResult) -> str:
        """Why this verify attempt produced NO verdict at all, phrased for a
        log line -- or "" when the verifier did reach one (pass or fail).

        Task 012 (#45): the verify path's "was this a verdict?" question used
        to be `result.error_message` alone, i.e. only pi's own in-band error.
        But an iteration the engine itself ended -- SIGINT (`interrupted`),
        the full `iteration_timeout_s` (`timed_out`), the startup-window
        watchdog (`no_traffic_timeout`) -- records no error_message at all,
        so it fell through to the verdict-miss bookkeeping below: the task
        was marked `validation-failed`, a validation attempt was burned, and
        `validationNotes` claimed "Verifier did not emit the task-verified
        sentinel" -- literally true of the bytes on disk, and thoroughly
        misleading about what happened, since no verifier ever finished
        reading the criteria. Three of those and the task is `failed`
        forever, with the record blaming the worker for the engine's own
        timeout.

        Absence of a verdict is not a negative verdict, whatever ended the
        attempt: every branch here means "the verifier never reached a
        verdict", and _verify_task must leave `status`, `validationAttempts`
        and `validationNotes` byte-for-byte alone. error_message stays first
        so its existing wording (and the log lines keyed off it) is
        unchanged.
        """
        if result.error_message:
            return f"errored out ({result.error_message!r})"
        if result.timed_out:
            return "hit its iteration timeout"
        if result.no_traffic_timeout:
            return "was killed by the startup watchdog (no LLM traffic)"
        if result.interrupted:
            return "was interrupted by a signal"
        return ""

    async def _verify_contract_ladder(
        self, tid: str, result: IterationResult
    ) -> IterationResult:
        """Task 001 (#119, requirement A.1): give a verify reply that is
        NOT yet a valid, on-task `verify` contract payload
        (`contract_enforcement.parse_response`'s three ordinary failure
        shapes -- absent, malformed JSON, schema-wrong-shape -- plus the
        two SEMANTIC checks this hat's payload must also clear,
        `transitions.verify_report_errors` and the `task_id == tid`
        cross-check `_verify_task` used to apply only AFTER the fact) the
        SAME uncharged re-ask ladder (`contract_enforcement.
        enforce_contract`) every other hat's payload already gets, instead
        of `_verify_task` reading the broken payload once and immediately
        burning a validation attempt on it.

        Mirrors `_review_contract_ladder`'s shape: skipped entirely when
        `_verify_no_verdict(result)` is non-empty -- ALL FOUR shapes it
        recognises (an engine-side cutoff -- interrupted/timed_out/
        no_traffic_timeout -- or pi's own in-band `error_message`, exit
        code 0 but no traffic to judge) are not a hat mistake to repair,
        they are `_verify_task`'s own bounded error-retry loop's job,
        unchanged by this method's existence. A reply that is ALREADY a
        valid, on-task payload -- whether `verified` or a genuinely
        `rejected` verdict with a `reason` -- also skips the ladder and
        comes back unchanged: there is nothing to repair, and a real
        negative verdict is not a mistake.

        The first ladder attempt reuses THIS already-completed, charged
        verify iteration's own final line -- no extra hat call, no
        double-charge (the caller captures `verify_iter_n` BEFORE calling
        this). Every further attempt is an uncharged
        `_ask_contract_fix("verify", ...)` re-ask, exactly like every other
        contract site -- `iterations_used` never advances for it, and its
        usage is accounted separately under `usage.byPhase["contract"]`.

        On ladder exhaustion, `result` (and therefore its `final_text`)
        comes back byte-for-byte unchanged, so `_verify_task`'s own
        parse-and-record block below sees exactly the original broken
        payload and produces exactly today's verdict-miss/validation-
        failed outcome -- this method never decides that outcome itself,
        only whether there was something to repair first.
        """
        if self._verify_no_verdict(result):
            return result
        try:
            parsed = json.loads(last_nonblank_line(result.final_text))
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict) and not contracts.validate_payload("verify", parsed):
            errors = transitions.verify_report_errors(parsed)
            if not errors and parsed.get("task_id") != tid:
                errors = [
                    f"'verify' contract reported task_id "
                    f"{parsed.get('task_id')!r} but task {tid!r} is "
                    "the one under verification"]
            if not errors:
                return result

        first_attempt_text = last_nonblank_line(result.final_text)
        event_loop = asyncio.get_running_loop()

        def respond(attempt: int, prior_errors: list[str] | None) -> str | None:
            if attempt == 0:
                return first_attempt_text
            raw, _usage = asyncio.run_coroutine_threadsafe(
                self._ask_contract_fix("verify", prior_errors or [],
                                       final_text=result.final_text),
                event_loop).result()
            return last_nonblank_line(raw)

        def extra_validate(payload: dict) -> list[str]:
            errors = transitions.verify_report_errors(payload)
            if not errors and payload.get("task_id") != tid:
                errors = [
                    f"'verify' contract reported task_id "
                    f"{payload.get('task_id')!r} but task {tid!r} is "
                    "the one under verification"]
            return errors

        enforcement = await asyncio.to_thread(
            contract_enforcement.enforce_contract,
            "verify", respond,
            max_retries=self.cfg.contract_retry_max,
            state=self.run,
            extra_validate=extra_validate,
        )
        if enforcement.ok:
            return dataclasses.replace(
                result, final_text=json.dumps(enforcement.payload))
        return result

    async def _verify_task(self, task: dict) -> bool:
        """Run one verify iteration (with bounded retry on transient agent/
        provider errors) for a newly-completed task.

        Task 118 (#60 pillar 1, fact 9): the verifier reports its verdict
        through the `verify` response contract (`{"task_id": ..., "verdict":
        "verified"|"rejected", "reason": ...}`, `contracts.HAT_SCHEMAS
        ["verify"]`) as its final line, exactly like every other hat task
        116 already moved -- it no longer edits `tasks.json` itself, and the
        engine no longer scans `result.final_text` for a literal
        `<task-verified>{id}</task-verified>` sentinel. The harness -- this
        method -- is what applies `validated`/`validation-failed`/`failed`
        to the task record, from the payload it just read back, the same
        "a report is a claim, the file is the fact" discipline
        `_apply_reported_task_status` already uses for review/reflect.

        A reply whose last line is not a well-formed `verify` contract
        payload for THIS task (absent, malformed JSON, a schema violation,
        a `task_id` naming some other task, or a `rejected` verdict with no
        `reason` -- `transitions.verify_report_errors`) is treated exactly
        like the pre-contract fallback always was: an explicit, non-error
        verdict miss, not a run-ending contract violation -- the verifier
        ran to completion and said nothing usable, which is still a verdict
        miss, not a crash.

        Returns True when the verifier reports `verdict: "verified"` for
        this task, False otherwise -- either because the task genuinely
        failed verification (status forced to validation-failed or failed)
        or because verification never reached a verdict at all / ran out of
        budget before producing one (task's status left untouched -- see
        _verify_no_verdict).
        """
        tid = task["id"]
        # Skip tasks that have already exhausted their verification budget
        if task.get("validationAttempts", 0) >= VALIDATION_ATTEMPT_LIMIT:
            log.warning("task %s already exhausted verification attempts; skipping", tid)
            return False
        title = task.get("title", "")
        criteria = task.get("successCriteria", "")
        attempts = task.get("validationAttempts", 0)
        # Task 004 (requirement A.1, #154): verify receives the SAME
        # resolved materiality rubric review does -- inline, adopted by
        # reference, or the engine default (`findings.resolve_materiality_
        # rubric`). `_review_materiality_rubric_context` computes that
        # purely from the contract text and the workspace root; nothing in
        # its body is review-specific, so verify reuses it verbatim rather
        # than re-resolving the same rubric a second way.
        extra = self._review_materiality_rubric_context() + (
            f"## Task under verification\n\n"
            f"- **id**: {tid}\n"
            f"- **title**: {title}\n"
            f"- **successCriteria**: {criteria}\n"
            f"- **validationAttempts**: {attempts} of {VALIDATION_ATTEMPT_LIMIT}\n"
        )
        # Task 004 (#121, requirement A.4): task-verify.md step 4 tells the
        # verifier to recognise the LAST available attempt from
        # `validationAttempts` itself -- name that condition explicitly here
        # (one below the limit) instead of leaving the verifier to compute
        # `VALIDATION_ATTEMPT_LIMIT - 1` from a bare number in prose. Absent
        # at `attempts == 0` (and at every count that is not one below the
        # limit) -- present ONLY on the last available attempt.
        if attempts == VALIDATION_ATTEMPT_LIMIT - 1:
            extra += (
                f"- **FINAL ATTEMPT**: this is the LAST available "
                f"validation attempt (`validationAttempts` is one below "
                f"`VALIDATION_ATTEMPT_LIMIT`) -- a rejecting verdict now "
                f"marks the task `failed`.\n"
            )

        result: IterationResult | None = None
        verified = False
        payload: dict | None = None
        contract_errors: list[str] = []
        error_retries = 0
        while self.budget_left():
            # Task 016 (requirement H/J): per-task verify runs one rung
            # down from the task's CURRENT effective tier -- `state.
            # task_verify_tier`, never `run_level_verify_tier` (that is the
            # run-ending pass in `_step_review`/`_maybe_grace_review`, a
            # separate dispatch from this per-task one).
            result = await self.run_iteration("verify", extra=extra,
                                               prompt_name="task-verify",
                                               tier=state.task_verify_tier(task))
            verify_iter_n = self.iterations_used  # captured after run_iteration increments it
            # Task 001 (#119, requirement A.1): give a not-yet-valid verify
            # payload the SAME uncharged re-ask ladder every other hat
            # already gets, before the parse below ever burns a validation
            # attempt on something a repaired re-ask would have fixed.
            result = await self._verify_contract_ladder(tid, result)

            try:
                parsed = json.loads(last_nonblank_line(result.final_text))
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                payload = parsed
                contract_errors = (contracts.validate_payload("verify", payload)
                                    or transitions.verify_report_errors(payload))
                if not contract_errors and payload.get("task_id") != tid:
                    contract_errors = [
                        f"'verify' contract reported task_id "
                        f"{payload.get('task_id')!r} but task {tid!r} is "
                        "the one under verification"]
            else:
                payload = None
                contract_errors = [
                    "response: no parseable 'verify' contract payload "
                    "(absent or malformed output)"]
            verified = not contract_errors and payload.get("verdict") == "verified"
            # Task 012 (#45): "" only when a verifier actually reached a
            # verdict; anything else means this attempt says nothing at all
            # about the task.
            no_verdict = self._verify_no_verdict(result)

            # Enrich verify iteration meta.json with verifiedTask + verifyOutcome
            itdir = self.run.iteration_dir(verify_iter_n)
            meta_path = itdir / "meta.json"
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text())
                except json.JSONDecodeError:
                    meta = {}
                meta["verifiedTask"] = tid
                if verified:
                    meta["verifyOutcome"] = "pass"
                elif no_verdict:
                    # "error" == no verdict reached (agent/provider error,
                    # timeout, interrupt); "fail" == a verifier judged the
                    # criteria unmet.
                    meta["verifyOutcome"] = "error"
                else:
                    meta["verifyOutcome"] = "fail"
                atomic_write_json(meta_path, meta)

            if verified:
                # Task 020 (requirement F bullet 4, folding the retired
                # vigilant-verified.json sidecar into the task record
                # itself): the terminal write IS the verified record now --
                # no separate approach-namespaced file to keep in sync.
                # Since tasks.json is rewritten fresh (renumbered from
                # "001") by every approach's own planning pass, this is
                # also #29's fix by construction: a later approach's task
                # "001" is a distinct dict with no `status` field carried
                # over from an earlier approach's file, so it can never be
                # misread as already `validated`.
                tasks_data = self.run.read_tasks()
                for t in tasks_data.get("tasks", []):
                    if t["id"] == tid:
                        t["status"] = "validated"
                        # Task 005 (requirement A.2, #154): a verifier's
                        # optional structured `notes` (`{clause, observed,
                        # suggested}` objects) ride onto the task record
                        # as `verifyNotes`, in this SAME write -- never a
                        # second write, never a sidecar file. Absent or
                        # non-list `notes` leaves `verifyNotes` unset
                        # exactly as before this task.
                        notes = payload.get("notes") if payload else None
                        if isinstance(notes, list):
                            t["verifyNotes"] = notes
                        break
                self.run.write_tasks(tasks_data)
                # Task 006 (requirement A.3, #155 engine half): the SAME
                # `notes`, appended to the run's own debt ledger -- through
                # `RunDir.record_debt`, the ONE write path, never a second
                # `debt.json` writer. Called unconditionally: a non-list or
                # empty `notes` is a documented no-op there.
                self.run.record_debt(tid, notes)
                self.run.emit("signal", signal="taskVerified", taskId=tid)
                log.info("task %s verified", tid)
                return True

            if no_verdict and error_retries < self.MAX_VERIFY_ERROR_RETRIES:
                error_retries += 1
                self.run.emit(
                    "log", level="warning",
                    message=(
                        f"verify iteration for task {tid} {no_verdict} "
                        f"before emitting a verdict; retrying verification "
                        f"({error_retries}/{self.MAX_VERIFY_ERROR_RETRIES}) "
                        "without consuming a validation attempt"))
                continue

            break

        if result is None:
            # The budget (iterations, deadline, or an abort) ran out between
            # the caller's check and here, so no verify iteration ran at all.
            # Same rule as every other no-verdict path (task 012, #45):
            # nothing was observed about this task, so nothing is recorded
            # against it.
            self.run.emit(
                "log", level="warning",
                message=(
                    f"no verify iteration ran for task {tid} (no budget "
                    "left); leaving task status, validationAttempts and "
                    "validationNotes unchanged (not a validation failure)"))
            return False

        if not verified and self._verify_no_verdict(result):
            # Either exhausted the bounded retry budget or ran out of
            # iteration budget while retrying. Task 012 (#45): the verifier
            # never reached a verdict -- an infrastructure fault, a timeout or
            # an interrupt, not a verified failure -- so the task's status,
            # validationAttempts and validationNotes are left exactly as they
            # were. The message says WHICH of the two things happened: an
            # operator reading it must be able to tell "the verifier judged
            # this unmet" from "the verifier never got to judge".
            reason = self._verify_no_verdict(result)
            what = (f"kept erroring ({result.error_message!r})"
                    if result.error_message
                    else f"never reached a verdict ({reason})")
            self.run.emit(
                "log", level="error",
                message=(
                    f"verify iteration for task {tid} {what}; leaving task "
                    "status, validationAttempts and validationNotes "
                    "unchanged (not a validation failure)"))
            return False

        # Verification failed with an explicit (non-error) verdict miss: a
        # verifier ran to completion but did not report `verdict: "verified"`
        # for this task via the `verify` contract (task 118) -- either a
        # genuine `rejected` verdict with a reason, or a reply that carried
        # no valid contract payload at all (`contract_errors`, set above).
        # Ensure status is validation-failed and increment the counter.
        tasks_data = self.run.read_tasks()
        for t in tasks_data.get("tasks", []):
            if t["id"] == tid:
                attempts = t.get("validationAttempts", 0) + 1
                t["validationAttempts"] = attempts
                if t.get("status") not in ("validation-failed", "failed"):
                    t["status"] = "validation-failed"
                    if contract_errors:
                        t["validationNotes"] = (
                            "Verifier's final reply carried no valid "
                            "'verify' contract payload for this task, so "
                            "the run treats it as a negative verdict: "
                            + "; ".join(contract_errors))
                    else:
                        t["validationNotes"] = (payload or {}).get(
                            "reason", "").strip() or (
                            "Verifier ran to completion and reported "
                            "verdict: \"rejected\" with no reason given.")
                    # Task 014 (#61's stagnation input): this validation
                    # failure landed while the task's effective tier was
                    # already at `tiers.TOP_TIER` -- there is nowhere left
                    # to escalate to on the next retry, so that fact is
                    # recorded on the record itself, before this task's
                    # NEXT re-entry ever tries (and no-ops) an escalation.
                    if state.mark_tier_ladder_exhausted_if_at_top(t):
                        self.run.emit(
                            "signal", signal="tierLadderExhausted",
                            taskId=tid, tier=state.task_effective_tier(t))
                if attempts >= VALIDATION_ATTEMPT_LIMIT:
                    # Task 009 (requirement C.1, #114): a contract that
                    # carries a petition protocol -- detected the same
                    # default-safe way requirement A.3 (#110) detects a
                    # materiality rubric: by what is actually PRESENT in the
                    # CONTRACT's own text (`petitions.contract_declares_
                    # petition_protocol` over `self._contract_text()`)
                    # rather than a flag nobody would remember to set --
                    # routes exhaustion to `petitioned`, filing the
                    # last-attempt handoff already written into
                    # `validationNotes` above as the petition's evidence
                    # (`petitions.file_exhaustion_petition`), INSTEAD of
                    # forcing `failed`.
                    #
                    # Task 011 (requirement C.3, #114) corrected the
                    # discriminant: it used to be whether the WORKSPACE
                    # carried any `<wave>-petitions.md` file, which routed a
                    # run whose contract says nothing about petitions to
                    # `petitioned` merely because the repo still carried an
                    # EARLIER wave's petitions file. The contract -- and
                    # nothing else -- decides the branch now; the workspace
                    # file is only the filing TARGET, created on demand
                    # (`petitions.ensure_petitions_file`) so its absence can
                    # never become a second, hidden discriminant.
                    #
                    # A contract that declares no protocol (backward
                    # compatibility, requirement C.3) falls straight through
                    # to the `failed` branch below, exactly as it always has;
                    # so does a filing `file_exhaustion_petition` itself
                    # refuses (a duplicate obligation) -- exhaustion must
                    # always land the task SOMEWHERE, never nowhere, and
                    # `failed` is the safe fallback either way.
                    contract_text = self._contract_text()
                    if petitions.contract_declares_petition_protocol(
                            contract_text):
                        if petitions.ensure_petitions_file(
                                self.workspace, contract_text) is None:
                            petition_errors = [
                                "petition: the contract declares a petition "
                                "protocol but no petitions file could be "
                                "created under artifacts/reports/"]
                        else:
                            petition_errors = (
                                petitions.file_exhaustion_petition(
                                    self.workspace, t))
                    else:
                        petition_errors = [
                            petitions.NO_PETITION_PROTOCOL_NOTICE]
                    if not petition_errors:
                        t["status"] = "petitioned"
                        log.warning(
                            "task %s consumed all %d validation attempts; "
                            "contract carries a petition protocol, so the "
                            "last-attempt handoff is filed as a petition and "
                            "the task routes to 'petitioned' for adjudication "
                            "rather than 'failed'", tid, attempts)
                    else:
                        # Task 024 (#33): the engine forcing `failed` here
                        # means ONE thing -- the validation rounds are used
                        # up -- and it says so on the record, because the
                        # same status is also what an agent writes when it
                        # judges a requirement unmeetable. Without the label
                        # the two are indistinguishable to every later reader
                        # (operator, review prompt, `ralphctl tasks`), and the
                        # run's routing (see `_unresolved_failures`) would
                        # have to guess.
                        t["status"] = "failed"
                        t["failureKind"] = TASK_FAILURE_VALIDATION_EXHAUSTED
                        log.warning(
                            "task %s consumed all %d validation attempts "
                            "(failureKind=%s); this records the rounds spent, not "
                            "a judgement that the requirement is unmeetable",
                            tid, attempts, TASK_FAILURE_VALIDATION_EXHAUSTED)
                break
        self.run.write_tasks(tasks_data)
        self._emit_task_changes()
        return False

    def _warn_if_batched(self, before: dict, after: dict) -> None:
        """One task per worker iteration is a design invariant (checkpointing,
        steering, and vigilant verification all key off iteration boundaries).
        The engine can't roll back extra completions, but it must make the
        violation visible."""
        was = {t["id"]: t.get("status") for t in before.get("tasks", [])}
        newly = [t["id"] for t in after.get("tasks", [])
                 if t.get("status") == "completed" and was.get(t["id"]) != "completed"]
        if len(newly) > 1:
            msg = (f"worker completed {len(newly)} tasks in one iteration "
                   f"({', '.join(newly)}); design is one task per iteration")
            log.warning(msg)
            self.run.emit("log", level="warning", message=msg)

    def _relabel_vigilant_claims(self, tasks_data: dict) -> dict:
        """Task 020 (requirement F bullet 4): the harness-owned half of the
        vigilant claim -> verify transition, expressed on the task record
        itself rather than the retired `vigilant-verified.json` sidecar
        (fact 2, #29).

        Task 116 (fact 9) landed that cutover: `_step_worker` now writes
        `awaiting-validation` directly through `_apply_worker_completion_
        report` the moment a worker's own `verdict: "complete"` contract
        report is applied, so a fresh `completed` claim from that channel
        never actually reaches this method any more. This relabel stays as
        a defensive, idempotent no-op for anything else that might still
        leave a bare `completed` behind on disk (a run resumed from a
        pre-116 approach's tasks.json, or a future hat this method has not
        been told about yet) -- not the primary channel any more, but
        cheap insurance against exactly the failure mode #29 already
        taught this codebase to distrust: a second, undeclared path to the
        same status word. This is the one place that
        distinction actually matters: called with the record freshly
        re-read right after a worker iteration's subprocess has already
        exited (never while it is still running, matching requirement F's
        "task state changes only at step transitions" -- this call sits
        squarely between two of them), it relabels every `completed` task
        to the vigilant claim state `awaiting-validation` and persists
        that before anything downstream ever computes `pending_verify`.

        Idempotent by construction: a task already at `awaiting-validation`
        or `validated` is untouched (this method only ever matches
        `completed`), so calling it again on an unchanged record -- e.g. a
        resumed process's very first read, before any new worker iteration
        of its own has run -- is a no-op read, not a spurious write. That
        idempotence, not a separate sidecar, is what makes guard 4's
        property (a crash between completion and verification still leads
        to verification after resume) hold from disk state alone: the
        crash simply leaves the task at whichever of `completed` /
        `awaiting-validation` it last reached, and the next process's
        first call here (or, if the crash landed after this call already
        ran, the unchanged `awaiting-validation` it already reads) still
        surfaces it to `pending_verify` -- there is no window in which the
        task reads as anything else.

        Retires `_warn_if_verify_gate_empty` (task 026/#29's tripwire for a
        broken gate reading as suspiciously quiet): that mechanism existed
        because a SEPARATE approach-namespaced record could desync from
        tasks.json (exactly what #29's key collision did). With the record
        folded into the task itself there is no second copy to desync
        from -- `pending_verify` computing empty is now always correct by
        construction, so the tripwire has nothing left to catch.
        """
        changed = False
        for t in tasks_data.get("tasks", []):
            if t.get("status") == "completed":
                t["status"] = "awaiting-validation"
                changed = True
        if changed:
            self.run.write_tasks(tasks_data)
            self._emit_task_changes()
        return tasks_data

    def _archive_approach(self, approach: int) -> None:
        """Archive this approach's handoff artifacts into `approaches/NN/`
        before a fresh approach overwrites the run root's own copies.

        Task 019 (requirement D.4, #125): beside the three files this
        always archived (`tasks_file`, `notes_file`, review's own
        `findings_file`), also archives the peer verdict gate's OWN
        markdown findings (`verdict_gate_findings_file`) -- a no-op copy
        when the gate is off or approved (the file was never written),
        exactly like `findings_file` itself is a no-op copy on a run that
        never rejected. Both structured JSON sidecars
        (`findings_json_file`, `verdict_gate_findings_json_file`) are
        archived separately by `_archive_findings_json` below, which
        ATTRIBUTES authorship on the way rather than copying byte-for-
        byte -- `_write_composite_prd` below is what later reads these
        archived copies back to fold both judges' findings, with
        authorship, into the next approach's composite PRD.
        """
        dest = self.run.root / "approaches" / f"{approach:02d}"
        dest.mkdir(parents=True, exist_ok=True)
        for f in (self.run.tasks_file, self.run.notes_file,
                 self.run.findings_file, self.run.verdict_gate_findings_file):
            if f.exists():
                shutil.copy2(f, dest / f.name)
        self._archive_findings_json(self.run.findings_json_file, dest, "review")
        self._archive_findings_json(
            self.run.verdict_gate_findings_json_file, dest, "verdict_gate")

    def _archive_findings_json(self, src: Path, dest_dir: Path, author: str) -> None:
        """Task 019 (requirement D.4, #125): archive one structured
        findings sidecar (`src`) into `dest_dir`, attributing `author` to
        every finding on the way (`findings.attribute_authorship`) -- the
        archived copy always carries an `author` field, even when the hat
        that wrote `src` omitted one, because the engine derives it from
        which of the two fixed channels `src` IS, not from anything the
        hat claimed.

        A no-op when `src` does not exist (the ordinary case for the
        gate's own sidecar on a gate-off run, or for either sidecar before
        a live hat has ever written one). A present but UNREADABLE
        sidecar (`findings.read_findings_json` raises) is archived byte-
        for-byte instead of silently dropped -- the same "never invisible"
        posture `_review_findings_channel` documents for a present-but-
        invalid channel -- there is simply nothing to attribute
        authorship to in that case.
        """
        if not src.exists():
            return
        try:
            items = findings.read_findings_json(src)
        except ValueError:
            shutil.copy2(src, dest_dir / src.name)
            return
        attributed = findings.attribute_authorship(items, author)
        findings.write_findings_json(dest_dir / src.name, attributed)

    def _write_composite_prd(self, approach: int) -> None:
        """Fold every archived approach's handoff artifacts into the next
        approach's composite PRD.

        Task 019 (requirement D.4, #125): beside review's own markdown/
        JSON findings (already folded before this task), also folds the
        peer verdict gate's own markdown (`verdict_gate_findings_file`,
        `verdict-gate-findings.md`) and its structured JSON sidecar
        (already attributed `author: "verdict_gate"` by
        `_archive_findings_json` above) -- the two judges' findings are
        distinguishable in the composite text both by NAME (the markdown
        section headers and JSON sidecar filenames name which channel
        each came from) and by the `author` field carried by every
        structured entry.
        """
        parts = [self.run.prd_file.read_text(),
                 "\n\n---\n\n# Previous attempt history\n"]
        for adir in sorted((self.run.root / "approaches").iterdir()):
            review_md = adir / state.FINDINGS_FILE
            gate_md = adir / state.VERDICT_GATE_FINDINGS_FILE
            notes = adir / "notes.md"
            parts.append(f"\n## Approach {adir.name}\n")
            if notes.exists():
                parts.append(f"\n### Final notes\n{notes.read_text()}\n")
            if review_md.exists():
                parts.append(
                    f"\n### Review findings (unmet requirements, `{state.FINDINGS_FILE}`) "
                    "-- author: review\n"
                    f"{review_md.read_text()}\n")
            if gate_md.exists():
                parts.append(
                    f"\n### Peer verdict-gate findings (unmet requirements, "
                    f"`{state.VERDICT_GATE_FINDINGS_FILE}`) "
                    "-- author: verdict_gate\n"
                    f"{gate_md.read_text()}\n")
            for json_path in (adir / state.FINDINGS_JSON_FILE,
                              adir / state.VERDICT_GATE_FINDINGS_JSON_FILE):
                if not json_path.exists():
                    continue
                try:
                    items = findings.read_findings_json(json_path)
                except ValueError:
                    continue
                if items:
                    parts.append(
                        f"\n### Structured findings ({json_path.name})\n"
                        + json.dumps(items, indent=2) + "\n")
        parts.append("\nAddress ALL review findings above in this new attempt. "
                     "Do not blindly repeat the previous approach.\n")
        from .state import atomic_write
        atomic_write(self.run.composite_prd_file, "".join(parts))
