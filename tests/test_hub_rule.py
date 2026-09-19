"""Task 013 (v0.23 PRD requirement A.2, #224): the hub's `POST
/api/runs/<id>/rule` proxy -- the run page's operator-ruling textarea's
landing route.

Same shape as `test_steering_post_proxies_and_creates_steering_file` in
test_cli_ui.py: a real (no-Docker) engine via the `live` fixture, so the
proxy is proven against production code on both sides, not a stub.
"""

from __future__ import annotations

from test_cli_ui import StubEngineApi, _write_run_with_api, ui  # noqa: F401

__all__ = ["ui"]


def test_rule_post_proxies_and_creates_ruling_file(tmp_path, live, ui):
    hold_file = tmp_path / "hold-hubrule"
    run = live(run_id="hubrule", job={"iterations": 12, "max_approaches": 3,
                                       "on_complete": "idle"},
               stub_env={"STUB_TASKS": "1", "STUB_HOLD_FILE": str(hold_file),
                        "STUB_HOLD_PHASE": "worker", "STUB_HOLD_TIMEOUT_S": "30"})
    run.wait_api()

    server = ui(run.registry)
    code, resp = server.post(f"/api/runs/{run.run_id}/rule",
                              {"text": "hub ruling text", "name": "hubtest"})
    assert code == 202, resp
    assert "file" in resp

    ruling_files = list((run.run_dir / "rulings").glob("*.md"))
    assert ruling_files, "ruling POST via the hub never wrote a file"
    assert any("hub ruling text" in f.read_text() for f in ruling_files)

    # Deliberately never lands in the steering record -- see A.2's own
    # "distinct command" requirement.
    steering_files = list((run.run_dir / "steering").glob("*.md")) \
        if (run.run_dir / "steering").exists() else []
    assert not any("hub ruling text" in f.read_text() for f in steering_files)

    hold_file.touch()
    run.wait_terminal()


def test_rule_post_unknown_run_404(tmp_path, ui):
    registry = tmp_path / "registry"
    server = ui(registry)
    code, _resp = server.post("/api/runs/nope/rule", {"text": "x"})
    assert code == 404


def test_rule_proxy_forwards_the_control_token(tmp_path, ui):
    registry = tmp_path / "registry"
    token = "the-run-control-token"
    engine = StubEngineApi()
    try:
        _write_run_with_api(registry, "run-rule-ctl", engine, control_token=token,
                            state="running", health="degraded")
        server = ui(registry)
        code, body = server.post("/api/runs/run-rule-ctl/rule", {"text": "hi"})
        assert code == 202, body
        posts = [(p, t) for m, p, t in engine.requests if m == "POST"]
        assert [p for p, _t in posts] == ["/rule"], engine.requests
        assert engine.control_tokens == [token], engine.control_tokens
    finally:
        engine.close()
