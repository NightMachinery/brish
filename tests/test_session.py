"""Workers run in a session of their own (setsid).

No terminal signal and no signal to the caller's process group reaches a
worker or its commands, which have no controlling terminal either. Python
still gets its KeyboardInterrupt; BrishPopen.kill() is the only interrupt.
Every test runs in both modes. run_py starts each child in a session of its
own, so signalling the child's process group touches nothing else.

The commands wait with zselect (a builtin), not with sleep: they must not
depend on an external program that something else might signal.
"""

from tests.conftest import check

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
    #: so, opening /dev/tty fails at once (ENXIO), and a password prompt
    #: that wants the terminal falls back to stdin instead of waiting.
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
        assert "OPEN \"OSError: [Errno 6] Device not configured: '/dev/tty'\\n\"" in out or "OPEN \"OSError: [Errno 6] No such device or address: '/dev/tty'\\n\"" in out, out
        assert "GETPASS 'secret\\n' 0 True\n" in out, out
        assert "KBI\n" in out and "NO-KBI" not in out, out  # after the echoed ^C
        assert "RESULT 0 'x\\nafter-kept\\n' ''\n" in out, out
        assert "STATE 'kept\\n'\n" in out, out
        '''.format(wait=WAIT),
        timeout=120,
    )
