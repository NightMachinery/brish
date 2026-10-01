"""Legacy mode (brish2.zsh over FIFOs): what it carries exactly since the
backports from binary mode. Every test here is skipped in binary mode, which
has its own, stronger tests in test_binary.py."""

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
