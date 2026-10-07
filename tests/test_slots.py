"""Worker slots: one worker's death, or a restart, never stalls the others.

Each worker index is a slot with a lock of its own. A dead worker is replaced
alone, at its slot's next fresh acquire (or before it, by an eager
replacement), and a restart swaps in new workers wherever a slot's lock is
free, and elsewhere once the lock is released: neither waits for a worker in
use. A lock whose thread has ended gets a new lock and a new worker. See
readme.org, "When a Worker Dies", and docs/protocol.org, "Slots".

Run in both modes, as the rest of the suite. Bounds are relative to a boot
measured in the same child, so that a loaded machine does not fail them.
BRISH_SOAK_SECS sets the soak's length (default 60 seconds).
"""

import os
import stat
import subprocess
import sys

import pytest

from tests.conftest import BINARY, ROOT, check

V040 = "11a0f80"  # release 0.4.0, the last one before slots
SOAK_SECS = float(os.environ.get("BRISH_SOAK_SECS", "60"))

HELPERS = r'''
import collections, gc, random, traceback
from tests.conftest import ps_rows, session_members
STREAM = "while :; do print -r -- tick; sleep 0.1; done"
#: Ignores SIGINT: kill() ends it at step 4, which SIGKILLs its worker.
STUBBORN = "trap '' INT; while :; do sleep 0.1; done"
PID = "zmodload zsh/system; print -r -- $sysparams[pid]"

def boot_bound(n=1, **kw):
    """A generous bound for starting a worker: three measured boots of an
    instance with `n` workers, plus 3 s."""
    t = time.monotonic()
    x = Brish(server_count=n, **kw)
    dt = time.monotonic() - t
    x.cleanup()
    return 3 * dt + 3

def within(fn, secs):
    """fn() in a thread: ("ok", value), ("exc", exception) or ("STILL WAITING",)."""
    out = []
    def go():
        try:
            out.append(("ok", fn()))
        except BaseException as e:
            out.append(("exc", e))
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(secs)
    return out[0] if out else ("STILL WAITING",)

def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False

def settle(fn, secs=10):
    """fn() until it is falsy, for up to `secs`; its last value."""
    deadline = time.monotonic() + secs
    while True:
        v = fn()
        if not v or time.monotonic() > deadline:
            return v
        time.sleep(0.1)

def stray(b):
    """Live processes in the sessions that Brish started, other than those
    of the bootstraps that `b` still has open: an old bootstrap that was not
    closed once none of its workers was in use, or a process it left."""
    open_ = {p.pid for p in b._boots}
    members = set(session_members(SCRATCH))
    return [pid for pid, _, g in ps_rows() if pid in members and g not in open_]

def retired_alive(b):
    """Retired workers of open bootstraps that still run."""
    out = []
    for p in list(b._boots):
        for i in p.slot_ids:
            if not p.retired[i]:
                continue
            pid = p.workers[i].pid if p.binary else p.legacy_pids[i]
            if pid and bm._in_group(pid, p.pid):
                out.append(pid)
    return out

def all_locks_free(b):
    """Whether another thread can take every slot lock at once."""
    def take():
        got = []
        try:
            for lock in b.locks:
                if not lock.acquire(timeout=5):
                    return False
                got.append(lock)
            return True
        finally:
            for lock in got:
                lock.release()
    return within(take, 30) == ("ok", True)

class Streamer:
    """A thread that runs `setup` and STREAM on worker `i` and counts the
    ticks, until end() closes it. The BrishPopen holds the worker's lock
    meanwhile."""
    def __init__(self, b, i, setup=""):
        self.b, self.i = b, i
        self.ticks = 0
        self.stop = threading.Event()
        self.started = threading.Event()
        self.result = self.error = None
        self.t = threading.Thread(target=self.run, args=(setup,), daemon=True)
        self.t.start()
        assert self.started.wait(30), "the stream did not start"
    def run(self, setup):
        try:
            with self.b.popen(setup + STREAM, server_index=self.i) as p:
                for _, chunk in p:
                    self.ticks += chunk.count(b"tick")
                    self.started.set()
                    if self.stop.is_set():
                        break
            self.result = p.retcode
        except BaseException as e:
            self.error = e
            self.started.set()
    def ticks_grow(self, secs=1.0):
        n = self.ticks
        time.sleep(secs)
        return self.ticks > n
    def end(self):
        self.stop.set()
        self.t.join(30)
        assert not self.t.is_alive(), "the stream did not end"
        assert self.error is None, repr(self.error)
        return self.result
'''


def run(code, timeout=120, setup="", **kw):
    return check(code, setup=HELPERS + setup, timeout=timeout, **kw)


def test_a_dying_worker_stalls_no_other_worker():
    #: A streams on worker 0 without end. B's command on worker 1 ignores
    #: SIGINT, so kill() ends it by SIGKILLing worker 1 (9001). C's calls
    #: then complete within a boot-time bound, on a free worker and on worker
    #: 1, while A streams on. Before slots, the death restarted the whole
    #: instance at the next call, and the restart waited for A's lock, so C
    #: waited for A's stream to end: forever. Only public API, so that it
    #: also runs (and fails) on 0.4.0.
    res = run(
        r'''
        import faulthandler; faulthandler.dump_traceback_later(100, exit=True)
        bound = boot_bound()
        b = Brish(server_count=3)
        a = Streamer(b, 0, "v=a; ")
        p = b.popen(STUBBORN, server_index=1)
        p.kill_grace = 0.3
        time.sleep(0.3)
        p.kill()
        assert p.wait() == 9001, p.retcode
        t0 = time.monotonic()
        got = within(lambda: b.send_cmd("print -r -- ok"), bound)
        assert got[0] == "ok" and got[1].out == "ok\n", (got, bound)
        got = within(lambda: b.send_cmd("print -r -- ok $brish_server_index", server_index=1), bound)
        assert got[0] == "ok" and got[1].out == "ok 2\n", (got, bound)
        print(f"the calls after the death took {time.monotonic() - t0:.3f} s; "
              f"bound {bound:.2f} s")
        assert a.ticks_grow(), "A stopped streaming"
        assert a.end() == 130
        r = b.send_cmd("print -r -- $v", server_index=0)
        assert r.out == "a\n", repr(r)  # A's worker kept its state
        b.cleanup()
        ''',
    )
    print(res.out)


@pytest.mark.parametrize("eager", [True, False], ids=["eager", "lazy"])
def test_state_survives_a_neighbours_death(eager):
    #: Each worker sets its own variable; worker 1 dies three ways. The
    #: others keep their state, and worker 1's replacement is fresh, has
    #: the right $brish_server_index, has run boot_cmd, and sees neither
    #: variable that Brish passes its bootstrap.
    run(
        r'''
        import faulthandler; faulthandler.dump_traceback_later(100, exit=True)
        bound = boot_bound()
        b = Brish(server_count=3, boot_cmd="booted=yes")
        b.eager_replacement = EAGER
        for method in ("exit", "sigkill", "kill"):
            for i in range(3):
                assert b.send_cmd(f"v={method}{i}", server_index=i).retcode == 0
            if method == "exit":
                r = b.send_cmd("exit 3", server_index=1)
                assert r.retcode == 3, repr(r)
            elif method == "sigkill":
                pid = int(b.send_cmd(PID, server_index=1).out)
                os.kill(pid, signal.SIGKILL)
                assert not settle(lambda: alive(pid))  # see the leak test
            else:
                p = b.popen(STUBBORN, server_index=1)
                p.kill_grace = 0.3
                p.kill()
                assert p.wait() == 9001, p.retcode
            for i in (0, 2):
                r = b.send_cmd("print -r -- $v $brish_server_index", server_index=i)
                assert r.out == f"{method}{i} {i + 1}\n", (method, i, r)
            got = within(lambda: b.send_cmd(
                "print -r -- ${v-unset} $brish_server_index $booted "
                "${BRISH_SERVER_INDEX_OFFSET-unset} ${BRISH_SESSION-unset}",
                server_index=1), bound)
            assert got[0] == "ok", (method, got)
            assert got[1].out == "unset 2 yes unset unset\n", (method, got[1])
            for i in (0, 2):
                r = b.send_cmd("print -r -- $v", server_index=i)
                assert r.out == f"{method}{i}\n", (method, i, r)
        b.cleanup()
        ''',
        setup=f"EAGER = {eager!r}\n",
    )


def test_a_restart_never_waits_for_a_worker_in_use():
    #: restart() returns once the new workers are up and the free slots have
    #: them; a slot in use keeps its worker (and state) for its holder, and
    #: gets the new one at its next fresh acquire after the release. The same
    #: for %BRISH_RESTART sent by a thread that holds a worker lock.
    res = run(
        r'''
        import faulthandler; faulthandler.dump_traceback_later(100, exit=True)
        bound = boot_bound(3, boot_cmd="booted=yes")
        b = Brish(server_count=3, boot_cmd="booted=yes")
        for i in range(3):
            b.send_cmd(f"v=old{i}", server_index=i)
        a = Streamer(b, 0, "v=a; ")
        t0 = time.monotonic()
        got = within(b.restart, bound)
        dt = time.monotonic() - t0
        assert got == ("ok", False), (got, bound)
        print(f"restart() with a stream running took {dt:.3f} s; bound {bound:.2f} s")
        for i in (1, 2):
            got = within(lambda: b.send_cmd("print -r -- ${v-unset} $booted", server_index=i), 5)
            assert got[0] == "ok" and got[1].out == "unset yes\n", (i, got)
        assert a.ticks_grow(), "A stopped streaming"
        assert a.end() == 130
        r = b.send_cmd("print -r -- ${v-unset} $booted $brish_server_index", server_index=0)
        assert r.out == "unset yes 1\n", repr(r)

        #: %BRISH_RESTART from a thread that holds worker 0.
        for i in range(3):
            b.send_cmd(f"v=two{i}", server_index=i)
        lock, _ = b.acquire_lock(server_index=0)
        try:
            r = b.send_cmd("%BRISH_RESTART")
            assert r.out.startswith("Restarted; ") and "worker 0" in r.out, repr(r)
            for i in (1, 2):
                r = b.send_cmd("print -r -- ${v-unset}", server_index=i)
                assert r.out == "unset\n", (i, r)
            r = b.send_cmd("print -r -- ${v-unset}", server_index=0)
            assert r.out == "two0\n", repr(r)  # its holder keeps the old worker
        finally:
            lock.release()
        r = b.send_cmd("print -r -- ${v-unset} $booted", server_index=0)
        assert r.out == "unset yes\n", repr(r)
        assert b.restart() is True
        b.cleanup()
        ''',
    )
    print(res.out)


def test_a_held_dead_worker_fails_fast_and_blocks_nobody_else():
    #: A thread holds worker 0 (acquire_lock), whose worker dies: its next
    #: call there raises BrishWorkerDiedException at once, other threads
    #: are not blocked, and after the release the slot has a new worker.
    run(
        r'''
        import faulthandler; faulthandler.dump_traceback_later(100, exit=True)
        bound = boot_bound()
        b = Brish(server_count=2)
        lock, _ = b.acquire_lock(server_index=0)
        released = False
        try:
            assert b.send_cmd("v=held", server_index=0).retcode == 0
            assert b.send_cmd("exit 4", server_index=0).retcode == 4
            t0 = time.monotonic()
            for call in (b.send_cmd, b.popen):
                try:
                    call("print -r no", server_index=0)
                    raise SystemExit("no BrishWorkerDiedException")
                except bm.BrishWorkerDiedException as e:
                    assert "holds a worker lock" in str(e), e
            assert time.monotonic() - t0 < 3, time.monotonic() - t0
            got = within(lambda: b.send_cmd("print -r other"), 5)
            assert got[0] == "ok" and got[1].out == "other\n", got
            waiter = {}
            t = threading.Thread(target=lambda: waiter.update(
                r=b.send_cmd("print -r -- ${v-unset} $brish_server_index", server_index=0)))
            t.start()
            time.sleep(0.5)
            assert t.is_alive(), "worker 0 was taken while its lock was held"
            lock.release()
            released = True
            t.join(bound)
            assert not t.is_alive(), "no new worker after the release"
            assert waiter["r"].out == "unset 1\n", repr(waiter["r"])
        finally:
            if not released:
                lock.release()
        b.cleanup()
        ''',
    )


def test_a_lock_of_an_ended_thread_gets_a_new_lock_and_worker():
    #: A thread that ends holding a worker lock (it never released an
    #: acquire_lock, or it left a BrishPopen running and referenced
    #: elsewhere) can never release it. The slot gets a new lock and a new
    #: worker; the old worker and its command are stopped, and no worker is
    #: lost. Before slots, such a lock stalled every call that waited for it,
    #: and restart() and cleanup() with them.
    run(
        r'''
        import faulthandler; faulthandler.dump_traceback_later(100, exit=True)
        bound = boot_bound()
        b = Brish(server_count=2)

        def leak_lock():
            b.acquire_lock(server_index=0)
            b.send_cmd("v=leaked", server_index=0)
        t = threading.Thread(target=leak_lock)
        t.start()
        t.join()
        old = b.locks[0]
        got = within(lambda: b.send_cmd("print -r -- ${v-unset} $brish_server_index",
                                        server_index=0), bound)
        assert got[0] == "ok" and got[1].out == "unset 1\n", got
        assert b.locks[0] is not old

        #: server_index=None, with the only lock held by an ended thread.
        one = Brish(server_count=1)
        t = threading.Thread(target=one.acquire_lock)
        t.start()
        t.join()
        got = within(lambda: one.send_cmd("print -r none"), bound)
        assert got[0] == "ok" and got[1].out == "none\n", got
        one.cleanup()

        #: A BrishPopen that streams on, kept alive after its thread ended.
        keep = {}
        def leak_popen():
            p = b.popen("v=p; " + STREAM, server_index=1)
            next(p)
            keep["p"], keep["pid"] = p, p._worker_pid
        t = threading.Thread(target=leak_popen)
        t.start()
        t.join()
        got = within(lambda: b.send_cmd("print -r -- ${v-unset} $brish_server_index",
                                        server_index=1), bound)
        assert got[0] == "ok" and got[1].out == "unset 2\n", got
        assert not settle(lambda: alive(keep["pid"])), "the old worker runs on"
        #: Collected now (an orphan of an ended thread): it reads what is
        #: left of its old worker's pipes, and closes them.
        del keep["p"]
        gc.collect()
        got = within(lambda: b.send_cmd("print -r -- ${v-unset}", server_index=1), 10)
        assert got[0] == "ok" and got[1].out == "unset\n", got

        #: No worker is lost: two threads use both workers at once.
        outs = {}
        def use(i):
            outs[i] = b.send_cmd("sleep 0.5; print -r -- $brish_server_index", server_index=i).out
        ts = [threading.Thread(target=use, args=(i,)) for i in range(2)]
        t0 = time.monotonic()
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        assert outs == {0: "1\n", 1: "2\n"}, outs
        assert time.monotonic() - t0 < 5
        assert all_locks_free(b)
        assert not settle(lambda: stray(b)), stray(b)
        b.cleanup()
        assert not settle(lambda: session_members(SCRATCH)), session_members(SCRATCH)
        ''',
    )


def test_old_bootstraps_close_and_nothing_leaks():
    #: Many deaths and restarts, also while a worker is in use: a bootstrap
    #: is closed once none of its workers is in use or pending, retired
    #: workers do not run on, and cleanup() leaves no process behind.
    run(
        r'''
        import faulthandler; faulthandler.dump_traceback_later(170, exit=True)
        b = Brish(server_count=3, boot_cmd="booted=yes")
        for k in range(12):
            i, kind = k % 3, k % 4
            if kind == 0:
                assert b.send_cmd("exit 1", server_index=i).retcode == 1
            elif kind == 1:
                assert b.restart() is True
            elif kind == 2:
                pid = int(b.send_cmd(PID, server_index=i).out)
                os.kill(pid, signal.SIGKILL)
                #: Gone before the next request: in legacy mode, a request
                #: that reaches a dying worker gets 9001 (the frozen wire
                #: cannot tell whether it ran).
                assert not settle(lambda: alive(pid))
            else:
                p = b.popen(STUBBORN, server_index=i)
                p.kill_grace = 0.2
                p.kill()
                assert p.wait() == 9001
            for j in range(3):
                r = b.send_cmd("print -r -- ok $booted", server_index=j)
                assert r.out == "ok yes\n", (k, j, r)
        #: Three restarts while worker 0 streams: each replaces the last one's
        #: unused pending worker for slot 0.
        a = Streamer(b, 0)
        for _ in range(3):
            assert b.restart() is False
        assert a.end() == 130
        for j in range(3):
            assert b.send_cmd("print -r ok", server_index=j).out == "ok\n"
        assert not settle(lambda: stray(b)), stray(b)
        assert not settle(lambda: retired_alive(b)), retired_alive(b)
        assert not b._warmers
        assert {id(p) for p in b._boots} == {id(s.p) for s in b._slots}, b._boots
        assert all(s.pending is None for s in b._slots)
        print("open bootstraps:", len(b._boots), file=sys.stderr)
        b.cleanup()
        assert not b._boots
        assert not settle(lambda: session_members(SCRATCH)), session_members(SCRATCH)
        ''',
        timeout=180,
    )


def test_soak():
    #: Random deaths (exit N, a worker that SIGKILLs itself, and SIGKILLs
    #: from outside at any time), restarts and %BRISH_RESTART, commands
    #: that ignore SIGINT and are killed, endless streams closed at random,
    #: and plain calls, all at once. Every result is a documented one, no
    #: thread hangs, every lock ends up free, and no process leaks.
    res = run(
        r'''
        import faulthandler; faulthandler.dump_traceback_later(SECS + 150, exit=True)
        N = 4
        b = Brish(server_count=N, boot_cmd="booted=yes")
        stop = time.monotonic() + SECS
        errors, counts = [], collections.Counter()
        NOTE = bm.WORKER_DIED_NOTE

        def died(r):
            return r.retcode == 9001 and NOTE in r.err

        def died_after(r, out):
            #: Legacy mode, as in 0.4.0: an outside SIGKILL that lands after
            #: the worker wrote its reply's end on stdout, with the status,
            #: but before it ended stderr. The status is the command's own,
            #: and the note says that the worker died.
            return (not b.binary and r.retcode == 0 and r.out == out
                    and r.err == NOTE + "\n")

        def plain(rng):
            try:
                r = b.send_cmd("print -r -- ok-$booted")
            except bm.BrishWorkerDiedException:
                return "twice"  # killed from outside twice before it ran
            if died(r):
                return "killed"
            if died_after(r, "ok-yes\n"):
                return "killed-after"
            assert (r.retcode, r.out, r.err) == (0, "ok-yes\n", ""), repr(r)
            return "ok"

        def indexed(rng):
            i = rng.randrange(N)
            try:
                r = b.send_cmd("print -r -- $brish_server_index $booted", server_index=i)
            except bm.BrishWorkerDiedException:
                return "twice"
            if died(r):
                return "killed"
            if died_after(r, f"{i + 1} yes\n"):
                return "killed-after"
            assert (r.retcode, r.out, r.err) == (0, f"{i + 1} yes\n", ""), (i, repr(r))
            return "ok"

        def exiter(rng):
            time.sleep(rng.uniform(0.2, 1.0))
            n = rng.randrange(1, 100)
            try:
                r = b.send_cmd(f"exit {n}", server_index=rng.randrange(N))
            except bm.BrishWorkerDiedException:
                return "twice"
            if died(r):
                return "killed"
            assert r.retcode == n, (n, repr(r))
            return "exit"

        def self_killer(rng):
            time.sleep(rng.uniform(0.2, 1.0))
            try:
                r = b.send_cmd("zmodload zsh/system; kill -KILL $sysparams[pid]",
                               server_index=rng.randrange(N))
            except bm.BrishWorkerDiedException:
                return "twice"
            assert died(r), repr(r)
            return "killed"

        def outside_killer(rng):
            time.sleep(rng.uniform(0.5, 2.0))
            slots = b._slots
            i = rng.randrange(N)
            p = slots[i].p
            if p.binary:
                pid = p.workers[i].pid
            else:
                bm._legacy_read_pids(p, 1.0)
                pid = p.legacy_pids[i]
            if pid and not p.retired[i] and bm._in_group(pid, p.pid):
                os.kill(pid, signal.SIGKILL)
                return "sigkill"
            return "none"

        def restarter(rng):
            time.sleep(rng.uniform(0.5, 2.0))
            if rng.random() < 0.5:
                assert b.restart() in (True, False)
                return "restart"
            r = b.send_cmd("%BRISH_RESTART")
            assert r.out.startswith("Restarted"), repr(r)
            return "%BRISH_RESTART"

        def stubborn(rng):
            p = b.popen(STUBBORN)
            p.kill_grace = 0.2
            time.sleep(rng.uniform(0, 0.5))
            p.kill()
            rc = p.wait()
            assert rc == 9001, rc
            return "stubborn"

        def streamer(rng):
            n = rng.randrange(1, 20)
            with b.popen(STREAM) as p:
                for _, chunk in p:
                    n -= chunk.count(b"tick")
                    if n <= 0:
                        break
            #: 130 when closed; 9001 when killed from outside meanwhile.
            assert p.retcode in (130, 9001), p.retcode
            return f"stream-{p.retcode}"

        def loop(name, fn, seed):
            rng = random.Random(seed)
            try:
                while time.monotonic() < stop:
                    counts[name + ":" + fn(rng)] += 1
            except BaseException:
                errors.append((name, traceback.format_exc()))

        kinds = ([("plain", plain)] * 3 + [("indexed", indexed)] * 2
                 + [("exiter", exiter), ("self_killer", self_killer),
                    ("outside_killer", outside_killer), ("restarter", restarter),
                    ("stubborn", stubborn)] + [("streamer", streamer)] * 2)
        ts = [threading.Thread(target=loop, args=(name, fn, k), daemon=True)
              for k, (name, fn) in enumerate(kinds)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(SECS + 90)
        hung = [t.name for t in ts if t.is_alive()]
        assert not hung, ("deadlock", hung)
        assert not errors, errors[:3]
        print("soak:", dict(sorted(counts.items())))
        for name, _ in kinds:
            assert any(k.startswith(name + ":") for k in counts), (name, counts)
        assert all_locks_free(b)
        for i in range(N):
            r = b.send_cmd("print -r -- $brish_server_index $booted", server_index=i)
            assert r.out == f"{i + 1} yes\n", (i, r)
        assert not settle(lambda: stray(b)), stray(b)
        assert not settle(lambda: retired_alive(b)), retired_alive(b)
        b.cleanup()
        assert not settle(lambda: session_members(SCRATCH)), session_members(SCRATCH)
        ''',
        setup=f"SECS = {SOAK_SECS!r}\n",
        timeout=SOAK_SECS + 240,
    )
    print(res.out)


CANCEL_HELPERS = r"""
SENTINEL = os.path.join(SCRATCH, "sentinel")
CMD = "print -r -- ran; : > " + SENTINEL
class Flag:
    #: A `cancelled` callable that turns true `after` seconds from now, and
    #: counts its calls.
    def __init__(self, after):
        self.at = time.monotonic() + after
        self.calls = 0
    def __call__(self):
        self.calls += 1
        return time.monotonic() >= self.at
class Holder:
    #: A thread that holds worker i's lock until release().
    def __init__(self, b, i):
        self.go, self.done = threading.Event(), threading.Event()
        def run():
            lock, _ = b.acquire_lock(server_index=i)
            self.go.set()
            self.done.wait()
            lock.release()
        self.t = threading.Thread(target=run, daemon=True)
        self.t.start()
        assert self.go.wait(30)
    def release(self):
        self.done.set()
        self.t.join(30)
def cancelled_in(fn, secs=30):
    #: (seconds until fn() raised BrishCancelledException, its message).
    t = time.monotonic()
    try:
        fn()
    except bm.BrishCancelledException as e:
        return time.monotonic() - t, str(e)
    raise AssertionError("not cancelled")
"""


def test_a_cancelled_call_that_waits_for_a_worker_runs_nothing():
    #: popen, send_cmd, z, zpopen and acquire_lock with cancelled=f call f
    #: while they wait for a worker that other threads hold, and raise
    #: BrishCancelledException soon after it turns true (not a lock_sleep
    #: later), without the lock; an exception from f propagates the same
    #: way. Nothing runs, and the workers serve the next calls.
    run(
        r"""
        import faulthandler; faulthandler.dump_traceback_later(100, exit=True)
        b = Brish(server_count=2)
        for i in range(2):
            b.send_cmd(f"v=kept{i}", server_index=i)
        h0, h1 = Holder(b, 0), Holder(b, 1)
        for call in (
            lambda f: b.popen(CMD, server_index=0, cancelled=f),
            lambda f: b.popen(CMD, cancelled=f),  # server_index=None, lock_sleep=1
            lambda f: b.popen(CMD, lock_sleep=None, cancelled=f),
            lambda f: b.send_cmd(CMD, server_index=1, cancelled=f),
            lambda f: b.send_cmd(CMD, cancelled=f),
            lambda f: b.z("{CMD:e}", cancelled=f),
            lambda f: b.zpopen("{CMD:e}", server_index=1, cancelled=f),
            lambda f: b.acquire_lock(cancelled=f),
            lambda f: b.acquire_lock(server_index=0, cancelled=f),
        ):
            f = Flag(0.3)
            dt, msg = cancelled_in(lambda: call(f))
            assert 0.25 < dt < 0.9, dt
            assert f.calls >= 2, f.calls  # it polled while it waited (once is enough on a loaded machine)
            assert "nothing ran" in msg, msg
        def boom():
            raise ZeroDivisionError("from cancelled")
        try:
            b.popen(CMD, server_index=0, cancelled=boom)
        except ZeroDivisionError:
            pass
        else:
            raise AssertionError("the callable's exception did not propagate")
        h0.release()
        h1.release()
        assert not os.path.exists(SENTINEL)
        assert all_locks_free(b)
        for i in range(2):
            r = b.send_cmd("print -r -- $v", server_index=i)
            assert r.out == f"kept{i}\n", (i, r)
        #: A free worker: cancelled is asked once, just before the request.
        f = Flag(0)
        dt, _ = cancelled_in(lambda: b.popen(CMD, cancelled=f))
        assert f.calls == 1, f.calls
        f = Flag(0)
        dt, _ = cancelled_in(lambda: b.acquire_lock(server_index=1, cancelled=f))
        assert f.calls == 1, f.calls
        never = lambda: False
        lock, i = b.acquire_lock(server_index=1, cancelled=never)
        lock.release()
        with b.popen("print -r -- fine", cancelled=never) as p:
            assert p.wait() == 0
        assert b.send_cmd("print -r -- fine", cancelled=never).out == "fine\n"
        assert not os.path.exists(SENTINEL)
        b.cleanup()
        """,
        setup=CANCEL_HELPERS,
    )


@pytest.mark.parametrize("eager", [True, False], ids=["eager", "lazy"])
def test_a_cancelled_call_keeps_the_replacement_it_waited_for(eager):
    #: Worker 0 died. The next popen on it waits for its replacement (eager:
    #: the one starting in the background; lazy: one it starts itself, which
    #: is not interrupted), and its client goes away meanwhile. It raises
    #: BrishCancelledException without running anything: eagerly as soon as
    #: it sees the flag, lazily once the boot is done. The replacement stays
    #: in the slot, so the next call does not boot again, and nothing leaks.
    run(
        r"""
        import faulthandler; faulthandler.dump_traceback_later(100, exit=True)
        BOOT = 1.5
        b = Brish(server_count=2, boot_cmd=f"sleep {BOOT}; booted=yes")
        b.eager_replacement = EAGER
        b.send_cmd("v=kept1", server_index=1)
        assert b.send_cmd("exit 3", server_index=0).retcode == 3
        f = Flag(0.3)
        dt, _ = cancelled_in(lambda: b.popen(CMD, server_index=0, cancelled=f))
        if EAGER:
            assert dt < BOOT - 0.3, dt  # did not wait for the boot to end
        else:
            assert dt > BOOT - 0.1, dt  # the boot it started ran to its end
        assert not os.path.exists(SENTINEL)
        if EAGER:
            assert settle(lambda: b._slots[0].warming is not None, 30) is False
        t = time.monotonic()
        r = b.send_cmd("print -r -- ${booted-unset} $brish_server_index ${v-unset}", server_index=0)
        took = time.monotonic() - t
        assert r.out == "yes 1 unset\n", repr(r)
        assert took < BOOT - 0.3, took  # it found the replacement ready
        r = b.send_cmd("print -r -- $v", server_index=1)
        assert r.out == "kept1\n", repr(r)
        #: The callable's own exception, during the wait for an eager boot.
        if EAGER:
            assert b.send_cmd("exit 4", server_index=0).retcode == 4
            def boom():
                raise ZeroDivisionError("from cancelled")
            try:
                b.popen(CMD, server_index=0, cancelled=boom)
            except ZeroDivisionError:
                pass
            else:
                raise AssertionError("the callable's exception did not propagate")
            r = b.send_cmd("print -r -- ${booted-unset}", server_index=0)
            assert r.out == "yes\n", repr(r)
        assert not os.path.exists(SENTINEL)
        assert all_locks_free(b)
        assert not settle(lambda: stray(b)), stray(b)
        assert not settle(lambda: retired_alive(b)), retired_alive(b)
        b.cleanup()
        assert not settle(lambda: session_members(SCRATCH)), session_members(SCRATCH)
        """,
        setup=CANCEL_HELPERS + f"EAGER = {eager!r}\n",
    )


def git_show(rev, path):
    p = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{rev}:{path}"], capture_output=True
    )
    if p.returncode != 0:
        pytest.skip(f"git history not available ({rev}:{path})")
    return p.stdout


@pytest.fixture(scope="module")
def v040(tmp_path_factory):
    """Release 0.4.0: its brishmod.py and quoting.py, and its worker scripts
    in v040/, next to which its brishmod is loaded to run them."""
    d = tmp_path_factory.mktemp("v040")
    (d / "v040").mkdir()
    for name in ("brish2.zsh", "brish3.zsh", "trapint.zsh"):
        f = d / "v040" / name
        f.write_bytes(git_show(V040, "brish/" + name))
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    (d / "brishmod_v040.py").write_bytes(git_show(V040, "brish/brishmod.py"))
    (d / "quoting_v040.py").write_bytes(git_show(V040, "brish/quoting.py"))
    return d


LOADERS = r'''
from tests.wire_corpus import load_brishmod, api_corpus
SRC = @SRC@
def src(name):
    with open(os.path.join(SRC, name)) as f:
        return f.read()
OLD_FILE = os.path.join(SRC, "v040", "brishmod.py")
TREE_FILE = os.path.join(ROOT, "brish", "brishmod_v040_test.py")
def v040_on(file, tag):
    """0.4.0's brishmod, running the worker scripts next to `file`."""
    return load_brishmod("brishmod_v040_" + tag, src("brishmod_v040.py"), file,
                         quoting_src=src("quoting_v040.py"))
def diff(a, b):
    assert len(a) == len(b), (len(a), len(b))
    return [(x[0], x[1], y[1]) for x, y in zip(a, b) if x != y]
'''


def loaders(d):
    return LOADERS.replace("@SRC@", repr(str(d)))


@pytest.mark.skipif(
    sys.version_info >= (3, 14),
    reason="the 0.4.0 templates read ast.Constant.s, which Python 3.14 removed",
)
def test_0_4_0_python_runs_these_worker_scripts_as_its_own(v040):
    #: 0.4.0's Python spawns the worker scripts next to its file. With this
    #: tree's scripts (which read the index offset only when it is set, and
    #: 0.4.0 never sets it), every result is the one it gets from its own
    #: scripts: the API corpus, and a death and restart with three workers.
    check(
        r'''
        import faulthandler; faulthandler.dump_traceback_later(170, exit=True)
        def deaths(mod):
            b = mod.Brish(server_count=3, binary=BINARY, boot_cmd="booted=yes")
            res = []
            q = "print -r -- ${v-unset} $brish_server_index $booted ${BRISH_SERVER_INDEX_OFFSET-unset}"
            for i in range(3):
                b.send_cmd(f"v=kept{i}", server_index=i)
            r = b.send_cmd("exit 3", server_index=1)
            res.append((r.retcode, r.out, r.err))
            for i in range(3):
                r = b.send_cmd(q, server_index=i)
                res.append((r.retcode, r.out, r.err))
            for i in range(3):
                b.send_cmd(f"v=again{i}", server_index=i)
            res.append(b.restart())
            for i in range(3):
                r = b.send_cmd(q, server_index=i)
                res.append((r.retcode, r.out, r.err))
            b.cleanup()
            return res
        want = api_corpus(v040_on(OLD_FILE, "o"), SCRATCH, binary=BINARY)
        got = api_corpus(v040_on(TREE_FILE, "t"), SCRATCH, binary=BINARY)
        bad = diff(want, got)
        assert not bad, (len(bad), bad[:3])
        want = deaths(v040_on(OLD_FILE, "o2"))
        got = deaths(v040_on(TREE_FILE, "t2"))
        assert want == got, (want, got)
        #: 0.4.0 restarts the whole instance after a death.
        assert got[1][1] == "unset 1 yes unset\n", got
        ''',
        setup=loaders(v040),
        timeout=180,
    )


def test_send_cmd_costs_what_it_did_in_0_4_0(v040):
    #: send_cmd("true"), interleaved with 0.4.0's Python on its own scripts,
    #: in this mode: with one worker and server_index=None, and with four
    #: workers and an explicit server_index. The medians are printed; the
    #: bound is loose, since a loaded machine makes single runs noisy.
    res = check(
        r'''
        import statistics
        T = time.perf_counter
        Old = v040_on(OLD_FILE, "perf").Brish

        def per_call(b, n, kw):
            t = T()
            for _ in range(n):
                b.send_cmd("true", **kw)
            return (T() - t) / n

        for n, kw in ((1, {}), (4, {"server_index": 2})):
            old = Old(server_count=n, binary=BINARY)
            new = Brish(server_count=n, binary=BINARY)
            for b in (old, new):
                per_call(b, 100, kw)
            lat = {"old": [], "new": []}
            for k in range(20):
                for name in (("old", "new") if k % 2 == 0 else ("new", "old")):
                    lat[name].append(per_call(old if name == "old" else new, 300, kw))
            om, nm = statistics.median(lat["old"]), statistics.median(lat["new"])
            print(f"send_cmd('true') {'binary' if BINARY else 'legacy'}, {n} workers {kw}: "
                  f"0.4.0 {om * 1e6:.1f} us, slots {nm * 1e6:.1f} us, ratio {nm / om:.3f}")
            assert nm <= om * 1.25 + 10e-6, (om, nm)
            old.cleanup()
            new.cleanup()
        ''',
        setup=loaders(v040),
        timeout=300,
    )
    print(res.out)


def test_cancelled_is_never_called_under_the_instance_lock():
    #: A cancelled() that takes a lock of the caller's own, held by a thread
    #: that needs the instance lock (a fresh acquire), while a cleanup runs:
    #: the call that waits for the cleanup must not hold the instance lock
    #: while it asks, or the three threads wait for each other for good.
    run(
        r"""
        import faulthandler
        b = Brish(server_count=2)
        U = threading.Lock()
        flag = {"v": False}
        def cancelled():
            with U:
                return flag["v"]
        ready, go = threading.Event(), threading.Event()
        res = {}
        def holder():
            lock, _ = b.acquire_lock(server_index=0)
            ready.set()
            go.wait()
            with U:
                time.sleep(0.3)  # the waiter is in cancelled() meanwhile
                res["h"] = b.send_cmd("print -r -- h", server_index=1).out
            lock.release()
        def cleaner():
            b.cleanup()
            res["c"] = True
        def waiter():
            try:
                b.send_cmd("true", cancelled=cancelled)
                res["w"] = "ran"
            except Exception as e:
                res["w"] = type(e).__name__
        th = threading.Thread(target=holder, daemon=True); th.start(); assert ready.wait(30)
        tc = threading.Thread(target=cleaner, daemon=True); tc.start(); time.sleep(0.3)
        tw = threading.Thread(target=waiter, daemon=True); tw.start(); time.sleep(0.3)
        go.set()
        for t in (th, tc, tw):
            t.join(20)
        if any(t.is_alive() for t in (th, tc, tw)):
            faulthandler.dump_traceback(all_threads=True)
            os._exit(3)
        assert res["h"] == "h\n" and res["c"], res
        """
    )


def test_the_index_offset_check_ignores_the_startup_files_options():
    #: Startup files that leave sh_glob on (or emulate sh): the worker
    #: scripts still accept the offset, and its default, 0.
    run(
        r"""
        zd = os.path.join(SCRATCH, "zd-shglob")
        os.makedirs(zd)
        with open(os.path.join(zd, ".zshenv"), "w") as f:
            f.write("setopt sh_glob\n")
        os.environ["ZDOTDIR"] = zd
        b = Brish(server_count=2)
        try:
            assert b.send_cmd("print -r -- $brish_server_index", server_index=1).out == "2\n"
            assert b.send_cmd("exit 3", server_index=1).retcode == 3
            #: The replacement bootstrap gets the offset 1.
            r = b.send_cmd("print -r -- $brish_server_index", server_index=1)
            assert (r.retcode, r.out) == (0, "2\n"), r
        finally:
            b.cleanup()
        """
    )


def test_a_restart_shares_only_a_restart_that_began_after_it():
    #: A restart that was already starting its workers when restart() was
    #: called may have read the startup files before a change the caller
    #: made: the call must start its own set, not return that one's.
    run(
        r"""
        cfg = os.path.join(SCRATCH, "cfg")
        zd = os.path.join(SCRATCH, "zd-slow")
        os.makedirs(zd)
        with open(os.path.join(zd, ".zshenv"), "w") as f:
            f.write('probe_cfg=$(<$PROBE_CFG); [[ -n $PROBE_SLOW ]] && sleep 1.5\n')
        open(cfg, "w").write("old")
        os.environ.update(ZDOTDIR=zd, PROBE_CFG=cfg)
        b = Brish(server_count=2)
        try:
            os.environ["PROBE_SLOW"] = "1"
            got = {}
            t = threading.Thread(target=lambda: got.setdefault("x", b.restart()))
            t.start()
            time.sleep(0.6)  # that restart's bootstrap has read "old" and sleeps
            open(cfg, "w").write("new")
            assert b.restart() in (True, False)
            t.join(30)
            outs = [b.send_cmd("print -r -- $probe_cfg", server_index=i).out for i in range(2)]
            assert outs == ["new\n", "new\n"], outs
        finally:
            b.cleanup()
        """
    )


def test_brish_restart_checks_cancelled_first():
    run(
        r"""
        b = Brish(server_count=1)
        try:
            b.send_cmd("v=kept")
            for call in (lambda: b.send_cmd("%BRISH_RESTART", cancelled=lambda: True),
                         lambda: b.popen("%BRISH_RESTART", cancelled=lambda: True)):
                try:
                    call()
                    raise AssertionError("no BrishCancelledException")
                except bm.BrishCancelledException:
                    pass
                assert b.send_cmd("print -r -- ${v-unset}").out == "kept\n"
            #: Not cancelled: it restarts.
            assert b.send_cmd("%BRISH_RESTART", cancelled=lambda: False).out == "Restarted succesfully."
            assert b.send_cmd("print -r -- ${v-unset}").out == "unset\n"
        finally:
            b.cleanup()
        """
    )
