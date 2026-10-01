"""The corpus for the legacy wire-compatibility tests (test_wire_compat.py).

Two corpora:
- RAW: requests sent with the raw driver in tests/legacy_wire.py. Replies are
  compared byte for byte, as they appear on the FIFOs.
- api_corpus(): calls through an old brishmod module (the pre-binary release
  9599fc3, or master 6e97135), which spawns whichever brish2.zsh sits next to
  its __file__. Results are compared field by field.

Every command is inert: it prints, reads stdin, or changes shell state.
Nothing here depends on the clock, PIDs or the worker script's path, so two
runs in the same directory and environment must agree exactly.
"""

import os
import sys
import threading
import types

BIG = bytes(range(1, 256)) * 1200  # NUL-free, about 300 KB
UNICODE = "HARRY: “Hermione,” — café × ünïcödé 🐍"

#: (cmd, stdin, fork, worker index). Worker indices need a 3-worker driver.
RAW = [
    (b"echo hi", b"", False, 0),
    (b"print -rn -- no-newline", b"", False, 0),
    (b"print -r -- trailing; print", b"", False, 0),
    (b"true", b"", False, 0),
    (b"false", b"", False, 0),
    (b"(exit 7)", b"", False, 0),
    (b"print -r -- $?", b"", False, 0),
    (b"return 3", b"", False, 0),
    (b"print -r -- before; return 4; print -r -- after", b"", False, 0),
    (b'print -r -- "$cmd"', b"", False, 0),
    (b'print -r -- $# "$*" $funcstack ${#funcstack} $ZSH_SUBSHELL', b"", False, 0),
    (b'print -r -- $# $ZSH_SUBSHELL', b"", True, 0),
    (b"return 2", b"", True, 0),
    (b"exit 5", b"", True, 0),
    (b"typeset -p brish_server_index brish_fork MARKER", b"", False, 0),
    (b"typeset -p brish_fork", b"", True, 0),
    (b"print -r -- " + UNICODE.encode(), b"", False, 0),
    (b"printf 'cr\\r\\ncrlf\\r\\n\\rlone\\xff\\xfe\\x01'", b"", False, 0),
    (b"printf 'mid\\0nul\\n'", b"", False, 0),
    (b"\nfor i in 1 2 3\ndo\n  print -r -- line $i\ndone\n# a comment\n", b"", False, 0),
    (b"x=outer; f() { print -r -- f:$1:$x }; cd /", b"", False, 0),
    (b"print -r -- $x; f arg; pwd", b"", False, 0),
    (b"x=forked; cd /tmp", b"", True, 0),
    (b"print -r -- $x; pwd", b"", False, 0),
    (b"[[ -p /dev/stdin ]] && print -r pipe; cat; print -r -- end", b"", False, 0),
    (b"[[ -p /dev/stdin ]] && print -r pipe; cat", b"", True, 0),
    (b"cat", b"line1\nline2\r\n\xff", False, 0),
    (b"cat", b"line1\nline2\r\n\xff", True, 0),
    (b'print -r -- "$brish_stdin"; print -r -- ${#brish_stdin}', UNICODE.encode(), False, 0),
    (b"wc -c | tr -d ' '", BIG, False, 0),
    (b"wc -c | tr -d ' '", BIG, True, 0),
    (b"head -c 5", BIG, False, 0),
    (b"true", BIG, False, 0),
    (b"cat >&2", b"to stderr", False, 0),
    (b"print -r out; print -ru2 err; print -r out2; return 6", b"", False, 0),
    (b"print -ru2 forked-err; exit 9", b"", True, 0),
    (b"print -rn -- ${(l:200000::o:)}", b"", False, 0),
    (b"print -rn -- ${(l:6000::e:)} >&2", b"", False, 0),
    (b"trap 'print -ru2 ZERR-TRAP' ZERR", b"", False, 0),
    (b"false", b"", False, 0),
    (b"false", b"x", False, 0),
    (b"false", b"", True, 0),
    (b"f() { false }; f; print -r -- after $?", b"", False, 0),
    (b"true", b"", False, 0),
    (b"trap - ZERR", b"", False, 0),
    (b"print -r -- $brish_server_index", b"", False, 1),
    (b"y=one; print -r -- $y", b"", False, 1),
    (b"print -r -- ${y-unset} $brish_server_index", b"", False, 2),
    (b"print -r -- $y", b"", False, 1),
    (b"setopt | grep -c . >/dev/null; print -r -- ok", b"", False, 2),
]


def run_raw(worker, env, tmpdir):
    """Run RAW against `worker`. Returns [(stdout_raw, stderr_raw), ...]."""
    from tests.legacy_wire import RawLegacy

    w = RawLegacy(worker, n=3, env=env, tmpdir=tmpdir)
    try:
        out = []
        for cmd, stdin, fork, index in RAW:
            o, e, eof_o, eof_e = w.send(cmd, stdin, fork, index)
            assert not (eof_o or eof_e), ("worker died", cmd, o, e)
            out.append((o, e))
        return out
    finally:
        w.close()


def load_brishmod(name, src, file, quoting_src=None):
    """Execute an old brishmod.py source as module `name`, with __file__ set
    to `file`, so that it spawns the brish2.zsh next to `file`."""
    mod = types.ModuleType(name)
    mod.__file__ = file
    if quoting_src is not None:
        #: master's `from .quoting import ...` resolves to its own quoting.py.
        pkg = types.ModuleType(name + "_pkg")
        pkg.__path__ = []
        q = types.ModuleType(name + "_pkg.quoting")
        exec(compile(quoting_src, file + "-quoting", "exec"), q.__dict__)
        sys.modules[pkg.__name__] = pkg
        sys.modules[q.__name__] = q
        mod.__package__ = pkg.__name__
    sys.modules[name] = mod
    exec(compile(src, file, "exec"), mod.__dict__)
    return mod


def _fields(r):
    return (r.retcode, r.out, r.err)


def api_corpus(mod, scratch, **brish_kw):
    """Run the API-level corpus through `mod` (an old brishmod). Returns a list
    of (label, value) pairs."""
    Brish = mod.Brish
    res = []

    def rec(label, value):
        res.append((label, value))

    b = Brish(server_count=1, **brish_kw)
    c = b.send_cmd
    for label, args, kw in [
        ("plain", ("echo hello",), {}),
        ("print", ("print -r -- a b  c",), {}),
        ("false", ("false",), {}),
        ("status", ("(exit 7)",), {}),
        ("return", ("print -r -- x; return 4; print -r -- y",), {}),
        ("unicode", ("print -r -- " + UNICODE,), {}),
        ("unicode-quoted", ("echo 'sth × another (ver.-)'",), {}),
        ("cr", ("printf 'a\\r\\nb\\rc'",), {}),
        ("multiline", ("\nfor i in 1 2 3\ndo\n  echo $i\ndone\n",), {}),
        ("cmdvar", ('print -r -- "$cmd"',), {}),
        ("define", ("v=1; fn() { print -r -- fn:$1:$v }; cd " + scratch,), {}),
        ("persist", ("print -r -- $v; fn 2; pwd",), {}),
        ("fork-set", ("v=2; cd /",), {"fork": True}),
        ("after-fork", ("print -r -- $v; pwd",), {}),
        ("fork-print", ("print -r -- $brish_server_index forked",), {"fork": True}),
        ("fork-exit", ("exit 7",), {"fork": True}),
        ("stdin", ("cat",), {"cmd_stdin": "abc\ndef"}),
        ("stdin-fork", ("cat",), {"cmd_stdin": "abc\ndef", "fork": True}),
        ("stdin-var", ('print -r -- "$brish_stdin"',), {"cmd_stdin": UNICODE}),
        ("stdin-large", ("wc -c | tr -d ' '",), {"cmd_stdin": "x" * 300_000}),
        ("stdin-large-head", ("head -c 3",), {"cmd_stdin": "y" * 300_000}),
        ("stdin-unread", ("true",), {"cmd_stdin": "z" * 100_000}),
        ("no-stdin", ("cat",), {}),
        ("stdin-int", ("cat",), {"cmd_stdin": 12}),
        ("stderr", ("print -r out; print -ru2 err; return 3",), {}),
        ("stdout-large", ("print -rn -- ${(l:100000::o:)}",), {}),
        ("nul-cmd", ("print a\0b",), {}),
        ("nul-stdin", ("cat",), {"cmd_stdin": "a\0b"}),
    ]:
        rec(label, _fields(c(*args, **kw)))

    #: zstring interpolation and the readme examples.
    z = b.z
    name = "A$ron"
    rec("readme-hello", _fields(z("echo Hello {name}")))
    alist = ["# Fruits", "1. Orange", "2. Rambutan", "3. Strawberry"]
    rec("readme-list", _fields(z("for i in {alist} ; do echo $i ; done")))
    for i in range(10):
        cmd = "(( {i} % 2 == 0 )) && echo {i} || {{ echo Bad Odds'!' >&2 }}"
        rec(f"readme-odds-{i}", _fields(z(cmd)))
    rec("readme-home", bool(z("test -e ~/")))
    r = z("""echo This is stdout
           echo This is stderr >&2
           (exit 6) # this is the return code""")
    rec("readme-res", (_fields(r), r.outrs, r.longstr))
    rec("readme-fork-exit", z("exit 7", fork=True).retcode)
    a = "1\n2\n3\n4\n5\n"
    rec("readme-herestring", _fields(z("<<<{a} wc -l | tr -d ' '")))
    rec("readme-wc", _fields(z("wc -l | tr -d ' '", cmd_stdin=a)))
    rec("readme-cat", _fields(z("cat")))
    python_var = "$HOME"
    rec("readme-var", _fields(z("echo {python_var}")))
    rec("readme-var-e", _fields(z("echo {python_var:e}")))
    rec("readme-bool", (z("test -n {True:bool}").retcode, z("test -n {False:bool}").retcode))
    rec("readme-join", _fields(z("echo {'    '.join(map(str,alist))}")))
    rec("readme-f", _fields(z("echo {67:f}")))
    rec("readme-s", _fields(z("echo {[11, 45]!s}")))
    untrusted_input = " ; echo do evil | cat"
    rec("readme-eval", _fields(z("eval {untrusted_input}")))
    rec("readme-safe", _fields(z("echo {untrusted_input}")))
    q = [UNICODE, "it's", "$x `y`", "a\nb", "tab\there"]
    rec("quote-list", _fields(z("print -rl -- {q}")))
    rec("iter0", list(z("print -rn -- a$'\\0'b$'\\0'").iter0()))
    b.cleanup()

    #: boot_cmd, restart, and three workers used from three threads.
    bb = Brish(boot_cmd="cd " + scratch + "; booted=yes", server_count=3, **brish_kw)
    rec("boot", [_fields(bb.send_cmd("print -r -- $booted; pwd", server_index=i)) for i in range(3)])
    bb.send_cmd("a=56", server_index=0)
    rec("before-restart", _fields(bb.send_cmd("print -r -- ${a-unset}", server_index=0)))
    bb.restart()
    rec("after-restart", _fields(bb.send_cmd("print -r -- ${a-unset} $booted", server_index=0)))
    results = {}

    def worker(i):
        out = []
        for k in range(15):
            out.append(_fields(bb.send_cmd(
                f"print -r -- $brish_server_index {k}; t{i}=$((${{t{i}:-0}} + 1)); print -ru2 -- $t{i}",
                server_index=i,
            )))
            out.append(_fields(bb.send_cmd("cat", cmd_stdin=f"{i}-{k}\n" * 50, server_index=i)))
        results[i] = out

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(120)
        assert not t.is_alive(), "a corpus thread hung"
    rec("threads", [results[i] for i in range(3)])
    bb.cleanup()
    return res
