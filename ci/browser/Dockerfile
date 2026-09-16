# The browser tier's image (task 011, requirement H, #205 step 3).
#
# `heavy.yml`'s `browser-tier` runs in THIS image, pinned by digest, and takes
# `playwright-cli`, Google Chrome and Chrome's system dependencies from it --
# not from the runner host's ambient provisioning (which drifted once already:
# the VPS-to-workstation move silently lost python3.12.3, #205's own
# motivation) and not from a job-time `npm install` (which makes the versions a
# property of whenever the job happened to run, and of npm's availability at
# that moment).
#
# Base: `mcr.microsoft.com/playwright:v1.62.1-noble`, pinned by digest. It
# supplies Ubuntu 24.04 + Node 22 + git + a uid-1000 `ubuntu` account +
# playwright's own browser system dependencies, but NOT `@playwright/cli` and
# NOT real Google Chrome (only playwright-managed Chromium), which is why this
# image exists at all.
FROM mcr.microsoft.com/playwright@sha256:dcc5531e97840b9b5e794f2814476b21571c5124a3fca2267d73041f56e7580e

LABEL org.opencontainers.image.source=https://github.com/n-orlov/ralphd
LABEL org.opencontainers.image.description="ralphd heavy browser tier: playwright-cli + Google Chrome, pinned"

# The exact binary `tests/test_browser_hub.py` shells out to
# (`shutil.which("playwright-cli")`), at the version `docs/ci.md` records as
# the host provisioning pin, so containerizing the tier changes the toolchain's
# SOURCE and not its version.
ARG PLAYWRIGHT_CLI_VERSION=0.1.19

# Gotcha (same one `container/Dockerfile` documents for the product image):
# playwright-cli's DEFAULT browser channel is `chrome`, not chromium, so bare
# `playwright-cli open` needs real Google Chrome at /opt/google/chrome/chrome;
# `install chrome` puts it there and `install-deps chromium` pulls the shared
# system libs. `npx` is run from the package dir so it resolves the playwright
# bundled with @playwright/cli instead of downloading a second copy.
#
# `python3.12-venv` is this image's other job: the base ships `python3.12` but
# not `ensurepip`, so `python3 -m venv` fails with "ensurepip is not available"
# on the bare base (verified against this exact digest). The browser tier
# builds its venv from the image's interpreter, so the module belongs here
# rather than in an apt install the job re-runs every time.
RUN npm install -g --registry https://registry.npmjs.org \
        "@playwright/cli@${PLAYWRIGHT_CLI_VERSION}" \
    && cd "$(npm root -g)/@playwright/cli" \
    && npx playwright install-deps chromium \
    && npx playwright install chrome \
    && apt-get update \
    && apt-get install -y --no-install-recommends python3.12-venv \
    && rm -rf /var/lib/apt/lists/*

# Fail the BUILD, not the lane, if either tool is missing: the tier's own
# toolchain assertion step reports these versions, and it can only report what
# this layer proved present.
RUN playwright-cli --version \
    && google-chrome --version \
    && python3 -m venv /tmp/_venv_probe \
    && rm -rf /tmp/_venv_probe
