"""Worker-facing goals that hold in both modes: G6 (no deadlock), G7 (no hang
on worker death), G8 (interrupt safety) and G9 (restart under concurrency).

Binary-only refinements are asserted when BINARY is set.
"""

from tests.conftest import check

#: Child-side helpers, prepended to snippets that need them.
HELPERS = r'''
def worker_pid(b, i=0):
    r = b.send_cmd("zmodload zsh/system; print -rn -- $sysparams[pid]", server_index=i)
    assert r.retcode == 0, repr(r)
    return int(r.out)

def gone(pid, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.01)
    return False

def assert_ok(b, **kw):
    r = b.send_cmd("echo ok", **kw)
    assert (r.retcode, r.out, r.err) == (0, "ok\n", ""), repr(r)

def timed(f, bound):
    t = time.time()
    r = f()
    dt = time.time() - t
    assert dt < bound, (dt, repr(r))
    return r
'''


def wcheck(code, **kw):
    return check(code, setup=HELPERS, **kw)


def test_g6_no_deadlock():
    wcheck(
        r'''
        b = Brish(server_count=1)
        cases = [
            ("print -rn -- ${(l:9000::e:)} >&2; print -rn -- ${(l:9000::o:)}", 9000, 9000),
            ("print -rn -- ${(l:9000::o:)}; print -rn -- ${(l:9000::e:)} >&2", 9000, 9000),
            ("print -rn -- ${(l:200000::e:)} >&2; print -rn -- ${(l:200000::o:)}", 200000, 200000),
            ("print -rn -- ${(l:100000::x:)}", 100000, 0),
            ("print -rn -- ${(l:100000::x:)} >&2", 0, 100000),
            ("print -rn -- ${(l:300000::y:)} | cat", 300000, 0),
            ("{ print -rn -- ${(l:300000::y:)} >&2 } | cat", 0, 300000),
        ]
        for cmd, nout, nerr in cases:
            for fork in (False, True):
                r = timed(lambda: b.send_cmd(cmd, fork=fork), 10)
                assert (r.retcode, len(r.out), len(r.err)) == (0, nout, nerr), (cmd, fork, r.retcode, len(r.out), len(r.err))
        assert_ok(b)
        b.cleanup()
        ''',
        timeout=60,
    )


def test_g7_worker_death_returns_a_result():
    wcheck(
        r'''
        b = Brish(server_count=1)
        cases = [
            ("print *.nonexistent_zzz", None),
            ("exit 3", 3),
            ("setopt errexit; false", None),
            ("break", 0),
            ("x=; : ${x:?}", None),
        ]
        for cmd, want in cases:
            r = timed(lambda: b.send_cmd(cmd), 10)
            if BINARY and want is not None:
                assert r.retcode == want, (cmd, repr(r))
            elif want == 0:
                assert r.retcode in (0, 9001), (cmd, repr(r))
            else:
                assert r.retcode not in (0,), (cmd, repr(r))
                if want is not None:
                    assert r.retcode in (want, 9001), (cmd, repr(r))
            if r.retcode == 9001:
                assert r.err.endswith("brish: worker died during this command\n"), repr(r)
            assert_ok(b)
        b.cleanup()
        ''',
        timeout=90,
    )


def test_g7_nomatch_reports_the_error():
    wcheck(
        r'''
        b = Brish(server_count=1)
        r = timed(lambda: b.send_cmd("print *.nonexistent_zzz"), 10)
        assert "no matches found" in r.err, repr(r)
        if BINARY:
            assert r.retcode == 1, repr(r)
        assert_ok(b)
        b.cleanup()
        ''',
        timeout=60,
    )


def test_g7_syntax_error_does_not_rerun_the_previous_command():
    wcheck(
        r'''
        b = Brish(server_count=1)
        r = b.send_cmd("print -r first")
        assert r.out == "first\n", repr(r)
        r = timed(lambda: b.send_cmd("fi"), 10)
        assert "parse error" in r.err, repr(r)
        if BINARY:
            #: Legacy mode re-runs the previous command here with status 0
            #: (brish2.zsh calls the old function after the failed definition).
            assert (r.retcode, r.out) == (1, ""), repr(r)
        r = b.send_cmd("echo x")
        assert (r.retcode, r.out) == (0, "x\n"), repr(r)
        b.cleanup()
        ''',
        timeout=60,
    )


def test_g7_kill_during_a_command():
    wcheck(
        r'''
        b = Brish(server_count=1)
        pid = worker_pid(b)
        threading.Timer(0.5, lambda: os.kill(pid, signal.SIGKILL)).start()
        r = timed(lambda: b.send_cmd("print -r started; repeat 200 { sleep 0.05 }; print -r finished"), 15)
        assert r.retcode == 9001, repr(r)
        assert r.out == "started\n", repr(r)
        assert r.err.endswith("brish: worker died during this command\n"), repr(r)
        assert_ok(b)
        assert worker_pid(b) != pid
        b.cleanup()
        ''',
        timeout=60,
    )


def test_g7_kill_between_requests_is_invisible():
    wcheck(
        r'''
        b = Brish(server_count=1)
        pid = worker_pid(b)
        os.kill(pid, signal.SIGKILL)
        assert gone(pid)
        timed(lambda: assert_ok(b), 15)
        assert worker_pid(b) != pid
        b.cleanup()
        ''',
        timeout=60,
    )


def test_g7_unusable_shell_raises():
    wcheck(
        r'''
        try:
            Brish(shell=["/usr/bin/false"])
            raise SystemExit("no exception from the constructor")
        except bm.BrishWorkerDiedException:
            pass
        b = Brish(delayed_init=True, shell=["/usr/bin/false"])
        for _ in range(2):
            try:
                timed(lambda: b.send_cmd("true"), 35)
                raise SystemExit("no exception from send_cmd")
            except bm.BrishWorkerDiedException:
                pass
        ''',
        timeout=90,
    )


def test_g8_sigint_during_a_command():
    wcheck(
        r'''
        b = Brish(server_count=1)
        assert_ok(b)
        threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGINT)).start()
        try:
            b.send_cmd("sleep 2; echo late")
            raise SystemExit("no KeyboardInterrupt")
        except KeyboardInterrupt:
            pass
        timed(lambda: assert_ok(b), 10)
        threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGINT)).start()
        try:
            b.send_cmd("sleep 2; echo late")
            raise SystemExit("no KeyboardInterrupt")
        except KeyboardInterrupt:
            pass
        timed(b.restart, 5)
        assert_ok(b)
        b.cleanup()
        time.sleep(2)  # let the abandoned `sleep 2` finish, so it is no orphan
        ''',
        timeout=60,
    )


def test_g8_interrupt_during_a_large_stdin_write():
    #: The payload is lines of an inert command that would create a sentinel
    #: file if a worker ever parsed stdin as code.
    wcheck(
        r'''
        sentinel = os.path.join(SCRATCH, "stdin-ran-as-code")
        line = "print -rn INJECTED >> " + sentinel + "\n"
        payload = line * (8_000_000 // len(line))
        b = Brish(server_count=1)
        def boom(*a):
            raise KeyboardInterrupt
        signal.signal(signal.SIGALRM, boom)
        interrupted = 0
        for delay in (0.001, 0.005, 0.02, 0.05):
            signal.setitimer(signal.ITIMER_REAL, delay)
            try:
                b.send_cmd("wc -c", cmd_stdin=payload)
            except KeyboardInterrupt:
                interrupted += 1
            signal.setitimer(signal.ITIMER_REAL, 0)
            r = b.send_cmd("echo ok")
            assert (r.retcode, r.out) == (0, "ok\n"), repr(r)
            r = b.send_cmd("wc -c", cmd_stdin="abc")
            assert r.out.strip() == "3", repr(r)
        assert interrupted, "the timer never interrupted a write"
        assert not os.path.exists(sentinel), "stdin was executed as code"
        b.cleanup()
        ''',
        timeout=120,
    )


def test_g9_restart_under_concurrency():
    wcheck(
        r'''
        b = Brish(server_count=3)
        pids = [worker_pid(b, i) for i in range(3)]
        errors, results = [], []
        stop = time.time() + 3

        def sender(i):
            while time.time() < stop:
                try:
                    r = b.send_cmd("echo ok", server_index=i)
                    results.append(r.retcode)
                    if r.retcode not in (0, 9001) or (r.retcode == 0 and r.out != "ok\n"):
                        errors.append(repr(r))
                except Exception as e:
                    errors.append(repr(e))

        ts = [threading.Thread(target=sender, args=(i,)) for i in range(3)]
        for t in ts:
            t.start()
        time.sleep(0.5)
        os.kill(pids[0], signal.SIGKILL)
        os.kill(pids[1], signal.SIGKILL)
        for t in ts:
            t.join(30)
            assert not t.is_alive(), "a sender thread hung"
        assert not errors, errors[:3]
        assert results.count(0) > 10, results[:20]
        new = [worker_pid(b, i) for i in range(3)]
        assert not set(new) & set(pids[:2]), (pids, new)
        b.cleanup()
        ''',
        timeout=90,
    )


def test_g9_delayed_init_keeps_encoding():
    wcheck(
        r'''
        b = Brish(delayed_init=True, encoding="latin-1")
        for cmd in ("true", "%BRISH_RESTART", "true"):
            b.send_cmd(cmd)
            r = b.send_cmd("print -rn -- $'\\xe9'")
            assert r.out == "\xe9", repr(r)
        b.restart()
        r = b.send_cmd("print -rn -- $'\\xe9'")
        assert r.out == "\xe9", repr(r)
        b.cleanup()
        ''',
        timeout=60,
    )


def test_real_environment_smoke():
    #: The user's startup files load (slower, and `pipefail` may be on).
    wcheck(
        r'''
        b = Brish(server_count=2)
        assert_ok(b)
        r = b.send_cmd("true", cmd_stdin="x" * 1_000_000)
        assert r.retcode == 0, repr(r)
        r = b.send_cmd("print -rn -- ${(l:20000::e:)} >&2; print ok")
        assert (r.retcode, r.out, len(r.err)) == (0, "ok\n", 20000), repr(r)
        r = b.send_cmd("exit 4")
        assert r.retcode in (4, 9001), repr(r)
        assert_ok(b)
        b.cleanup()
        ''',
        real_env=True,
        timeout=120,
    )
