"""Worker locks and restarts.

A thread that holds a worker lock (acquire_lock, or a running BrishPopen)
keeps that worker until it releases the lock: a dead worker is replaced, and a
restart swaps in a new one, only at the slot's next fresh acquire. So a call
that finds its held worker dead fails fast, and neither a restart nor another
slot's replacement ever waits for a held lock.
"""

from tests.conftest import check


def test_a_lock_holder_never_waits_for_a_restart():
    #: Thread A holds worker 0. Thread B's command exits worker 1, so B's next
    #: call on worker 1 gets a new worker. Before slots, B restarted the whole
    #: instance and waited for worker 0's lock (and A's next call waited for
    #: B, a deadlock, until the restart was deferred). Now B replaces worker 1
    #: alone, without waiting for A, and worker 0 keeps its state.
    check(
        r'''
        import faulthandler; faulthandler.dump_traceback_later(25, exit=True)
        b = Brish(server_count=2)
        lock, i = b.acquire_lock(server_index=0)
        assert b.send_cmd("v=kept", server_index=0).retcode == 0
        r = b.send_cmd("exit 3", server_index=1)
        assert r.retcode == 3, repr(r)
        got = {}
        def replacer():
            got["b"] = b.send_cmd("echo replaced", server_index=1)
        t = threading.Thread(target=replacer)
        t.start()
        t.join(15)
        assert not t.is_alive(), "worker 1's replacement waited for worker 0's lock"
        assert got["b"].out == "replaced\n", repr(got["b"])
        r = b.send_cmd("print -r -- a-$v", server_index=0)
        assert (r.retcode, r.out) == (0, "a-kept\n"), repr(r)
        r = b.z("print -r -- {'z'}-$v", server_index=0)
        assert r.out == "z-kept\n", repr(r)
        lock.release()
        r = b.send_cmd("print -r -- after-$v", server_index=0)
        assert r.out == "after-kept\n", repr(r)  # worker 0 was never replaced
        b.cleanup()
        ''',
        timeout=40,
    )


def test_a_dead_worker_fails_fast_while_its_lock_is_held():
    check(
        r'''
        import faulthandler; faulthandler.dump_traceback_later(25, exit=True)
        b = Brish(server_count=2)
        lock, i = b.acquire_lock(server_index=0)
        try:
            r = b.send_cmd("exit 4", server_index=0)
            assert r.retcode == 4, repr(r)
            t = time.monotonic()
            try:
                b.send_cmd("echo no", server_index=0)
                raise SystemExit("no BrishWorkerDiedException")
            except bm.BrishWorkerDiedException as e:
                assert "holds a worker lock" in str(e), e
            assert time.monotonic() - t < 3, time.monotonic() - t
            #: The other worker still runs commands for this thread.
            r = b.send_cmd("echo other", server_index=1)
            assert r.out == "other\n", repr(r)
            #: restart() and %BRISH_RESTART replace worker 1 at once, and
            #: worker 0 only after the release.
            assert b.restart() is False
            r = b.send_cmd("%BRISH_RESTART")
            assert r.out.startswith("Restarted; ") and "worker 0" in r.out, repr(r)
            assert "worker 1" not in r.out, repr(r)
        finally:
            lock.release()
        r = b.send_cmd("echo yes", server_index=0)
        assert (r.retcode, r.out) == (0, "yes\n"), repr(r)
        b.cleanup()
        ''',
        timeout=40,
    )


def test_holding_two_locks_does_not_deadlock_with_a_restart():
    #: A thread may hold two worker locks at once; a concurrent restart takes
    #: the locks without blocking on one while holding another.
    check(
        r'''
        import faulthandler; faulthandler.dump_traceback_later(25, exit=True)
        b = Brish(server_count=3)
        stop = time.time() + 3
        errors = []
        def holder():
            while time.time() < stop:
                l0, _ = b.acquire_lock(server_index=0)
                try:
                    time.sleep(0.01)
                    l1, _ = b.acquire_lock(server_index=1)
                    try:
                        r = b.send_cmd("echo ok", server_index=1)
                        if r.out != "ok\n":
                            errors.append(r)
                    finally:
                        l1.release()
                finally:
                    l0.release()
        def restarter():
            while time.time() < stop:
                b.restart()
                time.sleep(0.05)
        ts = [threading.Thread(target=holder), threading.Thread(target=restarter)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(15)
            assert not t.is_alive(), "deadlock"
        assert not errors, errors[:3]
        b.cleanup()
        ''',
        timeout=40,
    )
