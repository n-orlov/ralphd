"""v0.39 (#321): `spend.warning` at 80 % of a cap, and the engine's own POST of
`spend.warning` and `spend.exhausted` to the configured notifier.

Driven like tests/test_spend_enforcement.py: a scripted runner whose
iterations return injected costs. The notifier is a real local HTTP server,
so the request on the wire is what is compared.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from ralphd.engine import hat_tools, spend_caps
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import scripted_supervisor
from tests.test_spend_enforcement import _caps, _plan, _work

pytestmark = [pytest.mark.fast, pytest.mark.smoke]

_supervisor = scripted_supervisor(
    run_id="spend-warn", iterations=60, vigilant=False, on_complete="exit",
    infra_outage_budget_s=1000.0)


def _run(tmp_path, handlers, **cfg):
    root = tmp_path / "run"
    sup = _supervisor(root, handlers, **cfg)
    RunDir(root=root).update_status(state="starting")
    final = asyncio.run(asyncio.wait_for(sup._run_job_core(), 120))
    return sup, final, json.loads((root / "status.json").read_text())


def _events(sup, type_: str) -> list[dict]:
    path = Path(sup.run.root) / "events.jsonl"
    rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    return [e for e in rows if e.get("type") == type_]


class _Notifier:
    """A fake notifier: records every request's path, content type and body."""

    def __init__(self):
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.requests.append({
                    "method": "POST", "path": self.path,
                    "content_type": self.headers.get("Content-Type"),
                    "body": json.loads(body)})
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/notify"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


# -- criterion 1: once per cap, once per approach for a per-approach cap -------

def test_an_overall_cap_warns_once_when_its_spend_first_reaches_80_percent(tmp_path):
    sup, final, status = _run(
        tmp_path, {"planning": [_plan(0.0)], "worker": [_work(0.45)]},
        spend_caps=_caps(overall={"worker": 1.0}))
    assert final == "exhausted", status
    [warn] = _events(sup, "spend.warning")
    assert (warn["hat"], warn["level"], warn["cap"], warn["spend"],
            warn["threshold"]) == ("worker", "overall", 1.0, 0.9, 0.8)
    # the third iteration took the counter past the cap; no second warning
    assert status["spend"]["overall"]["worker"] == 1.35
    [exhausted] = _events(sup, "spend.exhausted")
    assert exhausted["spend"] == 1.35


def test_a_per_approach_cap_warns_once_in_each_approach(tmp_path):
    sup, final, status = _run(
        tmp_path, {"planning": [_plan(0.0)], "worker": [_work(0.45)]},
        spend_caps=_caps(per_approach={"worker": 1.0}), max_approaches=2)
    assert final == "exhausted", status
    warnings = _events(sup, "spend.warning")
    assert [(w["level"], w["approach"], w["spend"]) for w in warnings] == [
        ("per_approach", 1, 0.9), ("per_approach", 2, 0.9)]


def test_a_cap_below_80_percent_never_warns(tmp_path):
    sup, final, status = _run(
        tmp_path, {"planning": [_plan(0.0)], "worker": [_work(0.1)]},
        spend_caps=_caps(overall={"worker": 100.0}), iterations=4)
    assert _events(sup, "spend.warning") == []
    assert "spendWarned" not in status


def test_a_total_cap_warns_when_the_run_total_reaches_80_percent(tmp_path):
    sup, final, status = _run(
        tmp_path, {"planning": [_plan(0.5)], "worker": [_work(0.2)]},
        spend_caps=_caps(overall={"total": 1.0}))
    [warn] = _events(sup, "spend.warning")
    assert (warn["hat"], warn["spend"]) == ("total", 0.9)


def test_a_warning_is_not_repeated_by_a_supervisor_that_resumes_the_run(tmp_path):
    sup, final, status = _run(
        tmp_path, {"planning": [_plan(0.0)], "worker": [_work(0.45)]},
        spend_caps=_caps(overall={"worker": 1.0}))
    assert len(_events(sup, "spend.warning")) == 1
    again = _supervisor(Path(sup.run.root), {"planning": [_plan(0.0)],
                                             "worker": [_work(0.45)]},
                        spend_caps=_caps(overall={"worker": 1.0}))
    again._warn_after_iteration(1, "worker")
    assert len(_events(again, "spend.warning")) == 1


def test_a_raised_cap_warns_again_at_its_new_80_percent():
    cap = spend_caps.SpentCap("worker", "overall", 1.0, 0.9)
    raised = spend_caps.SpentCap("worker", "overall", 2.0, 1.7)
    assert (spend_caps.warning_key(cap, 1) != spend_caps.warning_key(raised, 1))


def test_a_live_iteration_that_reaches_80_percent_warns_before_it_ends():
    told: list = []
    watch = spend_caps.SpendWatch(
        caps=_caps(overall={"worker": 1.0}), hat="worker",
        prior={"overall": {"worker": 0.5}, "per_approach": {}},
        warn=told.append)
    assert watch.observe(0.2) is None and told == []
    assert watch.observe(0.35) is None
    assert [(c.hat, c.level, c.spend) for c in told] == [("worker", "overall", 0.85)]


# -- criteria 2 and 3: the engine POSTs to the notifier, or does not ------------

def test_the_engine_posts_warning_and_exhausted_in_the_notify_tools_request_shape(
        tmp_path):
    with _Notifier() as notifier:
        sup, final, status = _run(
            tmp_path, {"planning": [_plan(0.0)], "worker": [_work(0.45)]},
            spend_caps=_caps(overall={"worker": 1.0}),
            notifier_url=notifier.url)
        # what the `notify` hat tool sends for the same url
        hat_tools.send_notification(sup.run, "from the notify tool", notifier.url)
    assert final == "exhausted", status
    *spend_requests, tool_request = notifier.requests
    assert len(spend_requests) == 2
    warning, exhausted = (r["body"]["text"] for r in spend_requests)
    assert warning.startswith("spend warning:") and "$0.90 of its $1.00" in warning
    assert exhausted.startswith("spend cap spent:") and "$1.35 of its $1.00" in exhausted
    for request in spend_requests:
        assert set(request["body"]) == set(tool_request["body"]) == {"text"}
        assert request["method"] == tool_request["method"]
        assert request["path"] == tool_request["path"]
        assert request["content_type"] == tool_request["content_type"]
    sent = _events(sup, "notification.sent")
    assert len(sent) == 3
    assert {e["endpoint"] for e in sent} == {notifier.url}


def test_with_no_notifier_url_no_request_is_made_and_the_run_carries_on(
        tmp_path, monkeypatch):
    posts: list = []
    monkeypatch.setattr(hat_tools, "_http_post_text",
                        lambda *a, **k: posts.append(a) or (200, None))
    sup, final, status = _run(
        tmp_path, {"planning": [_plan(0.0)], "worker": [_work(0.45)]},
        spend_caps=_caps(overall={"worker": 1.0}))
    assert posts == []
    assert final == "exhausted" and status["exhaustedBudget"] == "spend:worker"
    assert len(_events(sup, "spend.warning")) == 1
    assert len(_events(sup, "spend.exhausted")) == 1
    assert _events(sup, "notification.sent") == []


def test_an_unreachable_notifier_never_stops_the_run(tmp_path, monkeypatch):
    monkeypatch.setattr(hat_tools, "_http_post_text",
                        lambda *a, **k: (None, "connection refused"))
    sup, final, status = _run(
        tmp_path, {"planning": [_plan(0.0)], "worker": [_work(0.45)]},
        spend_caps=_caps(overall={"worker": 1.0}),
        notifier_url="http://127.0.0.1:9/notify")
    assert final == "exhausted" and status["exhaustedBudget"] == "spend:worker"
    assert len(_events(sup, "notification.failed")) == 2


def test_a_run_with_no_caps_writes_no_warning_and_posts_nothing(
        tmp_path, monkeypatch):
    posts: list = []
    monkeypatch.setattr(hat_tools, "_http_post_text",
                        lambda *a, **k: posts.append(a) or (200, None))
    sup, final, status = _run(
        tmp_path, {"planning": [_plan(0.0)], "worker": [_work(5.0)]},
        iterations=3, notifier_url="http://127.0.0.1:9/notify")
    assert posts == [] and _events(sup, "spend.warning") == []
    assert "spendWarned" not in status
