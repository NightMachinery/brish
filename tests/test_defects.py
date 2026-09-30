"""Characterization tests for the transport defects F3 to F11.

Each test asserts the correct behaviour. A test marked `xfail(strict=True)`
documents a defect that is still present in the mode under test; the marker is
removed by the commit that fixes it, so a fix that silently regresses fails
the suite again.

All worker traffic runs in a child process with a timeout (see conftest).
"""

import pytest

from tests.conftest import BINARY, check, run_py

#: Findings still open in this mode. Keyed by finding id.
OPEN = {
    "F3": "NUL framing: output can forge the terminator",
    "F4": "stdin travels as str(cmd_stdin) through a text FIFO",
    "F5": "bytes values are interpolated as ints or reprs",
    "F7": "zstring rewrites CR in the template's literal text",
}


def open_defect(fid):
    """xfail(strict) while finding `fid` is open in this mode."""
    return pytest.mark.xfail(fid in OPEN, reason=OPEN.get(fid, ""), strict=True)


@open_defect("F3")
def test_f3_output_cannot_forge_the_terminator():
    check(
        r'''
        b = Brish(server_count=1)
        cases = [
            ("$'a\\n\\0\\n5\\nabc'", "a\n\0\n5\nabc"),
            ("$'\\0'", "\0"),
            ("$'x\\n\\0'", "x\n\0"),
        ]
        for literal, want in cases:
            r = b.send_cmd("print -rn -- " + literal)
            assert (r.retcode, r.out) == (0, want), (literal, repr(r))
            r2 = b.send_cmd("print -r second; print -ru2 err2; (exit 4)")
            assert (r2.retcode, r2.out, r2.err) == (4, "second\n", "err2\n"), repr(r2)
        ''',
        timeout=20,
    )


@open_defect("F4")
def test_f4_stdin_carries_bytes():
    check(
        r'''
        b = Brish(server_count=1)
        r = b.send_cmd("cat", cmd_stdin=b"abc")
        assert (r.retcode, r.out) == (0, "abc"), repr(r)
        r = b.send_cmd("cat", cmd_stdin="a\0b")
        assert (r.retcode, r.out) == (0, "a\0b"), repr(r)
        ''',
        timeout=20,
    )


@open_defect("F5")
def test_f5_bytes_values_interpolate_exactly():
    check(
        r'''
        b = Brish(server_count=1)
        v = b"abc"
        r = b.z("print -rn -- {v}")
        assert r.out == "abc", repr(r)
        lst = [b"x y", "z"]
        r = b.z("print -rn -- {lst}")
        assert r.out == "x y z", repr(r)
        ''',
        timeout=20,
    )


@open_defect("F6")
def test_f6_quoting_is_not_injectable():
    check(
        r'''
        sentinel = os.path.join(SCRATCH, "injected")
        b = Brish(server_count=1)
        v = "\x1c'; print -rn INJECTED > " + sentinel + " ; #"
        r = b.z("print -rn -- {v}")
        assert not os.path.exists(sentinel), "quoted value was executed as code"
        assert r.out == v, repr(r)
        ''',
        timeout=20,
    )


@open_defect("F7")
def test_f7_zstring_keeps_cr():
    check(
        r'''
        b = Brish(delayed_init=True)
        assert "\r" in b.zstring("print -rn -- a\rb"), repr(b.zstring("print -rn -- a\rb"))
        ''',
        timeout=20,
    )


@open_defect("F8")
def test_f8_large_stderr_does_not_deadlock():
    check(
        r'''
        b = Brish(server_count=1)
        r = b.send_cmd("print -rn -- ${(l:9000::e:)} >&2; print ok")
        assert (r.retcode, r.out, len(r.err)) == (0, "ok\n", 9000), repr(r)
        r = b.send_cmd("print ok; print -rn -- ${(l:9000::e:)} >&2")
        assert (r.retcode, r.out, len(r.err)) == (0, "ok\n", 9000), repr(r)
        ''',
        timeout=20,
    )


@open_defect("F9")
def test_f9_worker_death_returns():
    check(
        r'''
        b = Brish(server_count=1)
        r = b.send_cmd("exit 3")
        assert r.retcode in (3, 9001), repr(r)
        r = b.send_cmd("echo ok")
        assert (r.retcode, r.out) == (0, "ok\n"), repr(r)
        r = b.send_cmd("print *.nonexistent_zzz")
        assert r.retcode != 0, repr(r)
        r = b.send_cmd("echo ok")
        assert (r.retcode, r.out) == (0, "ok\n"), repr(r)
        ''',
        timeout=30,
    )


@open_defect("F10")
def test_f10_interrupt_does_not_desynchronise():
    check(
        r'''
        b = Brish(server_count=1)
        b.send_cmd("true")
        threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGINT)).start()
        try:
            b.send_cmd("sleep 1; echo late")
            raise SystemExit("no KeyboardInterrupt")
        except KeyboardInterrupt:
            pass
        r = b.send_cmd("echo ok")
        assert (r.retcode, r.out) == (0, "ok\n"), repr(r)
        ''',
        timeout=30,
    )


@open_defect("F11")
def test_f11_delayed_init_keeps_encoding_across_restart():
    check(
        r'''
        b = Brish(delayed_init=True, encoding="latin-1")
        r = b.send_cmd("%BRISH_RESTART")
        r = b.send_cmd("print -rn -- $'\\xe9'")
        assert r.out == "\xe9", repr(r)
        ''',
        timeout=30,
    )


@open_defect("F11")
def test_f11_restart_keeps_encoding():
    check(
        r'''
        b = Brish(encoding="latin-1")
        b.restart()
        r = b.send_cmd("print -rn -- $'\\xe9'")
        assert r.out == "\xe9", repr(r)
        b.cleanup()
        ''',
        timeout=30,
    )


@open_defect("F11")
def test_f11_brish_restart_command_does_not_deadlock():
    #: One thread holds worker 0; a second queues `%BRISH_RESTART` on it; a
    #: third calls restart(). Today the second thread can win worker 0 and then
    #: wait for `self.lock`, which the third holds while it waits for worker 0.
    check(
        r'''
        b = Brish(server_count=2)
        for _ in range(6):
            ts = [
                threading.Thread(target=b.send_cmd, args=("sleep 0.3",), kwargs=dict(server_index=0)),
                threading.Thread(target=b.send_cmd, args=("%BRISH_RESTART",), kwargs=dict(server_index=0)),
            ]
            for t in ts:
                t.start()
                time.sleep(0.05)
            time.sleep(0.1)
            b.restart()
            for t in ts:
                t.join()
            r = b.send_cmd("echo ok")
            assert r.out == "ok\n", repr(r)
        b.cleanup()
        ''',
        timeout=40,
    )


@open_defect("F11")
def test_f11_concurrent_restart_never_leaks_uninitialized():
    check(
        r'''
        b = Brish(server_count=2)
        errors = []
        stop = time.time() + 4

        def sender(i):
            while time.time() < stop:
                try:
                    r = b.send_cmd("echo ok", server_index=i)
                    if r.out != "ok\n":
                        errors.append(("bad", r))
                except Exception as e:
                    errors.append(("exc", repr(e)))

        def restarter():
            while time.time() < stop:
                b.restart()

        ts = [threading.Thread(target=sender, args=(i,)) for i in (0, 1)]
        ts.append(threading.Thread(target=restarter))
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert not errors, errors[:3]
        b.cleanup()
        ''',
        timeout=40,
    )
