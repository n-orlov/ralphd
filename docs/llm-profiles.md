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

1. Copy the host's pi provider/model configuration (`~/.pi/agent/settings.json`,
   `models.json`, `auth.json`) into `/config/pi/`.
2. **Resolve `!command` apiKey references.** pi supports
   `apiKey: "!some-command args"` (shell out per request). Such helper commands
   exist on the host, not in the container, so `ralphctl start` executes them
   on the host and injects the literal resolved value into the copied
   `models.json` (mode 0600, in the job's config dir — never the run dir).
   Trade-off: the value is frozen at start time; for long jobs with
   short-lived tokens, rotate mid-run via `PUT /config/llm` / `ralphctl llm set`.
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
