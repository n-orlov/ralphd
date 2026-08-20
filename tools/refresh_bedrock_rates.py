#!/usr/bin/env python3
"""Regenerate the built-in AWS Bedrock rate table in `engine/pricing_aws.py`.

Task 009 (#14). The table this script writes is a *mirror*, and a mirror with
no documented way to refresh it is a future lie: AWS changes prices, new model
ids appear, and a stale table would quietly derive wrong money. So the table is
generated, never hand-edited, and this script is the documented refresh path.

Source of truth (in order):

1. `pi-ai`'s bundled Bedrock provider data, which pi itself uses to price a
   request client-side:
   `<pi install>/node_modules/@earendil-works/pi-ai/dist/providers/data/amazon-bedrock.json`
   (key `bedrock-converse-stream`, per-model `cost` block, USD per MILLION
   tokens). Using the same numbers pi uses means a ralphd-derived cost and a
   pi-quoted cost cannot disagree on the rate, only on who computed it.
2. <https://aws.amazon.com/bedrock/pricing/> -- the human source those numbers
   mirror. Spot-check a couple of families after every refresh.

Usage:

    python tools/refresh_bedrock_rates.py                    # autodetect pi-ai
    python tools/refresh_bedrock_rates.py path/to/amazon-bedrock.json
    python tools/refresh_bedrock_rates.py --check            # CI-style diff only

It rewrites, in `src/ralphd/engine/pricing_aws.py`, exactly the region between
the `# BEGIN GENERATED RATES` / `# END GENERATED RATES` markers, plus the
`AS_OF` and `SOURCE_VERSION` lines. Everything else in that module (the
resolver wiring, staleness, docs) is hand-written and left alone.

Two deliberate transformations of the mirrored data, both documented in
`pricing_aws.py`:

* Region-prefixed ids (`eu.anthropic.claude-opus-5`) keep their OWN rate rather
  than being collapsed onto the region-less id: EU is ~10% above us-east and
  some ids differ far more, so collapsing would silently misprice.
* An explicit `0` cache rate in the mirror is DROPPED, not stored, so
  `ModelRate.per_mtok` falls back to the input rate. A zero cache rate means
  "this model has no prompt caching tier in the mirror", and pricing real
  cached tokens at $0 is the exact class of lie #10/#14 exist to remove;
  overstating is recoverable, a silent $0 is not.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGET = REPO / "src" / "ralphd" / "engine" / "pricing_aws.py"
BEGIN = "    # BEGIN GENERATED RATES"
END = "    # END GENERATED RATES"
API_KEY = "bedrock-converse-stream"
RATE_KEYS = ("input", "output", "cacheRead", "cacheWrite")

# Where a pi install keeps the provider data, relative to a node_modules root.
PI_DATA_REL = Path("@earendil-works/pi-ai/dist/providers/data/amazon-bedrock.json")
PI_SEARCH = (
    Path("/usr/lib/node_modules/@earendil-works/pi-coding-agent/node_modules"),
    Path("/usr/local/lib/node_modules/@earendil-works/pi-coding-agent/node_modules"),
    Path.home() / ".local/share/pi/node_modules",
)


def find_source() -> Path:
    for root in PI_SEARCH:
        cand = root / PI_DATA_REL
        if cand.is_file():
            return cand
    raise SystemExit(
        "could not find pi-ai's amazon-bedrock.json; pass the path explicitly "
        "(see the module docstring for where it lives)"
    )


def source_version(path: Path) -> str:
    """`pi-ai@<version>` for provenance, or the file's own mtime date."""
    for parent in path.parents:
        pkg = parent / "package.json"
        if pkg.is_file():
            try:
                data = json.loads(pkg.read_text())
            except ValueError:
                break
            name, version = data.get("name"), data.get("version")
            if name and version:
                return f"{name}@{version}"
    stamp = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.UTC).date()
    return f"{path.name} (mtime {stamp.isoformat()})"


def render_rates(models: dict[str, dict]) -> list[str]:
    lines: list[str] = []
    for model_id in sorted(models):
        cost = models[model_id].get("cost") or {}
        parts: list[str] = []
        for key in RATE_KEYS:
            raw = cost.get(key)
            if raw is None:
                continue
            value = float(raw)
            if key in ("cacheRead", "cacheWrite") and value == 0.0:
                continue  # see the module docstring: never store a $0 cache rate
            parts.append(f'"{key}": {value!r}')
        if not parts:
            print(f"  skipping {model_id}: no usable rate", file=sys.stderr)
            continue
        body = "{" + ", ".join(parts) + "}"
        line = f'    "{model_id}": {body},'
        if len(line) <= 99:
            lines.append(line)
        else:
            lines.append(f'    "{model_id}":')
            lines.append(f"        {body},")
    return lines


def generated_region(text: str) -> list[str]:
    """The current contents of the generated region, markers excluded."""
    lines = text.splitlines()
    return lines[lines.index(BEGIN) + 1: lines.index(END)]


def rewrite(text: str, rates: list[str], as_of: str, version: str) -> str:
    lines = text.splitlines()
    try:
        start = lines.index(BEGIN)
        stop = lines.index(END)
    except ValueError as exc:  # pragma: no cover - only if the markers are removed
        raise SystemExit(f"{TARGET}: generated-region markers missing") from exc
    out = lines[: start + 1] + rates + lines[stop:]
    body = "\n".join(out) + "\n"
    body = re.sub(r'^AS_OF = ".*"$', f'AS_OF = "{as_of}"', body, count=1, flags=re.MULTILINE)
    return re.sub(
        r'^SOURCE_VERSION = ".*"$', f'SOURCE_VERSION = "{version}"', body, count=1, flags=re.MULTILINE
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", nargs="?", help="path to pi-ai's amazon-bedrock.json")
    ap.add_argument("--check", action="store_true", help="exit 1 if the table is out of date")
    ap.add_argument("--as-of", default=dt.datetime.now(dt.UTC).date().isoformat())
    args = ap.parse_args(argv)

    source = Path(args.source) if args.source else find_source()
    data = json.loads(source.read_text())
    models = data.get(API_KEY) or {}
    if not isinstance(models, dict) or not models:
        raise SystemExit(f"{source}: no {API_KEY!r} model table")

    rates = render_rates(models)
    version = source_version(source)
    current = TARGET.read_text()
    if args.check:
        # Compare only the generated region: AS_OF legitimately differs.
        same = generated_region(current) == rates
        print("rate table up to date" if same else "rate table OUT OF DATE: rerun without --check")
        return 0 if same else 1
    TARGET.write_text(rewrite(current, rates, args.as_of, version))
    print(f"{TARGET}: {len(models)} models from {version} (as-of {args.as_of})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
