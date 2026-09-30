"""G12: binary mode against legacy mode, interleaved in one process so that
machine load affects both alike. The measured ratios are printed; run with
`-s` to see them."""

from tests.conftest import binary_only, check


@binary_only
def test_g12_performance():
    res = check(
        r'''
        import statistics
        legacy = Brish(binary=False, server_count=1)
        binary = Brish(binary=True, server_count=1)
        T = time.perf_counter

        def per_call(b, n):
            t = T()
            for _ in range(n):
                b.send_cmd("true")
            return (T() - t) / n

        for b in (legacy, binary):
            per_call(b, 50)
        lat = {"legacy": [], "binary": []}
        for _ in range(5):
            lat["legacy"].append(per_call(legacy, 400))
            lat["binary"].append(per_call(binary, 400))
        lm, bm_ = statistics.median(lat["legacy"]), statistics.median(lat["binary"])
        print(f"true: legacy {lm*1e6:.0f} us, binary {bm_*1e6:.0f} us, speedup {lm/bm_:.2f}x")
        assert bm_ <= lm * 1.1, (lm, bm_)

        def timed(f):
            t = T()
            r = f()
            return T() - t, r

        big = {}
        for mb in (10, 40, 50):
            data = os.urandom(mb << 20)
            for _ in range(3):
                dt, r = timed(lambda: binary.send_cmd("command wc -c", cmd_stdin=data))
                assert int(r.out) == len(data), repr(r)[:200]
                big[mb] = min(dt, big.get(mb, dt))
        print(f"stdin: 10 MiB {big[10]:.2f} s, 40 MiB {big[40]:.2f} s, 50 MiB {big[50]:.2f} s, 40/10 ratio {big[40]/big[10]:.2f}")
        assert big[50] < 2, big
        assert big[40] / big[10] < 6, big

        n = 20_000_000
        lines = "command yes | command head -c %d" % n
        single = "command perl -e 'print \"x\" x %d'" % n
        for name, cmd in (("many-line", lines), ("single-line", single)):
            tl, tb = [], []
            for _ in range(5):
                dt, r = timed(lambda: legacy.send_cmd(cmd))
                assert len(r.out) == n, (name, len(r.out))
                tl.append(dt)
                dt, r = timed(lambda: binary.send_cmd(cmd))
                assert len(r.outb) == n, (name, len(r.outb))
                tb.append(dt)
            print(f"{name} stdout {n} B: legacy {min(tl):.3f} s, binary {min(tb):.3f} s, ratio {min(tl)/min(tb):.2f}x")
            if name == "many-line":
                assert min(tb) < min(tl), (tl, tb)
            else:
                assert min(tb) <= min(tl) * 1.15, (tl, tb)
        legacy.cleanup()
        binary.cleanup()
        ''',
        real_env=True,
        timeout=600,
    )
    print(res.out)
