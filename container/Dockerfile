FROM python:3.12-slim-bookworm

# system deps + node 22 (pi >=0.75 requires node >=22.19; with an older node,
# npm silently resolves an ancient pi version — pin both to fail loudly instead)
ARG PI_VERSION=0.84.1
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates jq ripgrep procps \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g --registry https://registry.npmjs.org \
        "@earendil-works/pi-coding-agent@${PI_VERSION}"

# static docker CLI (client only — no daemon, no containerd, no buildx).
# Used with `ralphctl start --allow-docker`, which mounts the host docker
# socket; siblings run on the HOST daemon (volume paths resolve host-side).
ARG DOCKER_VERSION=29.7.2
RUN curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_VERSION}.tgz" \
      | tar -xz -C /usr/local/bin --strip-components=1 docker/docker \
    && docker --version

# playwright-cli browser automation (web-UI e2e verification).
# Gotcha: playwright-cli's DEFAULT browser channel is 'chrome', not chromium —
# bare `playwright-cli open` needs Google Chrome at /opt/google/chrome/chrome,
# so `install chrome` is mandatory; `install-deps chromium` pulls the shared
# system libs. Run npx from the package dir so it resolves the playwright
# bundled with @playwright/cli instead of downloading another copy.
ARG PLAYWRIGHT_CLI_VERSION=0.1.14
RUN npm install -g --registry https://registry.npmjs.org \
        "@playwright/cli@${PLAYWRIGHT_CLI_VERSION}" \
    && cd "$(npm root -g)/@playwright/cli" \
    && npx playwright install-deps chromium \
    && npx playwright install chrome \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 -s /bin/bash agent \
    && mkdir -p /workspace /run/ralphd /config \
    && chown -R agent:agent /workspace /run/ralphd /home/agent

COPY --chown=agent:agent . /opt/ralphd
RUN pip install --no-cache-dir /opt/ralphd

# Requirement I (#48): NO `USER agent` here, on purpose. The engine starts as
# root and immediately drops its *effective* uid to `agent` while keeping root
# as its real and saved uid (src/ralphd/engine/privsep.py), so:
#   * every file it writes -- run dir, workspace, $HOME -- is owned by uid 1000
#     exactly as before, and
#   * no process running as `agent` can signal it, because kill(2) permission
#     is decided by the target's real/saved uid. `pkill -f ralphd-engine` from
#     inside a task iteration is EPERM instead of the end of the run.
# Iterations themselves are spawned back down to uid 1000 (`user=`/`group=`),
# so the agent's own latitude is unchanged. HOME is the agent's home for both:
# the engine's $HOME config overlay, its ~/.creds placement and pi's own
# settings must all resolve to the one home pi reads.
WORKDIR /workspace
ENV HOME=/home/agent \
    RALPHD_RUN_DIR=/run/ralphd \
    RALPHD_CONFIG_DIR=/config \
    RALPHD_WORKSPACE_DIR=/workspace \
    PI_OFFLINE=1

EXPOSE 7777
ENTRYPOINT ["/opt/ralphd/container/entrypoint.sh"]
