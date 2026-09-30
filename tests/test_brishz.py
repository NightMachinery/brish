"""E9: brish.brishz with a fake `brishzq.zsh` first on PATH. The real client
is never run."""

import os
import stat

from tests.conftest import check

FAKE = r"""#!/bin/sh
# Test double for the brishzq.zsh client: reports how it was called.
printf 'FAKE-CLIENT in=%s binary=%s args=' "$brishz_in" "${brishz_binary-unset}" >&2
for a in "$@"; do printf '[%s]' "$a" >&2; done
cat
exit 3
"""


def fake_client(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    f = d / "brishzq.zsh"
    f.write_text(FAKE)
    f.chmod(f.stat().st_mode | stat.S_IXUSR)
    return {"PATH": f"{d}{os.pathsep}{os.environ['PATH']}", "brishz_binary": None}


def test_brishz_text_mode_is_unchanged(tmp_path):
    check(
        r'''
        from brish.brishz import brishz
        r = brishz(["a b", "c"], stdin="x\r\ny")
        assert r.retcode == 3, repr(r)
        assert r.err == "FAKE-CLIENT in=MAGIC_READ_STDIN binary=unset args=[a b][c]", repr(r)
        #: text=True: universal newlines, as before.
        assert r.out == "x\ny", repr(r)
        assert (r.cmd, r.cmd_stdin) == (["a b", "c"], "x\r\ny")
        ''',
        env=fake_client(tmp_path),
        timeout=30,
    )


def test_brishz_bytes_mode(tmp_path):
    env = fake_client(tmp_path)
    check(
        r'''
        from brish.brishz import brishz
        data = bytes(range(256)) + b"\r\n\0"
        r = brishz(["x"], stdin=data, binary=True)
        assert r.retcode == 3, repr(r)
        assert r.outb == data, repr(r)
        assert r.errb == b"FAKE-CLIENT in=MAGIC_READ_STDIN binary=y args=[x]", repr(r)
        assert r.cmd_stdin == data
        r = brishz(["x"], stdin="é\udcff", binary=True)
        assert r.outb == "é".encode() + b"\xff", repr(r)
        r = brishz(["x"], stdin=bytearray(b"ba"), binary=True)
        assert r.outb == b"ba" and r.cmd_stdin == b"ba", repr(r)
        r = brishz(["x"], stdin=None, binary=True)
        assert r.outb == b"" and r.cmd_stdin is None, repr(r)
        #: The env var opts in too.
        os.environ["brishz_binary"] = "y"
        r = brishz(["y"], stdin=b"\xff")
        assert r.outb == b"\xff" and b"binary=y" in r.errb, repr(r)
        os.environ["brishz_binary"] = "n"
        r = brishz(["y"], stdin="t")
        assert r.out == "t" and "binary=n" in r.err and r.outb == b"t", repr(r)
        ''',
        env=env,
        timeout=30,
    )
