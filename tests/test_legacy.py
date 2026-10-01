"""Legacy mode (brish2.zsh over FIFOs): what it carries exactly since the
backports from binary mode. Every test here is skipped in binary mode, which
has its own, stronger tests in test_binary.py."""

import os

from tests.conftest import check, legacy_only

pytestmark = legacy_only


def test_replies_are_byte_exact():
    #: Anything but a NUL at the start of a line, which can forge the
    #: delimiter (test_defects F3). NUL inside a line is fine.
    check(
        r'''
        import random
        rnd = random.Random(7)
        blob = bytes(rnd.randrange(1, 256) for _ in range(1 << 20))
        payloads = [blob, bytes(range(1, 256)), b"a\r\nb\r", b"\r", b"\r\n", b"x\0y\n",
                    b"\xff\xfe", b"", b"\n", b"\n\n", b"no newline", b"\xc3"]
        b = Brish(server_count=1)
        for i, data in enumerate(payloads):
            path = os.path.join(SCRATCH, f"p{i}")
            with open(path, "wb") as f:
                f.write(data)
            for fork in (False, True):
                r = b.send_cmd(f"cat {path}", fork=fork)
                assert (r.retcode, r.outb, r.errb) == (0, data, b""), (i, fork, repr(r)[:300])
                r = b.send_cmd(f"cat {path} >&2; print -rn -- $'\\r'", fork=fork)
                assert (r.retcode, r.outb, r.errb) == (0, b"\r", data), (i, fork, repr(r)[:300])
        #: Text views are decoded from those bytes, without newline translation.
        r = b.send_cmd("print -rn -- $'a\\r\\nb\\r\\xff'; print -rn -- $'e\\r' >&2")
        assert (r.out, r.err) == ("a\r\nb\r\\xff", "e\r"), repr(r)
        assert (r.outb, r.errb) == (b"a\r\nb\r\xff", b"e\r"), repr(r)
        assert list(r.iterb()) == [b"a\r", b"b\r\xff"], repr(r)
        b.cleanup()
        l1 = Brish(server_count=1, encoding="latin-1")
        r = l1.send_cmd("print -rn -- $'\\xe9\\r\\n'")
        assert (r.out, r.outb) == ("\xe9\r\n", b"\xe9\r\n"), repr(r)
        l1.cleanup()
        ''',
        timeout=120,
    )


def test_stdin_and_command_types():
    check(
        r'''
        import pathlib
        b = Brish(server_count=1)
        res = CmdResult.from_bytes(0, b"\xfe\n\n", b"", "c", "")
        cases = [
            (b"a\xffb", b"a\xffb"),
            (bytearray(b"ba"), b"ba"),
            (memoryview(b"mv"), b"mv"),
            ("é\udcff\r\n", "é".encode() + b"\xff\r\n"),
            (12, b"12"),
            (pathlib.Path("/x y"), b"/x y"),
            (res, b"\xfe"),
            ("", b""),
            (None, b""),
        ]
        for value, want in cases:
            for fork in (False, True):
                r = b.send_cmd("cat", cmd_stdin=value, fork=fork)
                assert (r.retcode, r.outb) == (0, want), (value, fork, repr(r))
            r = b.send_cmd('print -rn -- "$brish_stdin"', cmd_stdin=value)
            assert r.outb == want, (value, repr(r))
        #: Stored stdin: str, bytes and None as passed, bytes-like as bytes,
        #: anything else as str().
        assert b.send_cmd("true", cmd_stdin=None).cmd_stdin is None
        assert b.send_cmd("true", cmd_stdin=b"x").cmd_stdin == b"x"
        assert b.send_cmd("true", cmd_stdin=bytearray(b"x")).cmd_stdin == b"x"
        assert b.send_cmd("true", cmd_stdin="x").cmd_stdin == "x"
        assert b.send_cmd("true", cmd_stdin=5).cmd_stdin == "5"
        #: Commands may be bytes-like, and %BRISH_RESTART matches as bytes.
        r = b.send_cmd(b"print -rn -- $'\\xff'; print -rn \xfe")
        assert r.outb == b"\xff\xfe", repr(r)
        r = b.send_cmd(bytearray(b"print -rn ok"))
        assert r.outb == b"ok", repr(r)
        r = b.send_cmd(b"%BRISH_RESTART")
        assert r.out == "Restarted succesfully.", repr(r)
        #: A NUL cannot travel on the legacy wire: retcode 9000, worker untouched.
        for kw in (dict(cmd="print a\0b"), dict(cmd=b"cat", cmd_stdin=b"a\0b"),
                   dict(cmd="cat", cmd_stdin="a\0b")):
            r = b.send_cmd(**kw)
            assert r.retcode == 9000 and "Illegal input" in r.err, (kw, repr(r))
            assert r.cmd_stdin == kw.get("cmd_stdin", ""), repr(r)
        #: An unencodable str raises before anything is sent.
        try:
            b.send_cmd("cat", cmd_stdin="\ud800")
            raise SystemExit("no error for a lone surrogate")
        except UnicodeEncodeError as e:
            assert "surrogateescape" in str(e), e
        r = b.send_cmd("echo ok")
        assert (r.retcode, r.out) == (0, "ok\n"), repr(r)
        b.cleanup()
        #: latin-1 instances encode str as latin-1.
        l1 = Brish(server_count=1, encoding="latin-1")
        r = l1.send_cmd("cat", cmd_stdin="\xe9")
        assert (r.outb, r.out) == (b"\xe9", "\xe9"), repr(r)
        l1.cleanup()
        ''',
        timeout=60,
    )


def test_interpolation_and_zstring():
    check(
        r'''
        import pathlib
        b = Brish(server_count=1)
        v = b"\xff\0a b'"
        assert b.z("print -rn -- {v}").outb == v
        assert b.z("print -rn -- {bytearray(v)}").outb == v
        assert b.z("print -rn -- {memoryview(v)}").outb == v
        assert b.z("print -rn -- x{None}y").outb == b"xy"
        lst = [b"\xff", "a b", 3, pathlib.Path("/p q")]
        assert b.z("print -rn -- {lst}").outb == b"\xff a b 3 /p q"
        res = CmdResult.from_bytes(0, b"\xfe\n\n", b"", "c", "")
        assert b.z("print -rn -- {res}").outb == b"\xfe"
        os.mkdir(os.path.join(SCRATCH, "d i r"))
        entry = [e for e in os.scandir(SCRATCH) if e.name == "d i r"][0]
        assert b.z("print -rn -- {entry}").outb == os.path.join(SCRATCH, "d i r").encode()
        raw = b"print -rn -- \xff"
        assert b.z("{raw:e}").outb == b"\xff"
        #: The quoted text is ASCII for bytes that are not printable UTF-8.
        b.zstring("{v}").encode("ascii")
        #: Template literal text keeps CR, NUL and lone surrogates; a NUL in
        #: the command is then refused by the wire.
        s = b.zstring("print -rn -- $'x'\r\0\udcff")
        assert s == " print -rn -- $'x'\r\0\udcff ", repr(s)
        assert b.z("print -rn -- 'a\rb'").outb == b"a\rb"
        assert b.z("print -rn -- 'a\udcffb'").outb == b"a\xffb"
        assert b.z("print -rn -- a\0b").retcode == 9000
        b.cleanup()
        ''',
        timeout=60,
    )


def test_every_byte_through_a_worker():
    check(
        r'''
        b = Brish(server_count=1)
        suffixes = ("", "'", '"', "\\", "$", " ;x")
        bad = []
        for i in range(256):
            for s in suffixes:
                v = bytes([i]) + s.encode()
                if v == b"\0":
                    continue  # a lone NUL line is the reply delimiter (F3)
                r = b.z("print -rn -- {v}")
                if (r.retcode, r.outb) != (0, v):
                    bad.append((v, repr(r)))
        assert not bad, (len(bad), bad[:3])
        b.cleanup()
        ''',
        timeout=120,
    )


def test_zp_passes_bytes_through(tmp_path):
    from tests.conftest import run_py

    #: NUL-free, so that no output line can start with the delimiter.
    data = bytes(range(1, 256)) * 50 + os.urandom(100_000).replace(b"\0", b"")
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


def test_loop_control_and_exit():
    #: A command's break N or continue N cannot escape the worker loop, and
    #: exit is answered with its status; the instance restarts afterwards.
    check(
        r'''
        b = Brish(server_count=1)
        for cmd, out in (("print -r a; break 2; print -r no", "a\n"),
                         ("print -r b; continue 2; print -r no", "b\n"),
                         ("print -r c; continue 3; print -r no", "c\n"),
                         ("print -r d; break 3; print -r no", "d\n"),
                         ("print -r e; exit 4", "e\n"),
                         #: POSIX_TRAPS (part of `emulate sh`) changes when zsh
                         #: runs EXIT traps.
                         ("print -r f; f() { emulate sh; }; f; exit 4", "f\n"),
                         ("print -r g; setopt posixtraps; g() { exit 4; }; g", "g\n")):
            r = b.send_cmd(cmd)
            want = 4 if "exit" in cmd else 0
            assert (r.retcode, r.out, r.err) == (want, out, ""), (cmd, repr(r))
            r = b.send_cmd("echo ok")
            assert (r.retcode, r.out) == (0, "ok\n"), (cmd, repr(r))
        b.cleanup()
        ''',
        timeout=60,
    )


#: Prints "LEAK n" for every open fd of a fresh process that is this
#: worker's request FIFO, then "checked".
FD_PROBE = (
    r"command zsh -fc 'zmodload zsh/stat; want=$(zstat +device -- $1):$(zstat +inode -- $1); "
    r"for f in {0..255}; do x=$(zstat -f $f +device 2>/dev/null):$(zstat -f $f +inode 2>/dev/null); "
    r"[[ $x == $want ]] && print -r LEAK $f; done; print -r checked' zsh $stdins[$brish_server_index]"
)


def test_commands_never_inherit_the_request_fifo():
    #: The worker reads requests on its fd 0. A command that could read it
    #: would eat the next request, and a background job that holds it would
    #: keep a dead worker's FIFO open.
    check(
        f'''
        PROBE = {FD_PROBE!r}
        b = Brish(server_count=1)
        shapes = ["{{p}}", "{{{{ {{p}} }}}} &; wait", "( {{p}} ) &; wait", "print -r -- $({{p}})",
                  "cat <({{p}})", "{{p}} | cat", "exec 3<&0; {{p}}"]
        for fork in (False, True):
            for stdin in ("", "x", None):
                for shape in shapes:
                    r = b.send_cmd(shape.format(p=PROBE), fork=fork, cmd_stdin=stdin)
                    assert (r.retcode, r.out, r.err) == (0, "checked\\n", ""), (fork, stdin, shape, repr(r))
        #: The probe itself works.
        r = b.send_cmd(PROBE + " 9<$stdins[$brish_server_index]")
        assert r.out == "LEAK 9\\nchecked\\n", repr(r)
        b.cleanup()
        ''',
        timeout=120,
    )


def test_hostile_user_state():
    #: The legacy counterpart of test_binary's G10: state a command leaves
    #: behind must not break the worker loop or its framing.
    check(
        r'''
        def normal(b, why):
            r = b.send_cmd("\\builtin \\print -r ok; \\builtin \\print -ru2 e", cmd_stdin=b"in")
            assert (r.retcode, r.outb, r.errb) == (0, b"ok\n", b"e\n"), (why, repr(r))
            r = b.send_cmd("cat", cmd_stdin=b"x\xff")
            assert (r.retcode, r.outb) == (0, b"x\xff"), (why, repr(r))
            r = b.send_cmd("cat")
            assert (r.retcode, r.outb) == (0, b""), (why, repr(r))
            r = b.send_cmd("\\builtin \\print -r forked", fork=True)
            assert (r.retcode, r.outb) == (0, b"forked\n"), (why, repr(r))

        hostile = [
            "print() { echo SHADOW; }; read() { echo SHADOW; }; eval() { echo SHADOW; }",
            "exec() { echo SHADOW; }; trap() { echo SHADOW; }; typeset() { echo SHADOW; }",
            "true() { echo SHADOW; }; test() { echo SHADOW; }; unsetopt() { echo SHADOW; }",
            "zselect() { echo SHADOW; }; zmodload() { echo SHADOW; }; break() { echo SHADOW; }",
            "unsetopt aliases; alias -g print=SHADOW builtin=SHADOW read=SHADOW; setopt aliases",
            "MARKER=x cmd=x brish_stdin=x brish_fork=x",
            "set -- x y z",
            "setopt ksharrays",
            "setopt shwordsplit",
            "setopt nomultibyte",
            "setopt nounset",
            "setopt pipefail",
            "emulate sh",
            "emulate ksh",
            "IFS=:",
            "trap ': $((++__dbg))' DEBUG",
            "trap 'return 1' ZERR",
            "setopt errreturn",
            "setopt errexit; true",
            "exec >/dev/null 2>&1",
        ]
        for h in hostile:
            b = Brish(server_count=1)
            r = b.send_cmd(h)
            normal(b, h)
            normal(b, h)
            #: exit is still answered with its status.
            r = b.send_cmd("\\builtin \\print -r bye; \\builtin exit 3")
            assert (r.retcode, r.outb) == (3, b"bye\n"), (h, repr(r))
            b.cleanup()
        #: A ZERR trap that prints must not fire for worker internals.
        b = Brish(server_count=1)
        b.send_cmd("trap 'builtin print -ru2 ZERR-FIRED' ZERR")
        normal(b, "zerr-print")
        r = b.send_cmd("false")
        assert r.retcode == 1 and r.errb == b"ZERR-FIRED\n", repr(r)
        normal(b, "zerr-print after false")
        b.cleanup()
        ''',
        timeout=180,
    )


def test_startup_files_cannot_break_the_worker(tmp_path):
    #: Global aliases and functions that shadow builtins, defined by the
    #: startup files, are in place before the worker script is parsed.
    zdot = tmp_path / "zdot"
    zdot.mkdir()
    (zdot / ".zshenv").write_text(
        "alias zz='echo aliased'\n"
        "alias -g read=SHADOW_R print=SHADOW_P typeset=SHADOW_T MARKER=SHADOW_M\n"
        "print() { echo SHADOW; }; read() { echo SHADOW; }; exec() { echo SHADOW; }\n"
        "trap() { echo SHADOW; }; wait() { echo SHADOW; }; eval() { echo SHADOW; }\n"
    )
    check(
        r'''
        b = Brish(server_count=2)
        for i in (0, 1):
            r = b.send_cmd('\\builtin \\print -r -- $brish_server_index', server_index=i)
            assert (r.retcode, r.out, r.err) == (0, f"{i + 1}\n", ""), repr(r)
        r = b.send_cmd("zz")
        assert r.out == "aliased\n", repr(r)
        #: The argv reservation is kept, so tools can rewrite the process title.
        r = b.send_cmd("zmodload zsh/system; command ps -ww -o command= -p $sysparams[pid]")
        assert "BR" + "I" * 2048 + "SH" in r.out, r.out[:200]
        r = b.send_cmd("cat", cmd_stdin="in")
        assert r.out == "in", repr(r)
        r = b.send_cmd("\\builtin exit 4")
        assert r.retcode == 4, repr(r)
        r = b.send_cmd("echo next")
        assert r.out == "next\n", repr(r)
        b.cleanup()
        ''',
        env={"ZDOTDIR": str(zdot)},
        timeout=60,
    )
