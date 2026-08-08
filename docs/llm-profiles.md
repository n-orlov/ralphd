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
fast_model: provider/cheap-model-id # optional: "fast" tier for cost strategies

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
- `model`/`fast_model` map onto the job's model-strategy tiers unless overridden by
  `--model*` flags.
- `--llm-env KEY=VAL` on `start` layers on top of the profile.
- `ralphctl llm test <profile>` verifies a profile end-to-end with a one-token
  completion in a throwaway container.

## Built-in profiles

Two names are always available without a profile file:

### `host` (the default)

Forward the host's existing LLM setup into the container:

1. Copy the host's pi provider/model configuration (`~/.pi` settings relevant to
   providers) into `/config/pi/`.
2. Forward the well-known credential env vars that are actually set on the host
   (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, AWS_* vars, and the
   documented pi provider vars).
3. If the host pi config references Bedrock, also mount `~/.aws` read-only.

"Whatever works in `pi` on your laptop works in the container."

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
`ralphctl llm set`.

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
fronts. Key rotation mid-run: `ralphctl llm set <run-id> --profile gateway`
re-resolves `${cmd:…}` and pushes the fresh key via `PUT /config/llm`.

## Mid-run rotation

`PUT /config/llm` (wrapped by `ralphctl llm set`) replaces the container's LLM env
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
