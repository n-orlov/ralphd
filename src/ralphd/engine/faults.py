"""Pure-function classification of a failed iteration as an *infra* fault
(the LLM endpoint/provider/network itself is broken) or a *work* fault (the
agent ran, made LLM calls, and then genuinely failed/exited nonzero).

Task 001a: filed after a live incident where two consecutive worker
iterations hung the *full* 45-minute iteration timeout on a transient
"getaddrinfo ENOTFOUND" gateway-DNS glitch before finally dying with
exit=-2. The pre-existing no-progress escalation guard
(`LoopSupervisor._check_instant_failure`, task 059) only catches *instant*
(sub-`INSTANT_FAILURE_MAX_DURATION_S`) exits with zero work signal; it never
saw this failure because the process didn't exit quickly, it hung. This
module is deliberately independent of that guard (kept unmodified/untouched,
its own tests remain green) -- it exists to classify a *different* shape of
failure: one that either never produced any LLM traffic before eventually
dying/timing out, or died with a recognizably infra-shaped error message
regardless of duration.
"""

from __future__ import annotations

import re

FaultClass = str  # "infra" | "work" | "signal"

# The three verdicts, as constants, because task 013 (#49) added the third one
# and "is this class one of the ones we know about" became a question worth
# being able to ask in one place (tests/test_fault_classifier.py asserts the
# tuple is exactly what the ladder below can return).
FAULT_CLASS_INFRA = "infra"
FAULT_CLASS_WORK = "work"
# Task 013 (#49): an iteration a signal ended after it had reached the model.
# It is neither of the other two: the endpoint was not broken (nothing here
# looks like an outage, so retrying it as one would fight whatever sent the
# signal), and the agent did not fail on its own either -- it was terminated
# before it could. Scoring that as `work` was the defect: `work` is the class
# that burns approach/task-failure bookkeeping, so an iteration killed by an
# OOM kill, a stray `pkill` (requirement I's own subject) or a `docker stop`
# of the agent's process group used to be charged to the agent as a failure it
# never committed. It is deliberately NOT retried (the infra wrapper acts on
# `"infra"` alone): a signal usually means something outside the run wants it
# to stop, and relaunching into that is how a retry loop fights its operator.
FAULT_CLASS_SIGNAL = "signal"
FAULT_CLASSES: tuple[str, ...] = (FAULT_CLASS_INFRA, FAULT_CLASS_WORK,
                                 FAULT_CLASS_SIGNAL)

# Task 014 (#49 part 2): how long an iteration may have run and still have its
# bare in-band `aborted` read as a provider-side stream abort rather than as the
# agent's own failure. SPEC 16 Q1 called this discriminator "a threshold nobody
# can derive from first principles"; the `selfdev-v06-release` run supplies it
# empirically. Iteration 145 of that run recorded `error: aborted` with
# `faultClass: "work"` **39 seconds** into an `iteration_timeout_s` of
# 45 minutes (2700s, the default) -- the leading edge of a DNS outage whose next
# five iterations matched an infra signature and were correctly retried and
# refunded. Same outage, opposite treatment, decided only by whether a token had
# been emitted before the stream died; and 145 was charged for an approach AND
# destroyed a steering note.
#
# 120s is chosen as three times that observed 39s (so the shape that motivated
# the rule is not sitting on the boundary) while staying ~4% of the default
# 45-minute cap: an iteration doing real work reaches the model, plans, edits
# files and runs tests, which is minutes at the very least, so a *worked* whole
# iteration cannot land under this bar. Deliberately absolute rather than a
# fraction of `iteration_timeout_s`: the cap is not recorded in an iteration's
# meta.json, so a fraction could not be re-derived by `state.fault_explanation`
# from the on-disk record and the explanation would diverge from the verdict.
ABORTED_STREAM_MAX_DURATION_S = 120.0

# A *bare* `aborted` -- the whole error text, not `aborted: Premature close`
# (which the `stream` signature family owns) and not a longer message that
# merely mentions the word. pi writes exactly this string as the in-band
# errorMessage for a stream that was aborted at either end.
_BARE_ABORTED_RE = re.compile(r"^aborted[.!]?$", re.IGNORECASE)


def is_bare_aborted(error_text: str | None) -> bool:
    """True when ``error_text`` is pi's bare ``aborted`` and nothing else
    (task 014, #49)."""
    return bool(_BARE_ABORTED_RE.match((error_text or "").strip()))

# Text signatures that mean "the fault is in reaching/using the LLM
# endpoint itself", independent of whether any traffic was ever observed.
#
# Task 002 (#11): one reviewable table, one commented line per *family*,
# covering every shape observed against the real gateway/Bedrock stack in
# the deck-phase1 incidents on top of the pre-existing network families.
# This table is the whole contract for "is this the provider's fault"; it is
# asserted family-by-family (plus negative cases) in
# tests/test_fault_classifier.py.
#
# Task 025 (#18.4): each row now carries the family label it used to carry
# only as a comment, because the *explanation* surfaces (`ralphctl fault`,
# the hub's fault dialog) have to name WHICH signature matched -- an
# operator told "infra fault" and nothing else still has to guess whether
# the endpoint's name failed to resolve or the provider was throttling. The
# labels are part of the table, not a second table keyed by pattern, so a
# new pattern cannot be added without saying which family it belongs to.
INFRA_SIGNATURES: tuple[tuple[str, str], ...] = (
    # DNS: name never resolved, or resolver reported a temporary failure.
    ("dns", r"ENOTFOUND"),
    ("dns", r"EAI_AGAIN"),
    ("dns", r"getaddrinfo"),
    # TCP connect/teardown: refused, reset, timed out, unreachable host/net.
    ("tcp", r"ECONNREFUSED"),
    ("tcp", r"ECONNRESET"),
    ("tcp", r"ETIMEDOUT"),
    ("tcp", r"EHOSTUNREACH"),
    ("tcp", r"ENETUNREACH"),
    # Half-closed/broken socket while a response was streaming.
    ("stream", r"EPIPE"),
    ("stream", r"socket hang up"),
    ("stream", r"premature close"),
    # TLS: handshake and certificate validation failures.
    ("tls", r"TLS handshake"),
    ("tls", r"SSL handshake"),
    ("tls", r"certificate verify failed"),
    # The client SDK's own opaque transport failure -- pi surfaces a gateway
    # outage verbatim as "Connection error." with zero token usage.
    ("sdk", r"connection error"),
    # HTTP gateway/proxy 5xx returned instead of a model response.
    ("http-5xx", r"\bbad gateway\b"),
    ("http-5xx", r"\bgateway timeout\b"),
    ("http-5xx", r"\bservice unavailable\b"),
    ("http-5xx", r"ServiceUnavailable"),  # AWS/Bedrock naming (no space)
    ("http-5xx", r"internal server error"),
    ("http-5xx", r"\b50[234]\b"),
    # Provider back-pressure: HTTP 429, Anthropic's 529, throttling.
    ("backpressure", r"\b429\b"),
    ("backpressure", r"\b529\b"),
    ("backpressure", r"rate[ _-]?limit"),  # rate limit / rate-limit / ratelimit
    ("backpressure", r"throttl"),  # throttled / throttling / ThrottlingException
    ("backpressure", r"overloaded"),  # Overloaded / overloaded_error
    # Bedrock mid-stream fault, and capacity/quota exhaustion upstream.
    ("bedrock-stream", r"ModelStreamErrorException"),
    ("capacity", r"quota"),
    ("capacity", r"capacity"),
    # NOT in this table, deliberately (task 003, #11): a bare "aborted".
    # pi records that exact string as the in-band errorMessage whenever the
    # agent process takes a SIGINT -- which the *operator* can cause
    # (POST /abort, POST /interrupt) just as easily as a provider-side
    # stream abort can. The text alone cannot tell the two apart, so that
    # shape is decided from the `operator_abort` / `produced_traffic`
    # inputs below instead of from a signature. ("aborted: Premature
    # close" and friends still match via their own family above.)
)

# What each family MEANS, in one clause, for whoever is reading the
# explanation rather than the table. Every family in INFRA_SIGNATURES has an
# entry (asserted in tests/test_fault_explanation.py).
INFRA_FAMILY_DESCRIPTIONS = {
    "dns": "the endpoint's name did not resolve",
    "tcp": "the connection was refused, reset, timed out or unreachable",
    "stream": "the response stream was cut mid-flight",
    "tls": "the TLS handshake or certificate validation failed",
    "sdk": "the client SDK reported an opaque transport failure",
    "http-5xx": "the gateway returned a 5xx instead of a model response",
    "backpressure": "the provider pushed back (429/529, throttling, overloaded)",
    "bedrock-stream": "Bedrock faulted mid-stream",
    "capacity": "capacity or quota was exhausted upstream",
}

# Per-row regexes, in table order: the ONE matcher. There is deliberately no
# combined alternation any more -- "does anything match" and "which row
# matched" must be the same question, asked once.
_INFRA_SIGNATURE_RES = tuple(
    (family, pattern, re.compile(pattern, re.IGNORECASE))
    for family, pattern in INFRA_SIGNATURES
)

# Why a fault got the class it got -- the classifier explaining ITSELF, in one
# clause per branch of the ladder below, so the CLI and the hub cannot word the
# same verdict differently (task 025, #18.4).
FAULT_REASON_NOT_A_FAILURE = "no failure signal recorded (nothing to explain)"
# Steering 004 (task 025, #18.4): the abort/interrupt carve-out has THREE
# wordings, because its input has three possible causes and only one of them
# can be established. `operator_abort` (LoopSupervisor.operator_abort_requested)
# is true for POST /abort, for POST /interrupt *and* for the engine giving up on
# its own (an exhausted outage budget, a signal from anywhere -- loop.py's own
# comment says the flag cannot tell them apart), so the explanation must not
# name the operator unless `operator_abort_recorded`
# (LoopSupervisor._operator_abort_recorded) says an abort came from outside.
# The live counter-example: a `pkill` against the engine set `_abort_reason` to
# "signal 15" with no operator anywhere near a /abort request.
FAULT_REASON_OPERATOR_ABORT = (
    "an operator-requested abort/interrupt -- never retried as an outage")
FAULT_REASON_ABORT_RECORDED = (
    "an abort or interrupt is recorded for this run -- nothing here is retried "
    "as an outage (who asked for it is not established: a POST /abort, an "
    "operator interrupt and the engine giving up on its own all set this same "
    "flag)")
FAULT_REASON_ABORT_INTERRUPTED = (
    "a signal ended this iteration and an abort/interrupt is recorded for the "
    "run -- nothing here is retried as an outage (what sent the signal is not "
    "established; the run's own abort reason is reported verbatim beside this)")
# Task 013 (#49): this reason now also carries its own class
# (`FAULT_CLASS_SIGNAL`); the wording was already right, the verdict beside it
# was not. The text is deliberately unchanged so the surfaces that quote it
# (`ralphctl fault`, the hub dialog) read the same as before.
FAULT_REASON_INTERRUPTED = (
    "a signal ended this iteration after it had reached the model -- it did "
    "not fail on its own")
FAULT_REASON_NO_TRAFFIC_TIMEOUT = (
    "the startup watchdog fired: no LLM traffic within the startup window")
FAULT_REASON_SIGNATURE = "the error text matched a known infra signature"
FAULT_REASON_WORK = (
    "the agent reached the model and then failed, with no infra signature "
    "in its error")
FAULT_REASON_NO_TRAFFIC_UNCLASSIFIED = (
    "no LLM traffic at all and no recognized signature -- an unclassifiable "
    "no-traffic failure is treated as infra")
# Task 014 (#49 part 2): the answer to SPEC 16 Q1 -- a bare `aborted` that
# arrived after real traffic, with no abort recorded for the run, and well
# inside the iteration's own timeout, is the provider hanging up mid-stream.
FAULT_REASON_ABORTED_STREAM = (
    "a bare `aborted` ended this iteration well inside its own timeout with no "
    "abort recorded for the run -- the provider hung up mid-stream")


def matched_signature(error_text: str | None) -> dict | None:
    """Which row of ``INFRA_SIGNATURES`` matches ``error_text``, or None
    (task 025, #18.4).

    Returns ``{"family", "description", "pattern", "match"}``: the family
    label, its one-clause meaning, the regex source that fired and the exact
    substring it matched in the error. Table order decides when several rows
    could match, and this is the ONLY place the patterns are matched, so "is
    there a match" here can never disagree with ``classify_fault``'s own
    verdict (asserted over every family case in
    tests/test_fault_explanation.py).
    """
    text = error_text or ""
    if not text:
        return None
    for family, pattern, regex in _INFRA_SIGNATURE_RES:
        found = regex.search(text)
        if found:
            return {"family": family,
                    "description": INFRA_FAMILY_DESCRIPTIONS.get(family, ""),
                    "pattern": pattern,
                    "match": found.group(0)}
    return None


def explain_fault(
    *,
    error_text: str = "",
    exit_code: int | None = None,
    interrupted: bool = False,
    timed_out: bool = False,
    no_traffic_timeout: bool = False,
    produced_traffic: bool = False,
    operator_abort: bool = False,
    operator_abort_recorded: bool = False,
    duration_s: float | None = None,
) -> dict:
    """``classify_fault``'s verdict *with its reasoning*, from exactly the same
    inputs (task 025, #18.4).

    Returns ``{"faultClass", "reason", "signature"}`` where ``faultClass`` is
    what ``classify_fault`` returns (None / "infra" / "work" / "signal"),
    ``reason`` is
    the one clause from the ``FAULT_REASON_*`` vocabulary naming which branch
    of the ladder decided it, and ``signature`` is ``matched_signature``'s dict
    (present whenever the error text matches a known signature, even when
    another branch -- the operator carve-out -- overrode the verdict, because
    an operator reading the explanation wants to know the text looked infra
    even if it was not retried).

    ``classify_fault`` delegates here, so there is exactly ONE ladder: an
    explanation can never describe a decision the engine did not make.

    Steering 004: ``operator_abort`` means "an abort or interrupt is recorded
    for this run", NOT "an operator did this" -- the caller's flag
    (``LoopSupervisor.operator_abort_requested``) is equally true for a POST
    /abort, a POST /interrupt and the engine giving up on its own, and its own
    docstring says it cannot tell them apart. So the reason for that branch is
    worded neutrally unless ``operator_abort_recorded``
    (``LoopSupervisor._operator_abort_recorded``: an abort that arrived from
    outside) establishes the operator; when it does, and only then, this says
    "operator-requested". ``operator_abort_recorded`` is an *explanation-only*
    input: it never changes ``faultClass`` (asserted in
    tests/test_cli_fault_explanation.py).

    Task 013 (#49): a signal that ended an iteration which HAD reached the
    model is now its own class, ``FAULT_CLASS_SIGNAL`` -- see that constant for
    why it is neither ``"work"`` nor ``"infra"`` and why it is not retried.

    Task 014 (#49 part 2, closing SPEC 16 Q1): ``duration_s`` is how long the
    agent process actually ran (``IterationResult.duration_s``; the on-disk
    re-derivation passes the iteration's recorded ``durationS``). It decides
    exactly ONE shape -- a bare ``aborted`` after real traffic, with no abort
    recorded, that ended within ``ABORTED_STREAM_MAX_DURATION_S`` -- which is
    ``"infra"`` rather than ``"work"``. An unknown (``None``) duration never
    fires that branch: nothing is known, so nothing is reclassified.

    Known and deliberate imprecision that remains (issue #49, which replaced
    the now-closed #23 as the owner of this taxonomy): an engine-side give-up
    and an abort from anywhere are still classified ``"work"`` by the abort
    carve-out above -- deliberately, because that branch's whole job is "never
    retry this as an outage" -- even though nobody attempted any work; only the
    *wording* distinguishes those. And a signal-terminated iteration that never
    reached the model stays ``"infra"`` by the no-traffic rule below, on
    purpose: with zero traffic it is textually indistinguishable from a
    provider-side stream abort, and treating "nothing ran" as retryable and
    refundable is exactly the accounting rule that rule exists to enforce.
    """
    signature = matched_signature(error_text)
    is_failure = (
        no_traffic_timeout
        or timed_out
        or interrupted
        or bool((error_text or "").strip())
        or (exit_code is not None and exit_code != 0)
    )
    if not is_failure:
        return {"faultClass": None, "reason": FAULT_REASON_NOT_A_FAILURE,
                "signature": signature}
    if operator_abort:
        # An abort/interrupt is on the record, so nothing here is retryable
        # (see the task 003 paragraph in `classify_fault`) -- but say only
        # what the inputs support about WHO ended it (steering 004).
        if operator_abort_recorded:
            reason = FAULT_REASON_OPERATOR_ABORT
        elif interrupted:
            reason = FAULT_REASON_ABORT_INTERRUPTED
        else:
            reason = FAULT_REASON_ABORT_RECORDED
        return {"faultClass": "work", "reason": reason,
                "signature": signature}
    if no_traffic_timeout:
        return {"faultClass": "infra",
                "reason": FAULT_REASON_NO_TRAFFIC_TIMEOUT,
                "signature": signature}
    if signature is not None:
        return {"faultClass": "infra", "reason": FAULT_REASON_SIGNATURE,
                "signature": signature}
    if produced_traffic:
        # Steering 004: "the agent reached the model and then failed" is the
        # wrong answer to "what killed my iteration?" when a signal did -- it
        # got the *reason* right here. Task 013 (#49) finishes the job: the
        # verdict is no longer `work` either, because `work` is what burns the
        # bookkeeping for a failure the agent did not commit. `interrupted`
        # means the agent process died on a signal (a negative exit code, or
        # 130 -- runner.py sets it from nothing else), and this branch is only
        # reached with no abort recorded for the run (the carve-out above owns
        # that case) and no infra signature in the error.
        if interrupted:
            return {"faultClass": FAULT_CLASS_SIGNAL,
                    "reason": FAULT_REASON_INTERRUPTED, "signature": None}
        # Task 014 (#49 part 2): the answer to SPEC 16 Q1. A bare `aborted`
        # with a CLEAN exit status (so no signal: `interrupted` is False and
        # the branch above did not fire) that arrived within
        # ABORTED_STREAM_MAX_DURATION_S is the provider hanging up mid-stream,
        # not the agent failing: no real work iteration finishes -- let alone
        # fails with a one-word error -- inside two minutes, and every other
        # reading of a bare `aborted` is already owned by a branch above (an
        # operator/engine abort by the carve-out, a signal by the branch
        # above, `aborted: Premature close` by the `stream` signature family).
        # Being `infra` it is retried and refunded, which is the whole point:
        # iteration 145 of selfdev-v06-release was charged an approach for one
        # of these while the five iterations after it were refunded.
        if (is_bare_aborted(error_text) and duration_s is not None
                and duration_s <= ABORTED_STREAM_MAX_DURATION_S):
            return {"faultClass": FAULT_CLASS_INFRA,
                    "reason": FAULT_REASON_ABORTED_STREAM, "signature": None}
        return {"faultClass": FAULT_CLASS_WORK, "reason": FAULT_REASON_WORK,
                "signature": None}
    return {"faultClass": "infra",
            "reason": FAULT_REASON_NO_TRAFFIC_UNCLASSIFIED,
            "signature": None}


def classify_fault(
    *,
    error_text: str = "",
    exit_code: int | None = None,
    interrupted: bool = False,
    timed_out: bool = False,
    no_traffic_timeout: bool = False,
    produced_traffic: bool = False,
    operator_abort: bool = False,
    operator_abort_recorded: bool = False,
    duration_s: float | None = None,
) -> FaultClass | None:
    """Classify one finished iteration's failure signal.

    Returns ``None`` when this wasn't a failure at all (clean exit code 0,
    *no error recorded*, not interrupted, not timed out, no startup-window
    kill) -- callers should not retry or otherwise react to a success.

    Task 001 (#11): a non-empty ``error_text`` is a failure signal in its
    own right, regardless of ``exit_code``. pi records an in-band agent
    error (``message_end`` with ``stopReason: "error"``) and can still
    exit 0 -- the observed shape is a gateway/provider error surfaced as
    an assistant error message with zero token usage, after which the
    process shuts down cleanly. Keying "was this a failure?" off the exit
    code alone silently scored those iterations as successes: no retry, no
    refund, and the iteration budget burned on iterations that never ran.
    An error was recorded, so it failed; only the *class* (infra vs work)
    is decided by the rest of this function.

    Returns ``"infra"`` when the failure looks like a broken LLM
    endpoint/provider/network rather than a genuine attempted-but-failed
    work iteration:

    - the engine's own startup-window watchdog killed the process because
      it produced zero observable LLM traffic within the configured window
      (`no_traffic_timeout=True`) -- always infra, regardless of exit code
      or error text (task 001a's core scenario: a hang, not an instant
      exit);
    - the captured error text matches a known infra signature from
      ``INFRA_SIGNATURES`` (DNS, TCP connect/reset, broken stream, TLS,
      the SDK's "Connection error.", a gateway 5xx, provider back-pressure
      like 429/529/throttling/overloaded, a Bedrock stream fault, or
      capacity/quota exhaustion) -- infra even if some traffic happened to
      precede it;
    - any other failure that produced no LLM traffic at all and doesn't
      match a recognized infra signature is *still* classified infra
      (deliberately -- an unclassifiable no-traffic failure is far more
      likely to be an environment/startup fault than a genuine work
      failure, and scoring it as "work" would let it silently burn
      approach/task-failure bookkeeping instead of being retried).

    Returns ``"work"`` for the case squarely inside the agent's own
    responsibility: it produced real LLM traffic (assistant text and/or
    token usage were observed) and *then* exited nonzero or timed out, with
    no recognized infra signature in its error text -- and for an
    *operator-initiated* termination (see below), which is nobody's fault but
    must never be retried as an outage.

    Task 014 (#49 part 2) narrows ``"work"`` once more, closing SPEC 16 Q1: a
    bare ``aborted`` that arrived after real traffic, with no abort recorded
    for the run and a clean exit status, within
    ``ABORTED_STREAM_MAX_DURATION_S`` of the iteration's start is ``"infra"``
    (a stream the provider hung up on), so it is retried and refunded instead
    of costing an approach. Past that threshold it stays ``"work"``.

    Task 013 (#49): returns ``"signal"`` for the shape that used to be folded
    into ``"work"`` -- an iteration that reached the model and was then ended
    by a signal, with no abort recorded for this run. It was terminated before
    it could fail on its own, so it is not the agent's failure; nothing about
    it looks like an endpoint outage either, so it is not retried. See
    ``FAULT_CLASS_SIGNAL``.

    Task 003 (#11): ``operator_abort`` is the caller's real abort/interrupt
    bookkeeping (``LoopSupervisor.operator_abort_requested``: POST /abort
    recorded an abort reason, or POST /interrupt actually delivered a
    SIGINT to the running agent). It exists because pi records a SIGINT as
    the in-band error text ``"aborted"`` with no traffic and no exit code
    of its own -- textually identical whether the signal came from a
    provider-side stream abort (a genuine transient infra fault worth
    retrying, which is what a bare ``"aborted"`` with no traffic is
    classified as) or from the operator asking this run to stop. Retrying
    the latter would fight the operator: the wrapper would sit in backoff
    and re-run the very iteration that was just aborted. So when an
    operator-initiated abort/interrupt is recorded, the failure is never
    ``"infra"`` -- regardless of error text, traffic or watchdog state --
    which keeps it out of the infra retry loop entirely.

    Task 025 (#18.4): the ladder itself lives in ``explain_fault``, which
    returns this same verdict together with the reason it chose and the
    signature that matched. This function is the verdict half of that one
    decision -- there is no second copy of the branching to drift.

    Steering 004: ``operator_abort_recorded`` is accepted and forwarded purely
    so the *explanation* can distinguish an abort that came from outside from
    the engine giving up on its own; it does not (and must not) change any
    verdict this function returns. Wording the difference was task 025's scope;
    re-classifying these shapes is issue #49 -- task 013 took the signal case
    out of ``"work"``. (This paragraph used to name #23, now closed.)
    """
    return explain_fault(
        error_text=error_text,
        exit_code=exit_code,
        interrupted=interrupted,
        timed_out=timed_out,
        no_traffic_timeout=no_traffic_timeout,
        produced_traffic=produced_traffic,
        operator_abort=operator_abort,
        operator_abort_recorded=operator_abort_recorded,
        duration_s=duration_s,
    )["faultClass"]
