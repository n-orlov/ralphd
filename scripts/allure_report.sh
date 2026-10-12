#!/usr/bin/env bash
# Generate an Allure report from a directory of Allure results.
#
#   scripts/allure_report.sh <results-dir> <report-dir>
#
# The Allure CLI is a pinned download. Its archive is checked against a pinned
# SHA-256 BEFORE anything in it is unpacked or run; a mismatch exits non-zero
# (exit status 3) and no report is written. A verified, unpacked copy is kept
# in the runner tool cache ($RUNNER_TOOL_CACHE, else ~/.cache) and reused by
# the next run without downloading again.
#
# The CLI is a Java program. A `java` already on PATH is used as it is; when
# there is none, a pinned Temurin JRE is fetched and verified the same way, so
# the script needs nothing but bash, curl, tar and a SHA-256 tool.
#
# Environment (the pins themselves are not overridable):
#   ALLURE_TOOL_CACHE  directory holding the verified copies
#   ALLURE_CLI_URL     where to fetch the Allure archive from (default: Maven
#                      Central). A file:// URL works; the pinned checksum still
#                      applies to whatever it serves.
#   ALLURE_JRE_URL     the same for the JRE archive
set -euo pipefail

ALLURE_VERSION="2.34.1"
ALLURE_SHA256="df13c5883429edd5041a24d6d072a8944e65274d1d76a441fd45d2585511349a"
JRE_VERSION="21.0.8+9"
JRE_SHA256="968c283e104059dae86ea1d670672a80170f27a39529d815843ec9c1f0fa2a03"

ALLURE_DEFAULT_URL="https://repo.maven.apache.org/maven2/io/qameta/allure/allure-commandline/${ALLURE_VERSION}/allure-commandline-${ALLURE_VERSION}.tgz"
JRE_DEFAULT_URL="https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.8%2B9/OpenJDK21U-jre_x64_linux_hotspot_21.0.8_9.tar.gz"

die() { echo "allure_report.sh: $1" >&2; exit "${2:-1}"; }

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

SCRATCH=()
cleanup() { [ "${#SCRATCH[@]}" -eq 0 ] || rm -rf "${SCRATCH[@]}"; }
trap cleanup EXIT

[ "$#" -eq 2 ] || die "usage: allure_report.sh <results-dir> <report-dir>" 2
RESULTS="$1"
REPORT="$2"
[ -d "$RESULTS" ] || die "results directory not found: $RESULTS" 2
[ -n "$(ls -A "$RESULTS")" ] || die "results directory is empty: $RESULTS" 2

CACHE_BASE="${ALLURE_TOOL_CACHE:-${RUNNER_TOOL_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}}}"
mkdir -p "$CACHE_BASE"

# fetch_verified <name> <url> <sha256> <dest>: download, verify, unpack, then
# publish <dest> atomically. A <dest> carrying a `.verified` marker for this
# exact checksum is reused untouched.
fetch_verified() {
  local name="$1" url="$2" want="$3" dest="$4" tmp got
  if [ "$(cat "$dest/.verified" 2>/dev/null || true)" = "$want" ]; then
    echo "allure_report.sh: reusing verified $name from $dest" >&2
    return 0
  fi
  tmp="$(mktemp -d "$CACHE_BASE/.allure-fetch.XXXXXX")"
  SCRATCH+=("$tmp")
  echo "allure_report.sh: fetching $name" >&2
  curl -fsSL --retry 3 --retry-delay 5 --connect-timeout 30 --max-time 600 \
    -o "$tmp/archive" "$url" || die "could not download $name" 4
  got="$(sha256_of "$tmp/archive")"
  if [ "$got" != "$want" ]; then
    die "checksum mismatch for $name: expected $want, got $got; nothing was unpacked" 3
  fi
  mkdir "$tmp/unpacked"
  tar -xzf "$tmp/archive" -C "$tmp/unpacked" --strip-components=1
  printf '%s' "$want" > "$tmp/unpacked/.verified"
  mkdir -p "$(dirname "$dest")"
  rm -rf "$dest"
  mv "$tmp/unpacked" "$dest"
}

fetch_verified "Allure CLI ${ALLURE_VERSION}" "${ALLURE_CLI_URL:-$ALLURE_DEFAULT_URL}" \
  "$ALLURE_SHA256" "$CACHE_BASE/allure/${ALLURE_VERSION}/x64"

if ! command -v java >/dev/null 2>&1; then
  [ "$(uname -s)-$(uname -m)" = "Linux-x86_64" ] \
    || die "no java on PATH and no pinned JRE for $(uname -s)-$(uname -m)" 5
  fetch_verified "Temurin JRE ${JRE_VERSION}" "${ALLURE_JRE_URL:-$JRE_DEFAULT_URL}" \
    "$JRE_SHA256" "$CACHE_BASE/temurin-jre/${JRE_VERSION}/x64"
  export JAVA_HOME="$CACHE_BASE/temurin-jre/${JRE_VERSION}/x64"
  export PATH="$JAVA_HOME/bin:$PATH"
fi

"$CACHE_BASE/allure/${ALLURE_VERSION}/x64/bin/allure" generate "$RESULTS" -o "$REPORT" --clean
echo "allure_report.sh: report written to $REPORT" >&2
