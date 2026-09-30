"""Binary-mode goals: G1 to G5, G10, G13, G14, and the worker-side contracts
of G11. Every test here is skipped in legacy mode."""

import os
import subprocess

import pytest

from brish.brishmod import _StreamParser, _parse_trailer
from tests.conftest import ROOT, binary_only, check, run_py

pytestmark = binary_only


def test_parser_at_every_split():
    nonce = b"0123456789abcdef0123456789abcdef"
    start = b"\0BRISH3-START:" + nonce + b"\n"
    end = b"\0BRISH3-END:" + nonce + b":"
    payload = (
        b"x\0\n"
        + b"\0BRISH3-END:" + b"f" * 32 + b":0\n"  # wrong nonce
        + b"\0BRISH3-START:" + nonce[:-1] + b"\n"  # truncated start
        + bytes(range(256))
        + b"\0BRISH3-END:" + nonce  # prefix without the colon
    )
    stream = b"garbage\0BRISH3-ST" + start + payload + end + b"42:exit\n" + b"trailing" + start
    for cut in range(len(stream) + 1):
        for chunks in ([stream[:cut], stream[cut:]],):
            st = _StreamParser(start, end)
            for c in chunks:
                st.feed(c)
            assert st.done and st.payload() == payload and st.trailer == b"42:exit", cut
    st = _StreamParser(start, end)
    for i in range(len(stream)):
        st.feed(stream[i : i + 1])
    assert st.done and st.payload() == payload
    #: NUL-free chunks skip the END search; the END that follows them must
    #: still be found when it is split at any point.
    plain = b"x" * 1000
    stream = start + plain + end + b"0\n"
    for size in range(1, len(end) + 3):
        st = _StreamParser(start, end)
        for i in range(0, len(stream), size):
            st.feed(stream[i : i + size])
        assert st.done and st.payload() == plain and st.trailer == b"0", size
    assert _parse_trailer(b"42:exit") == (42, True)
    assert _parse_trailer(b"0") == (0, False)
    assert _parse_trailer(b"x") == (9001, True)


def test_g1_byte_exact_output():
    check(
        r'''
        b = Brish(server_count=1)
        payloads = [bytes(range(256)), b"\0", b"a\n\0", b"a\r\0", b"\0\n5\nabc",
                    b"a\r\0\r8\rb", b"\r", b"\r\n", b"\n\0\n0\n", os.urandom(1 << 20),
                    b"", b"\n", b"\0BRISH3-END:" + b"0" * 32 + b":0\n"]
        for data in payloads:
            for fork in (False, True):
                r = b.send_cmd("cat", cmd_stdin=data, fork=fork)
                assert (r.retcode, r.outb, r.errb) == (0, data, b""), (fork, data[:40], repr(r)[:300])
                r = b.send_cmd("cat >&2", cmd_stdin=data, fork=fork)
                assert (r.retcode, r.outb, r.errb) == (0, b"", data), (fork, data[:40], repr(r)[:300])
        #: Text views are exact: no newline translation.
        r = b.send_cmd("cat", cmd_stdin=b"a\r\nb\r\xff")
        assert r.out == "a\r\nb\r\\xff", repr(r)
        b.cleanup()
        ''',
        timeout=60,
    )


def test_g2_stdin_types():
    check(
        r'''
        import pathlib
        b = Brish(server_count=1)
        cases = [
            (b"a\0b", b"a\0b"),
            (bytearray(b"ba"), b"ba"),
            (memoryview(b"mv"), b"mv"),
            ("é\udcff", "é".encode() + b"\xff"),
            (12, b"12"),
            (pathlib.Path("/x y"), b"/x y"),
            ("", b""),
        ]
        for value, want in cases:
            r = b.send_cmd("cat", cmd_stdin=value)
            assert r.outb == want, (value, repr(r))
            r = b.send_cmd('print -rn -- "$brish_stdin"', cmd_stdin=value)
            assert r.outb == want, (value, repr(r))
        #: None is /dev/null; "" is the default empty pipe.
        r = b.send_cmd("[[ -p /dev/stdin ]] && print pipe; [[ -c /dev/stdin ]] && print chr; cat", cmd_stdin=None)
        assert (r.out, r.cmd_stdin) == ("chr\n", None), repr(r)
        r = b.send_cmd("[[ -p /dev/stdin ]] && print pipe; cat")
        assert r.out == "pipe\n", repr(r)
        r = b.send_cmd("[[ -c /dev/stdin ]] && print chr", cmd_stdin=None, fork=True)
        assert r.out == "chr\n", repr(r)
        #: Stored stdin: str and bytes as passed, bytes-like as bytes, others str().
        assert b.send_cmd("true", cmd_stdin=b"x").cmd_stdin == b"x"
        assert b.send_cmd("true", cmd_stdin=bytearray(b"x")).cmd_stdin == b"x"
        assert b.send_cmd("true", cmd_stdin="x").cmd_stdin == "x"
        assert b.send_cmd("true", cmd_stdin=5).cmd_stdin == "5"
        #: An unencodable str raises before anything is sent.
        try:
            b.send_cmd("cat", cmd_stdin="\ud800")
            raise SystemExit("no error for a lone surrogate")
        except UnicodeEncodeError as e:
            assert "surrogateescape" in str(e), e
        r = b.send_cmd("echo ok")
        assert r.out == "ok\n", repr(r)
        #: latin-1 instances encode str stdin as latin-1.
        l1 = Brish(server_count=1, encoding="latin-1")
        r = l1.send_cmd("cat", cmd_stdin="\xe9")
        assert (r.outb, r.out) == (b"\xe9", "\xe9"), repr(r)
        l1.cleanup()
        b.cleanup()
        ''',
        timeout=60,
    )


def test_g3_command_text():
    check(
        r'''
        import pathlib
        b = Brish(server_count=1)
        #: (a) bytes-like commands, and %BRISH_RESTART after normalisation.
        r = b.send_cmd(b"print -rn -- $'\\xff'; print -rn \xfe")
        assert r.outb == b"\xff\xfe", repr(r)
        r = b.send_cmd(bytearray(b"print -rn ok"))
        assert r.outb == b"ok", repr(r)
        r = b.send_cmd(b"%BRISH_RESTART")
        assert r.out == "Restarted succesfully.", repr(r)
        #: (b) interpolation.
        v = b"\xff\0a b'"
        assert b.z("print -rn -- {v}").outb == v
        assert b.z("print -rn -- {bytearray(v)}").outb == v
        assert b.z("print -rn -- {memoryview(v)}").outb == v
        assert b.z("print -rn -- x{None}y").outb == b"xy"
        lst = [b"\xff", "a b", 3, pathlib.Path("/p q")]
        assert b.z("print -rn -- {lst}").outb == b"\xff a b 3 /p q"
        res = CmdResult.from_bytes(0, b"\xfe\n\n", b"", "c", "")
        assert b.z("print -rn -- {res}").outb == b"\xfe"
        assert b.z("print -rn -- {pathlib.Path('/a b')}").outb == b"/a b"
        os.mkdir(os.path.join(SCRATCH, "d i r"))
        entry = [e for e in os.scandir(SCRATCH) if e.name == "d i r"][0]
        assert b.z("print -rn -- {entry}").outb == os.path.join(SCRATCH, "d i r").encode()
        raw = b"print -rn -- \xff"
        assert b.z("{raw:e}").outb == b"\xff"
        s = b.zstring("{v}")
        s.encode("utf-8")  # quoted text is surrogate-free
        #: (c) template literal text keeps CR, NUL and lone surrogates.
        s = b.zstring("print -rn -- $'x'\r\0\udcff")
        assert s == " print -rn -- $'x'\r\0\udcff ", repr(s)
        r = b.z("print -rn -- 'a\rb'")
        assert r.outb == b"a\rb", repr(r)
        r = b.z("print -rn -- 'a\udcffb'")
        assert r.outb == b"a\xffb", repr(r)
        b.cleanup()
        ''',
        timeout=60,
    )


def test_g4_every_byte_through_a_worker():
    check(
        r'''
        b = Brish(server_count=1)
        suffixes = ("", "'", '"', "\\", "$", " ;x")
        bad = []
        for i in range(256):
            for s in suffixes:
                v = bytes([i]) + s.encode()
                r = b.z("print -rn -- {v}")
                if (r.retcode, r.outb) != (0, v):
                    bad.append((v, repr(r)))
        assert not bad, (len(bad), bad[:3])
        b.cleanup()
        ''',
        timeout=120,
    )


def test_g5_unforgeable_framing():
    check(
        r'''
        b = Brish(server_count=1)
        fake_end = "$'\\0BRISH3-END:'" + "0" * 32 + ":0$'\\n'"
        fake_start = "$'\\0BRISH3-START:'" + "0" * 32 + "$'\\n'"
        cases = [
            ("print -rn -- $'\\0'", b"\0", b""),
            ("print -rn -- $'a\\n\\0\\n5\\nabc'", b"a\n\0\n5\nabc", b""),
            ("print -rn -- $'\\n\\0\\n0\\n' >&2", b"", b"\n\0\n0\n"),
            ("print -rn -- " + fake_end + "; print -rn -- " + fake_end + " >&2", None, None),
            ("print -rn -- " + fake_start + fake_end, None, None),
            ("print -rn -- $'\\0BRISH3-END:'$BRISH3_NONCE:5$'\\n'", None, None),
        ]
        for cmd, out, err in cases:
            r = b.send_cmd(cmd)
            assert r.retcode == 0, (cmd, repr(r))
            if out is not None:
                assert (r.outb, r.errb) == (out, err), (cmd, repr(r))
            else:
                assert b"BRISH3-END" in r.outb, (cmd, repr(r))
            r2 = b.send_cmd("print -r second; print -ru2 err2; (exit 4)")
            assert (r2.retcode, r2.outb, r2.errb) == (4, b"second\n", b"err2\n"), (cmd, repr(r2))
        #: Output written between requests is discarded before START.
        r = b.send_cmd("{ sleep 0.3; print -r late; print -ru2 late2 } &")
        assert r.retcode == 0 and r.outb == b"", repr(r)
        time.sleep(0.8)
        r2 = b.send_cmd("print -r next")
        assert (r2.retcode, r2.outb, r2.errb) == (0, b"next\n", b""), repr(r2)
        #: A command cannot read the nonce.
        r = b.send_cmd('print -rn -- "$BRISH3_NONCE"')
        assert r.outb == b"", repr(r)
        r = b.send_cmd('print -rn -- "$BRISH3_NONCE"', fork=True)
        assert r.outb == b"", repr(r)
        r = b.send_cmd('print -rn -- "$*"', fork=True)
        assert r.outb == b"", repr(r)
        b.cleanup()
        ''',
        timeout=60,
    )


def test_g10_hostile_user_state():
    check(
        r'''
        def fresh():
            return Brish(server_count=1)

        def normal(b, why):
            r = b.send_cmd("\\builtin \\print -r ok; \\builtin \\print -ru2 e", cmd_stdin=b"in")
            assert (r.retcode, r.outb, r.errb) == (0, b"ok\n", b"e\n"), (why, repr(r))
            r = b.send_cmd("cat", cmd_stdin=b"\0x\xff")
            assert (r.retcode, r.outb) == (0, b"\0x\xff"), (why, repr(r))
            r = b.send_cmd("cat")
            assert (r.retcode, r.outb) == (0, b""), (why, repr(r))

        hostile = [
            "print() { echo SHADOW; }; read() { echo SHADOW; }; eval() { echo SHADOW; }",
            "sysread() { echo SHADOW; }; syswrite() { echo SHADOW; }; true() { echo SHADOW; }",
            "unsetopt aliases; alias -g print=SHADOW builtin=SHADOW; setopt aliases",
            "BRISH3_NONCE=x BRISH3_RET=x BRISH3_FORK=x BRISH3_CMD=x BRISH3_INREQ=x BRISH3_STDIN_MODE=x cmd=x brish_stdin=x",
            "set -- x y z",
            "setopt ksharrays",
            "setopt shwordsplit",
            "setopt nomultibyte",
            "setopt nounset",
            "emulate sh",
            "emulate ksh",
            "IFS=:",
            "trap ': $((++__dbg))' DEBUG",
            "trap 'return 1' ZERR",
            "setopt errreturn",
            "setopt errexit; true",
        ]
        for h in hostile:
            b = fresh()
            r = b.send_cmd(h)
            normal(b, h)
            normal(b, h)
            b.cleanup()
        #: exec >/dev/null while a background job holds the stream.
        b = fresh()
        r = b.send_cmd("{ sleep 0.3; builtin print -r late } & exec >/dev/null 2>&1; builtin print hidden")
        assert r.outb == b"", repr(r)
        time.sleep(0.6)
        normal(b, "exec")
        b.cleanup()
        #: A ZERR trap that prints must not fire for worker internals.
        b = fresh()
        b.send_cmd("trap 'builtin print -ru2 ZERR-FIRED' ZERR")
        normal(b, "zerr-print")
        r = b.send_cmd("false")
        assert r.retcode == 1 and b"ZERR-FIRED" in r.errb, repr(r)
        normal(b, "zerr-print after false")
        b.cleanup()
        ''',
        timeout=120,
    )


def test_g11_worker_side_contracts(tmp_path):
    zdot = tmp_path / "zdot"
    zdot.mkdir()
    #: The worker is a script with a zsh shebang, so startup files load.
    (zdot / ".zshenv").write_text("typeset -g BRISH_TEST_ZSHENV=loaded\nalias zz='print -r aliased'\n")
    check(
        r'''
        b = Brish(server_count=2)
        for i in (0, 1):
            r = b.send_cmd('print -r -- $brish_server_index $BRISH_TEST_ZSHENV', server_index=i)
            assert r.out == f"{i + 1} loaded\n", repr(r)
        r = b.send_cmd('print -rn -- "$cmd"')
        assert r.out == 'print -rn -- "$cmd"', repr(r)
        r = b.send_cmd('print -rn -- "$brish_stdin"', cmd_stdin=b"\0s")
        assert r.outb == b"\0s", repr(r)
        #: Non-fork commands run in tmp_block_8182782, so `return` works.
        r = b.send_cmd("print -r $funcstack[1]; return 5; print -r unreachable")
        assert (r.retcode, r.out) == (5, "tmp_block_8182782\n"), repr(r)
        #: Aliases from the startup files still work in commands.
        r = b.send_cmd("zz")
        assert r.out == "aliased\n", repr(r)
        #: The argv reservation is kept, so tools can rewrite the process title.
        r = b.send_cmd("ps -ww -o command= -p $sysparams[pid]")
        assert "BR" + "I" * 2048 + "SH" in r.out, r.out[:200]
        #: State persists between non-fork requests, not after fork ones.
        b.send_cmd("x=1", server_index=0)
        b.send_cmd("x=2", server_index=0, fork=True)
        assert b.send_cmd("print -r $x", server_index=0).out == "1\n"
        b.cleanup()
        ''',
        env={"ZDOTDIR": str(zdot)},
        timeout=60,
    )


def test_g13_zp_passes_bytes_through(tmp_path):
    data = bytes(range(256)) * 50 + os.urandom(100_000)
    f = tmp_path / "blob"
    f.write_bytes(data)
    res = run_py(
        f"""
        from brish import zp
        path = {str(f)!r}
        zp("cat {{path}}")
        """,
        timeout=60,
    )
    assert res.rc == 0, res
    assert res.outb == data
    res = run_py(
        r'''
        import io, sys
        from brish import zp, zpe
        print('a'); zp('printf b'); print('c')
        v = b"\xff"
        zpe("print -rn -- {v}; print -rn e >&2")
        s = io.StringIO()
        w = b"x\xffy"
        brish.bsh.zp("print -rn -- {w}", file=s)
        assert s.getvalue() == "x\\xffy", repr(s.getvalue())
        ''',
        timeout=60,
    )
    assert res.rc == 0, res
    assert res.outb == b"a\nbc\n", res
    assert res.errb == b"\xffe", res


def test_g14_old_brishmod_still_works():
    #: A long-running process may still have the pre-binary brishmod.py
    #: loaded while this tree is on disk. It spawns brish2.zsh by path.
    old = subprocess.run(
        ["git", "-C", str(ROOT), "show", "9599fc3:brish/brishmod.py"],
        capture_output=True,
    )
    if old.returncode != 0:
        pytest.skip("git history not available")
    check(
        f"""
        import importlib.util, types
        src = {old.stdout.decode()!r}
        mod = types.ModuleType("brish.brishmod_v1")
        mod.__file__ = os.path.join(ROOT, "brish", "brishmod.py")
        exec(compile(src, mod.__file__, "exec"), mod.__dict__)
        b = mod.Brish(server_count=1)
        assert b.defaultShell[0].endswith("brish2.zsh"), b.defaultShell
        r = b.send_cmd("print -r ok; print -ru2 e; (exit 2)")
        assert (r.retcode, r.out, r.err) == (2, "ok\\n", "e\\n"), r
        b.cleanup()
        """,
        timeout=60,
    )


def test_g14_v1_frame_makes_the_worker_exit_without_running_it(tmp_path):
    sentinel = tmp_path / "ran"
    check(
        rf'''
        import subprocess
        worker = os.path.join(ROOT, "brish", "brish3.zsh")
        req_r, req_w = os.pipe()
        out_r, out_w = os.pipe()
        err_r, err_w = os.pipe()
        p = subprocess.Popen([worker, "--", "BRISH3-FDS", f"{{req_r}},{{out_w}},{{err_w}}"],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             pass_fds=(req_r, out_w, err_w))
        for fd in (req_r, out_w, err_w):
            os.close(fd)
        #: What v1 Python would write: cmd NUL stdin NUL fork NUL newline.
        frame = b"print -rn RAN > {sentinel}\0\0\0\n"
        os.write(req_w, frame)
        hello = os.read(out_r, 100)
        assert hello.startswith(b"\0BRISH3-HELLO:"), hello
        rest = b""
        while True:
            c = os.read(out_r, 4096)
            if not c:
                break
            rest += c
        err = b""
        while True:
            c = os.read(err_r, 4096)
            if not c:
                break
            err += c
        assert b"bad request header" in err, err
        assert not os.path.exists({str(sentinel)!r}), "the v1 frame was executed"
        p.stdin.close()
        assert p.wait(timeout=10) == 0
        #: A bootstrap started the v1 way (no fd triples) refuses to start.
        p = subprocess.run([worker, "--", "BRIIISH"], input=b"a\0b\0c\0\n",
                           capture_output=True, timeout=20)
        assert p.returncode == 64 and b"BRISH3-FDS" in p.stderr, p
        ''',
        timeout=60,
    )


def test_g14_worker_without_hello_is_refused():
    check(
        r'''
        brish2 = os.path.join(ROOT, "brish", "brish2.zsh")
        t = time.time()
        try:
            Brish(binary=True, shell=[brish2, "--", "BRIIISH"], startup_timeout=2)
            raise SystemExit("no exception")
        except bm.BrishWorkerDiedException as e:
            assert "not a BRISH3 worker" in str(e), e
        assert time.time() - t < 10
        try:
            Brish(binary=True, shell=["/bin/sleep", "30"], startup_timeout=1)
            raise SystemExit("no exception")
        except bm.BrishWorkerDiedException as e:
            assert "not a BRISH3 worker" in str(e), e
        ''',
        timeout=60,
    )


def test_g7_death_is_noticed_while_a_child_holds_the_pipes():
    #: `sleep` keeps the response pipes open after the worker dies, so no EOF
    #: arrives; the liveness check notices the death instead.
    check(
        r'''
        b = Brish(server_count=1)
        pid = int(b.send_cmd("print -rn -- $sysparams[pid]").out)
        threading.Timer(0.3, lambda: os.kill(pid, signal.SIGKILL)).start()
        t = time.time()
        r = b.send_cmd("print -r started; sleep 3; print -r finished")
        dt = time.time() - t
        assert dt < 2.5, dt
        assert (r.retcode, r.out) == (9001, "started\n"), repr(r)
        assert r.err.endswith("brish: worker died during this command\n"), repr(r)
        r = b.send_cmd("echo ok")
        assert r.out == "ok\n", repr(r)
        b.cleanup()
        time.sleep(3)  # let the orphaned sleep finish
        ''',
        timeout=60,
    )


def test_g8_stale_workers_are_avoided():
    check(
        r'''
        b = Brish(server_count=2)
        threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGINT)).start()
        try:
            b.send_cmd("sleep 2; print -r late", server_index=0)
            raise SystemExit("no KeyboardInterrupt")
        except KeyboardInterrupt:
            pass
        t = time.time()
        r = b.send_cmd("print -r quick")
        assert r.out == "quick\n" and time.time() - t < 1, (time.time() - t, repr(r))
        #: The stale worker resynchronises: its next reply is its own.
        r = b.send_cmd("print -r own", server_index=0)
        assert (r.retcode, r.out) == (0, "own\n"), repr(r)
        b.cleanup()
        ''',
        timeout=60,
    )
