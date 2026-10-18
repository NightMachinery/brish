"""brish.zn runs its command only when night.sh is installed (NIGHTDIR is set)."""

from tests.conftest import check


def test_zn_without_night_runs_nothing():
    check(
        r'''
        import brish
        r = brish.zn("print -r -- hi")
        assert (r.retcode, r.out, r.err) == (127, "", "night.sh not found"), repr(r)
        #: The default instance never started its workers.
        assert brish.bsh._slots is None
        ''',
        env={"NIGHTDIR": None},
        timeout=30,
    )


def test_zn_with_night_runs():
    check(
        r'''
        import brish
        r = brish.zn("print -r -- hi")
        assert (r.retcode, r.outrs) == (0, "hi"), repr(r)
        brish.bsh.cleanup()
        ''',
        env={"NIGHTDIR": "/nonexistent-night"},
        timeout=60,
    )
