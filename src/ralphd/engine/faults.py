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

import math
import re
from datetime import datetime, timezone

FaultClass = str  # "infra" | "work" | "signal" | "rate_limit" | "refused"

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
# The provider refused the call because the subscription's rate-limit window
# is exhausted. It is never `infra` (an outage retry would burn the outage
# budget against a window with a known reset), never `work` (the agent did
# nothing wrong) and never `signal`. The loop waits until the window resets
# and re-runs the same work (`LoopSupervisor._wait_out_rate_limit`). It is
# decided only from STRUCTURED evidence (`rate_limit_evidence`), which only the
# claude-code runner reports: a pi error is text, and a pi text `429` or `rate
# limit` stays `infra` through the `backpressure` signature family.
FAULT_CLASS_RATE_LIMIT = "rate_limit"
# The provider (or the claude-code harness itself) refused the call in a way
# that waiting does not fix: an HTTP 400/401/403/404 that is not a rate limit,
# such as a bad request, a rejected credential, a missing permission or model,
# the subscription's third-party/extra-usage refusal, or a Claude Code CLI
# below the minimum version. It is never `infra` (retrying it as an outage
# burns the outage budget on a call that can never succeed), never `work` and
# never `rate_limit`. The loop does not retry it: the run pauses for the
# operator (`LoopSupervisor._pause_for_provider_refusal`), and an unpause
# re-runs the same work. Decided by `matched_refusal` over
# `REFUSED_SIGNATURES`.
FAULT_CLASS_REFUSED = "refused"
FAULT_CLASSES: tuple[str, ...] = (FAULT_CLASS_INFRA, FAULT_CLASS_WORK,
                                 FAULT_CLASS_SIGNAL, FAULT_CLASS_RATE_LIMIT,
                                 FAULT_CLASS_REFUSED)

# The rate-limit wait used when the provider reported no `resetsAt`: wait
# this long, then re-probe with the same work.
RATE_LIMIT_UNKNOWN_RESET_WAIT_S = 900.0

# The `pauseDetail.reason` of the pause a run takes when its next rate-limit
# wait would pass `JobConfig.rate_limit_max_wait_s` (v0.30 F.3).
RATE_LIMIT_CEILING_REASON = "rate_limit_ceiling"

# Task 014 (#49 part 2) introduced a duration bound here,
# `ABORTED_STREAM_MAX_DURATION_S = 120.0`, to decide whether a bare in-band
# `aborted` after real traffic was the provider hanging up mid-stream (infra)
# or the agent's own failure (work). It was derived from a SAMPLE OF ONE:
# iteration 145 of `selfdev-v06-release` recorded `error: aborted` with
# `faultClass: "work"` 39 seconds into a 45-minute cap, and 120s was chosen as
# "three times that observed 39s".
#
# Steering 005 (run `selfdev-v09-fsm-loop`) retired it. The bound argued only
# that a *worked* iteration cannot land under 120s -- a defence against false
# positives, and sound -- but offered nothing for the other direction, and a
# provider can hang up on a stream at any point, so the population of genuine
# stream-abort durations has no upper bound to calibrate against. That run's
# own 15 bare-`aborted` incidents landed at 115s, 156s, 173s, 174s, 218s, 272s,
# 279s, 307s, 339s, 364s, 530s, 800s, 996s, 1439s and 2542s -- 14 of 15 ABOVE
# the 120s bound, every one a genuine provider-side abort, every one
# misclassified `work` by the bound, and three of them (94/100/101: 279s,
# 173s, 339s) failed an approach of that run via the stagnation guard while
# nothing about the plan was actually stuck.
#
# The fix decides the branch on the SHAPE that was always the real
# discriminator instead (see the branch below) and drops duration entirely: an
# agent that fails at its own work reports what failed -- an assertion, a
# traceback, a nonzero exit -- it does not exit 0 with the single word
# "aborted" as its whole error text. That conjunction (bare text, a clean
# exit, traffic present, no infra signature, not interrupted, no abort
# recorded for the run) is the discriminator; a clock added nothing to it and,
# empirically, actively hurt it. There is deliberately no replacement
# constant -- a bigger arbitrary number is the same mistake with a longer
# fuse.

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

# v0.30 G.1: the refusals that waiting does not fix -- the twin of
# INFRA_SIGNATURES above, one (family, regex) row per shape, matched by
# `matched_refusal` alone. A provider family counts only together with an
# HTTP 400, 401, 403 or 404: the structured `http_status` when the runner
# reported one (claude-code), else the first 4xx/5xx status token in the
# error text (pi writes `<status> <body>`). A 429 or a 5xx never refuses,
# whatever the body says. The `cc-*` families are the claude-code harness's
# own pre-flight refusals: no request reached a provider, so they need no
# status. Table order decides when several rows could match; the
# extra-usage row precedes invalid-request because its body also carries
# `invalid_request_error`.
REFUSED_SIGNATURES: tuple[tuple[str, str], ...] = (
    # The Claude Code CLI is older than CLAUDE_CODE_MIN_VERSION (or its
    # version cannot be read), or is not there at all.
    ("cc-version", r"claude_code_version_too_old"),
    ("cc-cli-missing", r"claude_code_cli_not_found"),
    # The agent uid resolved to root, so the wrapper was never spawned.
    ("cc-root", r"claude_code_runs_as_root"),
    # A subscription token sent from a third-party client (#284).
    ("extra-usage", r"Third-party apps now draw from your extra usage"),
    # The API's own error types (and the Agent SDK's names for them).
    ("invalid-request", r"invalid_request(?:_error)?"),
    ("authentication", r"authentication_(?:error|failed)"),
    ("permission", r"permission_error"),
    # `model_not_found` is the gateway/Bedrock spelling of the same 404.
    ("not-found", r"not_found_error|model_not_found"),
)

# What each refusal family MEANS, for the explanation surfaces. Every family
# in REFUSED_SIGNATURES has an entry (asserted in
# tests/test_fault_explanation.py).
REFUSED_FAMILY_DESCRIPTIONS = {
    "cc-version": "the Claude Code CLI is below the minimum version ralphd runs",
    "cc-cli-missing": "the Claude Code CLI was not found",
    "cc-root": "the claude-code harness refuses to run the agent as root",
    "extra-usage": ("the subscription refused a third-party client: it would "
                    "draw from extra usage, not the plan limits"),
    "invalid-request": "the provider rejected the request as invalid",
    "authentication": "the provider rejected the credentials",
    "permission": "the credentials lack permission for this request",
    "not-found": "the provider does not know the model or resource asked for",
}

# The families that need no HTTP status: the harness refused before any
# request was sent.
REFUSAL_FAMILIES_WITHOUT_STATUS = frozenset(
    {"cc-version", "cc-cli-missing", "cc-root"})

# The HTTP statuses a provider refusal family requires.
REFUSED_HTTP_STATUSES = (400, 401, 403, 404)

_REFUSED_SIGNATURE_RES = tuple(
    (family, pattern, re.compile(pattern, re.IGNORECASE))
    for family, pattern in REFUSED_SIGNATURES
)

# The first HTTP-status-shaped token in an error text: pi writes an API
# error as `<status> <json body>`.
_HTTP_STATUS_IN_TEXT_RE = re.compile(r"\b([45]\d\d)\b")


def refusal_http_status(error_text: str | None,
                        http_status: int | None = None) -> int | None:
    """The HTTP status a refusal is judged by: the structured `http_status`
    when the runner reported one, else the first 4xx/5xx token in the
    error text, else None."""
    if isinstance(http_status, int) and not isinstance(http_status, bool):
        return http_status
    found = _HTTP_STATUS_IN_TEXT_RE.search(error_text or "")
    return int(found.group(1)) if found else None


def matched_refusal(error_text: str | None, http_status: int | None = None,
                    error_type: str | None = None) -> dict | None:
    """Which row of ``REFUSED_SIGNATURES`` refuses this error, or None
    (v0.30 G.1).

    The row's pattern is searched in ``error_text``, then in ``error_type``.
    A provider family also needs ``refusal_http_status`` to be 400, 401, 403
    or 404; the ``cc-*`` families need no status. Returns the same shape as
    ``matched_signature`` (``family``, ``description``, ``pattern``,
    ``match``) plus ``httpStatus``, so ``ralphctl fault`` prints it the same
    way.
    """
    text = error_text or ""
    etype = error_type or ""
    if not text and not etype:
        return None
    status = refusal_http_status(text, http_status)
    for family, pattern, regex in _REFUSED_SIGNATURE_RES:
        found = regex.search(text) or regex.search(etype)
        if not found:
            continue
        if (family not in REFUSAL_FAMILIES_WITHOUT_STATUS
                and status not in REFUSED_HTTP_STATUSES):
            continue
        return {"family": family,
                "description": REFUSED_FAMILY_DESCRIPTIONS.get(family, ""),
                "pattern": pattern,
                "match": found.group(0),
                "httpStatus": status}
    return None

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
# Task 014 (#49 part 2), reworded by steering 005 (run selfdev-v09-fsm-loop):
# the answer to SPEC 16 Q1 -- a bare `aborted` with a clean exit that arrived
# after real traffic, with no abort recorded for the run, is the provider
# hanging up mid-stream, whatever its duration.
FAULT_REASON_ABORTED_STREAM = (
    "a bare `aborted` with a clean exit ended this iteration after real "
    "traffic, with no abort recorded for the run -- the provider hung up "
    "mid-stream")


# Task T08 (v0.30 F.1): a rate limit reported by the provider in structured
# form. The verdict says which piece of evidence decided it.
FAULT_REASON_RATE_LIMIT = (
    "the provider reported its rate limit as reached -- the run waits until "
    "the window resets and re-runs the same work, without spending an "
    "iteration or the outage budget")

# v0.30 G.1: a refusal that waiting does not fix.
FAULT_REASON_REFUSED = (
    "the provider refused the call with a non-transient 4xx (or the "
    "claude-code harness refused to start) -- it is not retried; the run "
    "pauses for the operator, and an unpause re-runs the same work")


def parse_resets_at(value) -> float | None:
    """A rate-limit `resetsAt` as UTC epoch seconds, or None.

    Accepts int or float epoch seconds (a value above 1e12 is read as
    milliseconds), a numeric string, and ISO 8601 with `Z` or an offset (a
    value with neither is read as UTC). Anything else is None.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(
                    text[:-1] + "+00:00" if text[-1] in "Zz" else text)
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
    else:
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    if number > 1e12:
        number /= 1000.0
    return number


def _rate_limit_window(info: dict | None) -> dict | None:
    """The `unifiedWindows` entry for the info's own `rateLimitType`."""
    if not isinstance(info, dict):
        return None
    windows = info.get("unifiedWindows")
    if not isinstance(windows, dict):
        return None
    window = windows.get(info.get("rateLimitType"))
    return window if isinstance(window, dict) else None


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def effective_utilization(info: dict | None) -> float | None:
    """How much of the rate-limit window is used, from 0 to 1, or None.

    The top-level `utilization` when the provider sent one. Otherwise the
    `unifiedWindows` entry for `rateLimitType`, and otherwise the highest
    utilization over all the windows. The real CLI reports utilization only
    per window."""
    if not isinstance(info, dict):
        return None
    top = _number(info.get("utilization"))
    if top is not None:
        return top
    window = _rate_limit_window(info)
    if window is not None:
        own = _number(window.get("utilization"))
        if own is not None:
            return own
    windows = info.get("unifiedWindows")
    if isinstance(windows, dict):
        values = [_number(w.get("utilization")) for w in windows.values()
                  if isinstance(w, dict)]
        values = [v for v in values if v is not None]
        if values:
            return max(values)
    return None


def rate_limit_resets_at(info: dict | None) -> float | None:
    """When the reported window resets, as UTC epoch seconds, or None. The
    `unifiedWindows` entry for `rateLimitType` wins over the top level."""
    if not isinstance(info, dict):
        return None
    window = _rate_limit_window(info)
    if window is not None:
        resets = parse_resets_at(window.get("resetsAt"))
        if resets is not None:
            return resets
    return parse_resets_at(info.get("resetsAt"))


def rate_limit_evidence(*, rate_limit: dict | None = None,
                        rate_limit_fresh: bool = False,
                        http_status: int | None = None,
                        error_type: str | None = None,
                        error_text: str = "") -> str | None:
    """Which structured evidence says the provider rate-limited this
    iteration, or None. The rule (v0.30 F.1):

    - a rate-limit report that arrived during this iteration
      (`rate_limit_fresh`) with `status == "rejected"`;
    - `error_type == "rate_limit"` (the SDK's assistant-message error), or
      the API's own `rate_limit_error`;
    - HTTP 429 together with an error type, a `rateLimitType` on the last
      report, or `rate_limit_error` in the error text.

    A report carried over from an earlier iteration is not evidence: each
    iteration is a new CLI process that reports its window on its first
    request, so an old `rejected` can be stale. A bare text `429` (pi) is not
    evidence either; it stays `infra`.
    """
    info = rate_limit if isinstance(rate_limit, dict) else None
    if rate_limit_fresh and info is not None and info.get("status") == "rejected":
        return "rate limit report: status rejected"
    etype = (error_type or "").strip()
    if etype in ("rate_limit", "rate_limit_error"):
        return f"provider error type {etype}"
    if http_status == 429 and not isinstance(http_status, bool):
        if etype:
            return f"HTTP 429 with error type {etype}"
        if info is not None and info.get("rateLimitType"):
            return f"HTTP 429 with rate limit type {info['rateLimitType']}"
        if "rate_limit_error" in (error_text or ""):
            return "HTTP 429 with rate_limit_error"
    return None


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
    rate_limit: dict | None = None,
    rate_limit_fresh: bool = False,
    http_status: int | None = None,
    error_type: str | None = None,
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

    Task 014 (#49 part 2, closing SPEC 16 Q1) added a shape -- a bare
    ``aborted`` after real traffic, with a clean exit and no abort recorded --
    that reads as ``"infra"`` rather than ``"work"``. Steering 005 (run
    selfdev-v09-fsm-loop) removed the duration bound that used to gate it: the
    branch below now fires on the shape alone (see ``ABORTED_STREAM_MAX_DURATION_S``'s
    former definition, kept as a comment where it stood, for why an absolute
    clock was the wrong test -- a provider can hang up on a stream at any
    point, so no duration is disqualifying).

    Known and deliberate imprecision that remains (issue #49, which replaced
    the now-closed #23 as the owner of this taxonomy): an engine-side give-up
    and an abort from anywhere are still classified ``"work"`` by the abort
    carve-out above -- deliberately, because that branch's whole job is "never
    retry this as an outage" -- even though nobody attempted any work; only the
    *wording* distinguishes those. And a signal-terminated iteration that never
    reached the model stays ``"infra"`` by the no-traffic rule below, on
    purpose: with zero traffic it is textually indistinguishable from a
    provider-side stream abort, and treating "nothing ran" as retryable and
    refundable is exactly the accounting rule that rule exists to enforce. And
    (steering 005's own residual, named rather than re-boarded with a
    threshold): a genuine agent crash whose *entire* final error text happens
    to be the single word "aborted", with a clean exit code, is still read as
    the provider hanging up -- deliberately, because that is textually
    identical to the real thing and a real crash reporting nothing but that
    one word with a clean exit is vanishingly unlikely (a crash reports what
    crashed).

    v0.30 F.1: ``rate_limit``, ``rate_limit_fresh``, ``http_status`` and
    ``error_type`` are the structured provider evidence the claude-code
    runner reports. When ``rate_limit_evidence`` finds a rate limit in them,
    the class is ``"rate_limit"``. That rung sits after the abort carve-out
    (an abort is never waited out) and before the watchdog and signature
    rungs, so a rate limit is never ``infra``. The returned dict then also
    carries ``rateLimit``: ``{"evidence", "type", "resetsAt"}``.

    v0.30 G.1: the next rung is the refusal: when ``matched_refusal`` finds a
    ``REFUSED_SIGNATURES`` row (a 400/401/403/404 from either harness, or a
    claude-code pre-flight refusal), the class is ``"refused"`` and
    ``signature`` is the matched refusal row. It follows the rate-limit rung,
    so a 429 is never ``refused``, and precedes the watchdog and signature
    rungs, so a refusal is never retried as ``infra``.
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
    evidence = rate_limit_evidence(
        rate_limit=rate_limit, rate_limit_fresh=rate_limit_fresh,
        http_status=http_status, error_type=error_type, error_text=error_text)
    if evidence is not None:
        info = rate_limit if isinstance(rate_limit, dict) else {}
        return {"faultClass": FAULT_CLASS_RATE_LIMIT,
                "reason": FAULT_REASON_RATE_LIMIT, "signature": None,
                "rateLimit": {"evidence": evidence,
                              "type": info.get("rateLimitType"),
                              "resetsAt": rate_limit_resets_at(info)}}
    refusal = matched_refusal(error_text, http_status, error_type)
    if refusal is not None:
        return {"faultClass": FAULT_CLASS_REFUSED,
                "reason": FAULT_REASON_REFUSED, "signature": refusal}
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
        # the branch above did not fire) is the provider hanging up
        # mid-stream, not the agent failing, WHATEVER its duration (steering
        # 005, run selfdev-v09-fsm-loop, retired the duration bound this used
        # to carry -- see the comment above ABORTED_STREAM_MAX_DURATION_S's
        # former home): no real work iteration exits 0 with the single word
        # "aborted" as its whole error text, and every other reading of a
        # bare `aborted` is already owned by a branch above (an
        # operator/engine abort by the carve-out, a signal by the branch
        # above, `aborted: Premature close` by the `stream` signature family).
        # Being `infra` it is retried and refunded, which is the whole point:
        # iteration 145 of selfdev-v06-release was charged an approach for one
        # of these while the five iterations after it were refunded, and this
        # run's own iterations 94/100/101 were charged the same way for
        # landing a few seconds past an arbitrary clock.
        if is_bare_aborted(error_text) and exit_code == 0:
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
    rate_limit: dict | None = None,
    rate_limit_fresh: bool = False,
    http_status: int | None = None,
    error_type: str | None = None,
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
    for the run and a clean exit status, is ``"infra"`` (a stream the
    provider hung up on), so it is retried and refunded instead of costing an
    approach. Steering 005 (run selfdev-v09-fsm-loop) removed the duration
    bound this used to carry (``ABORTED_STREAM_MAX_DURATION_S``): the shape
    alone decides it now, at any duration -- see the comment where that
    constant used to be defined for the evidence that retired it.

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

    v0.30 F.1: returns ``"rate_limit"`` when the structured provider evidence
    (``rate_limit``, ``rate_limit_fresh``, ``http_status``, ``error_type``)
    says the subscription's rate limit was reached -- see
    ``rate_limit_evidence``. A pi error carries none of these, so pi's
    classes are unchanged.

    v0.30 G.1: returns ``"refused"`` for a non-transient refusal from either
    harness -- see ``matched_refusal``.
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
        rate_limit=rate_limit,
        rate_limit_fresh=rate_limit_fresh,
        http_status=http_status,
        error_type=error_type,
    )["faultClass"]
