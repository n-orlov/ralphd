#!/bin/sh
# Run the GitHub Actions runner ONCE with a just-in-time config, then exit.
#
# $1 is the path of a file holding the encoded JIT config (the scaler delivers
# it as a file, never through argv or the container's environment). The config
# already registers one single-use runner, so no `config.sh` step exists; the
# runner takes one job and the container exits with the runner's status.
set -eu
jit_file="${1:?usage: run-once.sh <jit config file>}"
cd /home/ubuntu/actions-runner
exec ./run.sh --jitconfig "$(cat "$jit_file")"
