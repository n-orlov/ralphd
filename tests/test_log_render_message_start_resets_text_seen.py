"""Task 019 (PRD G.8, #214): the confirmable full-renderer defect at
`_render_message_end` (`src/ralphd/cli/log_render.py`).

The PRD's own description of this defect ("`_render_message_end` consumes
`text_seen` at the first substantive text item, so one `message_end`
carrying two text blocks prints the second one twice") was written against
`af0b57e2`. `_render_message_end` is byte-identical to that commit --
extract the function from `git show af0b57e2:src/ralphd/cli/log_render.py`
and from the shipped file and the two bodies sha256 the same
(`4e5c010e...`) -- and it never assigns `state["text_seen"]` anywhere, so
nothing in it "consumes" the flag, and no combination of streamed/non-streamed text blocks this
module's own test drove through the full renderer ever produced a literal
duplicate line.

What DOES reproduce on demand, through the exact same renderer
(`render_to_lines` -> `render_log_line`), is a DROP in the opposite
direction: `text_seen`/`thinking_seen`/`toolcall_seen` are reset only at
an `ralphd.iteration` boundary, never between the several `message_start`
-> ... -> `message_end` cycles one iteration commonly has (one per
tool-calling round trip, docs/json.md's `AgentEvent`). Once any earlier
message in the iteration streamed so much as one character of text,
`text_seen` stays `True` for the rest of the iteration, so a LATER
message's `message_end` -- carrying text that arrived without any
preceding `text_delta` (e.g. a short non-streamed reply) -- prints NONE of
its text blocks, where it must print each of them exactly once.

The fix: `render_log_line` now resets the three collapse flags on
`message_start`, which was previously an unrecognized/silently-skipped
event type. This is independent of PRD G's side index (G.1-G.7, G.9,
G.10): no index code is touched, and this fix does not depend on it.

Erratum (task 019, third commit). Task 019's first commit `76e81f68`,
this module's original docstring and `log_render.py`'s own comment all
said "`git diff af0b57e2 HEAD -- src/ralphd/cli/log_render.py` is
empty". That was measured at `76e81f68`'s parent `244e1653`, where it is
indeed empty, but it is FALSE of `76e81f68` and of every later tip,
because this task's own fix adds a `message_start` branch to that file:
`git diff --quiet af0b57e2 HEAD -- src/ralphd/cli/log_render.py` exits 1.
No line count for that whole-file diff is asserted here. The second
commit's Erratum did assert one (`--numstat` reports `36 0`) and that
very commit falsified it by adding two more comment lines to the same
file (`38 0`); any later touch of `log_render.py` falsifies a fresh
number the same way, so measure it when you need it rather than reading
it here. The claim that survives measurement, and the only one made
above, is the narrower one that does not move with the surrounding file:
`_render_message_end`'s own 13-line body is unchanged since `af0b57e2` --
extract it from `git show af0b57e2:src/ralphd/cli/log_render.py` and from
the shipped file and both sha256 to `4e5c010e...`."""

import json

from ralphd.cli.log_render import new_render_state, render_to_lines


def _line(ev: dict) -> str:
    return json.dumps(ev)


def _two_message_transcript() -> str:
    """Message A streams one text block live and closes normally.
    Message B starts (a real `message_start` -- the exact event type
    `render_log_line` did not recognize before this task) and its
    `message_end` carries TWO text blocks, neither of which was ever
    streamed via `text_delta` -- the realistic shape of a short,
    non-streamed reply following an earlier, streamed one in the same
    iteration."""
    return "\n".join([
        _line({"type": "message_start", "message": {"role": "assistant", "content": []}}),
        _line({"type": "message_update", "assistantMessageEvent": {
            "type": "text_delta", "contentIndex": 0, "delta": "first"}}),
        _line({"type": "message_update", "assistantMessageEvent": {
            "type": "text_end", "contentIndex": 0, "content": "first"}}),
        _line({"type": "message_end", "message": {"content": [
            {"type": "text", "text": "first"}]}}),
        _line({"type": "message_start", "message": {"role": "assistant", "content": []}}),
        _line({"type": "message_end", "message": {"content": [
            {"type": "text", "text": "second block one"},
            {"type": "text", "text": "second block two"},
        ]}}),
    ])


def test_a_message_end_with_two_text_blocks_after_an_earlier_streamed_message_prints_each_once():
    """Drives the exact fixture above through the full renderer
    (`render_to_lines`, the same function `_pretty_log_lines`/
    `cmd_logs`'s plain, non-follow path calls) and asserts every one of
    the three text lines the transcript actually carries is rendered
    exactly once, in order. Against the shipped (pre-fix) behaviour this
    fails: message B's `message_end` renders NEITHER of its two blocks,
    because `state["text_seen"]` is still `True` from message A and is
    never reset at message B's own start."""
    state = new_render_state()
    lines = render_to_lines(_two_message_transcript(), False, state)
    assert lines == ["first", "second block one", "second block two"]


def test_a_message_end_replaying_already_streamed_text_is_still_suppressed():
    """The fix must not reintroduce a duplicate for the ordinary case
    `_render_message_end`'s guard exists for in the first place: text
    that WAS streamed live for the CURRENT message must still render
    exactly once (via the stream), not a second time when its own
    `message_end` replays it."""
    transcript = "\n".join([
        _line({"type": "message_start", "message": {"role": "assistant", "content": []}}),
        _line({"type": "message_update", "assistantMessageEvent": {
            "type": "text_delta", "contentIndex": 0, "delta": "streamed text"}}),
        _line({"type": "message_update", "assistantMessageEvent": {
            "type": "text_end", "contentIndex": 0, "content": "streamed text"}}),
        _line({"type": "message_end", "message": {"content": [
            {"type": "text", "text": "streamed text"}]}}),
    ])
    state = new_render_state()
    lines = render_to_lines(transcript, False, state)
    assert lines == ["streamed text"]


def test_message_start_with_no_prior_stream_still_renders_the_first_message_once():
    """A single message, no `message_start` reset needed to prove
    anything (state starts fresh already) -- pinned here so a future
    change to the reset can't accidentally start suppressing the very
    first message's own un-streamed text blocks."""
    transcript = "\n".join([
        _line({"type": "message_start", "message": {"role": "assistant", "content": []}}),
        _line({"type": "message_end", "message": {"content": [
            {"type": "text", "text": "block one"},
            {"type": "text", "text": "block two"},
        ]}}),
    ])
    state = new_render_state()
    lines = render_to_lines(transcript, False, state)
    assert lines == ["block one", "block two"]
