"""G4: the pure-Python quoter (`brish.quoting.zsh_quote_bytes`)."""

import os
import random
import subprocess

import pytest

from brish.quoting import zsh_quote_bytes
from tests.conftest import EMPTY_ZDOTDIR, check

ZSH_ENV = dict(os.environ, ZDOTDIR=str(EMPTY_ZDOTDIR))


def zsh(script_path, env=None, timeout=120):
    r = subprocess.run(
        ["zsh", "-f", str(script_path)],
        capture_output=True,
        timeout=timeout,
        env=dict(ZSH_ENV, **(env or {})),
    )
    return r


def allhex(b):
    return "$'" + "".join("\\x%02x" % x for x in b) + "'" if b else "''"


PIECES = [
    "a", "Z", "0", " ", "'", '"', "\\", "\n", "\t", "\r", "\0", "$", "`", "!",
    "~", "#", "=", "*", "?", "[", "]", "{", "}", "(", ")", "|", "&", ";", "<",
    ">", "^", "%", "@", "+", ",", ":", "-", "\u00e9", "\u0634", "\u06af",
    "\u4e2d", "\U0001f600", "\u0301", "\u200d", "\u200b", "\u00a0", "\u2028",
    "\ufeff", "\ue000", "\u0378", "\u0192", "\u2014", "\x7f", "\x1b", "\x85",
    "\u3000", "\U0001f468\u200d\U0001f469", "\u202e", "\ud7ff", "\x1c",
    "\u009c", "\u00ad", "\u0080", "\u009f",
]
RAW = [b"\x80", b"\xbf", b"\xc0\x80", b"\xed\xa0\x80", b"\xf4\x90\x80\x80",
       b"\xe2\x82", b"\xff", b"\xfe", b"\xc3", b"\x83", b"\x9b", b"\x9c",
       b"\xa7", b"\xdc", b"\x1c"]
WORDS = ["!", "-", "--", "=", "==", "~", "~/x", "#", "a#", "!x", "-n", "-e",
         "{}", "{a,b}", "*.py", "[[", "]]", "$(ls)", "`ls`", "${x}", "a\\",
         "\\n", "'", "''", "'''", "\"'\"", "%", "%%", "if", "then", "}",
         "!(x)", "@(x)", "+(x)", "a^b", "x~y", "^x", "=ls", "a=~b", "x\ny"]


def corpus(pairs, n_random=3000, seed=1234):
    rnd = random.Random(seed)
    c = [bytes([i]) for i in range(256)]
    if pairs:
        c += [bytes([i, j]) for i in range(256) for j in range(256)]
    c += [bytes(rnd.randrange(256) for _ in range(rnd.randrange(0, 41))) for _ in range(n_random)]
    for _ in range(n_random):
        parts = []
        for _ in range(rnd.randrange(1, 12)):
            if rnd.random() < 0.15:
                parts.append(rnd.choice(RAW))
            else:
                parts.append(rnd.choice(PIECES).encode("utf-8", "surrogatepass"))
        c.append(b"".join(parts))
    c += [w.encode() for w in WORDS]
    return c


#: Prints "<index>:<byte length>:<value>" for its single argument.
T_FUNC = r'''__t() { emulate -L zsh; setopt no_multibyte; local __i=$1; shift; if (( $# != 1 )); then builtin print -rn -- "$__i:E$#:"; return; fi; local __x="$1"; builtin print -rn -- "$__i:${#__x}:$__x"; }
'''


def roundtrip(tmp_path, quoted, pre="", env=None):
    """Evaluate each quoted word in `zsh -f` and return what zsh read back."""
    body = "".join(
        "eval " + allhex((f"__t {k} " + q).encode("utf-8")) + f" || builtin print -rn -- '{k}:P:'\n"
        for k, q in enumerate(quoted)
    )
    corpus_file = tmp_path / "corpus.zsh"
    corpus_file.write_text(body, encoding="utf-8")
    driver = tmp_path / "driver.zsh"
    driver.write_text(T_FUNC + pre + "\nsource " + str(corpus_file) + "\n")
    out = zsh(driver, env=env).stdout
    recs, i = {}, 0
    while i < len(out):
        j = out.index(b":", i)
        k = int(out[i:j])
        i = j + 1
        j = out.index(b":", i)
        tag = out[i:j]
        if tag.startswith(b"E"):
            val = ("ARGC", int(tag[1:]))
            i = j + 1
        elif tag == b"P":
            val = ("PARSE_ERROR",)
            i = j + 1
        else:
            n = int(tag)
            val = out[j + 1 : j + 1 + n]
            i = j + 1 + n
        recs.setdefault(k, val)
    return [recs.get(k, ("MISSING",)) for k in range(len(quoted))]


def test_rules():
    q = zsh_quote_bytes
    assert q(b"") == "''"
    assert q(b"plain_name-1.txt") == "plain_name-1.txt"
    assert q(b"a b") == "'a b'"
    assert q(b"it's") == "'it'\\''s'"
    assert q(b"'") == "\\'"
    assert q(b"$HOME") == "'$HOME'"
    assert q(b"a\nb") == "$'a\\nb'"
    assert q(b"\t\\'") == "$'\\t\\\\\\''"
    assert q(b"\xff") == "$'\\xff'"
    assert q(b"\0") == "$'\\x00'"
    assert q("\u00e9".encode()) == "\u00e9"
    assert q("\u00e9 x".encode()) == "'\u00e9 x'"
    assert q("\u00e9".encode(), ascii_only=True) == "$'\\xc3\\xa9'"
    #: The (q+) injection vector from F6 stays inside one word.
    assert q("\x1c'; x".encode()) == "$'\\x1c\\'; x'"
    #: A str argument is UTF-8 with surrogateescape.
    assert q("a\udcffb") == "$'a\\xffb'"


def test_output_is_surrogate_free_and_has_no_raw_controls():
    for b in corpus(pairs=False, n_random=2000):
        for ascii_only in (False, True):
            s = zsh_quote_bytes(b, ascii_only=ascii_only)
            s.encode("utf-8")  # no lone surrogates
            assert not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in s), (b, s)
            if ascii_only:
                assert s.isascii(), (b, s)


def test_roundtrip_all_bytes_and_pairs(tmp_path):
    c = corpus(pairs=True, n_random=2000)
    got = roundtrip(tmp_path, [zsh_quote_bytes(b) for b in c])
    bad = [(b, r) for b, r in zip(c, got) if r != b]
    assert not bad, (len(bad), bad[:5])


@pytest.mark.parametrize(
    "pre,env,ascii_only",
    [
        ("setopt rc_quotes", None, False),
        ("setopt no_multibyte", None, False),
        ("setopt extended_glob glob_subst", None, False),
        ("", {"LC_ALL": "C"}, False),
        ("setopt no_multibyte", {"LC_ALL": "C"}, True),
        ("", None, True),
    ],
    ids=["rc_quotes", "no_multibyte", "extendedglob+globsubst", "LC_ALL=C", "C+ascii", "ascii"],
)
def test_roundtrip_under_options(tmp_path, pre, env, ascii_only):
    c = corpus(pairs=False, n_random=3000, seed=99)
    got = roundtrip(tmp_path, [zsh_quote_bytes(b, ascii_only=ascii_only) for b in c], pre=pre, env=env)
    bad = [(b, r) for b, r in zip(c, got) if r != b]
    assert not bad, (len(bad), bad[:5])


def qplus(tmp_path, values):
    body = "".join(
        "x=" + allhex(b) + "; builtin print -rn -- \"${(q+)x}\"; builtin print -rn -- $'\\0'\n"
        for b in values
    )
    f = tmp_path / "qp.zsh"
    f.write_text(body)
    r = zsh(f, env={"LC_ALL": "en_US.UTF-8"})
    parts = r.stdout.split(b"\0")[:-1]
    assert len(parts) == len(values), r.stderr[:300]
    return parts


def test_text_matches_qplus_for_printable_utf8(tmp_path):
    rnd = random.Random(7)
    printable = [p for p in PIECES if p.isprintable() and p not in ("\x1c",)]
    vals = [bytes([i]) for i in range(0x20, 0x7F)]
    vals += [w.encode() for w in WORDS if w.isprintable()]
    for _ in range(3000):
        vals.append("".join(rnd.choice(printable) for _ in range(rnd.randrange(1, 10))).encode())
    #: Characters whose printability differs between Python's Unicode data
    #: and the platform's iswprint are not part of the parity claim.
    vals = [v for v in vals if not any(ch in v.decode() for ch in "\u00a0\u3000\u2028\u200b\u200d\ufeff\ue000\u0378\u202e")]
    ref = qplus(tmp_path, vals)
    diff = [(v, zsh_quote_bytes(v), r) for v, r in zip(vals, ref) if zsh_quote_bytes(v).encode() != r]
    assert not diff, (len(diff), diff[:5])


def test_readme_examples_keep_their_text():
    check(
        r'''
        b = Brish(delayed_init=True)
        python_var = "$HOME"
        alist = ["# Fruits", "1. Orange", "2. Rambutan", "3. Strawberry"]
        s = b.zstring("echo zstring constructs the command string that will be sent to zsh. It interpolates the Pythonic variables: {python_var} {alist}")
        assert s == " echo zstring constructs the command string that will be sent to zsh. It interpolates the Pythonic variables: '$HOME' '# Fruits' '1. Orange' '2. Rambutan' '3. Strawberry' ", repr(s)
        msg = "You can\u2019t make an omelet without breaking a few eggs."
        s = b.zstring("say {msg}")
        assert s == " say 'You can\u2019t make an omelet without breaking a few eggs.' ", repr(s)
        s = b.zstring("date +%Y")
        assert s == " date +%Y ", repr(s)
        ''',
        timeout=30,
    )


def test_quoting_starts_no_zsh():
    check(
        r'''
        def boom(*a, **k):
            raise AssertionError("a process was started for quoting")
        bm.Popen = boom
        import subprocess
        subprocess.Popen = boom
        b = Brish(delayed_init=True)
        words = [f"file {i} 'q' $x" for i in range(1000)]
        t = time.perf_counter()
        q = b.zsh_quote(words)
        dt = time.perf_counter() - t
        assert q.startswith("'file 0 '\\''q'\\'' $x'"), q[:40]
        assert dt < 0.5, dt
        assert b.p is None and bm._shared_brish.p is None
        s = b.zstring("print -r -- {words[0]} {None}")
        assert s == " print -r -- 'file 0 '\\''q'\\'' $x'  ", repr(s)
        ''',
        timeout=30,
    )


def test_z_roundtrip_valid_utf8_through_a_worker():
    #: Valid UTF-8 without NUL or CR: what the legacy transport can carry.
    check(
        r'''
        b = Brish(server_count=1)
        suffixes = ("", "'", '"', "\\", "$", " ;x")
        values = [chr(i) + s for i in range(128) for s in suffixes if chr(i) not in "\0\r"]
        values += [c + s for c in ("\x1c", "\u0080", "\u009c", "\u009f", "\u00ad", "\u00a7", "\u00dc") for s in suffixes]
        bad = []
        for v in values:
            r = b.z("print -rn -- {v}")
            if r.out != v or r.retcode != 0:
                bad.append((v, r))
        assert not bad, (len(bad), bad[:3])
        b.cleanup()
        ''',
        timeout=120,
    )
