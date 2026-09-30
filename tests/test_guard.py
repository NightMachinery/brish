"""The suite must test the tree it lives in, never an installed brish."""

from pathlib import Path

from tests.conftest import ROOT, check


def test_worktree_guard_in_process():
    import brish
    import brish.brishmod

    for mod in (brish, brish.brishmod):
        assert Path(mod.__file__).resolve().is_relative_to(ROOT), mod.__file__


def test_worktree_guard_child():
    #: The child prelude asserts the same thing before running the snippet.
    res = check("print(brish.__file__)")
    assert Path(res.out.strip()).resolve().is_relative_to(ROOT), res.out
