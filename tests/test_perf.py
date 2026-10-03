"""G12: binary mode against legacy mode, interleaved in one process so that
machine load affects both alike. The measured ratios are printed; run with
`-s` to see them.

Per-call latency is compared with the original legacy worker (brish2.zsh at
9599fc3, the last release before binary mode), which forked a stdin writer
for every command. Today's legacy worker forks nothing for an empty stdin
either, and is about as fast as binary mode (often faster: it does less per
request), so it is only checked against the original.

These are absolute timings, which a loaded machine can miss, so the test runs
only with `BRISH_TEST_PERF=1`."""

import stat
import subprocess

from tests.conftest import ROOT, binary_only, check, perf_only


@perf_only
@binary_only
def test_g12_performance(tmp_path):
    original = tmp_path / "brish2_original.zsh"
    original.write_bytes(subprocess.run(
        ["git", "-C", str(ROOT), "show", "9599fc3:brish/brish2.zsh"],
        capture_output=True, check=True).stdout)
    original.chmod(original.stat().st_mode | stat.S_IXUSR)
    res = check(
        r'''
        import statistics
        legacy = Brish(binary=False, server_count=1)
        binary = Brish(binary=True, server_count=1)
        original = Brish(binary=False, server_count=1,
                         defaultShell=[ORIGINAL, "--", "BR" + "I" * 2048 + "SH"])
        T = time.perf_counter

        def per_call(b, n):
            t = T()
            for _ in range(n):
                b.send_cmd("true")
            return (T() - t) / n

        for b in (legacy, binary, original):
            per_call(b, 50)
        lat = {"legacy": [], "binary": [], "original": []}
        for _ in range(5):
            lat["original"].append(per_call(original, 400))
            lat["legacy"].append(per_call(legacy, 400))
            lat["binary"].append(per_call(binary, 400))
        om, lm, bm_ = (statistics.median(lat[k]) for k in ("original", "legacy", "binary"))
        print(f"true: original legacy {om*1e6:.0f} us, legacy {lm*1e6:.0f} us, binary {bm_*1e6:.0f} us, "
              f"binary speedup over the original {om/bm_:.2f}x")
        assert bm_ <= om * 1.1, (om, bm_)
        assert lm <= om * 1.1, (om, lm)
        original.cleanup()

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
        setup=f"ORIGINAL = {str(original)!r}\n",
        real_env=True,
        timeout=600,
    )
    print(res.out)
