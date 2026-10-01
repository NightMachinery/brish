"""Worker locks and restarts.

A thread that holds a worker lock (acquire_lock, or a running BrishPopen) must
never restart the instance or wait for a restart: restart() takes the instance
lock and then waits for every worker lock, so such a thread deadlocks with it.
A pending restart is deferred until the next use by a thread that holds no
worker lock, and a call that finds its worker dead fails fast instead.
"""

from tests.conftest import check


def test_a_lock_holder_never_waits_for_a_restart():
    #: Thread A holds worker 0. Thread B's command exits worker 1, so B's next
    #: call restarts the instance: B takes the instance lock and waits for
    #: worker 0's lock. Before the fix, A's next call waited for the instance
    #: lock: a deadlock. Now A's call runs on its worker (the instance cannot
    #: be replaced while A holds the lock), and B restarts once A releases.
    check(
        r'''
        import faulthandler; faulthandler.dump_traceback_later(25, exit=True)
        b = Brish(server_count=2)
        lock, i = b.acquire_lock(server_index=0)
        assert b.send_cmd("v=kept", server_index=0).retcode == 0
        r = b.send_cmd("exit 3", server_index=1)
        assert r.retcode == 3, repr(r)
        got = {}
        def restarter():
            got["b"] = b.send_cmd("echo restarted", server_index=1)
        t = threading.Thread(target=restarter)
        t.start()
        time.sleep(0.5)  # B now waits for worker 0's lock inside restart()
        assert t.is_alive(), "the restart did not wait for the held lock"
        r = b.send_cmd("print -r -- a-$v", server_index=0)
        assert (r.retcode, r.out) == (0, "a-kept\n"), repr(r)
        r = b.z("print -r -- {'z'}-$v", server_index=0)
        assert r.out == "z-kept\n", repr(r)
        lock.release()
        t.join(10)
        assert not t.is_alive(), "the restart did not finish"
        assert got["b"].out == "restarted\n", repr(got["b"])
        r = b.send_cmd("print -r -- after-$v", server_index=0)
        assert r.out == "after-\n", repr(r)  # restarted: v is gone
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
            #: restart() and %BRISH_RESTART are only scheduled.
            assert b.restart() is False
            r = b.send_cmd("%BRISH_RESTART")
            assert r.out.startswith("Restart scheduled"), repr(r)
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
