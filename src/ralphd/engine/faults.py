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
_INFRA_TEXT_PATTERNS = (
    # DNS: name never resolved, or resolver reported a temporary failure.
    r"ENOTFOUND",
    r"EAI_AGAIN",
    r"getaddrinfo",
    # TCP connect/teardown: refused, reset, timed out, unreachable host/net.
    r"ECONNREFUSED",
    r"ECONNRESET",
    r"ETIMEDOUT",
    r"EHOSTUNREACH",
    r"ENETUNREACH",
    # Half-closed/broken socket while a response was streaming.
    r"EPIPE",
    r"socket hang up",
    r"premature close",
    # TLS: handshake and certificate validation failures.
    r"TLS handshake",
    r"SSL handshake",
    r"certificate verify failed",
    # The client SDK's own opaque transport failure -- pi surfaces a gateway
    # outage verbatim as "Connection error." with zero token usage.
    r"connection error",
    # HTTP gateway/proxy 5xx returned instead of a model response.
    r"\bbad gateway\b",
    r"\bgateway timeout\b",
    r"\bservice unavailable\b",
    r"ServiceUnavailable",  # AWS/Bedrock exception naming (no space)
    r"internal server error",
    r"\b50[234]\b",
    # Provider back-pressure: HTTP 429, Anthropic's 529, throttling.
    r"\b429\b",
    r"\b529\b",
    r"rate[ _-]?limit",  # rate limit / rate-limit / ratelimit
    r"throttl",  # throttled / throttling / ThrottlingException
    r"overloaded",  # Overloaded / overloaded_error
    # Bedrock mid-stream fault, and capacity/quota exhaustion upstream.
    r"ModelStreamErrorException",
    r"quota",
    r"capacity",
    # NOT in this table, deliberately (task 003, #11): a bare "aborted".
    # pi records that exact string as the in-band errorMessage whenever the
    # agent process takes a SIGINT -- which the *operator* can cause
    # (POST /abort, POST /interrupt) just as easily as a provider-side
    # stream abort can. The text alone cannot tell the two apart, so that
    # shape is decided from the `operator_abort` / `produced_traffic`
    # inputs below instead of from a signature. ("aborted: Premature
    # close" and friends still match via their own family above.)
)
_INFRA_TEXT_RE = re.compile("|".join(_INFRA_TEXT_PATTERNS), re.IGNORECASE)


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
      ``_INFRA_TEXT_PATTERNS`` (DNS, TCP connect/reset, broken stream, TLS,
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
    """
    is_failure = (
        no_traffic_timeout
        or timed_out
        or interrupted
        or bool((error_text or "").strip())
        or (exit_code is not None and exit_code != 0)
    )
    if not is_failure:
        return None
    if operator_abort:
        # The operator asked for this to stop; nothing here is retryable
        # (see the task 003 paragraph above).
        return "work"
    if no_traffic_timeout:
        return "infra"
    if _INFRA_TEXT_RE.search(error_text or ""):
        return "infra"
    if produced_traffic:
        return "work"
    return "infra"
