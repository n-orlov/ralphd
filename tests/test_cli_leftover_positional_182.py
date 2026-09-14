"""Task 071 (#182): argparse dropping a positional that follows a flag.

The operator measured that on some argparse patch levels (their own
reproducer named Python 3.12.3; this container runs 3.12.14, where every
form below already parses correctly -- these nodes pass VACUOUSLY here and
are not the evidence the fix works, see the task's own commit message) an
optional positional (`nargs="?"`) interleaved with a flag is dropped or
misrouted by argparse itself instead of being assigned to its destination,
and `nargs="*"` does not fix it.

`ralphd.cli.main._place_leftover_positional` is the fix: the ONE place that
folds a leftover token `main()`'s own `parser.parse_known_args()` did not
consume back into the first still-empty `nargs="?"` positional of the
affected subcommand, independent of whichever argparse version produced
that leftover (or of whether there is a leftover at all).

Every test here exercises that function DIRECTLY, never by shelling out to
`ralphctl` and never by handing real argv to `build_parser().parse_args()`
-- doing either would only ever observe THIS container's own (unaffected)
argparse. Instead each test hand-builds the `namespace`/`leftovers` pair the
function actually consumes, matching exactly what a buggy argparse would
have produced (fields already placed by argparse stay as they are; a field
argparse dropped stays `None` and its token is passed as the one leftover).
"""

from __future__ import annotations

import argparse

import pytest

from ralphd.cli.main import (
    _LEFTOVER_POSITIONAL_FIELDS,
    _place_leftover_positional,
    cmd_amend,
    cmd_steer,
)


def _amend_ns(**over):
    base = dict(json=False, command="amend", run_id="s1", task_id=None,
                text=None, file=None, waive=False, status=None, list=False,
                approach=None, force=False, wave=False)
    base.update(over)
    return argparse.Namespace(**base)


def _steer_ns(**over):
    base = dict(json=False, command="steer", run_id="s1", message=None,
                file=None, name=None, now=False, list=False)
    base.update(over)
    return argparse.Namespace(**base)


# --------------------------------------------------------------------------
# The five #182 forms named by the task, against `amend`
# --------------------------------------------------------------------------

def test_182_form_1_status_before_trailing_text(capsys):
    """`amend s1 007 --status skipped 'the ruling'`: argparse placed
    task_id and --status but dropped the trailing text as a leftover."""
    ns = _amend_ns(task_id="007", status="skipped")
    _place_leftover_positional("amend", ["the ruling"], ns,
                                _LEFTOVER_POSITIONAL_FIELDS["amend"])
    assert ns.text == "the ruling"
    assert ns.status == "skipped"


def test_182_form_2_waive_before_trailing_text():
    """`amend s1 007 --waive 'the ruling'`: same shape with --waive."""
    ns = _amend_ns(task_id="007", waive=True)
    _place_leftover_positional("amend", ["the ruling"], ns,
                                _LEFTOVER_POSITIONAL_FIELDS["amend"])
    assert ns.text == "the ruling"
    assert ns.waive is True


def test_182_form_3_list_before_extra_reaches_the_cli_own_mutex_message(capsys):
    """`amend r4 --list 001`: argparse dropped the run's task id as a
    leftover because it followed --list. Once `_place_leftover_positional`
    folds `001` into `task_id`, `cmd_amend` must refuse with its OWN
    list/send mutex message -- never argparse's generic "unrecognized
    arguments"."""
    ns = _amend_ns(run_id="r4", list=True)
    _place_leftover_positional("amend", ["001"], ns,
                                _LEFTOVER_POSITIONAL_FIELDS["amend"])
    assert ns.task_id == "001"
    with pytest.raises(SystemExit) as exc:
        cmd_amend(ns)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--list only shows" in err


def test_182_form_4_text_before_status_already_works_and_is_unchanged():
    """`amend s1 007 'the ruling' --status skipped`: text precedes the
    flag here, a shape every argparse patch level places correctly, so
    there is no leftover at all -- asserted, not assumed, by handing the
    function the namespace argparse would already have produced and an
    EMPTY leftover list, and checking nothing moves."""
    ns = _amend_ns(task_id="007", text="the ruling", status="skipped")
    _place_leftover_positional("amend", [], ns,
                                _LEFTOVER_POSITIONAL_FIELDS["amend"])
    assert ns.text == "the ruling"
    assert ns.status == "skipped"


def test_182_form_5_plain_trailing_text_already_works_and_is_unchanged():
    """`amend s1 007 'the ruling'`: no flags at all interleaved, so this
    already works on every argparse patch level -- asserted the same way
    as form 4, empty leftovers, nothing moves."""
    ns = _amend_ns(task_id="007", text="the ruling")
    _place_leftover_positional("amend", [], ns,
                                _LEFTOVER_POSITIONAL_FIELDS["amend"])
    assert ns.text == "the ruling"


# --------------------------------------------------------------------------
# The same placement, for `steer --list <extra>`
# --------------------------------------------------------------------------

def test_182_steer_list_before_extra_reaches_steers_own_mutex_message(capsys):
    """`steer <run> --list <extra>`: the same shape as form 3, on `steer`'s
    single `message` positional -- must reach `steer`'s own list-vs-send
    mutex message, never argparse's generic error."""
    ns = _steer_ns(list=True)
    _place_leftover_positional("steer", ["extra"], ns,
                                _LEFTOVER_POSITIONAL_FIELDS["steer"])
    assert ns.message == "extra"
    with pytest.raises(SystemExit) as exc:
        cmd_steer(ns)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--list only shows" in err


# --------------------------------------------------------------------------
# The function's own error paths
# --------------------------------------------------------------------------

def test_more_than_one_leftover_is_refused_by_name(capsys):
    ns = _amend_ns(task_id="007")
    with pytest.raises(SystemExit) as exc:
        _place_leftover_positional("amend", ["a", "b"], ns,
                                    _LEFTOVER_POSITIONAL_FIELDS["amend"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "amend" in err
    assert "'a'" in err and "'b'" in err


def test_no_unfilled_positional_is_refused_by_name(capsys):
    ns = _amend_ns(task_id="007", text="already here")
    with pytest.raises(SystemExit) as exc:
        _place_leftover_positional("amend", ["stray"], ns,
                                    _LEFTOVER_POSITIONAL_FIELDS["amend"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "amend" in err
    assert "'stray'" in err


def test_no_leftovers_is_a_pure_no_op():
    ns = _steer_ns(message="hello")
    _place_leftover_positional("steer", [], ns,
                                _LEFTOVER_POSITIONAL_FIELDS["steer"])
    assert ns.message == "hello"
