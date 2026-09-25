"""Frozen closure sets for the v0.7 and v0.8 waves.

This module holds no tests. It keeps three literal sets that other checks
import by name: `tests/test_commit_closing_keyword_scope.py` (which issues a
commit's closing keyword may name) and `tests/test_left_open_issues_stay_open.py`
(which issues must still be open on GitHub). The values are historical facts
that will never change, so they are pinned here rather than parsed out of any
document.
"""

from __future__ import annotations


def closed_issues_v07() -> set[int]:
    """The issues the v0.7 wave closed."""
    return {28, *range(30, 50), 52, 53, 55}


def left_open_issues_v07() -> set[int]:
    """The issues the v0.7 wave deliberately left open."""
    return {24, 25, 26, 27, 50, 51, 54}


def closed_issues_v08() -> set[int]:
    """The issues the v0.8 wave closed."""
    return {24, 26, 27, 50, 51, 54, 56, 57, 58}
