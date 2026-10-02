"""Brish.popen: output that streams while the command runs, and kill().

Every test runs in both modes. Timing assertions use wide margins: they tell
"arrives while the command runs" from "arrives when it ends".
"""

from tests.conftest import BINARY, check, legacy_only

HELPERS = r'''
from brish.brishmod import BrishPopen, BrishWorkerDiedException
def collect(p, t0=None):
    """[(seconds since t0, stream, chunk)] until the end."""
    t0 = time.monotonic() if t0 is None else t0
    evs = []
    for stream, chunk in p:
        assert isinstance(chunk, bytes) and chunk, (stream, chunk)
        assert stream in ("out", "err"), stream
        evs.append((time.monotonic() - t0, stream, chunk))
    return evs
def joined(evs, stream="out"):
    return b"".join(c for _, s, c in evs if s == stream)
def first_time(evs, needle, stream="out"):
    """When the joined stream first contained `needle`."""
    acc = b""
    for t, s, c in evs:
        if s == stream:
            acc += c
            if needle in acc:
                return t
    raise AssertionError((needle, evs))
def kill_later(p, delay):
    t = threading.Timer(delay, p.kill)
    t.start()
    return t
def same_server_ok(b, i, want_v="kept"):
    r = b.send_cmd("print -r -- ok-$v", server_index=i)
    assert (r.retcode, r.out, r.err) == (0, f"ok-{want_v}\n", ""), repr(r)
'''


def run(code, timeout=60, **kw):
    return check(code, setup=HELPERS, timeout=timeout, **kw)


def test_lines_arrive_while_the_command_runs():
    run(
        r'''
        b = Brish(server_count=1)
        with b.popen("for i in 1 2 3 4 5; do print -r line$i; sleep 0.25; done; return 4") as p:
            assert p.retcode is None
            evs = collect(p)
        assert p.retcode == 4, p.retcode
        assert joined(evs) == b"".join(b"line%d\n" % i for i in range(1, 6)), evs
        ts = [first_time(evs, b"line%d" % i) for i in range(1, 6)]
        assert ts[0] < 0.2, ts
        for a, c in zip(ts, ts[1:]):
            assert c - a > 0.12, ts
        assert ts[-1] - ts[0] > 0.8, ts
        b.cleanup()
        '''
    )


def test_progress_bar_and_partial_lines():
    run(
        r'''
        b = Brish(server_count=1)
        cmd = "for i in 1 2 3; do printf '\\r%d%%' $((i * 33)); sleep 0.3; done; printf 'part'; sleep 0.3; printf 'ial\\n'"
        with b.popen(cmd) as p:
            evs = collect(p)
        assert p.retcode == 0
        want = b"\r33%\r66%\r99%partial\n"
        assert joined(evs) == want, evs
        ts = [first_time(evs, x) for x in (b"\r33%", b"\r66%", b"\r99%", b"part", b"partial")]
        for a, c in zip(ts, ts[1:]):
            assert c - a > 0.15, (ts, evs)
        assert b.send_cmd(cmd).outb == want
        b.cleanup()
        '''
    )


def test_stderr_separately_and_merged():
    run(
        r'''
        b = Brish(server_count=1)
        with b.popen("print -r o1; print -ru2 e1; sleep 0.3; print -r o2; print -ru2 e2; return 2") as p:
            evs = collect(p)
        assert p.retcode == 2
        assert joined(evs, "out") == b"o1\no2\n", evs
        assert joined(evs, "err") == b"e1\ne2\n", evs
        assert first_time(evs, b"e2", "err") - first_time(evs, b"e1", "err") > 0.15, evs
        #: Merged in the shell: one stream, in order.
        cmd = "{ print -r a; print -ru2 b; sleep 0.2; print -r c; print -ru2 d } 2>&1"
        with b.popen(cmd) as p:
            evs = collect(p)
        assert {s for _, s, _ in evs} == {"out"}, evs
        assert joined(evs) == b"a\nb\nc\nd\n", evs
        b.cleanup()
        '''
    )


def test_nul_in_the_output():
    run(
        r'''
        b = Brish(server_count=1)
        cmd = "printf 'x\\0'; sleep 0.4; printf 'y\\0BR'; sleep 0.4; printf 'z\\n'"
        with b.popen(cmd) as p:
            evs = collect(p)
        assert p.retcode == 0
        want = b"x\0y\0BRz\n"
        if BINARY:
            assert joined(evs) == want, evs
            #: "x" comes at once; a NUL is held only while it could start the
            #: END marker ("\0" and "\0BR" could, "\0y" cannot).
            tx, ty, tz = (first_time(evs, s) for s in (b"x", b"x\0y", b"z"))
            assert ty - tx > 0.25 and tz - ty > 0.25, evs
            assert first_time(evs, b"x\0y\0BR") >= tz - 0.05, evs
        else:
            #: Legacy holds back only a trailing newline (or newline + NUL).
            assert joined(evs) == want, evs
            tx, ty, tz = (first_time(evs, s) for s in (b"x\0", b"y\0BR", b"z"))
            assert ty - tx > 0.25 and tz - ty > 0.25, evs
        b.cleanup()
        '''
    )


def test_buffer_gives_the_send_cmd_result():
    run(
        r'''
        b = Brish(server_count=1)
        cases = [
            ("print -r out; print -ru2 err; return 3", ""),
            ("printf 'a\\r\\nb'", ""),
            ("cat", "some\nstdin"),
            ("print -rn -- ${(l:300000::o:)}", ""),
            ("print -r -- caf\xe9 \U0001f40d", ""),
        ]
        if BINARY:
            cases.append(("printf 'a\\0b\\0BRISH3-END:'", ""))
        for cmd, stdin in cases:
            want = b.send_cmd(cmd, cmd_stdin=stdin)
            p = b.popen(cmd, cmd_stdin=stdin, buffer=True)
            assert p.wait() == want.retcode
            r = p.result
            assert (r.retcode, r.outb, r.errb, r.out, r.err) == (
                want.retcode, want.outb, want.errb, want.out, want.err), (cmd, r, want)
            assert (r.cmd, r.cmd_stdin) == (cmd, stdin)
        p = b.popen("sleep 0.3; print -r late", buffer=True)
        assert (p.retcode, p.result) == (None, None)  # not ended yet
        assert p.wait() == 0 and p.result.out == "late\n"
        p = b.popen("true")
        p.wait()
        try:
            p.result
            raise SystemExit("result without buffer=True")
        except ValueError:
            pass
        b.cleanup()
        '''
    )


def test_fork_stdin_and_zpopen():
    run(
        r'''
        b = Brish(server_count=1)
        b.send_cmd("v=kept")
        with b.popen("v=changed; cat; print -r -- $v", cmd_stdin="in\n", fork=True) as p:
            evs = collect(p)
        assert joined(evs) == b"in\nchanged\n", evs
        same_server_ok(b, 0)
        name = "a b'c"
        with b.zpopen("print -r -- {name}") as p:
            assert joined(collect(p)) == b"a b'c\n"
        if not BINARY:
            p = b.popen("print a\0b")
            assert p.wait() == 9000 and p.retcode == 9000
        b.cleanup()
        '''
    )


def test_kill_an_external_command():
    run(
        r'''
        b = Brish(server_count=2)
        b.send_cmd("v=kept", server_index=1)
        t0 = time.monotonic()
        with b.popen("print -r before; sleep 100; print -r after", server_index=1) as p:
            kill_later(p, 0.4)
            evs = collect(p, t0)
        dt = time.monotonic() - t0
        assert p.retcode == 130, (p.retcode, evs)
        assert joined(evs) == b"before\n", evs
        assert dt < 1.9, dt  # before the SIGTERM step
        same_server_ok(b, 1)
        p.kill()  # idempotent, and harmless once the command has ended
        same_server_ok(b, 1)
        b.cleanup()
        '''
    )


def test_kill_an_in_shell_loop_and_a_fork():
    run(
        r'''
        b = Brish(server_count=1)
        b.send_cmd("v=kept")
        for cmd, fork in [
            ("print -r x; while :; do :; done", False),
            ("print -r x; f() { while :; do while :; do :; done; done }; f", False),
            ("print -r x; while :; do :; done", True),
            ("print -r x; cat", False),  # reads its (empty) stdin, then ends
            ("emulate sh; print -r x; while :; do :; done", True),
        ]:
            t0 = time.monotonic()
            with b.popen(cmd, fork=fork) as p:
                kill_later(p, 0.3)
                evs = collect(p, t0)
            dt = time.monotonic() - t0
            assert joined(evs) == b"x\n", (cmd, evs)
            if cmd.endswith("cat"):
                assert p.retcode == 0, (cmd, p.retcode)
            else:
                assert p.retcode == 130, (cmd, fork, p.retcode)
                assert dt < 1.9, (cmd, dt)
            same_server_ok(b, 0)
        b.cleanup()
        '''
    )


def test_kill_under_errexit_and_sh_emulation():
    #: zsh exits instead of unwinding when an interrupt meets err_exit (or
    #: err_return at the legacy worker's top level), or a special builtin
    #: under posix_builtins (`emulate sh`). The worker's trap turns those off,
    #: so the worker survives with its state; options that the command set
    #: globally stay as it left them.
    run(
        r'''
        b = Brish(server_count=1)
        opts = "${options[errexit]} ${options[errreturn]} ${options[posixbuiltins]} ${options[ksharrays]}"
        cases = [
            ("set -e; print -r x; sleep 100", "off off off off"),
            ("set -euo pipefail; print -r x; sleep 100", "off off off off"),
            ("setopt err_return; print -r x; sleep 100", "off off off off"),
            ("set -e; print -r x; while :; do :; done", "off off off off"),
            ("f() { emulate -L zsh; setopt err_exit; print -r x; sleep 100 }; f", "off off off off"),
            ("f() { emulate -L sh; print -r x; while :; do :; done }; f", "off off off off"),
            ("emulate sh; print -r x; eval 'while :; do :; done'", "off off on on"),
        ]
        #: The sh cases die only when the signal lands inside a special
        #: builtin, so they run several times.
        cases += [("emulate sh; set -e; print -r x; while :; do :; done", "off off on on")] * 4
        for cmd, want_opts in cases:
            b.send_cmd("emulate zsh; v=kept")
            with b.popen(cmd) as p:
                kill_later(p, 0.3)
                evs = collect(p)
            assert (p.retcode, joined(evs), joined(evs, "err")) == (130, b"x\n", b""), (cmd, p.retcode, evs)
            r = b.send_cmd("print -r -- ok-$v " + opts)
            assert r.out == f"ok-kept {want_opts}\n", (cmd, r)
        b.send_cmd("emulate zsh")
        #: A terminal-style Ctrl-C (the whole process group) during send_cmd.
        signal.signal(signal.SIGINT, lambda *a: None)
        got = {}
        t = threading.Thread(target=lambda: got.update(r=b.send_cmd("set -e; print -r x; sleep 100; print -r after")))
        t.start()
        time.sleep(0.5)
        os.killpg(os.getpgrp(), signal.SIGINT)
        t.join(10)
        assert (got["r"].retcode, got["r"].out) == (130, "x\n"), got
        same_server_ok(b, 0)
        b.cleanup()
        ''',
        timeout=120,
    )


def test_kill_with_a_user_int_trap():
    run(
        r'''
        b = Brish(server_count=1)
        b.send_cmd("v=kept")
        for fork in (False, True):
            with b.popen("trap 'print -r caught' INT; sleep 100; print -r after", fork=fork) as p:
                kill_later(p, 0.3)
                evs = collect(p)
            assert (p.retcode, joined(evs)) == (0, b"caught\nafter\n"), (fork, p.retcode, evs)
            same_server_ok(b, 0)
        #: The command's trap ended with the command.
        with b.popen("sleep 100") as p:
            kill_later(p, 0.3)
            collect(p)
        assert p.retcode == 130, p.retcode
        same_server_ok(b, 0)
        b.cleanup()
        '''
    )


def test_escalation_stops_at_the_descendants():
    #: A fork that ignores INT and TERM is SIGKILLed; the worker survives.
    run(
        r'''
        b = Brish(server_count=1)
        b.send_cmd("v=kept")
        t0 = time.monotonic()
        with b.popen("trap '' INT TERM; print -r stuck; while :; do :; done", fork=True) as p:
            p.kill_grace = 0.5
            kill_later(p, 0.3)
            evs = collect(p, t0)
        dt = time.monotonic() - t0
        assert p.retcode == 137, (p.retcode, evs)
        assert joined(evs) == b"stuck\n" and not joined(evs, "err"), evs
        assert 1.0 < dt < 4, dt
        same_server_ok(b, 0)
        #: A non-fork command whose child ignores them: the worker's own
        #: interrupt takes effect once the child is gone.
        with b.popen("print -r go; zsh -fc \"trap '' INT TERM; sleep 100\"") as p:
            p.kill_grace = 0.5
            kill_later(p, 0.3)
            evs = collect(p)
        assert p.retcode == 130, (p.retcode, evs)
        same_server_ok(b, 0)
        b.cleanup()
        '''
    )


def test_escalation_to_the_worker():
    #: The worker itself ignores INT and TERM: it is SIGKILLed, the retcode is
    #: 9001 with the usual note, and the instance restarts before its next use.
    run(
        r'''
        b = Brish(server_count=2)
        b.send_cmd("v=kept", server_index=0)
        t0 = time.monotonic()
        with b.popen("trap '' INT TERM; print -r stuck; while :; do :; done", server_index=0) as p:
            p.kill_grace = 0.5
            kill_later(p, 0.3)
            evs = collect(p, t0)
        dt = time.monotonic() - t0
        assert p.retcode == 9001, (p.retcode, evs)
        assert joined(evs) == b"stuck\n", evs
        assert joined(evs, "err").endswith(bm.WORKER_DIED_NOTE.encode() + b"\n"), evs
        assert 0.8 < dt < 4, dt
        r = b.send_cmd("print -r -- next-$v", server_index=0)
        assert (r.retcode, r.out) == (0, "next-\n"), repr(r)  # restarted
        b.cleanup()
        ''',
        allow_orphans=False,
    )


def test_leaving_early_kills_and_frees_the_worker():
    run(
        r'''
        b = Brish(server_count=1)
        b.send_cmd("v=kept")
        #: break
        t0 = time.monotonic()
        with b.popen("yes") as p:
            n = 0
            for s, c in p:
                n += len(c)
                if n > 1_000_000:
                    break
        assert p.retcode == 130, p.retcode
        assert time.monotonic() - t0 < 3
        same_server_ok(b, 0)
        #: break without a with block
        p = b.popen("print -r a; sleep 100")
        for s, c in p:
            break
        assert p.retcode == 130, p.retcode
        same_server_ok(b, 0)
        #: an exception inside the with block
        try:
            with b.popen("print -r a; sleep 100") as p:
                for s, c in p:
                    raise KeyError("boom")
        except KeyError:
            pass
        assert p.retcode == 130, p.retcode
        same_server_ok(b, 0)
        #: leaving before reading anything
        with b.popen("sleep 100") as p:
            pass
        assert p.retcode == 130, p.retcode
        same_server_ok(b, 0)
        #: wait() without kill reads to the end
        p = b.popen("print -r a; sleep 0.2; return 6")
        assert p.wait() == 6 and p.retcode == 6
        p.close()
        same_server_ok(b, 0)
        b.cleanup()
        '''
    )


def test_reads_belong_to_the_creating_thread():
    run(
        r'''
        b = Brish(server_count=2)
        p = b.popen("print -r a; sleep 100", server_index=0)
        err = []
        def other():
            try:
                next(iter(p))
            except RuntimeError as e:
                err.append(e)
            p.kill()  # allowed from any thread
        t = threading.Thread(target=other)
        t.start()
        t.join(10)
        assert err and "thread" in str(err[0]), err
        assert p.wait() == 130
        r = b.send_cmd("echo ok", server_index=0)
        assert r.out == "ok\n", repr(r)
        b.cleanup()
        '''
    )


def test_a_command_that_exits_the_worker():
    run(
        r'''
        b = Brish(server_count=1)
        b.send_cmd("v=kept")
        with b.popen("print -r bye; exit 3") as p:
            evs = collect(p)
        assert (p.retcode, joined(evs), joined(evs, "err")) == (3, b"bye\n", b""), (p.retcode, evs)
        r = b.send_cmd("print -r -- next-$v")
        assert (r.retcode, r.out) == (0, "next-\n"), repr(r)  # restarted
        b.cleanup()
        '''
    )


def test_the_bot_pattern_under_concurrency():
    #: The first consumer: a shared legacy-or-binary instance, commands run
    #: from executor threads under acquire_lock, killed from another thread,
    #: while other threads use send_cmd on the other workers.
    run(
        r'''
        import faulthandler; faulthandler.dump_traceback_later(100, exit=True)
        b = Brish(server_count=4)
        errors = []
        stop = time.time() + 8

        def bot(k):
            n = 0
            while time.time() < stop:
                n += 1
                cwd = os.path.join(SCRATCH, f"jd{k}")
                os.makedirs(cwd, exist_ok=True)
                fork = n % 2 == 0
                kill = n % 3 == 0
                cmd = "pwd; print -r -- arg; print -ru2 err\n" + ("sleep 100" if kill else "print -r done; return 5")
                lock, server_index = b.acquire_lock(server_index=None, lock_sleep=1)
                try:
                    b.z("typeset -g jd={cwd}", server_index=server_index)
                    b.send_cmd('cd "$jd"', server_index=server_index)
                    with b.popen('{ eval "$(< /dev/stdin)" } 2>&1', fork=fork,
                                 cmd_stdin=cmd, server_index=server_index) as p:
                        if kill:
                            kill_later(p, 0.3)
                        evs = collect(p)
                    rc = p.retcode
                    b.z("cd /tmp", server_index=server_index)
                    pwd = b.send_cmd("pwd", server_index=server_index).out
                finally:
                    lock.release()
                out = joined(evs)
                want = os.path.realpath(cwd).encode() + b"\narg\nerr\n"
                ok = {s for _, s, _ in evs} <= {"out"} and os.path.realpath(pwd.strip()) == os.path.realpath("/tmp")
                if kill:
                    ok = ok and rc == 130 and os.path.realpath(out.split(b"\n")[0]) == want.split(b"\n")[0]
                else:
                    ok = ok and rc == 5 and out.endswith(b"\narg\nerr\ndone\n")
                if not ok:
                    errors.append((k, n, fork, kill, rc, evs, pwd))

        def other(k):
            n = 0
            while time.time() < stop:
                n += 1
                r = b.send_cmd(f"print -r -- other-{k}-{n}; print -ru2 e", cmd_stdin="x")
                if (r.retcode, r.out, r.err) != (0, f"other-{k}-{n}\n", "e\n"):
                    errors.append(("other", k, n, r))

        ts = [threading.Thread(target=bot, args=(k,)) for k in range(2)]
        ts += [threading.Thread(target=other, args=(k,)) for k in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)
            assert not t.is_alive(), "a thread hung"
        assert not errors, errors[:3]
        b.cleanup()
        ''',
        timeout=120,
    )


def test_a_slow_reader_keeps_memory_flat():
    #: 200 MB through a reader that pauses: the command blocks instead of
    #: Python buffering what it has not read.
    run(
        r'''
        import resource
        def rss_mb():
            r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return r / (1 << 20) if sys.platform == "darwin" else r / 1024
        b = Brish(server_count=1)
        with b.popen("print -r warm") as p:
            collect(p)
        base = rss_mb()
        total = 200_000_000
        n = 0
        t0 = time.monotonic()
        with b.popen(f"yes abcdefghijklmnop | head -c {total}") as p:
            for i, (s, c) in enumerate(p):
                n += len(c)
                if i % 64 == 0:
                    time.sleep(0.01)
                if i == 100:
                    time.sleep(1)  # a long pause: the command must wait
        dt = time.monotonic() - t0
        grew = rss_mb() - base
        print(f"[measure] 200 MB slow reader: {dt:.2f}s, peak RSS grew {grew:.1f} MB", file=sys.stderr)
        assert (p.retcode, n) == (0, total), (p.retcode, n)
        assert grew < 40, grew
        b.cleanup()
        ''',
        timeout=240,
    )


@legacy_only
def test_legacy_learns_each_worker_pid_once():
    run(
        r'''
        b = Brish(server_count=2)
        assert b.p.legacy_pids == [None, None]
        with b.popen("print -r a", server_index=1) as p:
            collect(p)
        pids = list(b.p.legacy_pids)
        assert pids[0] is None and pids[1] > 1, pids
        r = b.send_cmd("zmodload zsh/system; print -r -- $sysparams[pid]", server_index=1)
        assert int(r.out) == pids[1], (r, pids)
        with b.popen("print -r b", server_index=1) as p:
            collect(p)
        assert b.p.legacy_pids == pids
        b.cleanup()
        '''
    )


@legacy_only
def test_legacy_pid_survives_background_output():
    #: A background job of an earlier command writes into every later reply,
    #: also into the internal PID request's; kill() must still find the PID.
    run(
        r'''
        b = Brish(server_count=1)
        import re
        r = b.send_cmd("v=kept; { repeat 3000 { print -r tick; sleep 0.005 } } &!; print -r -- job:$!")
        job = int(re.search(r"job:(\d+)", r.out).group(1))
        try:
            time.sleep(0.2)
            t0 = time.monotonic()
            with b.popen("sleep 100") as p:
                kill_later(p, 0.3)
                evs = collect(p)
            dt = time.monotonic() - t0
            assert b.p.legacy_pids[0] and b.p.legacy_pids[0] != job, b.p.legacy_pids
            #: The first SIGINT did it (without the PID, kill() signalled
            #: nothing and ended in 9001 after three graces).
            assert (p.retcode, p._stage) == (130, 1), (p.retcode, p._stage, dt)
            assert "ok-kept" in b.send_cmd("print -r -- ok-$v").out.split()
        finally:
            os.kill(job, signal.SIGKILL)
        b.cleanup()
        '''
    )


@legacy_only
def test_legacy_interrupt_while_learning_the_pid():
    #: An interrupt during the first popen's internal PID request (injected
    #: here; a Ctrl-C in the main thread in real life) must not leak the
    #: worker lock, and a reply it leaves unread must never be read as the
    #: answer to a later request, also not by a thread that holds the lock.
    run(
        r'''
        b = Brish(server_count=2)
        def lock_free(i):
            got = []
            def other():
                ok = b.locks[i].acquire(timeout=2)
                if ok:
                    b.locks[i].release()
                got.append(ok)
            t = threading.Thread(target=other)
            t.start()
            t.join()
            return got == [True]
        b.send_cmd("v=kept", server_index=0)
        #: 1. After the reply was read (in the ps call that checks the PID).
        real_child_pids = bm._child_pids
        def in_ps(pid):
            raise KeyboardInterrupt("in ps")
        bm._child_pids = in_ps
        try:
            b.popen("print -r x", server_index=0)
            raise SystemExit("no KeyboardInterrupt")
        except KeyboardInterrupt:
            pass
        finally:
            bm._child_pids = real_child_pids
        assert lock_free(0) and not b._holds_worker_lock(), b.locks
        assert b.p.free_server_count == 2, b.p.free_server_count
        #: The reply was complete: the worker is in sync and keeps its state.
        assert b.send_cmd("print -r -- ok-$v", server_index=0).out == "ok-kept\n"
        #: 2. While the PID reply is read, under the caller's own lock.
        real_read = bm._legacy_read_reply
        def in_read(f, with_rc=False):
            if with_rc:
                raise KeyboardInterrupt("in read")
            return real_read(f, with_rc)
        lock, _ = b.acquire_lock(server_index=0)
        try:
            bm._legacy_read_reply = in_read
            try:
                b.popen("print -r y", server_index=0)
                raise SystemExit("no KeyboardInterrupt")
            except KeyboardInterrupt:
                pass
            finally:
                bm._legacy_read_reply = real_read
            try:
                r = b.send_cmd("print -r z", server_index=0)
                raise SystemExit(f"the out-of-sync worker answered: {r!r}")
            except bm.BrishWorkerDiedException as e:
                assert "abandoned" in str(e), e
        finally:
            lock.release()
        assert lock_free(0) and not b._holds_worker_lock(), b.locks
        r = b.send_cmd("print -r -- ok-${v-unset}", server_index=0)
        assert r.out == "ok-unset\n", repr(r)  # the instance restarted
        b.cleanup()
        '''
    )


def test_same_thread_calls_skip_a_streaming_worker():
    #: Inside the loop over a BrishPopen, the worker it streams from belongs
    #: to the same thread (an RLock), so a call that took it would write into
    #: a busy worker. Calls skip it, or refuse at once, and nothing restarts.
    run(
        r'''
        Busy = bm.BrishWorkerBusyException
        assert issubclass(Busy, RuntimeError)
        def refused(call):
            t0 = time.monotonic()
            try:
                call()
            except Busy:
                assert time.monotonic() - t0 < 1, "not at once"
                return True
            return False
        b = Brish(server_count=2)
        for i in (0, 1):
            b.send_cmd(f"v=kept{i}", server_index=i)
        got = []
        with b.popen("print -r a; sleep 0.6; print -r b; return 7", server_index=0) as p:
            for s, c in p:
                got.append(c)
                if len(got) > 1:
                    continue
                r = b.send_cmd("print -r -- inner-$v")
                assert (r.retcode, r.out) == (0, "inner-kept1\n"), repr(r)
                with b.popen("print -r -- nested-$v; sleep 0.3; print -r end") as q:
                    assert q.server_index == 1, q.server_index
                    first = True
                    for _, c2 in q:
                        if first:
                            first = False
                            #: Both workers are this thread's now.
                            assert refused(lambda: b.send_cmd("true"))
                            assert refused(lambda: b.popen("true"))
                assert q.retcode == 0
                assert refused(lambda: b.send_cmd("true", server_index=0))
                assert refused(lambda: b.popen("true", server_index=0))
                assert refused(lambda: b.acquire_lock(server_index=0))
        assert (p.retcode, b"".join(got)) == (7, b"a\nb\n"), (p.retcode, got)
        #: Nothing was restarted, and both workers take commands again.
        same_server_ok(b, 0, "kept0")
        same_server_ok(b, 1, "kept1")
        #: Under the caller's own lock, as in the readme.
        lock, i = b.acquire_lock()
        try:
            with b.popen("print -r x; sleep 0.3", server_index=i) as p:
                for _ in p:
                    assert refused(lambda: b.send_cmd("true", server_index=i))
            same_server_ok(b, i, f"kept{i}")
        finally:
            lock.release()
        b.cleanup()
        #: One worker: refused at once instead of a deadlock or a restart.
        b = Brish(server_count=1)
        b.send_cmd("v=kept")
        with b.popen("print -r x; sleep 0.3; print -r y") as p:
            for _ in p:
                assert refused(lambda: b.send_cmd("true"))
                assert refused(lambda: b.popen("true"))
        assert p.retcode == 0
        same_server_ok(b, 0)
        b.cleanup()
        '''
    )


def test_kill_goes_by_the_command_not_the_reader():
    #: A reader that takes long over each chunk (a chat bot that edits a
    #: message per chunk, say) must not make kill() escalate past a command
    #: that ended at the first SIGINT, and must not keep one that ignores the
    #: signals from being stopped.
    run(
        r'''
        b = Brish(server_count=1)
        b.send_cmd("v=kept")
        for cmd in ("integer i; while :; do print -r -- line $((i++)); done", "yes"):
            t0 = time.monotonic()
            with b.popen(cmd) as p:
                p.kill_grace = 0.5
                kill_later(p, 0.3)
                n = 0
                for s, c in p:
                    n += 1
                    if time.monotonic() - t0 > 0.3:
                        time.sleep(0.2)
            dt = time.monotonic() - t0
            #: Before, the slow reader made each step look ignored: stage 4,
            #: and in legacy mode a SIGKILLed worker.
            assert (p.retcode, p._stage) == (130, 1), (cmd, p.retcode, p._stage, n, dt)
            same_server_ok(b, 0)
        #: Ignores INT and TERM and floods: still stopped, with a slow reader.
        t0 = time.monotonic()
        with b.popen("trap '' INT TERM; while :; do print -r -- 0123456789abcdef; done") as p:
            p.kill_grace = 0.5
            kill_later(p, 0.3)
            evs = []
            for s, c in p:
                evs.append((s, c))
                if time.monotonic() - t0 > 0.3:
                    time.sleep(0.1)
                assert time.monotonic() - t0 < 40, "never stopped"
        assert (p.retcode, p._stage) == (9001, 4), (p.retcode, p._stage)
        assert joined([(0,) + e for e in evs], "err").endswith(
            bm.WORKER_DIED_NOTE.encode() + b"\n"), evs[-3:]
        r = b.send_cmd("print -r -- ok-${v-unset}")
        assert r.out == "ok-unset\n", repr(r)  # restarted
        #: Once step 4 is taken, the result says the worker died, also when
        #: the reply came anyway (white box: the step is only recorded).
        b.send_cmd("v=kept")
        with b.popen("sleep 0.3; print -r done") as p:
            with p._mu:
                p._stage = 4
            evs = collect(p)
        assert p.retcode == 9001, (p.retcode, evs)
        assert joined(evs) == b"done\n", evs
        assert joined(evs, "err").endswith(bm.WORKER_DIED_NOTE.encode() + b"\n"), evs
        r = b.send_cmd("print -r -- ok-${v-unset}")
        assert r.out == "ok-unset\n", repr(r)  # restarted
        b.cleanup()
        ''',
        timeout=120,
    )


def test_close_from_another_thread_changes_nothing():
    #: Before, close() from another thread killed the command and then
    #: raised; a read through iter() killed it too.
    run(
        r'''
        b = Brish(server_count=1)
        p = b.popen("print -r a; sleep 0.8; print -r done; return 3", buffer=True)
        errs = []
        def other():
            for call in (p.close, p.wait, lambda: next(iter(p)), lambda: next(p)):
                try:
                    call()
                except RuntimeError as e:
                    errs.append(e)
        t = threading.Thread(target=other)
        t.start()
        t.join()
        assert len(errs) == 4 and all("thread" in str(e) for e in errs), errs
        #: The command was not killed: it ends on its own, with its output.
        assert (p.wait(), p._stage, p.result.out) == (3, 0, "a\ndone\n"), (p.retcode, p._stage, p.result)
        p.close()
        b.cleanup()
        '''
    )


def test_a_popen_collected_in_another_thread():
    #: An executor thread makes a BrishPopen and another thread drops the
    #: last reference. The command is killed at once; the worker lock belongs
    #: to the executor thread, which frees it at its next call (before, it
    #: stayed taken for good, and that thread counted as a lock holder).
    run(
        r'''
        import concurrent.futures as cf, gc
        b = Brish(server_count=1)
        b.send_cmd("v=kept")
        pool = cf.ThreadPoolExecutor(max_workers=1)
        box = {}
        def make():
            box["p"] = b.popen("print -r a; sleep 100")
        pool.submit(make).result()
        pid = box["p"]._worker_pid
        del box["p"]
        gc.collect()
        def lock_free():
            ok = b.locks[0].acquire(timeout=0.5)
            if ok:
                b.locks[0].release()
            return ok
        assert not lock_free()
        assert pool.submit(b._holds_worker_lock).result()
        t0 = time.monotonic()
        r = pool.submit(lambda: b.send_cmd("print -r -- ok-${v-unset}")).result(timeout=30)
        assert time.monotonic() - t0 < 10, time.monotonic() - t0
        #: Legacy mode restarts after an abandoned reply; binary mode skips it.
        want = "ok-kept\n" if BINARY else "ok-unset\n"
        assert (r.retcode, r.out) == (0, want), repr(r)
        assert lock_free()
        assert not pool.submit(b._holds_worker_lock).result()
        if BINARY:
            assert not bm._descendants(pid), bm._descendants(pid)  # the sleep was killed
        pool.shutdown()
        b.cleanup()
        '''
    )
