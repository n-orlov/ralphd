"""Task 072 (#182 item 2): a parser-walking guard over `build_parser()`.

Task 071 fixed the two subcommands the operator's own reproducer named
(`amend`, `steer`). This module does not trust that list by hand a second
time -- it WALKS the parser object `build_parser()` returns (subparsers and
every nested subparser reached through `add_subparsers()`) and asks the
same question of every one of them: does this subparser declare a
positional with `nargs` in `{"?", "*"}` beside at least one optional flag
(the shape `_place_leftover_positional` exists to repair, #182)? Every
subcommand that answers yes must be named in
`ralphd.cli.main._LEFTOVER_POSITIONAL_FIELDS`, covering every one of its
flagged positional's `dest` names -- or the guard fails, naming the
offending subcommand path and positional.

The walk is proven non-vacuous below: a fixture parser built inside this
test file (never shipped code) with an unrouted `nargs="?"` positional
beside a flag makes the exact same walk-and-check fail.

Like tests/test_cli_leftover_positional_182.py, the two subcommands this
walk newly discovered (`budget`, `suggestions`) are exercised by feeding
`_place_leftover_positional` their own hand-built flag-then-positional
`namespace`/`leftovers` pair directly -- never by handing real argv to
`build_parser().parse_args()`, since this container's own argparge (3.12.14)
does not reproduce the operator's patch-level bug (Python 3.12.3, disclosed
in task 071's commit and unreachable here) and would only prove the
unaffected path already works.
"""

from __future__ import annotations

import argparse

import pytest

from ralphd.cli.main import (
    _LEFTOVER_POSITIONAL_FIELDS,
    _place_leftover_positional,
    build_parser,
)


def _iter_subparsers(parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()):
    """Yield `(path, subparser)` for every subparser and nested subparser
    reachable from `parser`, discovered from the parser object itself --
    never from a hand-written list of subcommand names."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                path = prefix + (name,)
                yield path, sub
                yield from _iter_subparsers(sub, path)


def _find_affected(parser: argparse.ArgumentParser):
    """Return `{path: [positional actions]}` for every subparser (reached
    via `_iter_subparsers`) that declares a positional with `nargs` in
    `{"?", "*"}` alongside at least one optional flag other than the
    argparse-provided `-h`/`--help`."""
    affected: dict[tuple[str, ...], list[argparse.Action]] = {}
    for path, sub in _iter_subparsers(parser):
        positionals = [a for a in sub._actions
                       if not a.option_strings and a.nargs in ("?", "*")]
        flags = [a for a in sub._actions
                 if a.option_strings and a.dest != "help"]
        if positionals and flags:
            affected[path] = positionals
    return affected


def _unrouted(affected: dict[tuple[str, ...], list[argparse.Action]],
              table: dict[str, tuple]) -> list[tuple[tuple[str, ...], list[argparse.Action]]]:
    """Names every entry of `affected` that `table` (shaped like
    `_LEFTOVER_POSITIONAL_FIELDS`) does not fully cover: a nested path
    (`main()` only ever dispatches on the TOP-level `args.command`, so a
    nested-subcommand match could never be routed by this mechanism at
    all), a top-level command missing from `table`, or one whose table
    entry does not name every flagged positional's `dest`."""
    bad = []
    for path, positionals in affected.items():
        if len(path) != 1 or path[0] not in table:
            bad.append((path, positionals))
            continue
        routed_fields = {field for field, _default, _choices in table[path[0]]}
        dests = {a.dest for a in positionals}
        if not dests <= routed_fields:
            bad.append((path, positionals))
    return bad


def _assert_all_routed(parser: argparse.ArgumentParser, table: dict[str, tuple]) -> None:
    affected = _find_affected(parser)
    unrouted = _unrouted(affected, table)
    assert not unrouted, "unrouted nargs=?/* positional(s) beside a flag: " + "; ".join(
        f"{'/'.join(path)}: {[a.dest for a in positionals]}"
        for path, positionals in unrouted
    )


# --------------------------------------------------------------------------
# The guard itself: green at HEAD because every flagged subcommand really
# is routed through _place_leftover_positional.
# --------------------------------------------------------------------------

def test_every_optional_positional_beside_a_flag_is_routed_through_placement():
    _assert_all_routed(build_parser(), _LEFTOVER_POSITIONAL_FIELDS)


def test_the_walk_discovers_exactly_the_four_known_affected_subcommands():
    """Not just "no unrouted ones" -- pin the discovered set itself, so a
    future subcommand that adds a fifth affected shape is surfaced here
    too (the routedness check above would then fail for it on its own,
    but this pins today's inventory against silent drift in either
    direction)."""
    affected = _find_affected(build_parser())
    assert {path[0] for path in affected if len(path) == 1} == {
        "steer", "amend", "budget", "suggestions",
    }


# --------------------------------------------------------------------------
# Non-vacuous: the identical walk-and-check, against a fixture parser with
# a deliberately unrouted affected subcommand, fails.
# --------------------------------------------------------------------------

def _build_fixture_parser_with_an_unrouted_optional_positional() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fixture")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("widget")
    s.add_argument("--flag", action="store_true")
    s.add_argument("thing", nargs="?")
    # An unaffected sibling (no flag beside its optional positional) stays
    # unrouted too and must NOT be flagged -- proves the walk isn't simply
    # flagging every nargs="?" positional it sees.
    s2 = sub.add_parser("harmless")
    s2.add_argument("thing", nargs="?")
    return p


def test_the_same_walk_and_check_fails_against_a_fixture_with_an_unrouted_positional():
    fixture_parser = _build_fixture_parser_with_an_unrouted_optional_positional()
    with pytest.raises(AssertionError) as exc:
        _assert_all_routed(fixture_parser, table={})
    message = str(exc.value)
    assert "widget" in message
    assert "thing" in message
    # The unaffected sibling never appears -- the walk only flags a
    # positional that shares its subparser with a real optional flag.
    assert "harmless" not in message


def test_the_fixtures_harmless_sibling_is_not_flagged_at_all():
    affected = _find_affected(_build_fixture_parser_with_an_unrouted_optional_positional())
    assert set(affected) == {("widget",)}


# --------------------------------------------------------------------------
# The two newly-discovered subcommands, fed their own flag-then-positional
# argv form directly against _place_leftover_positional (never real argv,
# see the module docstring).
# --------------------------------------------------------------------------

def _budget_ns(**over):
    base = dict(json=False, command="budget", run_id="s1", iterations=None,
                approaches=None, timeout=None, iteration_timeout=None)
    base.update(over)
    return argparse.Namespace(**base)


def _suggestions_ns(**over):
    base = dict(json=False, command="suggestions", run_id="s1", action="show",
                target=None, yes=False, only=None, ignore_conflicts=False)
    base.update(over)
    return argparse.Namespace(**base)


def test_budget_flag_before_trailing_iterations_spec_is_folded_in():
    """`budget s1 --approaches +2 +10`: argparse placed --approaches but
    dropped the trailing `+10` iterations spec as a leftover."""
    ns = _budget_ns(approaches="+2")
    _place_leftover_positional("budget", ["+10"], ns,
                                _LEFTOVER_POSITIONAL_FIELDS["budget"])
    assert ns.iterations == "+10"
    assert ns.approaches == "+2"


def test_budget_with_no_leftover_is_a_pure_no_op():
    ns = _budget_ns(iterations="+5")
    _place_leftover_positional("budget", [], ns,
                                _LEFTOVER_POSITIONAL_FIELDS["budget"])
    assert ns.iterations == "+5"


def test_suggestions_flag_before_trailing_action_is_folded_in():
    """`suggestions s1 --yes apply`: argparse placed --yes but dropped the
    trailing `apply` action as a leftover, leaving `args.action` at its
    own default `"show"` -- exactly the still-unfilled state the (field,
    default, choices) triple exists to recognise even though the field is
    never `None`."""
    ns = _suggestions_ns(yes=True)
    _place_leftover_positional("suggestions", ["apply"], ns,
                                _LEFTOVER_POSITIONAL_FIELDS["suggestions"])
    assert ns.action == "apply"
    assert ns.yes is True


def test_suggestions_leftover_outside_choices_is_refused_by_name(capsys):
    ns = _suggestions_ns(yes=True)
    with pytest.raises(SystemExit) as exc:
        _place_leftover_positional("suggestions", ["delete"], ns,
                                    _LEFTOVER_POSITIONAL_FIELDS["suggestions"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "suggestions" in err
    assert "'delete'" in err


def test_suggestions_with_no_leftover_is_a_pure_no_op():
    ns = _suggestions_ns(action="apply", yes=True)
    _place_leftover_positional("suggestions", [], ns,
                                _LEFTOVER_POSITIONAL_FIELDS["suggestions"])
    assert ns.action == "apply"
