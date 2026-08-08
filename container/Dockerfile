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
