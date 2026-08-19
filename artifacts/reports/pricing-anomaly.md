# Why the same model was priced in one run and $0 in another (task 053, #10)

**Verdict: it is not intermittent and it is not a runtime pricing fetch that
flaked. Pricing is static data resolved from the *model definition* pi uses for
a request, and a custom model definition without a `cost` block prices every
token at $0.** The two runs in the issue mounted *different* `models.json`
snapshots, so the same model id resolved to a priced built-in entry in one run
and to an unpriced custom entry in the other.

All evidence below was read off this host (run dirs under `~/.ralphd/runs`,
job config snapshots under `~/.ralphd/configs`, and the pi package inside the
`ralphd:dev` image), via a read-only sibling container.

## 1. The two runs

| | `est6534-opus5-smoke` | `est6534-impl-phase2-sessions` |
|---|---|---|
| image (`host.json`) | `ralphd:dev` | `ralphd:dev` (same) |
| started | 2026-08-18T06:39:07Z | 2026-08-18T20:09:37Z (dir created 08:13) |
| `job.yaml` model | `amazon-bedrock/eu.anthropic.claude-opus-5` | same |
| `job.yaml` fast_model | `amazon-bedrock/eu.anthropic.claude-sonnet-5` | same |
| gateway (`llm-wiring.json`) | aigw `.../api/bedrock`, `bedrock-converse-stream` | identical env, identical baseUrl |
| `status.json` `usage.costUSD` | **0.320648** (145 261 tok) | **0** (545 173 664 tok) |
| `configs/<run>/pi/models.json`, provider `amazon-bedrock`, declared model ids | `eu.anthropic.claude-sonnet-5` **only** | `eu.anthropic.claude-opus-5`, `eu.anthropic.claude-sonnet-5` |
| snapshot mtime | 2026-08-18 06:39:07 | 2026-08-18 08:13:55 |

So the *only* material difference is the per-run snapshot of the
`invivo-aigw` LLM profile's `pi:` fragment, which `ralphctl start` writes to
`<config-dir>/pi/models.json` and mounts read-only at
`/home/agent/.pi/agent/models.json` (`src/ralphd/cli/main.py:815`, `:2234`).
The profile file `~/.ralphd/llm-profiles/invivo-aigw.yaml` has mtime
**2026-08-18 06:41** — two minutes *after* the smoke run was started, i.e. the
opus-5 entry was added to the profile between the two runs. Every run started
after that edit ships an opus-5 definition; the smoke run does not.

## 2. Why a *missing* definition is the priced case

pi's merge semantics (`pi docs/models.md`, "Overriding Built-in Providers"):

> - Built-in models are kept.
> - Custom models are upserted by `id` within the provider.
> - **If a custom model `id` matches a built-in model `id`, the custom model
>   replaces that built-in model.**

and the model field table in the same doc:

> | `cost` | No | **all zeros** | Per-million-token rates ... |

The image's built-in Bedrock catalog
(`node_modules/@earendil-works/pi-ai/dist/providers/data/amazon-bedrock.json`,
key `bedrock-converse-stream`) *does* carry rates for both models:

- `eu.anthropic.claude-opus-5` → `input 5.5, output 27.5, cacheRead 0.55, cacheWrite 6.875`
- `eu.anthropic.claude-sonnet-5` → `input 2.2, output 11, cacheRead 0.22, cacheWrite 2.75`

The profile fragment declares its models with `name/reasoning/input/
contextWindow/maxTokens` and **no `cost` key** — so each declared id replaces a
priced built-in entry with a zero-rate one.

Arithmetic proof from the smoke run's own transcript, iteration `0001`
(`~/.ralphd/runs/est6534-opus5-smoke/iterations/0001/output.jsonl`, a
`message_end` usage object):

```
"usage":{"input":2,"output":1215,"cacheRead":3178,"cacheWrite":803,
  "cost":{"input":0.000011,"output":0.0334125,
          "cacheRead":0.0017479,"cacheWrite":0.005520625,"total":0.040692...}}
```

`2 × 5.5/1e6 = 0.000011`, `1215 × 27.5/1e6 = 0.0334125`,
`3178 × 0.55/1e6 = 0.0017479`, `803 × 6.875/1e6 = 0.005520625` — an exact
match for the **built-in** opus-5 rates. The same run's iteration `0002`
(sonnet-5, the model that *was* declared in that snapshot) reports
`cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}` on ~6 500 tokens per
turn. One container, one session log format, one gateway, one minute apart:
priced vs zero split exactly on "is this id redefined in models.json".
That within-run control rules out gateway behaviour, credentials, network,
pi version and image drift.

In `est6534-impl-phase2-sessions` both ids are redefined, so every phase is
zero: `byPhase` planning/worker/verify/reflect/review all `costUSD: 0` over
545M tokens, and iteration `0001` (opus-5, 1 376 411 tok) records
`costUSD: 0` with per-turn `cost.total: 0`.

## 3. Corroboration across all 21 runs on this host

Correlating each run's `configs/<run>/pi/models.json` against
`status.json`'s total, the rule holds without exception: **a run reports $0 iff
the model it actually used is redefined in that run's `models.json` without a
`cost` block.**

| run | total costUSD | model used | redefined in its models.json? |
|---|---|---|---|
| `est6534-opus5-smoke` | 0.320648 | bedrock opus-5 | no (sonnet-5 only) |
| `est6534-impl-phase2-sessions` | 0 | bedrock opus-5 | yes |
| `est6534-impl-phase1-ui-safety` | 0 | bedrock opus-5 | yes |
| `deck-phase1` | 0 | bedrock opus-5 | yes |
| `selfdev-v05-resilience` (this run) | 0 (134M tok) | bedrock opus-5 | yes |
| `deck-phase0`, `deck-phase0b`, `deck-spike-sibling`, `est6534-aigw-smoke`, `est6534-aigw-smoke-high` | 0 | `aigw-openai/openai.gpt-5.6-*` | yes |
| `selfdev-roadmap-1..4`, `selfdev-vigilant-4`, `spike-playwright-1`, `tg-selftest` | 2.22 … 91.66 | `amazon-bedrock/us.anthropic.*` | no (only ollama/gpt entries declared) |

### The sharpest case: one run, one model id, 35 priced / 34 zero

`est6534-research-aigw` looks like the issue's "same model, different verdict"
*inside a single run*: 69 iterations, all logging `"model":"openai.gpt-5.6-sol"`,
35 priced (total $52.02) and 34 at exactly $0. The discriminator is the
`"provider"` field in the same transcripts, and it is a 100% split:

```
35 "aigw-openai"     priced
34 "bedrock-mantle"  zero
```

That run's `models.json` declares `openai.gpt-5.6-sol` under **both**
providers: the `aigw-openai` copy carries an explicit
`cost {input 5.5, output 33.0, cacheRead 0.55, cacheWrite 6.88}`, the
`bedrock-mantle` copy carries none. Check against iteration `0069`:
`92 × 33/1e6 = 0.003036`, `73361 × 0.55/1e6 = 0.04034855`,
`2322 × 6.88/1e6 = 0.01597536` — the models.json rates exactly. So even the
"flapping within one run" shape is fully explained by config, and its run total
($52.02) is a *partial* sum that silently omitted 34 iterations of real spend.

## 4. Is pricing fetched at runtime? Can it fall back to zeros?

**No runtime fetch, and therefore no network-shaped fallback.** Rates are
static JSON shipped inside the pi package in the image
(`pi-ai/dist/providers/data/*.json`); pi computes `usage.cost.{input,output,
cacheRead,cacheWrite,total}` client-side per turn from the resolved model
entry, and emits the block *always* — zero-filled when the entry has no rates
(see the `cost:{... total:0}` objects above; pi's own docs use
`cost: { input: 0, output: 0, ... }` as the local-model example). The gateway
never quotes a price and nothing is looked up over the wire.

Consequences worth stating plainly:

- A $0 run is **deterministic and reproducible** given its config snapshot; it
  is not a transient. The appearance of intermittency came from (a) a profile
  edit between two runs and (b) two providers declaring the same model id.
- Because the zero arrives as a *number*, ralphd could not previously tell
  "the provider says this cost nothing" from "nobody knows what this cost" —
  which is the exact defect #10 reports (`$0.0000` printed next to 545M tokens).
- `AWS_BEDROCK_SKIP_AUTH=1` / the aigw shim are irrelevant to pricing; they
  only affect the request path.

**Operator remedy (config, outside ralphd):** in
`~/.ralphd/llm-profiles/invivo-aigw.yaml`, either add a `cost:` block to each
declared model, or stop redefining built-ins and use pi's `modelOverrides`
(which supports a *partial* `cost` and keeps the built-in entry, `pi
docs/models.md` §Per-model Overrides) — that keeps the built-in rates while
still routing through the gateway `baseUrl`.

## 5. What v0.5 shipped as mitigation (tasks 049–052)

The config fix above is the operator's; ralphd's job was to stop reporting a
guess as a fact. Shipped in this run:

- **049** (`12c8131`) — `engine/runner.py:230-259`: a missing/zero provider
  price on an iteration with billed tokens is no longer coerced to `$0`. Each
  iteration records `costPriced: true|false`; `costUSD` keeps its pre-0.5
  meaning "money the provider quoted". **Which iterations were priced is now
  recorded on disk** — the concrete ask of this task. A no-traffic iteration is
  byte-identical to before.
- **050** (`4f791ce`) — `engine/loop.py:_merge_cost_status`: run/`byPhase`/
  `byApproach` buckets carry a monotone `costStatus`
  (absent → `derived` → `partial` → `unknown`), so a mixed run like
  `est6534-research-aigw` is labelled `partial` instead of presenting a
  subset sum as the total. Contract documented in `docs/api.md`.
- **051** (`e56d8c0`) — one renderer (`engine/state.format_cost`) prints
  `unavailable` / `$0.12+ (partial, rest unavailable)` on every surface (hub,
  `ralphctl status`, logs footer, TUI gauge); a fully-priced run's output is
  unchanged.
- **052** (`86bbc53`) — optional host-side `pricing:` map
  (`engine/pricing.py`, `<registry>/config.yaml` → `job.yaml` → `GET /config`)
  with `aliases` for gateway-local ids: used *only* when the provider quoted
  nothing, surfaced separately as `costDerivedUSD` / `~$… derived`, never
  merged into `costUSD`. This is the ralphd-side answer for exactly this
  gateway: e.g.
  `aliases: {"eu.anthropic.claude-opus-5": "anthropic/claude-opus-5"}` plus
  the published Bedrock rates gives a usable number without pretending the
  provider quoted it.

Tests covering the above: `tests/test_cost_unknown.py`,
`tests/test_usage_accounting.py`, `tests/test_cost_render_surfaces.py`,
`tests/test_pricing_map.py`.

## 6. Reproduction of the evidence

```sh
# read-only sibling on the HOST daemon (paths are host paths)
docker run --rm --label ralphd.run=$RALPHD_RUN_ID --label ralphd.role=sibling \
  -v /home/int21h/.ralphd:/h:ro alpine:3 sh -c '
    apk add -q jq
    jq -c .usage /h/runs/est6534-opus5-smoke/status.json
    jq -r "[.providers[\"amazon-bedrock\"].models[].id]" \
       /h/configs/est6534-opus5-smoke/pi/models.json
    jq -r "[.providers[\"amazon-bedrock\"].models[].id]" \
       /h/configs/est6534-impl-phase2-sessions/pi/models.json
    grep -o "\"usage\":{[^}]*}" \
       /h/runs/est6534-opus5-smoke/iterations/0001/output.jsonl | tail -1'

# built-in rates inside the image
python3 -c 'import json;d=json.load(open("/usr/lib/node_modules/@earendil-works/\
pi-coding-agent/node_modules/@earendil-works/pi-ai/dist/providers/data/amazon-bedrock.json"));\
print(d["bedrock-converse-stream"]["eu.anthropic.claude-opus-5"]["cost"])'
```

No secret values are reproduced here: `llm-wiring.json` and `models.json` carry
a gateway bearer token in `apiKey`, which was redacted when read.
