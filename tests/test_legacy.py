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
                         ("print -r e; exit 4", "e\n")):
            r = b.send_cmd(cmd)
            want = 4 if "exit" in cmd else 0
            assert (r.retcode, r.out, r.err) == (want, out, ""), (cmd, repr(r))
            r = b.send_cmd("echo ok")
            assert (r.retcode, r.out) == (0, "ok\n"), (cmd, repr(r))
        b.cleanup()
        ''',
        timeout=60,
    )
