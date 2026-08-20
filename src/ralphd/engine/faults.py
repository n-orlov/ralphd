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

FaultClass = str  # "infra" | "work"

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
FAULT_REASON_OPERATOR_ABORT = (
    "an operator-initiated abort/interrupt -- never retried as an outage")
FAULT_REASON_NO_TRAFFIC_TIMEOUT = (
    "the startup watchdog fired: no LLM traffic within the startup window")
FAULT_REASON_SIGNATURE = "the error text matched a known infra signature"
FAULT_REASON_WORK = (
    "the agent reached the model and then failed, with no infra signature "
    "in its error")
FAULT_REASON_NO_TRAFFIC_UNCLASSIFIED = (
    "no LLM traffic at all and no recognized signature -- an unclassifiable "
    "no-traffic failure is treated as infra")


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
) -> dict:
    """``classify_fault``'s verdict *with its reasoning*, from exactly the same
    inputs (task 025, #18.4).

    Returns ``{"faultClass", "reason", "signature"}`` where ``faultClass`` is
    what ``classify_fault`` returns (None / "infra" / "work"), ``reason`` is
    the one clause from the ``FAULT_REASON_*`` vocabulary naming which branch
    of the ladder decided it, and ``signature`` is ``matched_signature``'s dict
    (present whenever the error text matches a known signature, even when
    another branch -- the operator carve-out -- overrode the verdict, because
    an operator reading the explanation wants to know the text looked infra
    even if it was not retried).

    ``classify_fault`` delegates here, so there is exactly ONE ladder: an
    explanation can never describe a decision the engine did not make.
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
        # The operator asked for this to stop; nothing here is retryable
        # (see the task 003 paragraph in `classify_fault`).
        return {"faultClass": "work", "reason": FAULT_REASON_OPERATOR_ABORT,
                "signature": signature}
    if no_traffic_timeout:
        return {"faultClass": "infra",
                "reason": FAULT_REASON_NO_TRAFFIC_TIMEOUT,
                "signature": signature}
    if signature is not None:
        return {"faultClass": "infra", "reason": FAULT_REASON_SIGNATURE,
                "signature": signature}
    if produced_traffic:
        return {"faultClass": "work", "reason": FAULT_REASON_WORK,
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
    token usage were observed) and *then* exited nonzero/timed out/was
    interrupted, with no recognized infra signature in its error text --
    and for an *operator-initiated* termination (see below), which is
    nobody's fault but must never be retried as an outage.

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
    """
    return explain_fault(
        error_text=error_text,
        exit_code=exit_code,
        interrupted=interrupted,
        timed_out=timed_out,
        no_traffic_timeout=no_traffic_timeout,
        produced_traffic=produced_traffic,
        operator_abort=operator_abort,
    )["faultClass"]
