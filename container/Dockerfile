FROM python:3.12-slim-bookworm

# system deps + node 20 (for the pi CLI)
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates jq ripgrep procps \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @earendil-works/pi-coding-agent

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
