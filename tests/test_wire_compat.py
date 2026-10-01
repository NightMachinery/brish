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
