# The elastic runner pool's image (v0.34, requirement B.1, #303).
#
# One container of this image runs one GitHub Actions job: `run-once.sh` starts
# the runner a single time with a just-in-time config and the container exits
# when the job ends (B.4). The scaler (`ci/runner_scaler.py`) removes it by
# exact name afterwards. There is no `config.sh` registration step anywhere.
#
# Base: Ubuntu 24.04 (noble), pinned by the digest of the multi-arch index.
FROM ubuntu@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55

LABEL org.opencontainers.image.source=https://github.com/n-orlov/ralphd
LABEL org.opencontainers.image.description="ralphd elastic CI runner: one JIT job per container"

# The actions/runner release and the SHA-256 of its linux-x64 archive, as
# published in that release's notes. Bump both together.
ARG RUNNER_VERSION=2.338.0
ARG RUNNER_SHA256=af4b794c1bc41d73d40535e3fe092a39f9679cd8d965954c2aca25a05ca41d32

ENV DEBIAN_FRONTEND=noninteractive

# The packages the repo's jobs need. `docker-ce-cli` is the client only: the
# jobs reach the host's daemon through its socket, none runs in the container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl git gnupg jq python3 python3-pip python3-venv \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc \
    && echo "deb [signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

# uid 1000 is the base image's `ubuntu` account.
RUN mkdir -p /home/ubuntu/actions-runner && chown -R 1000:1000 /home/ubuntu
WORKDIR /home/ubuntu/actions-runner

RUN curl -fsSL -o runner.tar.gz \
        "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz" \
    && echo "${RUNNER_SHA256}  runner.tar.gz" | sha256sum -c - \
    && tar xzf runner.tar.gz \
    && rm runner.tar.gz \
    && ./bin/installdependencies.sh \
    && rm -rf /var/lib/apt/lists/* \
    && chown -R 1000:1000 /home/ubuntu

COPY --chown=1000:1000 run-once.sh /home/ubuntu/run-once.sh
RUN chmod 0755 /home/ubuntu/run-once.sh

# No ENTRYPOINT or CMD: the scaler's `docker create` argv names the command, so the
# one-job invocation is visible in (and tested from) that argv.
USER 1000
