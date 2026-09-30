"""The readme examples tangled into `test_tangled1.py`, run in a child process
in the caller's real zsh environment (startup files loaded)."""

from tests.conftest import ROOT, check


def test_tangled1_real_env():
    path = ROOT / "tests" / "test_tangled1.py"
    check(
        f"""
        import runpy
        ns = runpy.run_path({str(path)!r})
        for name in ("test1", "test2", "test3"):
            ns[name]()
        brish.bsh.cleanup()
        """,
        real_env=True,
        timeout=120,
    )
