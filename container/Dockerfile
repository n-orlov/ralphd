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

RUN useradd -m -u 1000 -s /bin/bash agent \
    && mkdir -p /workspace /run/ralphd /config \
    && chown -R agent:agent /workspace /run/ralphd /home/agent

COPY --chown=agent:agent . /opt/ralphd
RUN pip install --no-cache-dir /opt/ralphd

USER agent
WORKDIR /workspace
ENV RALPHD_RUN_DIR=/run/ralphd \
    RALPHD_CONFIG_DIR=/config \
    RALPHD_WORKSPACE_DIR=/workspace \
    PI_OFFLINE=1

EXPOSE 7777
ENTRYPOINT ["/opt/ralphd/container/entrypoint.sh"]
