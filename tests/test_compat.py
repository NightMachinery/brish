"""G11: compatibility of the public surface."""

import copy
import dataclasses
import json
import pickle

import pytest

from brish.brishmod import CmdResult
from tests.conftest import check

#: brish2.zsh itself is checked against the original worker, reply by reply,
#: in test_wire_compat.py.


def test_five_fields_and_repr():
    r = CmdResult(0, "o\n", "e", "cmd", "in")
    assert [f.name for f in dataclasses.fields(r)] == ["retcode", "out", "err", "cmd", "cmd_stdin"]
    assert repr(r) == "CmdResult(retcode=0, out='o\\n', err='e', cmd='cmd', cmd_stdin='in')"
    b = CmdResult.from_bytes(0, b"o\n", b"e", "cmd", "in")
    assert repr(b) == repr(r)
    assert dataclasses.asdict(b) == {"retcode": 0, "out": "o\n", "err": "e", "cmd": "cmd", "cmd_stdin": "in"}
    assert json.loads(json.dumps(dataclasses.asdict(b)))["out"] == "o\n"
    with pytest.raises(dataclasses.FrozenInstanceError):
        b.out = "x"


def test_byte_views():
    raw = b"a\r\n\xff\0b\n\n"
    r = CmdResult.from_bytes(3, raw, b"\xfe", "c", b"in")
    assert r.outb == raw and r.errb == b"\xfe"
    assert r.out == "a\r\n\\xff\x00b\n\n" and r.err == "\\xfe"
    assert r.outrsb == b"a\r\n\xff\0b"
    assert r.outerrb == raw + b"\xfe"
    assert list(r.iterb()) == [b"a\r", b"\xff\0b"]
    assert list(r.iter0b()) == [b"a\r\n\xff", b"b\n\n"]
    assert r.cmd_stdin == b"in"
    #: Mirrors of the text helpers, including one empty item for no output.
    e = CmdResult.from_bytes(0, b"", b"", "c", "")
    assert list(e.iterb()) == [b""] and list(e) == [""]
    assert list(e.iter0b()) == [b""] and list(e.iter0()) == [""]
    #: Other encodings decode eagerly; bytes stay exact.
    l1 = CmdResult.from_bytes(0, b"\xe9", b"", "c", "", encoding="latin-1", errors="strict")
    assert l1.out == "\xe9" and l1.outb == b"\xe9"


def test_text_results_have_derived_byte_views():
    r = CmdResult(0, "é\udcff", "e\ud800", "c", "")
    assert r.outb == "é".encode() + b"\xff"
    #: A lone surrogate that surrogateescape cannot encode falls back.
    assert r.errb == b"e\\ud800"


def test_positional_bytes_and_bytes_like_stdin():
    r = CmdResult(0, b"o\xff", bytearray(b"e"), "c", bytearray(b"s"))
    assert (r.out, r.outb, r.err, r.errb) == ("o\\xff", b"o\xff", "e", b"e")
    assert r.cmd_stdin == b"s" and type(r.cmd_stdin) is bytes
    m = CmdResult(0, "", "", b"cmd", memoryview(b"xy"))
    assert m.cmd_stdin == b"xy"
    assert "cmd_stdin:\nxy" in m.longstr and "cmd: cmd" in m.longstr


def test_eq_hash_pickle_copy_replace():
    a = CmdResult.from_bytes(0, b"x\xff", b"", "c", "")
    b = CmdResult.from_bytes(0, b"x\xff", b"", "c", "")
    c = CmdResult(0, "x\\xff", "", "c", "")  # same text, different bytes
    assert a == b and hash(a) == hash(b)
    assert a != c and hash(a) == hash(c)
    for clone in (pickle.loads(pickle.dumps(a)), copy.copy(a), copy.deepcopy(a)):
        assert clone == a and clone.outb == b"x\xff"
    r = dataclasses.replace(a, out="new")
    assert r.out == "new" and r.outb == b"new", "replace() must not keep stale bytes"
    assert a.outb == b"x\xff"


def test_jupyter_style_subclass():
    class JupyterResult(CmdResult):
        def __init__(self, *args, out_data=None, err_data=None, extra_data=None):
            super().__init__(*args)
            self.out_data = out_data
            self.err_data = err_data
            self.extra_data = extra_data

    j = JupyterResult(0, "o", "e", "c", "", out_data=[1])
    assert j.out_data == [1] and j.outb == b"o" and str(j) == "o"
    assert j.longstr.startswith("\ncmd: c")


def test_legacy_text_api_is_unchanged():
    r = CmdResult(1, "a\nb\n\n", "err", ["x"], "in")
    assert r.outrs == "a\nb" and list(r) == ["a", "b"] and str(r) == "a\nb"
    assert r.outerr == "a\nb\n\nerr" and r.summary == (1, "a\nb\n\n", "err")
    assert not r
    with pytest.raises(Exception, match="retcode=1"):
        r.assert_zero


def test_legacy_wire_bytes_are_unchanged():
    #: Record what legacy mode writes to the request FIFO and compare it with
    #: the v1 frame: cmd NUL stdin NUL fork NUL newline.
    check(
        r'''
        if BINARY:
            raise SystemExit(0)
        b = Brish(server_count=1)
        f = b.p.brish_stdins[0]
        seen = []
        class Rec:
            def write(self, s):
                seen.append(s)
                return f.write(s)
            def flush(self):
                return f.flush()
        b.p.brish_stdins[0] = Rec()
        r = b.send_cmd("cat", cmd_stdin="in")
        r2 = b.send_cmd("print -r x", fork=True)
        assert r.out == "in" and r2.out == "x\n", (r, r2)
        assert "".join(seen) == "cat\0in\0\0\nprint -r x\0\0y\0\n", seen
        b.p.brish_stdins[0] = f
        b.cleanup()
        ''',
        timeout=30,
    )
