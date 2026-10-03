"""Test harness for brish.

Anything that talks to a zsh worker can hang when the transport is broken, so
such tests run their body in a child Python process through `run_py`, which
enforces a timeout. On timeout it walks `ps -Ao pid,ppid` from the child's PID
and kills the whole tree by explicit PID (printing the PIDs first). Each child
runs in its own session, so anything it leaves behind (a background job, a
worker of older Brish code) shares the child's process group and is found and
killed the same way. Brish starts its bootstraps in sessions of their own,
which the child records (see PRELUDE): what is left in those sessions is
found and killed too.

The suite runs in two modes, selected by the `BRISH_BINARY` environment
variable exactly as the library reads it. Run it twice:

    python -m pytest -q
    BRISH_BINARY=1 python -m pytest -q

The timing tests in `test_perf.py` are skipped unless `BRISH_TEST_PERF=1`.
"""

import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: The tangled readme examples run at import time, in-process, with no
#: timeout. `test_readme.py` runs them in a child instead.
collect_ignore = ["test_tangled1.py"]
TAG_VAR = "BRISH_TEST_TAG"
SESSION_TAG = uuid.uuid4().hex


def _bool_from_str(value):
    #: Same rule as `brish.brishmod.bool_from_str`, duplicated so that this
    #: module does not import brish before the worktree guard has run.
    if isinstance(value, str) and value.lower() in ("", "0", "n", "no", "false"):
        return False
    return bool(value)


BINARY = _bool_from_str(os.environ.get("BRISH_BINARY", ""))
MODE = "binary" if BINARY else "legacy"

binary_only = pytest.mark.skipif(not BINARY, reason="binary mode only")
legacy_only = pytest.mark.skipif(BINARY, reason="legacy mode only")

#: Tests that assert absolute timings run only on request, since a loaded
#: machine misses them without any regression.
PERF = _bool_from_str(os.environ.get("BRISH_TEST_PERF", ""))
perf_only = pytest.mark.skipif(not PERF, reason="timing test; set BRISH_TEST_PERF=1 to run it")

_SCRATCH = Path(tempfile.mkdtemp(prefix="brish-tests-"))
EMPTY_ZDOTDIR = _SCRATCH / "zdotdir"
EMPTY_ZDOTDIR.mkdir()


def pytest_report_header(config):
    return f"brish mode: {MODE} (BRISH_BINARY={os.environ.get('BRISH_BINARY', '')!r}); root: {ROOT}"


def ps_rows():
    """[(pid, ppid, pgid)] for every process."""
    out = subprocess.run(
        ["ps", "-Ao", "pid=,ppid=,pgid="], capture_output=True, text=True
    ).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3:
            rows.append(tuple(int(x) for x in parts))
    return rows


def descendants(root):
    children = {}
    for pid, ppid, _ in ps_rows():
        children.setdefault(ppid, []).append(pid)
    found, stack = [], [root]
    while stack:
        for kid in children.get(stack.pop(), []):
            found.append(kid)
            stack.append(kid)
    return found


def group_members(pgid):
    return [pid for pid, _, g in ps_rows() if g == pgid]


#: Where a child records the sessions that Brish starts (see PRELUDE): one
#: line "PID START" per bootstrap, START being time.time() just after the
#: spawn.
SESSIONS_FILE = ".brish-sessions"


def _started_at(pid):
    """The start time of a live `pid` (whole seconds), or None."""
    out = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    try:
        return time.mktime(time.strptime(out, "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None


def session_members(scratch):
    """The live processes of the sessions that a child's Brish started.

    Every process of such a session is in the bootstrap's process group (the
    workers do no job control). A PID is not reused while a process group of
    that ID exists, so the group's members are the session's, unless the
    leader died and the group emptied, and a new process took the PID: a
    live leader therefore has to have started when it was recorded.
    """
    try:
        lines = (Path(scratch) / SESSIONS_FILE).read_text().split("\n")
    except FileNotFoundError:
        return []
    sessions = {}
    for line in lines:
        parts = line.split()
        if len(parts) == 2:
            sessions[int(parts[0])] = float(parts[1])
    rows = ps_rows()
    live = {pid for pid, _, _ in rows}
    found = []
    for leader, start in sessions.items():
        if leader in live:
            began = _started_at(leader)
            if began is None or not (start - 3 <= began <= start + 1):
                continue  # not ours any more
        found += [pid for pid, _, g in rows if g == leader]
    return found


def kill_pids(pids, why):
    pids = [p for p in pids if p > 1 and p != os.getpid()]
    if not pids:
        return
    print(f"[conftest] {why}: SIGKILL {pids}", file=sys.stderr)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


class ChildResult:
    def __init__(self, rc, outb, errb, timed_out, orphans):
        self.rc = rc
        self.outb = outb
        self.errb = errb
        self.out = outb.decode("utf-8", "backslashreplace")
        self.err = errb.decode("utf-8", "backslashreplace")
        self.timed_out = timed_out
        self.orphans = orphans

    def __repr__(self):
        return (
            f"ChildResult(rc={self.rc!r}, timed_out={self.timed_out}, "
            f"orphans={self.orphans})\n--- stdout ---\n{self.out}\n--- stderr ---\n{self.err}"
        )

    def ok(self):
        return self.rc == 0 and not self.timed_out


#: Prepended to every child snippet.
PRELUDE = """\
import os, sys, time, signal, threading
ROOT = {root!r}
import brish, brish.brishmod as bm
from brish.brishmod import Brish, CmdResult
assert os.path.realpath(brish.__file__).startswith(os.path.realpath(ROOT) + os.sep), brish.__file__
SCRATCH = {scratch!r}
BINARY = {binary!r}
class _RecordSession(bm.Popen):
    #: Records every session that Brish starts, for run_py's orphan check.
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        if kw.get("start_new_session"):
            with open(os.path.join(SCRATCH, {sessions!r}), "a") as f:
                f.write("%d %f\\n" % (self.pid, time.time()))
bm.Popen = _RecordSession
"""


def run_py(code, timeout=60, real_env=False, env=None, cwd=None, allow_orphans=False, setup=""):
    """Run `code` in a child python with a timeout. Returns a ChildResult.

    `real_env=False` points ZDOTDIR at an empty directory, so the workers
    start fast and deterministically. `real_env=True` keeps the caller's zsh
    startup files. `setup` is more code, dedented on its own and run first.
    """
    scratch = tempfile.mkdtemp(prefix="child-", dir=_SCRATCH)
    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = str(ROOT)
    child_env[TAG_VAR] = f"{SESSION_TAG}-{uuid.uuid4().hex[:8]}"
    child_env["TMPDIR"] = scratch
    if not real_env:
        child_env["ZDOTDIR"] = str(EMPTY_ZDOTDIR)
    if env:
        for k, v in env.items():
            if v is None:
                child_env.pop(k, None)
            else:
                child_env[k] = v
    src = (
        PRELUDE.format(root=str(ROOT), scratch=scratch, binary=BINARY, sessions=SESSIONS_FILE)
        + textwrap.dedent(setup)
        + textwrap.dedent(code)
    )
    p = subprocess.Popen(
        [sys.executable, "-c", src],
        env=child_env,
        cwd=cwd or scratch,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        #: The tree walk finds live descendants; the group scan also finds
        #: ones already reparented (which can hold our output pipes open).
        tree = descendants(p.pid)
        group = [
            pid for pid in group_members(p.pid) + session_members(scratch)
            if pid not in tree
        ]
        kill_pids(
            list(reversed(tree)) + group + [p.pid],
            f"timeout after {timeout}s in child {p.pid}",
        )
        try:
            out, err = p.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            kill_pids(
                group_members(p.pid) + session_members(scratch),
                f"second pass for child {p.pid}",
            )
            out, err = p.communicate(timeout=10)
    #: Whatever is left in the child's process group or in a session that its
    #: Brish started is an orphan: a worker or a background job that outlived
    #: the child. Give shutdown a moment.
    orphans = []
    deadline = time.time() + 3
    while True:
        orphans = group_members(p.pid) + session_members(scratch)
        if not orphans or time.time() > deadline:
            break
        time.sleep(0.05)
    if orphans:
        kill_pids(orphans, f"orphans of child {p.pid}")
    res = ChildResult("TIMEOUT" if timed_out else p.returncode, out, err, timed_out, orphans)
    if orphans and not allow_orphans and res.ok():
        res.rc = "ORPHANS"
    return res


def check(code, **kw):
    """Run `code` via run_py and fail the test unless the child succeeded."""
    res = run_py(code, **kw)
    assert res.ok(), repr(res)
    return res


@pytest.fixture
def scratch_dir():
    return Path(tempfile.mkdtemp(prefix="t-", dir=_SCRATCH))


def pytest_sessionfinish(session, exitstatus):
    import shutil

    shutil.rmtree(_SCRATCH, ignore_errors=True)
