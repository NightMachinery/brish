"""brish3.zsh stays compatible with master's binary-mode Python.

Python processes running master's code (6e97135) spawn the on-disk brish3.zsh
by path on every init() and restart(). master's brishmod.py is loaded with
__file__ pointing into this tree, so it runs this tree's worker, and every
result is compared with master's Python on master's own worker. These tests
do not use the library under test, so they run in binary mode only.
"""

import stat
import subprocess

import pytest

from tests.conftest import ROOT, binary_only, check

pytestmark = binary_only

MASTER = "6e97135"


def git_show(rev, path):
    p = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{rev}:{path}"], capture_output=True
    )
    if p.returncode != 0:
        pytest.skip(f"git history not available ({rev}:{path})")
    return p.stdout


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    d = tmp_path_factory.mktemp("bincompat")
    (d / "master").mkdir()
    worker = d / "master" / "brish3.zsh"
    worker.write_bytes(git_show(MASTER, "brish/brish3.zsh"))
    worker.chmod(worker.stat().st_mode | stat.S_IXUSR)
    (d / "brishmod_master.py").write_bytes(git_show(MASTER, "brish/brishmod.py"))
    (d / "quoting_master.py").write_bytes(git_show(MASTER, "brish/quoting.py"))
    return d


LOADERS = r'''
from tests.wire_corpus import load_brishmod, api_corpus, BIG
SRC = @SRC@
def src(name):
    with open(os.path.join(SRC, name)) as f:
        return f.read()
MASTER_FILE = os.path.join(SRC, "master", "brishmod.py")
TREE_FILE = os.path.join(ROOT, "brish", "brishmod_compat_test.py")
def master_on(file, tag):
    return load_brishmod("brishmod_bmaster_" + tag, src("brishmod_master.py"), file,
                         quoting_src=src("quoting_master.py"))
def extras(mod):
    """More of the corpus: shell errors, exits, big output, NUL bytes."""
    res = []
    b = mod.Brish(server_count=1, binary=True)
    c = b.send_cmd
    for label, args, kw in [
        ("nomatch", ("print *.nonexistent_zzz; print -r no",), {}),
        ("nomatch-after", ("print -r ok",), {}),
        ("errexit", ("setopt errexit errreturn; true",), {}),
        ("false-after", ("false; print -r survived",), {}),
        ("syntax", ("fi",), {}),
        ("break2", ("print -r a; break 2; print -r no",), {}),
        ("stderr-big", ("print -rnu2 -- ${(l:200000::e:)}; print -r out",), {}),
        ("big-out", ("cat",), {"cmd_stdin": BIG * 8}),
        ("nul-out", ("printf 'a\\0b\\0BRISH3-END:x\\n'",), {}),
        ("stdin-null", ("cat",), {"cmd_stdin": None}),
        ("exit", ("print -r bye; exit 3",), {}),
        ("after-exit", ("print -r -- next",), {}),
        ("fork-exit", ("exit 7",), {"fork": True}),
    ]:
        r = c(*args, **kw)
        res.append((label, (r.retcode, r.outb, r.errb)))
    b.cleanup()
    return res
def diff(a, b):
    assert len(a) == len(b), (len(a), len(b))
    return [(x[0], x[1], y[1]) for x, y in zip(a, b) if x != y]
'''


def loaders(sources):
    return LOADERS.replace("@SRC@", repr(str(sources)))


@pytest.mark.parametrize("real_env", [pytest.param(False, id="empty-zdotdir"), pytest.param(True, id="real-env")])
def test_master_python_on_this_worker(sources, real_env):
    check(
        r'''
        want = api_corpus(master_on(MASTER_FILE, "m"), SCRATCH, binary=True)
        got = api_corpus(master_on(TREE_FILE, "t"), SCRATCH, binary=True)
        bad = diff(want, got)
        assert not bad, (len(bad), bad[:3])
        want = extras(master_on(MASTER_FILE, "m2"))
        got = extras(master_on(TREE_FILE, "t2"))
        bad = diff(want, got)
        assert not bad, (len(bad), bad[:3])
        ''',
        setup=loaders(sources),
        real_env=real_env,
        timeout=180,
    )


def test_sigint_with_master_python(sources):
    #: master's Python starts the bootstrap in its own process group (a
    #: terminal Ctrl-C reaches the workers) and does not set BRISH_SESSION,
    #: so this worker defines no SIGINT trap and ignores SIGINT throughout,
    #: as master's worker did: a SIGINT to the worker and every process below
    #: it gives what it gives on master's worker. The commands wait with
    #: zselect, a builtin, so that nothing else's signals can reach them.
    check(
        r"""
        from tests.conftest import descendants
        W = "zmodload zsh/zselect; zselect -t 100"
        cases = [("print -r before; " + W + "; print -r after", False),
                 ("f() { " + W + " }; print -r before; f; print -r after", False),
                 ("print -r before; " + W + "; print -r after", True),
                 ("print -r before; " + sys.executable + " -c 'import time; time.sleep(1)'; print -r after", False)]
        def run(mod):
            b = mod.Brish(server_count=1, binary=True)
            pid = b.p.workers[0].pid
            assert b.send_cmd("v=kept").retcode == 0
            res = []
            for cmd, fork in cases:
                got = {}
                t = threading.Thread(target=lambda: got.update(r=b.send_cmd(cmd, fork=fork)))
                t.start()
                time.sleep(0.4)
                pids = descendants(pid) + [pid]
                print(f"[test] SIGINT {pids}", file=sys.stderr)
                for x in pids:
                    os.kill(x, signal.SIGINT)
                t.join(10)
                assert not t.is_alive()
                r = got["r"]
                res.append((cmd, fork, r.retcode, r.out, r.err, b.send_cmd("print -r -- $v").out))
            os.kill(pid, signal.SIGINT)  # idle
            time.sleep(0.2)
            res.append(("idle", b.send_cmd("print -r -- $v").out, b.p.workers[0].pid == pid))
            b.cleanup()
            return res
        want = run(master_on(MASTER_FILE, "m"))
        got = run(master_on(TREE_FILE, "t"))
        print(want)
        assert want == got, [(a, b) for a, b in zip(want, got) if a != b]
        assert want[0][2:] == (0, "before\nafter\n", "", "kept\n"), want[0]
        assert want[-1] == ("idle", "kept\n", True), want[-1]
        """,
        setup=loaders(sources),
        timeout=60,
    )
