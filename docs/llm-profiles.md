# LLM Profiles

ralphd delegates all model access to [pi](https://pi.dev), so anything pi supports
is usable: Anthropic, OpenAI, Google, AWS Bedrock, and any OpenAI- or
Anthropic-compatible endpoint via custom provider config (base URL + API key). The
CLI's job is to get the right **env vars, files, and pi config fragments** into the
container. That bundle is an **LLM profile**.

## Profile format

A profile is a YAML file in `~/.ralphd/llm-profiles/<name>.yaml`:

```yaml
# ~/.ralphd/llm-profiles/example.yaml
description: what this points at
model: provider/model-id            # default model for jobs using this profile
fast_model: provider/cheap-model-id # profile's own second model ref (task 006:
                                    # no longer forwarded into a started job's
                                    # job.yaml)
price_strategy: aws                 # optional: none|aws — built-in rate table for
                                    # routes this gateway does not price itself

env:                                # env vars set in the container
  SOME_API_KEY: "literal-value"     # literal … # pragma: allowlist secret
  OTHER_KEY: ${env:HOST_VAR}        # … or read from the host env at start time
  THIRD_KEY: ${file:~/.config/thing/key}   # … or read from a host file
  FOURTH_KEY: ${cmd:pass show thing/key}   # … or from a command (e.g. secret manager)

mounts:                             # host paths mounted read-only into the container
  - ~/.aws:/home/agent/.aws:ro

pi:                                 # merged into the container's pi provider config
  providers:
    my-gateway:
      baseUrl: https://gw.example.com/api/v1
      api: openai-completions       # or anthropic-messages
      apiKey: ${env:GW_API_KEY}     # resolved against `env:` above
      models:
        - id: some-model
          name: Some Model
```

Resolution rules:

- `${env:…}`, `${file:…}`, `${cmd:…}` are resolved **on the host by ralphctl at
  container start** — secrets travel as container env / tmpfs files, never through
  the run dir or image.
- `model` becomes the started job's plain `model` default unless overridden by
  `--model`/a template/the registry. `fast_model` is a profile-only value now
  (task 006 retired job.yaml's own `fast_model` key) — `ralphctl llm show`
  still surfaces it, but `start` no longer copies it into a job's job.yaml.
- `price_strategy` (v0.6, optional) declares which built-in rate table may derive a
  cost for routes this endpoint bills without quoting a price — `aws` for an
  AIGW/Bedrock-style gateway (it bills Bedrock list price, so ralphd's built-in
  table knows the rate), `none` to leave such a cost `unavailable`. Omit the key
  to have no opinion. `ralphctl start` uses it only when nothing more specific
  decided (explicit `--price-strategy` > `--template` > `ralphctl config set
  price_strategy` > this field), and writes the result into the run's `job.yaml`.
  A derived cost is always marked derived (`~$0.45 derived`), never passed off as
  a provider quote; see `docs/cli.md` ("Built-in AWS Bedrock rate table") and
  `docs/api.md` (`priceStrategy`). An unknown value here is an error at `start`
  time, not a silent fallback.
- `--llm-env KEY=VAL` on `start` layers on top of the profile.
- `ralphctl llm test <profile>` verifies a profile end-to-end with a one-token
  completion in a throwaway container.

## Built-in profiles

Two names are always available without a profile file:

### `host` (the default)

Forward the host's existing LLM setup into the container:

1. Copy the host's pi provider/model configuration (`~/.pi/agent/settings.json`,
   `models.json`, `auth.json`) into `/config/pi/`.
2. **Resolve `!command` apiKey references.** pi supports
   `apiKey: "!some-command args"` (shell out per request). Such helper commands
   exist on the host, not in the container, so `ralphctl start` executes them
   on the host and injects the literal resolved value into the copied
   `models.json` (mode 0600, in the job's config dir — never the run dir).
   Trade-off: the value is frozen at start time; for long jobs with
   short-lived tokens, rotate mid-run via the container API's
   `PUT /config/llm` (see "Mid-run rotation" below).
3. Forward ONLY standard, vendor-documented credential env vars when set:
   `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY`,
   `AWS_REGION`, `AWS_DEFAULT_REGION`, `AWS_PROFILE`, `AWS_ACCESS_KEY_ID`,
   `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`.
4. Mount `~/.aws` read-only when it exists.

**Design rule: no environment-specific vars are ever baked into ralphd code.**
Anything beyond the standard list — endpoint overrides, gateway bearer tokens,
SDK tuning knobs — must be forwarded explicitly per job:

```bash
ralphctl start ... --forward-env AWS_BEARER_TOKEN_BEDROCK \
                   --forward-env AWS_ENDPOINT_URL_BEDROCK_RUNTIME
# or wholesale by prefix:
ralphctl start ... --forward-env 'AWS_*'
```

Forward related vars **together**: a bearer token whose endpoint-override
variable is left behind will be sent to the vendor's real endpoint and
rejected (observed failure mode: `AccessDeniedException: Invalid API Key
format`). Prefix globs (`AWS_*`) are the safe way to keep a family of vars
intact. For a recurring setup, promote the flags into a named profile.

### `none`

Inject nothing; the operator supplies everything via `--llm-env`/`--env`/mounts.
For fully custom setups and debugging.

## Proven presets (shipped as examples)

These two ship in `examples/llm-profiles/` and are part of the v0.1 acceptance
tests — they prove the two mechanisms (SDK credential chain, custom gateway) that
everything else is a variation of. **The engine has no code specific to either;**
they are plain profiles.

### AWS Bedrock via the standard AWS credential chain

```yaml
# examples/llm-profiles/bedrock.yaml
description: AWS Bedrock, auth via host AWS CLI credentials/SSO
model: amazon-bedrock/anthropic.claude-opus-5
env:
  AWS_REGION: ${env:AWS_REGION}
  AWS_PROFILE: ${env:AWS_PROFILE}    # optional; omit to use default chain
mounts:
  - ~/.aws:/home/agent/.aws:ro
```

pi's Bedrock provider uses the standard AWS SDK credential chain, so mounting
`~/.aws` (config + SSO/credential cache) plus region is sufficient. Works with
static keys, SSO sessions, and assumed roles; token refresh caveats (e.g. expired
SSO mid-run) are surfaced as iteration failures and fixable live via
`PUT /config/llm`.

### Generic OpenAI/Anthropic-compatible gateway (endpoint + API key)

```yaml
# examples/llm-profiles/gateway.yaml
description: any bearer-token gateway exposing an Anthropic- or OpenAI-style API
model: my-gateway/big-model
fast_model: my-gateway/small-model
env:
  GW_API_KEY: ${cmd:aws secretsmanager get-secret-value --secret-id my-gw-key --query SecretString --output text}
pi:
  providers:
    my-gateway:
      baseUrl: https://my-gateway.example.com/api/v1
      api: anthropic-messages          # or openai-completions
      apiKey: ${env:GW_API_KEY}
      models:
        - id: big-model
        - id: small-model
```

This is the shape for corporate AI gateways (endpoint URL + rotating API key):
point `baseUrl` at the gateway, pick the wire API it speaks, list the model IDs it
fronts. Key rotation mid-run: re-resolve the profile on the host (`ralphctl
llm show gateway` prints it redacted) and push the fresh key with
`PUT /config/llm`.

### Worked cross-family example: one gateway, two model families, one bearer token

`pi.providers` is a map, so nothing above is limited to one provider per profile.
This ships as `examples/llm-profiles/aigw-cross-family.yaml` and is the profile
#60 pillar 4's "multi-provider auth stays a one-profile-per-run model" claim rests
on: it was verified against a live AIGW-style gateway that fronts a Bedrock-shaped
Anthropic route (`api: bedrock-converse-stream`) and an OpenAI-shaped route
(`api: openai-responses`) behind the **same bearer token** (see
`docs/prds/v0.11-a-model-per-hat.md`, "The gateway question is already answered —
cross-family is routable today").

```yaml
# examples/llm-profiles/aigw-cross-family.yaml
description: one gateway fronting a Bedrock-shaped Anthropic route and an OpenAI-shaped route, one bearer token
model: amazon-bedrock/eu.anthropic.claude-opus-5
env:
  AIGW_BEARER_TOKEN: ${env:AIGW_BEARER_TOKEN}
pi:
  providers:
    amazon-bedrock:
      baseUrl: https://aigw.example.com/api/bedrock
      api: bedrock-converse-stream
      apiKey: ${env:AIGW_BEARER_TOKEN}
      models:
        - id: eu.anthropic.claude-opus-5
        - id: eu.anthropic.claude-sonnet-5
    aigw-openai:
      baseUrl: https://aigw.example.com/api/openai/v1
      api: openai-responses
      apiKey: ${env:AIGW_BEARER_TOKEN}
      models:
        - id: openai.gpt-5.6-sol
        - id: openai.gpt-5.6-luna
        - id: openai.gpt-5.6-terra
```

Both `pi.providers` entries resolve their `apiKey` from the exact same `env:`
entry: reaching a second model family through this ONE profile needed no
profile-format change, no second `--llm` on `start`, and no extra auth
plumbing — it is a `pi.providers` map with a second key, same as any other
multi-model profile. **Multi-provider auth stays one profile per run.**

With that one profile started (`--llm aigw-cross-family`), the per-hat table
(`docs/cli.md`, "Per-hat model table") can now configure an Anthropic planner
and worker verified by a model from the *other* family entirely — #60 pillar
4's own acceptance configuration:

```yaml
# job.yaml (or --model-for HAT=REF / a --template / <registry>/config.yaml)
models:
  planning: amazon-bedrock/eu.anthropic.claude-opus-5
  worker:   amazon-bedrock/eu.anthropic.claude-sonnet-5
  verify:   aigw-openai/openai.gpt-5.6-terra     # cross-family inspector
```

Every ref on the right resolves through the single `aigw-cross-family` profile
above — no second profile, no second bearer token, no per-provider auth wiring.

The same profile also answers the per-*tier* question (`docs/cli.md`,
"Per-(hat, tier) override table", [SPEC.md](../SPEC.md) §4.7): a `worker`
task that escalates to `hard` provisions a *different* Anthropic rung than its
`trivial`/`normal` work, while `verify` climbs a whole ladder in the *other*
family — one gateway, one bearer token, still no second profile:

```yaml
# job.yaml (or --model-for-tier HAT:TIER=REF / a --template / <registry>/config.yaml)
model_tiers:
  worker:                                            # trivial/normal stay on models.worker above
    hard: amazon-bedrock/eu.anthropic.claude-opus-5  # escalated rung only -- bigger Anthropic model
  verify:                                            # a full OpenAI-family ladder, cheapest to priciest
    trivial: {model: aigw-openai/openai.gpt-5.6-sol,   effort: low}
    normal:  {model: aigw-openai/openai.gpt-5.6-luna,  effort: medium}
    hard:    {model: aigw-openai/openai.gpt-5.6-terra, effort: high}
```

`worker`'s `trivial`/`normal` rungs are left unset, so they fall through to
`models.worker` above unchanged (`amazon-bedrock/eu.anthropic.claude-sonnet-5`)
— only the escalated `hard` rung actually differs. `verify`'s three rungs all
come from the OpenAI-shaped route on the *same* gateway, keyed by the same
`AIGW_BEARER_TOKEN`, resolved by the same `tiers.resolve_bundle`
(`(hat, effective_tier, host_map) -> HatBundle`) that resolves every other
hat's ladder — a cross-family worker/verify split at the plain per-hat level
and a cross-family ladder at the per-tier level cost the same one profile.

## Mid-run rotation

`PUT /config/llm` replaces the container's LLM env
and pi fragments atomically; the next iteration's `pi` process picks them up. Use
cases: expired gateway keys, expired SSO, switching a stuck job to a different
model/provider without losing loop state.

## Security notes

- Profiles may *reference* secrets (`${env:}`/`${file:}`/`${cmd:}`); prefer that
  over literals so profile files are safe to share/commit.
- Resolved secret values exist only in container env / `/config` (tmpfs-backed for
  API-pushed creds) — never in `~/.ralphd/runs/` and never in API responses
  (`GET /config` redacts).
- `ralphctl llm show` prints the resolved profile with values masked.
