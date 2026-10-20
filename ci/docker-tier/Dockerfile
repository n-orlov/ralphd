# The docker tier's lane image (docs/ci.md). The Jenkinsfile's Docker lane
# runs `-m docker` in a container of this image, as the agent's uid, with the
# host docker socket and the workspace mounted at its own host path, so the
# tier's sibling containers resolve every bind mount it hands them.
#
# It carries what the retired GitHub runner image gave the tier: Ubuntu 24.04
# (python3 3.12.3, venv, git) and the docker CLI, the client only -- the tier
# talks to the agent's daemon through its socket. ci/jenkins-image.sh builds
# it on the agent, tagged by a hash of this directory, and never pushes it.
FROM ubuntu@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl git gnupg jq procps python3 python3-pip python3-venv \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc \
    && echo "deb [signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

RUN python3 --version && docker --version && command -v setpriv
