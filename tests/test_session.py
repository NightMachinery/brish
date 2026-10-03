"""Workers run in a session of their own (setsid).

No terminal signal and no signal to the caller's process group reaches a
worker or its commands, which have no controlling terminal either. Python
still gets its KeyboardInterrupt. Only Brish interrupts a command:
BrishPopen.kill(), or the one SIGINT for an abandoned BrishPopen.
Every test runs in both modes. run_py starts each child in a session of its
own, so signalling the child's process group touches nothing else.

The commands wait with zselect (a builtin), not with sleep: they must not
depend on an external program that something else might signal.
"""

import pytest

from tests.conftest import check, legacy_only

WAIT = "zmodload zsh/zselect; zselect -t {cs}"  # centiseconds


def test_group_signals_never_reach_workers():
    #: SIGHUP, SIGQUIT, SIGTSTP and SIGINT to the caller's process group,
    #: as a terminal sends them, while every worker runs a command (non-fork,
    #: fork, with stdin, streaming) and one has a background job: every
    #: command runs to its end, every worker keeps its PID and its state, a
    #: SIGTSTP stops none of them, and Python gets its KeyboardInterrupt.
    check(
        r'''
        W = {wait!r}
        b = Brish(server_count=4)
        pid_cmd = "zmodload zsh/system; print -r -- $sysparams[pid]"
        pids = [int(b.send_cmd(pid_cmd, server_index=i).out) for i in range(4)]
        for pid in pids:
            assert os.getsid(pid) != os.getsid(0), pid
            assert os.getpgid(pid) != os.getpgrp(), pid
        for i in range(4):
            b.send_cmd(f"v=kept{{i}}", server_index=i)
        flag = os.path.join(SCRATCH, "bg-done")
        b.send_cmd(f"( {{W.format(cs=150)}}; print -r done >| {{flag}} ) &", server_index=3)
        jobs = [
            (0, "print -r x; " + W.format(cs=150) + "; print -r after-$v", "", False),
            (1, "print -r x; " + W.format(cs=150) + "; print -r after-$v", "", True),
            (2, "print -r x; IFS= read -r l; " + W.format(cs=150) + "; print -r after-$l-$v", "in\n", False),
        ]
        got, errors = {{}}, []
        def run(i, cmd, stdin, fork):
            try:
                got[i] = b.send_cmd(cmd, cmd_stdin=stdin, fork=fork, server_index=i)
            except BaseException as e:
                errors.append(e)
        def stream():
            try:
                with b.popen("print -r x; " + W.format(cs=150) + "; print -r after-$v", server_index=3) as p:
                    got[3] = (b"".join(c for s, c in p if s == "out"), p.retcode)
            except BaseException as e:
                errors.append(e)
        ts = [threading.Thread(target=run, args=j) for j in jobs] + [threading.Thread(target=stream)]
        for t in ts:
            t.start()
        for sig in (signal.SIGHUP, signal.SIGQUIT, signal.SIGTSTP):
            signal.signal(sig, signal.SIG_IGN)
        t0 = time.monotonic()
        try:
            time.sleep(0.4)
            for sig in (signal.SIGHUP, signal.SIGQUIT, signal.SIGTSTP, signal.SIGINT):
                os.killpg(os.getpgrp(), sig)
                time.sleep(0.05)
            time.sleep(5)
            raise SystemExit("no KeyboardInterrupt")
        except KeyboardInterrupt:
            pass
        for t in ts:
            t.join(20)
            assert not t.is_alive()
        assert not errors, errors
        assert time.monotonic() - t0 < 10, time.monotonic() - t0  # nothing was stopped
        for i in (0, 1):
            r = got[i]
            assert (r.retcode, r.out, r.err) == (0, f"x\nafter-kept{{i}}\n", ""), repr(r)
        r = got[2]
        assert (r.retcode, r.out, r.err) == (0, "x\nafter-in-kept2\n", ""), repr(r)
        assert got[3] == (b"x\nafter-kept3\n", 0), got[3]
        for i in range(4):
            assert int(b.send_cmd(pid_cmd, server_index=i).out) == pids[i], i
            r = b.send_cmd("print -r -- $v", server_index=i)
            assert r.out == f"kept{{i}}\n", (i, repr(r))
        deadline = time.monotonic() + 5
        while not os.path.exists(flag) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert os.path.exists(flag)  # the background job ran on
        #: kill() is the interrupt: 130, and the worker keeps its PID and state.
        for fork in (False, True):
            with b.popen("print -r x; " + W.format(cs=1000) + "; print -r no", fork=fork, server_index=0) as p:
                threading.Timer(0.3, p.kill).start()
                out = b"".join(c for s, c in p if s == "out")
            assert (p.retcode, out) == (130, b"x\n"), (fork, p.retcode, out)
            assert int(b.send_cmd(pid_cmd, server_index=0).out) == pids[0]
            assert b.send_cmd("print -r -- $v", server_index=0).out == "kept0\n"
        b.cleanup()
        '''.format(wait=WAIT),
        timeout=90,
    )


def test_terminal_ctrl_c_reaches_python_alone():
    #: Python runs on a pseudo-terminal, as from an interactive shell, and a
    #: Ctrl-C is typed: Python gets its KeyboardInterrupt, and the command
    #: runs to its end. Commands have no controlling terminal: `tty` says
    #: so, `stty` fails (also on /dev/tty), opening /dev/tty fails at once
    #: (ENXIO), and a password prompt that wants the terminal falls back to
    #: stdin instead of waiting.
    check(
        r'''
        import pty, select, textwrap
        from tests.conftest import PRELUDE, SESSIONS_FILE
        W = {wait!r}
        body = textwrap.dedent("""
            py = sys.executable
            print("TTY-PY", os.isatty(0), flush=True)
            b = Brish(server_count=1)
            r = b.send_cmd("tty; print -r -- rc=$?")
            print("TTY", repr(r.out), flush=True)
            r = b.send_cmd("{{ : </dev/tty }} 2>/dev/null; print -r -- rc=$?")
            print("DEVTTY", repr(r.out), flush=True)
            r = b.send_cmd("stty -a >/dev/null 2>&1; print -r -- rc=$?; {{ stty -a </dev/tty }} >/dev/null 2>&1; print -r -- rc=$?")
            print("STTY", repr(r.out), flush=True)
            r = b.send_cmd(py + " -c 'open(\\"/dev/tty\\")' 2>&1 | tail -n 1")
            print("OPEN", repr(r.out), flush=True)
            t = time.monotonic()
            r = b.send_cmd(py + " -c 'import getpass; print(getpass.getpass(\\"pw: \\"))' 2>/dev/null", cmd_stdin="secret\\n")
            print("GETPASS", repr(r.out), r.retcode, time.monotonic() - t < 10, flush=True)
            got = {{}}
            th = threading.Thread(target=lambda: got.update(r=b.send_cmd(
                "v=kept; print -r x; " + W.format(cs=200) + "; print -r after-$v")))
            th.start()
            time.sleep(0.3)
            print("READY", flush=True)
            try:
                time.sleep(10)
                print("NO-KBI", flush=True)
            except KeyboardInterrupt:
                print("KBI", flush=True)
            th.join(20)
            r = got["r"]
            print("RESULT", r.retcode, repr(r.out), repr(r.err), flush=True)
            print("STATE", repr(b.send_cmd("print -r -- $v").out), flush=True)
            b.cleanup()
            print("END", flush=True)
        """)
        code = PRELUDE.format(root=ROOT, scratch=SCRATCH, binary=BINARY, sessions=SESSIONS_FILE) + "W = %r\n" % W + body
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.execv(sys.executable, [sys.executable, "-c", code])
            finally:
                os._exit(127)
        buf = b""
        def read_until(token, timeout):
            global buf
            deadline = time.monotonic() + timeout
            while token not in buf:
                left = deadline - time.monotonic()
                if left <= 0 or not select.select([fd], [], [], left)[0]:
                    raise AssertionError((token, buf))
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    raise AssertionError(("EOF", token, buf))
                buf += chunk
        read_until(b"READY", 60)
        os.write(fd, b"\x03")  # the terminal's Ctrl-C
        read_until(b"END", 30)
        _, status = os.waitpid(pid, 0)
        os.close(fd)
        out = buf.decode().replace("\r\n", "\n")
        print(out)
        assert os.waitstatus_to_exitcode(status) == 0, (status, out)
        assert "TTY-PY True\n" in out, out
        assert "TTY 'not a tty\\nrc=1\\n'\n" in out, out
        assert "DEVTTY 'rc=1\\n'\n" in out, out
        assert "STTY 'rc=1\\nrc=1\\n'\n" in out, out
        assert "OPEN \"OSError: [Errno 6] Device not configured: '/dev/tty'\\n\"" in out or "OPEN \"OSError: [Errno 6] No such device or address: '/dev/tty'\\n\"" in out, out
        assert "GETPASS 'secret\\n' 0 True\n" in out, out
        assert "KBI\n" in out and "NO-KBI" not in out, out  # after the echoed ^C
        assert "RESULT 0 'x\\nafter-kept\\n' ''\n" in out, out
        assert "STATE 'kept\\n'\n" in out, out
        '''.format(wait=WAIT),
        timeout=120,
    )


#: The end of a Python process that uses Brish, run in a child of the test
#: (a grandchild), which records the PIDs of its busy commands' processes in
#: SCRATCH/pids and prints READY once they all run. With HOW = "terminal" it
#: runs on a pseudo-terminal that the test then closes.
DEATH = r'''
pidfile = os.path.join(SCRATCH, "pids")
W = "zmodload zsh/zselect; zselect -t 6000"
rec = "zmodload zsh/system; print -r -- $sysparams[pid] >> " + pidfile + "; "
b = Brish(server_count=4)
jobs = [
    (0, rec + W, False),  # the worker itself waits
    (1, rec + "zsh -fc '" + rec + W + "'", False),  # a child of the worker waits
    (2, rec + W, True),  # a fork command's subshell waits
]
for i, cmd, fork in jobs:
    threading.Thread(target=b.send_cmd, args=(cmd,), kwargs=dict(fork=fork, server_index=i), daemon=True).start()
deadline = time.monotonic() + 30
while not (os.path.exists(pidfile) and len(open(pidfile).read().split()) == 4):
    assert time.monotonic() < deadline
    time.sleep(0.05)
print("READY", flush=True)
if HOW == "exit":
    sys.exit(0)
elif HOW == "exception":
    raise RuntimeError("the end")
elif HOW == "sigterm":
    os.kill(os.getpid(), signal.SIGTERM)
elif HOW == "sigkill":
    os.kill(os.getpid(), signal.SIGKILL)
time.sleep(60)
'''

#: Waits for every process of a scenario to be gone: the recorded PIDs and
#: every member of the sessions that Brish started.
GONE = r'''
from tests.conftest import PRELUDE, SESSIONS_FILE, session_members
def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
def left_behind(pids, timeout):
    deadline = time.monotonic() + timeout
    while True:
        left = sorted({p for p in pids if alive(p)} | set(session_members(SCRATCH)))
        if not left or time.monotonic() > deadline:
            return left
        time.sleep(0.1)
def grandchild(how, body):
    return PRELUDE.format(root=ROOT, scratch=SCRATCH, binary=BINARY, sessions=SESSIONS_FILE) + "HOW = %r\n" % how + body
'''


@pytest.mark.parametrize("how", ["exit", "exception", "sigterm", "sigkill", "terminal"])
def test_no_command_outlives_python(how):
    #: However Python ends, its workers and the commands they run go with
    #: it: the bootstrap reads its stdin until EOF, and then stops every
    #: worker still running a command, with every process below it (see
    #: docs/protocol.org, Processes). An idle worker exits by itself.
    check(
        r"""
        import pty, select, subprocess
        code = grandchild(HOW, DEATH)
        t0 = None
        if HOW == "terminal":
            pid, fd = pty.fork()
            if pid == 0:
                try:
                    os.execv(sys.executable, [sys.executable, "-c", code])
                finally:
                    os._exit(127)
            buf = b""
            deadline = time.monotonic() + 60
            while b"READY" not in buf:
                left = deadline - time.monotonic()
                assert left > 0 and select.select([fd], [], [], left)[0], buf
                try:
                    buf += os.read(fd, 4096)
                except OSError:
                    raise AssertionError(buf)
            t0 = time.monotonic()
            os.close(fd)  # the terminal goes away: SIGHUP
            _, status = os.waitpid(pid, 0)
            rc = os.waitstatus_to_exitcode(status)
            out = buf.decode(errors="replace")
        else:
            g = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            line = g.stdout.readline()
            assert line == b"READY\n", (line, g.stderr.read())
            t0 = time.monotonic()
            out, err = g.communicate(timeout=60)
            rc = g.returncode
            out = (line + out + err).decode(errors="replace")
        want = {"exit": 0, "exception": 1, "sigterm": -signal.SIGTERM,
                "sigkill": -signal.SIGKILL, "terminal": -signal.SIGHUP}[HOW]
        assert rc == want, (rc, out)
        pids = [int(x) for x in open(os.path.join(SCRATCH, "pids")).read().split()]
        assert len(pids) == 4, pids
        left = left_behind(pids, 20)
        assert not left, (left, out)
        print(HOW, "everything gone after", round(time.monotonic() - t0, 2), "s")
        """,
        setup=GONE + "DEATH = %r\nHOW = %r\n" % (DEATH, how),
        timeout=120,
    )


#: A worker whose reply a KeyboardInterrupt abandoned while its command
#: runs: cleanup() and restart() stop that command, and every process below
#: the worker, before they return.
STALE = r'''
pidfile = os.path.join(SCRATCH, "pids")
W = "zmodload zsh/zselect; zselect -t 6000"
rec = "zmodload zsh/system; print -r -- $sysparams[pid] >> " + pidfile + "; "
cmd, n = (rec + W, 1) if FORK else (rec + "zsh -fc '" + rec + W + "'", 2)
def interrupt_when_running():
    deadline = time.monotonic() + 30
    while not (os.path.exists(pidfile) and len(open(pidfile).read().split()) == n):
        if time.monotonic() > deadline:
            return
        time.sleep(0.05)
    os.kill(os.getpid(), signal.SIGINT)
b = Brish(server_count=2)
threading.Thread(target=interrupt_when_running, daemon=True).start()
try:
    b.send_cmd(cmd, fork=FORK, server_index=0)
    raise SystemExit("no KeyboardInterrupt")
except KeyboardInterrupt:
    pass
pids = [int(x) for x in open(pidfile).read().split()]
assert len(pids) == n and all(alive(p) for p in pids), pids
t = time.monotonic()
if HOW == "cleanup":
    b.cleanup()
else:
    assert b.restart() is True
    r = b.send_cmd("print -r ok", server_index=0)
    assert (r.retcode, r.out) == (0, "ok\n"), repr(r)
    b.cleanup()
dt = time.monotonic() - t
left = left_behind(pids, 5)
assert not left, (HOW, FORK, left)
assert dt < 15, dt
print(HOW, FORK, "took", round(dt, 2), "s")
'''


@pytest.mark.parametrize("how", ["cleanup", "restart"])
@pytest.mark.parametrize("fork", [False, True], ids=["nonfork", "fork"])
def test_cleanup_stops_an_abandoned_command(how, fork):
    check(
        STALE,
        setup=GONE + "HOW = %r\nFORK = %r\n" % (how, fork),
        timeout=90,
    )


#: A Python that ends while it writes a request with a large cmd_stdin:
#: SIGKILLed, or after it catches the KeyboardInterrupt and exits without
#: another Brish call. HOW is "sigkill" or "caught".
CUT = r'''
count = os.path.join(SCRATCH, "count")
signal.signal(signal.SIGINT, signal.default_int_handler)  # also when inherited ignored
b = Brish(server_count=1)
b.send_cmd("true")
def end():
    os.kill(os.getpid(), signal.SIGKILL if HOW == "sigkill" else signal.SIGINT)
threading.Timer(0.4, end).start()
try:
    b.send_cmd("wc -c > " + count + "; print -r done", cmd_stdin=b"y" * (8 << 20))
    print("send_cmd returned", flush=True)
except KeyboardInterrupt:
    print("caught KeyboardInterrupt", flush=True)
    sys.exit(0)
'''


@legacy_only
@pytest.mark.parametrize("how", ["sigkill", "caught"])
def test_a_request_cut_short_never_runs(how):
    #: Python's end closes the request FIFO in the middle of the request.
    #: The legacy worker exits without running it, as a binary worker does
    #: on EOF inside a payload (before, it ran the command on the part of
    #: the stdin that had arrived, after Python was gone: 3 of 3 runs).
    #: Legacy mode reads a request one byte per system call, so 8 MiB take
    #: seconds, and the end at 0.4 s cuts it.
    check(
        r"""
        import subprocess
        g = subprocess.run([sys.executable, "-c", grandchild(HOW, CUT)], capture_output=True, timeout=60)
        out = (g.stdout + g.stderr).decode(errors="replace")
        want = {"sigkill": -signal.SIGKILL, "caught": 0}[HOW]
        assert g.returncode == want, (g.returncode, out)
        assert "send_cmd returned" not in out, out  # the request was cut short
        left = left_behind([], 20)
        assert not left, (left, out)
        count = os.path.join(SCRATCH, "count")
        assert not os.path.exists(count), (open(count).read(), out)
        """,
        setup=GONE + "CUT = %r\nHOW = %r\n" % (CUT, how),
        timeout=120,
    )
