"""Brish.popen: output that streams while the command runs, and kill().

Every test runs in both modes. Timing assertions use wide margins: they tell
"arrives while the command runs" from "arrives when it ends".
"""

import pytest

from tests.conftest import BINARY, binary_only, check, legacy_only

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
def ps_cost():
    """Seconds that one `ps` run takes now: each kill step runs one, which
    takes a second or more on a loaded machine."""
    t = time.monotonic()
    bm._descendants(os.getpid())
    return time.monotonic() - t
def gone(pid, timeout=5):
    """Whether every descendant of `pid` has exited within `timeout`."""
    deadline = time.monotonic() + timeout
    while bm._descendants(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True
def same_server_ok(b, i, want_v="kept"):
    r = b.send_cmd("print -r -- ok-$v", server_index=i)
    assert (r.retcode, r.out, r.err) == (0, f"ok-{want_v}\n", ""), repr(r)
'''


def run(code, timeout=60, setup="", **kw):
    return check(code, setup=HELPERS + setup, timeout=timeout, **kw)


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


def test_kill_signals_the_worker_before_its_descendants():
    #: zsh runs the worker's trap only once the foreground child it waits
    #: for has exited, so a SIGINT to the worker first and to the child
    #: second makes the trap see the child die of it. The other way round, a
    #: kill thread held up between the two batches (here 50 ms after each,
    #: as the GIL or the OS can do) let the worker reap the child and go on
    #: before its own SIGINT arrived: the command ran on, or under set -e
    #: the child's 130 exited the worker (9001, and a restart).
    run(
        r'''
        real = bm._signal_pids
        def held_up(pids, sig):
            real(pids, sig)
            if pids:
                time.sleep(0.05)
        bm._signal_pids = held_up
        b = Brish(server_count=1)
        child = "zsh -fc 'zmodload zsh/zselect; zselect -t 10000'"
        for cmd in ("set -e; print -r x; " + child + "; print -r after",
                    "print -r x; " + child + "; print -r after"):
            for i in range(3):
                b.send_cmd("v=kept")
                with b.popen(cmd) as p:
                    kill_later(p, 0.3)
                    evs = collect(p)
                assert (p.retcode, p._stage, joined(evs)) == (130, 1, b"x\n"), (cmd, i, p.retcode, p._stage, evs)
                same_server_ok(b, 0)
        b.cleanup()
        ''',
        timeout=120,
    )


def test_no_sigkill_for_a_command_that_has_ended():
    #: Before step 4 SIGKILLs the worker, Brish reads what the pipes already
    #: hold and looks for the end of the reply. With a slow reader, the end
    #: of a command that ended just after a step waits behind output that
    #: Brish has not read, and step 3 then finds no processes below the
    #: worker (for a fork command: because its subshell has exited). Here
    #: the command has ended unread when step 3 is taken; it used to cost
    #: the worker (9001 and a restart).
    run(
        r'''
        b = Brish(server_count=1)
        for fork in (True, False):
            b.send_cmd("v=kept")
            body = "print -r x; print -ru2 e; " + ("exit 7" if fork else "return 7")
            with b.popen(body, fork=fork) as p:
                deadline = time.monotonic() + 10
                while p._worker_pid is None or bm._descendants(p._worker_pid):
                    assert time.monotonic() < deadline, fork
                    time.sleep(0.05)
                time.sleep(0.5)
                assert not p._finished, fork
                with p._mu:
                    p._stage = 2
                    p._signalled()
                p._escalate()
                assert (p._stage, p._finished) == (2, True), (fork, p._stage, p._finished)
                evs = collect(p)
            assert (p.retcode, joined(evs), joined(evs, "err")) == (7, b"x\n", b"e\n"), (fork, p.retcode, evs)
            same_server_ok(b, 0)
        b.cleanup()
        ''',
        timeout=60,
    )


def test_kill_leaves_the_stdin_whole():
    #: kill() sends SIGINT to every process below the worker, and so to the
    #: writer of the command's stdin. The writer ignores it, so a command
    #: that survives the signal (here one that ignores or traps INT) reads
    #: its whole stdin. With fork=True the worker's trap used to exit the
    #: writer with 130, and the command read only what the pipe held: rc 0
    #: on partial input, or 130 under pipefail. A non-fork writer blocked on
    #: a full pipe sometimes stopped at 64 KiB: `print` gives up on a write
    #: that the signal interrupts, `syswrite` goes on.
    run(
        r'''
        data = ("x" * 99 + "\n") * 20000
        b = Brish(server_count=1)
        loop = "trap '' INT; n=0; while IFS= read -r l; do n=$((n+1)); done; print -r -- $n"
        for cmd, fork in ((loop, True), ("set -o pipefail; " + loop, True), (loop, False)):
            for i in range(2):
                with b.popen(cmd, cmd_stdin=data, fork=fork) as p:
                    p.kill_grace = 60  # step 1 only
                    kill_later(p, 0.3)
                    evs = collect(p)
                got = (p.retcode, p._stage, joined(evs), joined(evs, "err"))
                assert got == (0, 1, b"20000\n", b""), (cmd, fork, i, got)
        #: The command waits in a child while the writer fills the pipe and
        #: blocks; the child dies of the SIGINT, the trap runs, and the
        #: command goes on.
        late = "command zsh -fc 'zmodload zsh/zselect; zselect -t 100 || :'; wc -c | tr -d ' '"
        for trap in ("trap 'print -ru2 caught' INT", "TRAPINT() { print -ru2 caught; return 0 }"):
            for i in range(4):
                with b.popen(trap + "; " + late, cmd_stdin="x" * 300000) as p:
                    p.kill_grace = 60
                    kill_later(p, 0.4)
                    evs = collect(p)
                got = (p.retcode, p._stage, joined(evs))
                assert got == (0, 1, b"300000\n"), (trap, i, got, evs)
        b.cleanup()
        ''',
        timeout=240,
    )


def test_stdin_keeps_a_non_fork_command_in_the_worker():
    #: A non-fork command reads its stdin from a process substitution, not
    #: through a pipeline: under `emulate sh`, which a command can leave
    #: behind, zsh runs the last element of a pipeline in a subshell. With
    #: stdin, a non-fork command then lost its state changes, its `exit`
    #: did not end the worker, and kill() gave 1 instead of 130.
    run(
        r'''
        b = Brish(server_count=1)
        w = '{ eval "$(< /dev/stdin)"; } 2>&1'
        base = b.send_cmd("print -r -- $ZSH_SUBSHELL").out
        for pre in ("true", "emulate sh"):
            b.send_cmd("emulate -R zsh; v=orig; cd /")
            b.send_cmd(pre)
            r = b.send_cmd(w, cmd_stdin="cd /tmp; v=changed; print -r -- $ZSH_SUBSHELL")
            assert (r.retcode, r.out) == (0, base), (pre, r)
            r = b.send_cmd('print -r -- "$v $PWD"')
            assert r.out == "changed /tmp\n", (pre, r)
            b.send_cmd("v=kept")
            r = b.send_cmd(w, cmd_stdin="exit 7")
            assert r.retcode == 7, (pre, r)
            #: The worker is gone: the instance restarted.
            r = b.send_cmd('print -r -- "${v-unset}"')
            assert r.out == "unset\n", (pre, r)
            b.send_cmd(pre + "; v=kept")
            with b.popen(w, cmd_stdin="print -r x; zmodload zsh/zselect; zselect -t 10000; print -r after") as p:
                kill_later(p, 0.5)
                evs = collect(p)
            assert (p.retcode, p._stage, joined(evs)) == (130, 1, b"x\n"), (pre, p.retcode, p._stage, evs)
            r = b.send_cmd('print -r -- "$v"')
            assert r.out == "kept\n", (pre, r)
        b.cleanup()
        ''',
        timeout=120,
    )


def test_kill_under_errexit_and_sh_emulation():
    #: zsh exits instead of unwinding when an interrupt meets err_exit (or
    #: err_return at the legacy worker's top level), or a special builtin
    #: under posix_builtins (`emulate sh`). The worker's trap turns those off,
    #: so the worker survives with its state. Options that the command set
    #: globally stay as it left them, except posix_builtins when the trap
    #: had to turn it off: that comes back as it was before the command.
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
            ("emulate sh; print -r x; eval 'while :; do :; done'", "off off off on"),
        ]
        #: The sh cases die only when the signal lands inside a special
        #: builtin, so they run several times.
        cases += [("emulate sh; set -e; print -r x; while :; do :; done", "off off off on")] * 4
        for cmd, want_opts in cases:
            b.send_cmd("emulate zsh; v=kept")
            with b.popen(cmd) as p:
                kill_later(p, 0.3)
                evs = collect(p)
            assert (p.retcode, joined(evs), joined(evs, "err")) == (130, b"x\n", b""), (cmd, p.retcode, evs)
            r = b.send_cmd("print -r -- ok-$v " + opts)
            assert r.out == f"ok-kept {want_opts}\n", (cmd, r)
        b.send_cmd("emulate zsh")
        #: A terminal-style Ctrl-C (the whole process group) during send_cmd
        #: reaches Python alone: the command runs to its end.
        seen = []
        signal.signal(signal.SIGINT, lambda *a: seen.append(a[0]))
        got = {}
        t = threading.Thread(target=lambda: got.update(r=b.send_cmd(
            "set -e; print -r x; zmodload zsh/zselect; zselect -t 150 || :; print -r after")))
        t.start()
        time.sleep(0.5)
        os.killpg(os.getpgrp(), signal.SIGINT)
        t.join(10)
        assert seen == [signal.SIGINT], seen
        assert (got["r"].retcode, got["r"].out) == (0, "x\nafter\n"), got
        b.send_cmd("set +e")
        same_server_ok(b, 0)
        b.cleanup()
        ''',
        timeout=120,
    )


def test_kill_restores_posix_builtins_as_before_the_command():
    #: The trap turns posix_builtins off where it runs, and cannot tell a
    #: global setting from one that a scope (emulate -c) undoes when the
    #: interrupt unwinds out of it. `always` restores the value from before
    #: the command; a worker left with posix_builtins on would die of the
    #: next failing special builtin.
    run(
        r'''
        b = Brish(server_count=1)
        pb = "${options[posixbuiltins]}"
        fatal = ". ./does-not-exist.sh; print -r survived"
        for cmd in ("emulate sh -c 'print -r x; while :; do :; done'",
                    "emulate sh -c 'print -r x; eval \"while :; do :; done\"'",
                    "f() { emulate -L sh; print -r x; while :; do :; done }; f"):
            b.send_cmd("emulate zsh; v=kept")
            with b.popen(cmd) as p:
                kill_later(p, 0.3)
                evs = collect(p)
            assert (p.retcode, joined(evs)) == (130, b"x\n"), (cmd, p.retcode, evs)
            assert b.send_cmd("print -r -- " + pb).out == "off\n", cmd
            r = b.send_cmd(fatal)
            assert (r.retcode, r.out) == (0, "survived\n"), (cmd, r)
            same_server_ok(b, 0)
        #: A worker in sh emulation keeps posix_builtins on.
        b.send_cmd("emulate sh; v=kept")
        with b.popen("print -r x; while :; do :; done") as p:
            kill_later(p, 0.3)
            evs = collect(p)
        assert p.retcode == 130, (p.retcode, evs)
        assert b.send_cmd("print -r -- " + pb).out == "on\n"
        b.send_cmd("emulate zsh")
        same_server_ok(b, 0)
        b.cleanup()
        '''
    )


def test_the_trap_writes_nothing_into_the_command():
    #: The worker's trap runs inside the interrupted command. Under set -x
    #: only its first line is traced (it turns xtrace off for the rest), and
    #: its assignments are global, which warn_nested_var does not report.
    run(
        r'''
        b = Brish(server_count=1)
        b.send_cmd("v=kept")
        for cmd, fork, traced in (
            ("f() { setopt local_options xtrace; sleep 100 }; f", False, True),
            ("set -x; sleep 100", True, True),
            ("setopt local_options warn_nested_var; f() { sleep 100 }; f", False, False),
        ):
            with b.popen(cmd, fork=fork) as p:
                kill_later(p, 0.3)
                evs = collect(p)
            assert p.retcode == 130, (cmd, p.retcode, evs)
            err = [l for l in joined(evs, "err").split(b"\n") if l]
            if traced:
                assert len(err) == 2 and err[0].endswith(b"> sleep 100"), (cmd, evs)
                assert err[1] == b"+TRAPINT:1> unsetopt xtrace", (cmd, evs)
            else:
                assert err == [], (cmd, evs)
            same_server_ok(b, 0)
        b.cleanup()
        '''
    )


def test_kill_with_a_user_int_trap():
    run(
        r'''
        b = Brish(server_count=1)
        b.send_cmd("v=kept")
        #: A trap string, a TRAPINT function, and a TRAPINT from a sourced
        #: file, which the worker must tell from the one trapint.zsh defines.
        traps = (
            "trap 'print -r caught' INT",
            "function TRAPINT { print -r caught; return 0 }",
            "source =(print -r -- 'function TRAPINT { print -r caught; return 0 }')",
        )
        for trap in traps:
            for fork in (False, True):
                with b.popen(trap + "; sleep 100; print -r after", fork=fork) as p:
                    kill_later(p, 0.3)
                    evs = collect(p)
                assert (p.retcode, joined(evs)) == (0, b"caught\nafter\n"), (trap, fork, p.retcode, evs)
                same_server_ok(b, 0)
                #: The command's trap ended with the command: the next one
                #: is unwound, and nothing else runs.
                with b.popen("sleep 100; print -r after") as p:
                    kill_later(p, 0.3)
                    evs = collect(p)
                assert (p.retcode, joined(evs)) == (130, b""), (trap, fork, p.retcode, evs)
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
        note = bm.WORKER_DIED_NOTE.encode() + b"\n"
        assert [(s, c) for _, s, c in evs[-1:]] == [("err", note)], evs
        assert 0.8 < dt < 4 + 6 * ps_cost(), dt
        r = b.send_cmd("print -r -- next-$v", server_index=0)
        assert (r.retcode, r.out) == (0, "next-\n"), repr(r)  # restarted
        #: stderr without a final newline: a newline chunk, then the note.
        with b.popen("trap '' INT TERM; print -rnu2 partial; while :; do :; done", server_index=0) as p:
            p.kill_grace = 0.5
            kill_later(p, 0.3)
            evs = collect(p)
        assert p.retcode == 9001, (p.retcode, evs)
        assert joined(evs, "err") == b"partial\n" + note, evs
        assert [(s, c) for _, s, c in evs[-2:]] == [("err", b"\n"), ("err", note)], evs
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


#: Legacy tests of the PID that kill() needs run with the bootstrap's PID
#: report (brish2.zsh) and with the internal PID request that a shell
#: without the report gets (forced here with pid_report=False).
PID_SOURCES = [pytest.param(True, id="report"), pytest.param(False, id="request")]


@legacy_only
@pytest.mark.parametrize("report", PID_SOURCES)
def test_legacy_learns_each_worker_pid_once(report):
    run(
        r'''
        b = Brish(server_count=2)
        if not REPORT:
            b.p.pid_report = False
        assert b.p.legacy_pids == [None, None]
        with b.popen("print -r a", server_index=1) as p:
            collect(p)
        pids = list(b.p.legacy_pids)
        if REPORT:
            assert all(pid and pid > 1 for pid in pids), pids  # every worker at once
        else:
            assert pids[0] is None and pids[1] > 1, pids
        for i in (0, 1):
            r = b.send_cmd("zmodload zsh/system; print -r -- $sysparams[pid]", server_index=i)
            assert pids[i] is None or int(r.out) == pids[i], (r, pids)
        with b.popen("print -r b", server_index=1) as p:
            collect(p)
        assert b.p.legacy_pids == pids
        b.cleanup()
        ''',
        setup=f"REPORT = {report!r}\n",
    )


@legacy_only
@pytest.mark.parametrize("report", PID_SOURCES)
def test_legacy_pid_survives_worker_state(report):
    #: State that a command left behind (a DEBUG trap that prints, digits in
    #: IFS) once separated the internal PID request's marker from the PID:
    #: kill() then signalled nothing, reported 9001 three graces later, and
    #: the command ran on after the restart.
    run(
        r'''
        for state in ["trap 'print -r -- dbg' DEBUG", "IFS=0123456789"]:
            b = Brish(server_count=1)
            if not REPORT:
                b.p.pid_report = False
            b.send_cmd("v=kept")
            b.send_cmd(state)
            with b.popen("print -r -- started; zmodload zsh/zselect; zselect -t 10000") as p:
                kill_later(p, 0.3)
                evs = collect(p)
            assert b.p.legacy_pids[0], (state, b.p.legacy_pids)
            assert (p.retcode, p._stage) == (130, 1), (state, p.retcode, p._stage, evs)
            assert b"started" in joined(evs), (state, evs)
            r = b.send_cmd("trap - DEBUG; IFS=$' \\t\\n'; print -r -- ok-$v")
            assert "ok-kept" in r.out, (state, repr(r))
            b.cleanup()
        ''',
        setup=f"REPORT = {report!r}\n",
    )


@legacy_only
@pytest.mark.parametrize("report", PID_SOURCES)
def test_legacy_pid_survives_background_output(report):
    #: A background job of an earlier command writes into every later reply,
    #: also into the internal PID request's; kill() must still find the PID.
    run(
        r'''
        b = Brish(server_count=1)
        if not REPORT:
            b.p.pid_report = False
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
        ''',
        setup=f"REPORT = {report!r}\n",
    )


@legacy_only
def test_legacy_interrupt_while_learning_the_pid():
    #: An interrupt during the first popen's internal PID request (injected
    #: here; a Ctrl-C in the main thread in real life) must not leak the
    #: worker lock, and a reply it leaves unread must never be read as the
    #: answer to a later request, also not by a thread that holds the lock.
    #: Only a shell without the bootstrap's PID report sends that request.
    run(
        r'''
        b = Brish(server_count=2)
        b.p.pid_report = False
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
        real_parent_pid = bm._parent_pid
        def in_ps(pid):
            raise KeyboardInterrupt("in ps")
        bm._parent_pid = in_ps
        try:
            b.popen("print -r x", server_index=0)
            raise SystemExit("no KeyboardInterrupt")
        except KeyboardInterrupt:
            pass
        finally:
            bm._parent_pid = real_parent_pid
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
    #: A refusal is checked only while the popen still runs: Brish may read
    #: the end, and free the worker, a few chunks before the last one is
    #: handed out (a loaded machine delays the loop body enough for that),
    #: and the owner reads nothing while its loop body runs.
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
        def refused_while(p, call):
            """refused(call), or p ended before the loop body ran."""
            return p.retcode is not None or refused(call)
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
                            assert refused_while(q, lambda: b.send_cmd("true"))
                            assert refused_while(q, lambda: b.popen("true"))
                assert q.retcode == 0
                assert refused_while(p, lambda: b.send_cmd("true", server_index=0))
                assert refused_while(p, lambda: b.popen("true", server_index=0))
                assert refused_while(p, lambda: b.acquire_lock(server_index=0))
        assert (p.retcode, b"".join(got)) == (7, b"a\nb\n"), (p.retcode, got)
        #: Nothing was restarted, and both workers take commands again.
        same_server_ok(b, 0, "kept0")
        same_server_ok(b, 1, "kept1")
        #: Under the caller's own lock, as in the readme.
        lock, i = b.acquire_lock()
        try:
            with b.popen("print -r x; sleep 0.3", server_index=i) as p:
                for _ in p:
                    assert refused_while(p, lambda: b.send_cmd("true", server_index=i))
            same_server_ok(b, i, f"kept{i}")
        finally:
            lock.release()
        b.cleanup()
        #: One worker: refused at once instead of a deadlock or a restart.
        b = Brish(server_count=1)
        b.send_cmd("v=kept")
        with b.popen("print -r x; sleep 0.3; print -r y") as p:
            for _ in p:
                assert refused_while(p, lambda: b.send_cmd("true"))
                assert refused_while(p, lambda: b.popen("true"))
        assert p.retcode == 0
        same_server_ok(b, 0)
        b.cleanup()
        '''
    )


def test_nested_calls_of_two_streaming_threads_never_wait_for_each_other():
    #: Two threads stream on the only two workers, and each makes one call
    #: with server_index=None inside its loop. Waiting there would hold the
    #: streaming worker while waiting for the other thread's (before: both
    #: waited for good, in both modes). The call is refused at once instead,
    #: and after the loop it works.
    run(
        r'''
        import faulthandler; faulthandler.dump_traceback_later(50, exit=True)
        Busy = bm.BrishWorkerBusyException
        b = Brish(server_count=2)
        both = threading.Barrier(2, timeout=20)
        out = {}
        def job(k):
            lock, i = b.acquire_lock(server_index=None, lock_sleep=0.05)
            try:
                with b.popen("print -r a; sleep 0.5; print -r b", server_index=i) as p:
                    for n, (s, c) in enumerate(p):
                        if n == 0:
                            both.wait()  # both threads stream now
                            t0 = time.monotonic()
                            try:
                                b.z("print -r -- {k}")
                                out[k] = "ran"
                            except Busy as e:
                                out[k] = ("busy", time.monotonic() - t0, str(e))
                after = b.z("print -r -- after-{k}")
                out[k, "after"] = (p.retcode, after.out)
            finally:
                lock.release()
        ts = [threading.Thread(target=job, args=(k,)) for k in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
            assert not t.is_alive(), ("a thread hung", out)
        for k in range(2):
            got = out[k]
            assert got[0] == "busy" and got[1] < 1, out
            assert "another thread" in got[2], got
            assert out[k, "after"] == (0, f"after-{k}\n"), out
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
        #: Ignores INT and TERM and floods, on stdout alone and on both
        #: streams in turn: still stopped, with a slow reader. (Before, the
        #: legacy reader queue counted as full at four tiny chunks, so with
        #: both streams kill() never got past step 1.) The pause between
        #: the two streams' lines keeps each read to a tiny chunk, which
        #: that bug needed.
        note = bm.WORKER_DIED_NOTE.encode() + b"\n"
        for cmd in ("trap '' INT TERM; while :; do print -r -- 0123456789abcdef; done",
                    "trap '' INT TERM; zmodload zsh/zselect; "
                    "while :; do print -r o; print -ru2 e; zselect -t 1 || :; done"):
            t0 = time.monotonic()
            with b.popen(cmd) as p:
                p.kill_grace = 0.5
                kill_later(p, 0.3)
                evs = []
                for s, c in p:
                    evs.append((s, c))
                    if time.monotonic() - t0 > 0.3:
                        time.sleep(0.1)
                    assert time.monotonic() - t0 < 40, ("never stopped", cmd, p._stage)
            assert (p.retcode, p._stage) == (9001, 4), (cmd, p.retcode, p._stage)
            #: The note is a chunk of its own, also after a flood of stderr.
            assert evs[-1] == ("err", note), (cmd, evs[-3:])
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
        it = iter(p)
        first = next(it)
        assert first[0] == "out" and first[1].startswith(b"a"), first
        def other():
            for call in (p.close, p.wait, lambda: next(iter(p)), lambda: next(p),
                         lambda: next(it)):
                try:
                    call()
                except RuntimeError as e:
                    errs.append(e)
        t = threading.Thread(target=other)
        t.start()
        t.join()
        assert len(errs) == 5 and all("thread" in str(e) for e in errs), errs
        #: The command was not killed: it ends on its own, with its output,
        #: and the owner's iterator goes on (before, the other thread's
        #: next(it) ended it).
        rest = list(it)
        assert first[1] + b"".join(c for _, c in rest) == b"a\ndone\n", (first, rest)
        assert (p.retcode, p._stage, p.result.out) == (3, 0, "a\ndone\n"), (p.retcode, p._stage, p.result)
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
        r = pool.submit(lambda: b.send_cmd("print -r -- ok-${v-unset}")).result(timeout=90)
        assert time.monotonic() - t0 < 60, time.monotonic() - t0  # not the sleep's 100 s
        #: A helper thread read the reply to its end, so the worker is in
        #: sync and keeps its state, in legacy mode too.
        assert (r.retcode, r.out) == (0, "ok-kept\n"), repr(r)
        assert lock_free()
        assert not pool.submit(b._holds_worker_lock).result()
        assert gone(pid), bm._descendants(pid)  # the sleep was killed
        pool.shutdown()
        b.cleanup()
        ''',
        timeout=120,
    )


#: A BrishPopen that is never closed, and is collected while Python ends, in
#: another thread than its owner. VARIANT "exc": an executor job makes it
#: and raises, and the main thread re-raises the job's exception, whose
#: traceback holds the object; "global": a thread keeps it in a global.
AT_EXIT = r'''
late = os.path.join(SCRATCH, "late")
cmd = "zmodload zsh/zselect; print -r a; zselect -t 6000; print -r late > " + late
b = Brish(server_count=2)
if VARIANT == "global":
    box = []
    t = threading.Thread(target=lambda: box.append(b.popen(cmd)))
    t.start()
    t.join()
else:
    import concurrent.futures as cf
    pool = cf.ThreadPoolExecutor(1)
    def job():
        p = b.popen(cmd)
        raise ValueError("boom")
    pool.submit(job).result()
'''


@pytest.mark.parametrize("variant", ["exc", "global"])
def test_an_unclosed_popen_collected_while_python_ends(variant):
    #: No helper thread can start then. Before, its start() waited for good
    #: on Python 3.10, and Python never exited (exc in both modes, global
    #: in binary mode). Now Python ends at once, and the bootstrap stops
    #: the busy worker: its command never gets to its last line.
    run(
        r'''
        import subprocess
        from tests.conftest import PRELUDE, SESSIONS_FILE, session_members
        code = (PRELUDE.format(root=ROOT, scratch=SCRATCH, binary=BINARY, sessions=SESSIONS_FILE)
                + "VARIANT = %r\n" % VARIANT + AT_EXIT)
        t = time.monotonic()
        try:
            g = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=30)
        except subprocess.TimeoutExpired:
            raise AssertionError("Python did not end")
        dt = time.monotonic() - t
        err = g.stderr.decode(errors="replace")
        assert g.returncode == (1 if VARIANT == "exc" else 0), (g.returncode, err)
        assert VARIANT != "exc" or "ValueError: boom" in err, err
        deadline = time.monotonic() + 20
        while session_members(SCRATCH) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not session_members(SCRATCH), session_members(SCRATCH)
        assert not os.path.exists(os.path.join(SCRATCH, "late"))
        print(VARIANT, "ended after", round(dt, 2), "s")
        ''',
        setup="AT_EXIT = %r\nVARIANT = %r\n" % (AT_EXIT, variant),
        timeout=120,
    )


def test_kill_waits_for_a_long_report_under_a_slow_reader():
    #: A program that handles SIGINT by writing a 200 KB report and exiting,
    #: read at one chunk every three seconds (a slow chat bot's pace), with
    #: the default grace: Brish reads up to 256 KiB ahead once the signal is
    #: out, sees the end, and takes no further step. (Before, legacy mode
    #: read only about 76 KiB ahead, took step 2, and its SIGTERM cut the
    #: report and killed an unrelated background job of the worker. At one
    #: chunk a second the old code passed too.)
    run(
        r"""
        prog = os.path.join(SCRATCH, "report.py")
        with open(prog, "w") as f:
            f.write("import sys, time\n"
                    "try:\n"
                    "    print('started', flush=True)\n"
                    "    time.sleep(100)\n"
                    "except KeyboardInterrupt:\n"
                    "    sys.stdout.write('r' * 200000 + '\\nreport done\\n')\n"
                    "    sys.stdout.flush()\n"
                    "    sys.exit(3)\n")
        b = Brish(server_count=1)
        b.send_cmd("v=kept; sleep 1000 &!")
        bg = int(b.send_cmd("print -r -- $!").out)
        try:
            out = b""
            py = sys.executable
            with b.popen(b.zstring("{{ {py} {prog} }} 2>&1")) as p:
                for s, c in p:
                    out += c
                    if not p._stage:
                        if b"started" in out:
                            p.kill()
                    else:
                        time.sleep(3)
            assert (p.retcode, p._stage) == (130, 1), (p.retcode, p._stage, len(out))
            assert out.endswith(b"r\nreport done\n") and len(out) == 200021, len(out)
            assert bm._alive(bg), "step 2 stopped the background job"
            same_server_ok(b, 0)
        finally:
            if bm._alive(bg):
                os.kill(bg, signal.SIGKILL)
        b.cleanup()
        """,
        timeout=180,
    )


@binary_only
def test_binary_a_cut_request_frame_takes_the_worker_out():
    #: An interrupt while the request frame is still being written leaves the
    #: worker holding part of a frame. It takes no request again: a thread
    #: that holds its lock gets BrishWorkerDiedException at once (before, its
    #: next request was read as the rest of the old frame, ran as code inside
    #: the old command, and hung), and the instance restarts after the
    #: release. Injected here; a Ctrl-C in the main thread in real life.
    run(
        r"""
        import faulthandler; faulthandler.dump_traceback_later(50, exit=True)
        b = Brish(server_count=2)
        real_write = os.write
        for op in ("popen", "send_cmd"):
            for i in (0, 1):
                b.send_cmd(f"v=kept{i}", server_index=i)
            gen = b._gen
            req = b.p.workers[0].req
            def cut(fd, data):
                if fd == req and len(data) > 1000:
                    real_write(fd, bytes(data[: len(data) - 500]))  # inside the stdin
                    raise KeyboardInterrupt("cut")
                return real_write(fd, data)
            lock, _ = b.acquire_lock(server_index=0)
            try:
                os.write = cut
                try:
                    getattr(b, op)("print -r first", cmd_stdin="x" * 1000, server_index=0)
                    raise SystemExit("no KeyboardInterrupt")
                except KeyboardInterrupt:
                    pass
                finally:
                    os.write = real_write
                t0 = time.monotonic()
                for call in (b.send_cmd, b.popen):
                    try:
                        r = call("print -r -- second-$v", cmd_stdin="y" * 600, server_index=0)
                        raise SystemExit(f"the half-fed worker took a request: {r!r}")
                    except bm.BrishWorkerDiedException as e:
                        assert "abandoned" in str(e), e
                assert time.monotonic() - t0 < 5, "not at once"
                #: The other worker works on under the same lock.
                r = b.send_cmd("print -r -- other-$v", server_index=1)
                assert r.out == "other-kept1\n", (op, r)
            finally:
                lock.release()
            #: After the release, the instance restarts before its next use.
            r = b.send_cmd("print -r -- next-${v-unset}", server_index=0)
            assert r.out == "next-unset\n", (op, r)
            assert b._gen != gen, op
        b.cleanup()
        """
    )


def test_a_popen_dropped_in_its_thread_frees_the_worker_at_once():
    #: With the cyclic garbage collector off, dropping a half-read BrishPopen
    #: frees its worker at once (before, a bound method kept on the object
    #: made a reference cycle, so the lock stayed taken until some later
    #: collection, often in another thread). Legacy mode keeps the worker's
    #: state when the reply was already read to its end (before, it
    #: restarted the instance).
    run(
        r"""
        import gc
        import faulthandler; faulthandler.dump_traceback_later(110, exit=True)
        gc.disable()
        b = Brish(server_count=1)
        b.send_cmd("v=kept")
        def lock_free():
            got = []
            def other():
                ok = b.locks[0].acquire(timeout=0.5)
                if ok:
                    b.locks[0].release()
                got.append(ok)
            t = threading.Thread(target=other)
            t.start()
            t.join()
            return got == [True]
        #: Half-read, the command still running.
        p = b.popen("print -r a; sleep 100")
        assert next(p)[0] == "out"
        pid = p._worker_pid
        del p
        assert lock_free() and not b._holds_worker_lock()
        assert gone(pid), bm._descendants(pid)  # the sleep was killed
        r = b.send_cmd("print -r -- ok-${v-unset}")
        want = "ok-kept\n" if BINARY else "ok-unset\n"  # legacy: an abandoned reply
        assert (r.retcode, r.out) == (0, want), repr(r)
        b.send_cmd("v=kept")
        #: Half-read, the command already ended and its reply read by Brish
        #: (legacy: by the reader threads).
        p = b.popen("print -r a; print -r b")
        assert next(p)[0] == "out"
        time.sleep(0.5)
        del p
        assert lock_free() and not b._holds_worker_lock()
        same_server_ok(b, 0)
        b.cleanup()
        """,
        timeout=120,
    )


def test_an_orphan_is_killed_with_every_step_and_holds_up_no_restart():
    #: A BrishPopen collected in another thread than its owner (an orphan)
    #: keeps the owner's worker lock until the owner's next call. Its command
    #: is killed with every step all the same, here one that ignores
    #: SIGINT (before, it got only SIGINT and ran on), the reply is read to
    #: its end, so the worker keeps its state in both modes, and a restart
    #: does not wait for the orphan (before, it waited for the owner's next
    #: call, and every thread without a worker lock with it).
    run(
        r"""
        import concurrent.futures as cf, gc, weakref
        import faulthandler; faulthandler.dump_traceback_later(170, exit=True)
        b = Brish(server_count=2)
        for i in (0, 1):
            b.send_cmd(f"v=kept{i}", server_index=i)
        pool = cf.ThreadPoolExecutor(max_workers=1)
        def orphan(cmd):
            box = {}
            def make():
                p = b.popen(cmd, server_index=0)
                p.kill_grace = 0.3
                box["p"] = p
            pool.submit(make).result()
            ref = weakref.ref(box["p"])
            pid = box["p"]._worker_pid
            del box["p"]
            gc.collect()
            return ref, pid
        def drained(ref, timeout=60):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                p = ref()
                if p is None or p._drained.wait(0.05):
                    return True
            return False
        #: 1. A command that ignores SIGINT: step 2 stops its sleep.
        ref, pid = orphan("trap '' INT; print -r a; sleep 100; print -r -- after $?")
        assert drained(ref), "the orphan's command was never stopped"
        assert gone(pid), bm._descendants(pid)
        r = pool.submit(lambda: b.send_cmd("print -r -- ok-$v", server_index=0)).result(timeout=60)
        assert (r.retcode, r.out) == (0, "ok-kept0\n"), repr(r)
        #: 2. A restart while an orphan holds its owner's lock.
        ref, pid = orphan("print -r a; sleep 100")
        assert drained(ref)
        gen = b._gen
        t0 = time.monotonic()
        done = []
        t = threading.Thread(target=lambda: done.append(b.restart()))
        t.start()
        t.join(60)
        assert done == [True], ("the restart waited for the orphan", time.monotonic() - t0)
        assert b._gen != gen
        r = b.send_cmd("print -r -- ok-${v-unset}", server_index=0)
        assert r.out == "ok-unset\n", repr(r)
        #: The owner frees the old generation's lock at its next call.
        r = pool.submit(lambda: b.send_cmd("print -r -- owner-${v-unset}", server_index=0)).result(timeout=60)
        assert r.out == "owner-unset\n", repr(r)
        assert not pool.submit(b._holds_worker_lock).result()
        pool.shutdown()
        b.cleanup()
        """,
        timeout=180,
    )


ORPHAN_HELPERS = r'''
import gc, weakref
SNOOZE = "zmodload zsh/zselect; print -r a; zselect -t 10000"
def drained(ref, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        p = ref()
        if p is None or p._drained.wait(0.05):
            return True
    return False
def within(fn, secs):
    """fn() in a thread: ("ok", value) or ("STILL WAITING",)."""
    out = []
    def go():
        try:
            out.append(("ok", fn()))
        except BaseException as e:
            out.append(("exc", repr(e)))
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(secs)
    return out[0] if out else ("STILL WAITING",)
'''


def test_an_orphan_made_under_acquire_lock_holds_up_no_restart():
    #: The readme's pattern: acquire_lock, then popen on that worker. The
    #: object is collected in another thread (an orphan). Once the owner has
    #: released its own acquire_lock level, the orphan's level is the only
    #: one left, so a restart from another thread does not wait for it
    #: (before, it waited until the owner's next call).
    run(
        r'''
        import concurrent.futures as cf
        b = Brish(server_count=2)
        pool = cf.ThreadPoolExecutor(max_workers=1)
        box = {}
        def make():
            lock, si = b.acquire_lock(server_index=0)
            try:
                p = b.popen(SNOOZE, server_index=si)
                p.kill_grace = 0.3
                next(p)
                box["p"] = p
                #: While the owner holds its own level too, the orphan's
                #: worker is not skippable.
            finally:
                lock.release()
        pool.submit(make).result()
        ref = weakref.ref(box.pop("p"))
        gc.collect()
        assert drained(ref)
        gen = b._gen
        got = within(b.restart, 20)
        assert got == ("ok", True), got
        assert b._gen != gen
        r = b.send_cmd("print -r ok", server_index=0)
        assert r.out == "ok\n", repr(r)
        r = pool.submit(lambda: b.send_cmd("print -r owner", server_index=0)).result(timeout=60)
        assert r.out == "owner\n", repr(r)
        assert not b._orphans
        assert not pool.submit(b._holds_worker_lock).result()
        pool.shutdown()
        b.cleanup()
        ''',
        setup=ORPHAN_HELPERS,
        timeout=120,
    )


def test_an_orphan_skipped_only_when_its_level_is_the_last():
    #: While the owner holds the orphan's worker lock for its own reasons
    #: too, the worker is not skipped: it may be in use.
    run(
        r'''
        b = Brish(server_count=1)
        lock, si = b.acquire_lock(server_index=0)
        p = b.popen(SNOOZE, server_index=si)
        p.kill_grace = 0.3
        next(p)
        #: Orphan it by hand, as a collection in another thread would.
        threading.Thread(target=p._orphan).start()
        assert drained(weakref.ref(p))
        assert b._orphan_held(b.p) == set(), b._orphan_held(b.p)
        lock.release()
        assert b._orphan_held(b.p) == {0}, b._orphan_held(b.p)
        #: The owner's next call frees it.
        r = b.send_cmd("print -r ok")
        assert r.out == "ok\n", repr(r)
        assert p._released and not b._orphans and not b._holds_worker_lock()
        del p
        b.cleanup()
        ''',
        setup=ORPHAN_HELPERS,
        timeout=60,
    )


def test_an_orphan_whose_owner_ends_after_the_drain():
    #: The owner thread is alive when the helper thread has drained its
    #: orphan, and ends later without calling the instance again. The next
    #: call of any thread restarts the instance (before, every call waited
    #: for that worker for good), and the orphan is then forgotten.
    run(
        r'''
        b = Brish(server_count=1)
        b.send_cmd("v=kept")
        go, box = threading.Event(), {}
        def owner():
            p = b.popen(SNOOZE)
            p.kill_grace = 0.3
            next(p)
            box["p"] = p
            del p
            go.wait()
        t = threading.Thread(target=owner)
        t.start()
        while "p" not in box:
            time.sleep(0.01)
        ref = weakref.ref(box.pop("p"))
        gc.collect()
        assert drained(ref)
        assert t.is_alive()
        go.set()
        t.join()
        #: An idle thread takes the dead owner's ident if it can, so that no
        #: caller inherits its lock by ident reuse.
        idle = threading.Event()
        threading.Thread(target=idle.wait, daemon=True).start()
        got = within(lambda: b.send_cmd("print -r -- ${v-unset}").out, 30)
        assert got == ("ok", "unset\n"), got
        r = b.send_cmd("print -r ok")
        assert r.out == "ok\n", repr(r)
        assert not b._orphans, list(b._orphans)
        idle.set()
        b.cleanup()
        ''',
        setup=ORPHAN_HELPERS,
        timeout=120,
    )


def test_two_owners_reap_their_orphans_at_once():
    #: Two owner threads, each with a drained orphan, call the instance at
    #: the same moment. Each frees its own orphan's worker (before, one
    #: could miss its orphan while the other held it, and its call raised
    #: BrishWorkerBusyException). A tiny switch interval makes it likely.
    run(
        r'''
        import queue
        b = Brish(server_count=2)
        class Owner:
            def __init__(self, i):
                self.i, self.q, self.r = i, queue.Queue(), queue.Queue()
                threading.Thread(target=self.run, daemon=True).start()
            def run(self):
                while True:
                    fn = self.q.get()
                    try:
                        v = fn()
                    except BaseException as e:
                        v = e
                    self.r.put(v)
                    v = fn = None
            def do(self, fn):
                self.q.put(fn)
                return self.r.get(timeout=60)
        owners = [Owner(0), Owner(1)]
        bad = []
        sys.setswitchinterval(1e-6)
        try:
            for n in range(40):
                ps = [o.do(lambda i=o.i: b.popen(SNOOZE, server_index=i)) for o in owners]
                for p in ps:
                    p.kill_grace = 0.3
                refs = [weakref.ref(p) for p in ps]
                del p
                ps.clear()
                gc.collect()
                for r in refs:
                    assert drained(r)
                bar = threading.Barrier(2)
                def call(i):
                    bar.wait()
                    return b.send_cmd("true", server_index=i).retcode
                for o in owners:
                    o.q.put(lambda i=o.i: call(i))
                res = [o.r.get(timeout=60) for o in owners]
                if res != [0, 0]:
                    bad.append((n, [repr(x)[:100] for x in res]))
                    for o in owners:
                        o.do(lambda i=o.i: b.send_cmd("true", server_index=i))
        finally:
            sys.setswitchinterval(0.005)
        assert not bad, bad[:3]
        assert not b._orphans
        b.cleanup()
        ''',
        setup=ORPHAN_HELPERS,
        timeout=300,
    )


def test_keyboard_interrupt_in_popen_start_never_keeps_the_lock():
    #: A KeyboardInterrupt comes at an eval-breaker check: after a call
    #: returns, or at a backward jump. Inside _start's try block, until
    #: _released is cleared, the except clause cannot tell that the worker
    #: lock was taken, so an interrupt there kept it for good (a call
    #: between taking the lock and recording it). A SIGALRM every 50 us
    #: raises one whenever it is handled in that stretch, with the
    #: acquire_lock pattern holding the worker; it must find no place to
    #: land, or the lock must not leak.
    run(
        r'''
        import inspect, re
        src, first = inspect.getsourcelines(bm.BrishPopen._start)
        lo = first + next(i for i, l in enumerate(src) if "b._acquire(server_index" in l)
        hi = first + next(i for i, l in enumerate(src) if "self._released = False" in l)
        filename = bm.BrishPopen._start.__code__.co_filename
        hits = []
        def handler(sig, frame):
            ln = frame.f_lineno if frame is not None else None
            if (ln is not None and lo < ln <= hi and frame.f_code.co_filename == filename
                    and frame.f_code.co_name in ("_start", "<genexpr>")):
                hits.append(ln)
                signal.setitimer(signal.ITIMER_REAL, 0)
                raise KeyboardInterrupt
        signal.signal(signal.SIGALRM, handler)
        b = Brish(server_count=2)
        me = threading.get_ident()
        def levels(lock):
            m = re.search(r"owner=(\d+) count=(\d+)", repr(lock))
            return int(m.group(2)) if int(m.group(1)) == me else 0
        leaks = []
        t0 = time.monotonic()
        signal.setitimer(signal.ITIMER_REAL, 0.00005, 0.00005)
        try:
            while time.monotonic() - t0 < 5 and not hits:
                lock, si = b.acquire_lock(server_index=0)
                try:
                    try:
                        with b.popen("true", server_index=si) as p:
                            for _ in p:
                                pass
                    except KeyboardInterrupt:
                        if levels(b.locks[0]) != 1:
                            leaks.append((hits[-1], levels(b.locks[0])))
                finally:
                    lock.release()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        assert not leaks, (leaks, hits)
        assert not b._holds_worker_lock()
        got = within(lambda: b.send_cmd("print -r ok", server_index=0).out, 30)
        assert got == ("ok", "ok\n"), got
        b.cleanup()
        ''',
        setup=ORPHAN_HELPERS,
        timeout=120,
    )


def test_result_from_another_thread_is_whole():
    #: Another thread that reads p.result as soon as retcode is set gets the
    #: whole output, and so does the owner afterwards (before, a read between
    #: the owner's moving a chunk to the buffer could miss it, and the short
    #: result was cached). A tiny switch interval makes such a race likely.
    run(
        r"""
        sys.setswitchinterval(1e-6)
        b = Brish(server_count=1)
        bad = []
        for n in range(40):
            p = b.popen("for i in {1..30}; do print -r -- line-$i; done", buffer=True)
            seen = []
            stop = threading.Event()
            def watch():
                while not stop.is_set():
                    r = p.result
                    if r is not None:
                        seen.append(r.out)
                        return
            t = threading.Thread(target=watch)
            t.start()
            p.wait()
            stop.set()
            t.join()
            want = "".join(f"line-{i}\n" for i in range(1, 31))
            if p.result.out != want or any(x != want for x in seen):
                bad.append((n, p.result.out, seen))
            p.close()
        sys.setswitchinterval(0.005)
        assert not bad, bad[:2]
        b.cleanup()
        """,
        timeout=120,
    )


def test_held_chunks_join_the_last_one_of_their_stream():
    #: White box. Once 4 chunks are held for the caller, a new chunk joins
    #: the last held chunk of its own stream, up to 64 KiB. Before, it
    #: joined only a last chunk of the same stream, so two streams in turn
    #: piled up one small chunk per read, and after kill() a slow caller
    #: had to take 128 KiB of them before the next step (binary mode, a
    #: flood of `print -r o; print -ru2 e` that ignores INT and TERM stayed
    #: at step 2 for more than 40 s at 0.1 s per chunk).
    run(
        r'''
        b = Brish(server_count=1)
        with b.popen("sleep 0.3") as p:
            want = {"out": [], "err": []}
            for i in range(30000):
                s = "out" if i % 2 else "err"
                c = b"%d," % i
                p._push(s, c)
                want[s].append(c)
            held = [(s, len(c)) for s, c in p._pending]
            evs = collect(p)
        assert len(held) <= 8, held
        assert all(n <= 65536 for _, n in held), held
        assert p.retcode == 0, p.retcode
        assert joined(evs) == b"".join(want["out"]), held
        assert joined(evs, "err") == b"".join(want["err"]), held
        b.cleanup()
        '''
    )
