"""G11 for legacy mode: brish2.zsh stays wire-compatible with old Python.

Python processes running older Brish code (the pre-binary release 9599fc3,
and master 6e97135) spawn the on-disk brish2.zsh by path on every init() and
restart(). So the worker in this tree must answer them exactly as the original
worker did. The original worker and the old Python sources come from git.

- The raw corpus is sent over the FIFOs by a driver independent of brish
  (tests/legacy_wire.py) and every reply is compared byte for byte with the
  original worker's.
- The API corpus runs through the old brishmod modules themselves, each loaded
  with __file__ pointing next to the worker under test, and compares every
  result field by field.

Both run with an empty ZDOTDIR and in the caller's real zsh environment
(startup files load; options such as nomatch and pipefail may be on). These
tests do not use the library under test, so they run in legacy mode only.
"""

import os
import stat
import subprocess

import pytest

from tests.conftest import ROOT, check, legacy_only

pytestmark = legacy_only

ORIGINAL = "9599fc3"  # the last release before binary mode
MASTER = "6e97135"  # master when the legacy backports started
ENVS = [pytest.param(False, id="empty-zdotdir"), pytest.param(True, id="real-env")]


def git_show(rev, path):
    p = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{rev}:{path}"], capture_output=True
    )
    if p.returncode != 0:
        pytest.skip(f"git history not available ({rev}:{path})")
    return p.stdout


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    d = tmp_path_factory.mktemp("wire")
    orig = d / "orig"
    orig.mkdir()
    worker = orig / "brish2.zsh"
    worker.write_bytes(git_show(ORIGINAL, "brish/brish2.zsh"))
    worker.chmod(worker.stat().st_mode | stat.S_IXUSR)
    for rev, path, name in [
        (ORIGINAL, "brish/brishmod.py", "brishmod_original.py"),
        (MASTER, "brish/brishmod.py", "brishmod_master.py"),
        (MASTER, "brish/quoting.py", "quoting_master.py"),
    ]:
        (d / name).write_bytes(git_show(rev, path))
    return d


#: Loads the old modules in the child. ORIG_FILE and TREE_FILE are paths
#: next to the original worker and next to this tree's worker.
LOADERS = r'''
import threading
from tests.wire_corpus import load_brishmod, api_corpus
SRC = {sources!r}
def src(name):
    with open(os.path.join(SRC, name)) as f:
        return f.read()
ORIG_WORKER = os.path.join(SRC, "orig", "brish2.zsh")
TREE_WORKER = os.path.join(ROOT, "brish", "brish2.zsh")
ORIG_FILE = os.path.join(SRC, "orig", "brishmod.py")
TREE_FILE = os.path.join(ROOT, "brish", "brishmod_wire_test.py")
def original_on(file, tag):
    return load_brishmod("brishmod_original_" + tag, src("brishmod_original.py"), file)
def master_on(file, tag):
    return load_brishmod("brishmod_master_" + tag, src("brishmod_master.py"), file,
                         quoting_src=src("quoting_master.py"))
def diff(a, b):
    assert len(a) == len(b), (len(a), len(b))
    return [(x[0], x[1], y[1]) for x, y in zip(a, b) if x != y]
def parallel(**jobs):
    #: One thread per run: startup files can take seconds per worker.
    out, errors = dict(), []
    def run(k, f):
        try:
            out[k] = f()
        except BaseException as e:
            errors.append(e)
    ts = [threading.Thread(target=run, args=kv) for kv in jobs.items()]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    if errors:
        raise errors[0]
    return out
'''


@pytest.mark.parametrize("real_env", ENVS)
def test_raw_replies_match_the_original(sources, real_env):
    check(
        r'''
        from tests.wire_corpus import RAW, run_raw
        r = parallel(want=lambda: run_raw(ORIG_WORKER, None, SCRATCH),
                     got=lambda: run_raw(TREE_WORKER, None, SCRATCH))
        want, got = r["want"], r["got"]
        bad = [(RAW[i][0], want[i], got[i]) for i in range(len(RAW)) if want[i] != got[i]]
        assert not bad, (len(bad), bad[:3])
        ''',
        setup=LOADERS.format(sources=str(sources)),
        env={"BRISH_BINARY": None},
        real_env=real_env,
        timeout=180,
    )


@pytest.mark.parametrize("real_env", ENVS)
def test_old_python_on_this_worker(sources, real_env):
    #: The baseline is the original Python on the original worker. master's
    #: legacy path reads text like the original Python, so it must give the
    #: same results on this tree's worker.
    check(
        r'''
        r = parallel(
            want=lambda: api_corpus(original_on(ORIG_FILE, "o"), SCRATCH),
            original=lambda: api_corpus(original_on(TREE_FILE, "t"), SCRATCH),
            master=lambda: api_corpus(master_on(TREE_FILE, "t"), SCRATCH, binary=False),
        )
        for k in ("original", "master"):
            bad = diff(r["want"], r[k])
            assert not bad, (k, len(bad), bad[:3])
        ''',
        setup=LOADERS.format(sources=str(sources)),
        env={"BRISH_BINARY": None},
        real_env=real_env,
        timeout=300,
    )


@pytest.mark.parametrize("real_env", ENVS)
def test_intended_differences(sources, real_env):
    #: Where this worker answers differently from the original on purpose.
    #: The original hangs an old Python (it spins on EOF) for most of these,
    #: so they are asserted here instead of in the corpus.
    check(
        r'''
        T = time.monotonic
        #: The original re-runs the previous command after a syntax error.
        b = original_on(ORIG_FILE, "o").Brish(server_count=1)
        b.send_cmd("print -r first")
        r = b.send_cmd("fi")
        assert (r.retcode, r.out) == (0, "first\n") and "parse error" in r.err, r
        b.cleanup()
        def run(name, mod, kw):
            b = mod.Brish(server_count=1, **kw)
            c = b.send_cmd
            assert c("print -r first").out == "first\n"
            want = [
                ("fi", 1, "", "parse error"),  # no re-run
                ("print *.nonexistent_zzz; print -r no", 1, "", "no matches found"),
                ("setopt errexit errreturn; true", 0, "", ""),
                ("false; print -r survived", 0, "survived\n", ""),  # not persistent
                ("f() { false; print -r after }; f", 0, "after\n", ""),
                ("break; print -r no", 0, "", ""),
                ("continue; print -r no", 0, "", ""),
                ("print -r a; break 2; print -r no", 0, "a\n", ""),
                ("print -r b; continue 2; print -r no", 0, "b\n", ""),
            ]
            for cmd, rc, out, err in want:
                r = c(cmd)
                assert (r.retcode, r.out) == (rc, out) and err in r.err, (name, cmd, r)
                if not err:
                    assert r.err == "", (name, cmd, r)
                r = c("echo ok")
                assert (r.retcode, r.out) == (0, "ok\n"), (name, cmd, r)
            #: exit: the real status. The worker is gone afterwards: the
            #: original Python gets EPIPE, master restarts and runs the command.
            for cmd, rc in (("sleep 2 & print -r bye; exit 3", 3),):
                t = T()
                r = c(cmd)
                assert (r.retcode, r.out, r.err) == (rc, "bye\n", ""), (name, cmd, r)
                assert T() - t < 1.5, (name, T() - t)
                if name == "original":
                    try:
                        c("echo next")
                        raise SystemExit("no BrokenPipeError for the original Python")
                    except BrokenPipeError:
                        pass
                    b = mod.Brish(server_count=1)
                    c = b.send_cmd
                else:
                    r = c("echo next")
                    assert (r.retcode, r.out) == (0, "next\n"), (name, r)
            if name == "master":
                b.cleanup()
            #: errexit kills the worker without an EXIT trap; the bootstrap
            #: answers for it, even while a background job holds the FIFOs,
            #: and the old Python no longer spins.
            b = mod.Brish(server_count=1, **kw)
            t = T()
            r = b.send_cmd("sleep 2 & setopt errexit; false")
            assert r.retcode == 9001, (name, r)
            assert T() - t < 1.5, (name, T() - t)
            if name == "master":
                #: master reads the 9001 as a reply (without the note) and
                #: finds the worker gone on its next request.
                r = b.send_cmd("echo next")
                assert (r.retcode, r.out) == (0, "next\n"), (name, r)
                b.cleanup()
        parallel(original=lambda: run("original", original_on(TREE_FILE, "t"), {}),
                 master=lambda: run("master", master_on(TREE_FILE, "t"), {"binary": False}))
        time.sleep(2)  # let the background sleeps finish
        ''',
        setup=LOADERS.format(sources=str(sources)),
        env={"BRISH_BINARY": None},
        real_env=real_env,
        allow_orphans=True,
        timeout=180,
    )


@pytest.mark.parametrize("real_env", ENVS)
@pytest.mark.parametrize("python", ["original", "master", "tree"])
def test_a_dying_worker_leaves_the_others_alone(sources, python, real_env):
    #: The bootstrap answers only for the worker that died. A busy worker
    #: finishes its own command, and the other workers' next replies are their
    #: own. "tree" is this tree's own Python. A restart drops the state of
    #: every worker, but each reply is still the answer to its own command.
    check(
        r"""
        import faulthandler; faulthandler.dump_traceback_later(60, exit=True)
        mod = {"original": lambda: original_on(TREE_FILE, "t"),
               "master": lambda: master_on(TREE_FILE, "t"),
               "tree": lambda: bm}[PY]()
        kw = {"binary": False} if PY == "master" else {}
        for method in ("kill", "exit", "errexit"):
            b = mod.Brish(server_count=3, **kw)
            c = b.send_cmd
            for i in (1, 2):
                assert c(f"v=kept{i}", server_index=i).retcode == 0
            r = c("zmodload zsh/system; print -r -- $sysparams[pid]", server_index=0)
            pid0 = int(r.out)
            assert pid0 > 1 and pid0 != b.p.pid, (PY, method, repr(r))
            busy = {}
            def run_busy():
                busy["r"] = c("sleep 1; print -r -- busy-$v", server_index=1)
            t = threading.Thread(target=run_busy)
            t.start()
            time.sleep(0.3)
            if method == "kill":
                print(f"[test] SIGKILL worker {pid0}", file=sys.stderr)
                os.kill(pid0, signal.SIGKILL)
            elif method == "exit":
                r = c("exit 4", server_index=0)
                assert (r.retcode, r.out) == (4, ""), (PY, method, repr(r))
            else:
                r = c("setopt errexit; false", server_index=0)
                assert (r.retcode, r.out) == (9001, ""), (PY, method, repr(r))
            t.join(30)
            r = busy["r"]
            assert (r.retcode, r.out, r.err) == (0, "busy-kept1\n", ""), (PY, method, repr(r))
            time.sleep(0.3)  # the bootstrap has reaped worker 0
            #: Which Pythons restart the instance (dropping v) at their next
            #: call: master after a 9001, this tree after any reported death.
            keep = {"original": True, "master": method != "errexit",
                    "tree": method == "kill"}[PY]
            for k in range(3):
                for i in (1, 2):
                    r = c(f"print -r -- {k}-$v", server_index=i)
                    want = f"{k}-kept{i}\n" if keep else f"{k}-\n"
                    assert (r.retcode, r.out, r.err) == (0, want, ""), (PY, method, k, i, repr(r))
            b.cleanup()
        """,
        setup=LOADERS.format(sources=str(sources)) + f"PY = {python!r}\n",
        env={"BRISH_BINARY": None},
        real_env=real_env,
        timeout=180,
    )


#: Commands that a SIGINT meets, as (cmd, stdin, fork). They wait with
#: zselect, a builtin, so that nothing else's signals can reach them.
SIGINT_CASES = r"""
W = "zmodload zsh/zselect; zselect -t 100"
SIGINT_CASES = [
    ("print -r before; " + W + "; print -r after", "", False),
    ("f() { " + W + " }; print -r before; f; print -r after", "", False),
    ("print -r before; " + W + "; print -r after", "", True),
    ("print -r before; cat >/dev/null; " + W + "; print -r after", "in", False),
    ("print -r before; " + sys.executable + " -c 'import time; time.sleep(1)'; print -r after", "", False),
]
def sigint_cases(mod, kw):
    from tests.conftest import descendants
    b = mod.Brish(server_count=2, **kw)
    c = b.send_cmd
    pid_cmd = "zmodload zsh/system; print -r -- $sysparams[pid]"
    r = c("v=kept; " + pid_cmd, server_index=0)
    pid = int(r.out)
    assert pid > 1 and pid != b.p.pid, repr(r)
    res = []
    for cmd, stdin, fork in SIGINT_CASES:
        got = {}
        t = threading.Thread(target=lambda: got.update(r=c(cmd, cmd_stdin=stdin, fork=fork, server_index=0)))
        t.start()
        time.sleep(0.4)
        pids = descendants(pid) + [pid]
        print(f"[test] SIGINT {pids}", file=sys.stderr)
        for x in pids:
            os.kill(x, signal.SIGINT)
        t.join(10)
        assert not t.is_alive(), cmd
        r = got["r"]
        res.append((cmd, fork, r.retcode, r.out, r.err, c("print -r -- $v", server_index=0).out))
    os.kill(pid, signal.SIGINT)  # idle
    time.sleep(0.2)
    res.append(("idle", [c("print -r -- ${v-unset}", server_index=i).out for i in (0, 1)],
                int(c(pid_cmd, server_index=0).out) == pid))
    b.cleanup()
    return res
"""


@pytest.mark.parametrize("python", ["original", "master"])
def test_sigint_under_old_python(sources, python):
    #: Old Python starts the bootstrap in its own process group (a terminal
    #: Ctrl-C reaches the workers) and does not set BRISH_SESSION, so this
    #: worker defines no SIGINT trap and ignores SIGINT throughout, as the
    #: original worker did (workers start as background jobs of a
    #: non-interactive zsh): a SIGINT to the worker and every process below
    #: it changes nothing that the original worker would not.
    check(
        r"""
        r = parallel(want=lambda: sigint_cases(original_on(ORIG_FILE, "o"), {}),
                     got=lambda: sigint_cases(
                         {"original": lambda: original_on(TREE_FILE, "t"),
                          "master": lambda: master_on(TREE_FILE, "t")}[PY](),
                         {"binary": False} if PY == "master" else {}))
        want, got = r["want"], r["got"]
        print(want)
        assert want == got, (PY, [(a, b) for a, b in zip(want, got) if a != b])
        #: The non-fork commands ran on to their end.
        assert want[0][2:] == (0, "before\nafter\n", "", "kept\n"), want[0]
        assert want[-1] == ("idle", ["kept\n", "unset\n"], True), want[-1]
        """,
        setup=LOADERS.format(sources=str(sources)) + SIGINT_CASES + f"PY = {python!r}\n",
        env={"BRISH_BINARY": None},
        timeout=90,
    )


def test_sigint_aborts_only_the_command(sources):
    #: This tree's Python: SIGINT to a worker (which comes from Brish alone,
    #: since the worker is in a session of its own: BrishPopen.kill, or the
    #: one SIGINT for an abandoned BrishPopen) aborts the
    #: running command, which reports 130 as a plain retcode line, and the
    #: worker lives on with its state. While idle the worker ignores it.
    check(
        r"""
        res = sigint_cases(bm, {})
        for cmd, fork, rc, out, err, v in res[:-1]:
            if sys.executable in cmd:  # Python reports its KeyboardInterrupt
                assert err.endswith("KeyboardInterrupt\n"), err
                err = ""
            assert (rc, out, err, v) == (130, "before\n", "", "kept\n"), (cmd, fork, rc, out, err, v)
        assert res[-1] == ("idle", ["kept\n", "unset\n"], True), res[-1]
        """,
        setup=LOADERS.format(sources=str(sources)) + SIGINT_CASES,
        env={"BRISH_BINARY": None},
        timeout=90,
    )
