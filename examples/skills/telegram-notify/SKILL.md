---
name: telegram-notify
description: Push a short progress or milestone update to the operator's Telegram, and send files (logs, charts, screenshots, reports) as artifacts. Use for milestones, decisions that need no answer, and anything the operator would otherwise have to watch a terminal for. It is send-only — there is no reply channel, so never wait for an answer.
---

# Telegram notifications to the operator

A send-only notifier runs on the operator's tailnet. It reaches a human who is
**not** watching this run's log. Use it to say what happened; never to ask a
question you need answered to continue.

**Vendored copy.** This file mirrors the operator-side `telegram-notify`
skill, which the harness mounts into `~/.pi/agent/skills/telegram-notify/`
from `/config/skills/telegram-notify` — read-only, host-supplied, and not
tracked in this repo. This copy exists (task 022, PRD requirement B.3, #140)
so the fallback described below has a source-controlled home alongside
`examples/skills/toolchain-sibling/SKILL.md`; it is not itself live-mounted
anywhere, and editing it does not change what the mounted skill says.

## Prefer the engine's own `notify` tool when it is present

If a live `notify` tool is registered for this iteration (the `hat_tools`
spool's own send, wired for the worker and steer hats — see
`engine/hat_tools.py`), **call that tool instead of POSTing directly**:

```json
{"text": "…your message…"}
```

The engine performs the POST itself from its own process, records a
`notification.sent` event (`ts`, `endpoint`, `textSha256`, `httpStatus`) and
the same entry in `run-record.json`'s notifications section, and answers a
failed send with an error rather than raising — the calling task is never
failed by a bad or missing notifier. That accounting is the whole reason to
prefer it: a direct HTTP push from inside the agent leaves no such record
behind.

**Fall back to the direct HTTP push below only when no `notify` tool is
registered** (a phase this run does not wire it for, or a `pi` invocation
outside this harness entirely). The rest of this file describes that
fallback exactly as the operator-side skill does.

- **Base URL:** `http://100.71.162.65:8090` — reachable only because this job was
  started with `--network host`. If a POST fails with a connection error, the
  network mode is wrong: **note it and carry on**. A failed notification is never
  a reason to fail a task, retry in a loop, or block.
- **No auth.** The tailnet is the trust boundary. Just POST.
- **Send-only.** Nothing comes back but a delivery result. Do not phrase a
  message as a question you intend to wait on, and do not poll for a reply.

## Text

```bash
curl -s --max-time 15 -X POST http://100.71.162.65:8090/notify \
  -H "Content-Type: application/json" \
  -d '{"text":"ralphd v0.8 — task 007 done: #51 budget top-ups survive a resume (3 tests)"}'
```

Build the JSON with `python3 -c` and `json.dumps` rather than by hand whenever the
text contains a quote, a backtick, a newline or anything an agent generated:

```bash
python3 - <<'EOF'
import json, urllib.request
body = json.dumps({"text": "…your message…"}).encode()
r = urllib.request.Request("http://100.71.162.65:8090/notify", data=body,
                           headers={"Content-Type": "application/json"})
print(urllib.request.urlopen(r, timeout=15).status)
EOF
```

Fields: `text` (auto-chunked at 4096 chars), `parse_mode`
(`plain` | `MarkdownV2` | `HTML`, default plain), `disable_notification`.

**Prefer `plain`.** `MarkdownV2` rejects a message with an unescaped `_`, `*`,
`[`, `` ` `` or `-` — which is most sentences about code — and the send then
fails for a formatting reason, not a real one. Plain text always goes through.

## Files — logs, charts, screenshots, reports

When the thing worth sending *is* a file, send the file. Do not paste 400 lines
of pytest output into a text message and do not leave it on disk described only
in prose.

```bash
curl -s --max-time 60 -X POST http://100.71.162.65:8090/send \
  -F 'file=@/run/ralphd/artifacts/reports/fastlane.txt' \
  -F 'caption=v0.8 fast lane at abc1234: 2851 passed, 0 failed'
```

Repeat `file=@` for an album (2–10 items). Fields: `caption` (media) or `text`,
`parse_mode`, `disable_notification`, `kind`
(`auto` | `photo` | `video` | `document` | `audio` | `animation`).

Gotchas that matter here:

- **PNG is sent as a document on purpose** — `sendPhoto` re-encodes to JPEG and
  strips metadata, which ruins screenshots and diagrams. Pass `kind=photo` only
  if you specifically want the inline compressed preview.
- **Captions cap at 1024 chars**, not 4096. A longer one is not truncated: the
  media is sent bare and the text follows as a second message.
- **Uploads cap at 50 MB** (10 MB for photos) → HTTP 413. Tail a big log first.
- Documents and audio cannot share an album with photos or video (→ 400).
- `/send-url` makes *Telegram* fetch the URL, so it cannot reach anything on the
  tailnet. For local files always use `/send`.

## What to send, and how often

The operator's ask: **progress and milestones.** One message per real event, not
per iteration. A good message is one line and self-contained — it names the run,
what changed, and the evidence.

Send:

- **starting** — the run id, the plan's task count, the model/strategy.
- **each task completed** — id, one clause of what it delivered, the commit sha
  and the test count. This is the heartbeat; keep it to one line.
- **a milestone** — a requirement finished, a full-suite sweep green (attach the
  log), an issue closed, an approach concluded.
- **something the operator would want to know now** — a task that failed
  validation twice, an infra outage that is eating the budget, a decision you
  took under an ambiguous PRD, a defect you found that is outside this PRD's
  scope (say you filed it rather than fixing it).
- **finishing** — terminal state, verdict, tasks completed/total, cost, and what
  is left.

Do not send: every tool call, every iteration boundary, "starting task N" *and*
"finished task N" (the second is enough), or a wall of text a log would hold
better. If you have sent two messages about the same task, you are over-sending.

## Rules

- **Never** include a token, bearer, password, `.env` content, or the contents of
  `/config/creds/` in a message. The notifier's own bot token lives on the
  operator's host and must never appear in a message body either.
- Failure is non-fatal, always. Wrap the call so a non-zero exit or a timeout
  cannot fail the task around it; record "notification failed" in your notes and
  move on.
- Never `sleep`-poll waiting for a human. There is no reply channel.
