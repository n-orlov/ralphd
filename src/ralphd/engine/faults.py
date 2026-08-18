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
# endpoint itself", independent of whether any traffic was ever observed:
# DNS resolution failures, refused/reset connections, TLS handshake
# failures, and a gateway-level 5xx returned before any tokens streamed.
_INFRA_TEXT_PATTERNS = (
    r"ENOTFOUND",
    r"ECONNREFUSED",
    r"ECONNRESET",
    r"EAI_AGAIN",
    r"ETIMEDOUT",
    r"EHOSTUNREACH",
    r"ENETUNREACH",
    r"getaddrinfo",
    r"TLS handshake",
    r"SSL handshake",
    r"certificate verify failed",
    r"\bbad gateway\b",
    r"\bgateway timeout\b",
    r"\bservice unavailable\b",
    r"\b50[234]\b",
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
    - the captured error text matches a known infra signature (DNS/
      ENOTFOUND, ECONNREFUSED/ECONNRESET, TLS/SSL handshake failure, or a
      gateway 5xx) -- infra even if some traffic happened to precede it;
    - any other failure that produced no LLM traffic at all and doesn't
      match a recognized infra signature is *still* classified infra
      (deliberately -- an unclassifiable no-traffic failure is far more
      likely to be an environment/startup fault than a genuine work
      failure, and scoring it as "work" would let it silently burn
      approach/task-failure bookkeeping instead of being retried).

    Returns ``"work"`` only for the one case squarely inside the agent's
    own responsibility: it produced real LLM traffic (assistant text and/or
    token usage were observed) and *then* exited nonzero/timed out/was
    interrupted, with no recognized infra signature in its error text.
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
    if no_traffic_timeout:
        return "infra"
    if _INFRA_TEXT_RE.search(error_text or ""):
        return "infra"
    if produced_traffic:
        return "work"
    return "infra"
