"""Dotted-version parsing and numeric comparison for the operator surfaces.

One helper, used wherever the CLI or the hub needs to ask "is this engine
older than release X": a version is dotted non-negative integers
(`0.31.0`), compared component by component as numbers, never as text
(`0.9.0 < 0.31.0`). A value that is not a string of dotted digits is treated
as absent (`None`), so a caller never has to decide what a malformed
`engineVersion` means.
"""

from __future__ import annotations


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
