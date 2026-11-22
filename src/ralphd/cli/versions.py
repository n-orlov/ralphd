"""Dotted-version parsing and numeric comparison for the operator surfaces.

One helper, used wherever the CLI or the hub needs to ask "is this engine
older than release X": a version is dotted non-negative integers
(`0.31.0`), compared component by component as numbers, never as text
(`0.9.0 < 0.31.0`). A value that is not a string of dotted digits is treated
as absent (`None`), so a caller never has to decide what a malformed
`engineVersion` means.
"""

from __future__ import annotations

import os


def parse_version(value: object) -> tuple[int, ...] | None:
    """`"0.31.0"` -> `(0, 31, 0)`; anything non-numeric or malformed -> `None`."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split(".")
    if not parts or not all(p.isascii() and p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def compare_versions(a: object, b: object) -> int | None:
    """-1, 0 or 1 as `a` is older than, equal to or newer than `b`; `None`
    when either side is absent or malformed. Missing trailing components
    count as zero, so `0.31` equals `0.31.0`."""
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        return None
    width = max(len(pa), len(pb))
    pa += (0,) * (width - len(pa))
    pb += (0,) * (width - len(pb))
    return (pa > pb) - (pa < pb)


class ProbeFailed(str):
    """A `<cli> --version` probe that could not finish: it timed out, or the
    file exists and could not be run. A `str` holding what happened ("timed
    out after 30 s", or the OSError text), so it is never mistaken for a
    version read (`None` is "ran, printed output with no version", an
    `EmptyProbeOutput` is "ran, printed nothing"; a plain `str` is a
    version)."""


class EmptyProbeOutput(str):
    """A `<cli> --version` probe that finished but printed nothing at all, on
    stdout or stderr. A `str` naming what happened ("exited 0 and printed
    nothing"). It carries no evidence of the CLI's version, so it is never
    "too old": that is reserved for a version read below the minimum or
    output that is present and carries no version (`None`)."""


def cli_file_exists(cli: str) -> bool:
    """Whether the CLI file is there at all, executable or not: `cli` itself
    when it names a path, else the first `PATH` entry holding a file of that
    name. `shutil.which` skips a file without the exec bit, which would make
    a non-executable CLI on `PATH` look missing when running it raised
    `PermissionError`."""
    if os.sep in cli or (os.altsep and os.altsep in cli):
        return os.path.isfile(cli)
    return any(os.path.isfile(os.path.join(d or os.curdir, cli))
               for d in os.environ.get("PATH", os.defpath).split(os.pathsep))
