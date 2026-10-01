"""A raw driver for the legacy wire format, independent of brish's Python code.

It speaks to a `brish2.zsh` worker exactly as the legacy Python does, but in
bytes, and returns each reply exactly as it appeared on the FIFOs: the stdout
bytes up to and including the retcode line, and the stderr bytes up to and
including the delimiter line. The wire-compatibility tests use it to compare
two worker scripts byte for byte.

The wire format (see docs/protocol.org, "Legacy mode"):
- bootstrap, on the worker's stdin: the request, stdout and stderr FIFO
  paths, each list newline-separated and NUL-terminated, then a newline;
- request: cmd NUL stdin NUL fork-flag ("y" or empty) NUL, then the newline
  that Python's print() adds, which becomes a leading newline of the next cmd;
- reply on stdout: output, newline NUL newline, retcode, newline; on stderr:
  output, newline NUL newline.
"""

import os
import shutil
import subprocess
import tempfile
import threading

DELIM_LINE = b"\0\n"
ARGV_RESERVATION = "BR" + "I" * 2048 + "SH"


def _read_reply(fd, with_rc):
    """Read from `fd` until a line equal to NUL+newline (and, on stdout, one
    more line). Returns (raw, eof)."""
    buf = b""
    pos = 0  # start of the current line
    want_rc = False
    while True:
        while True:
            nl = buf.find(b"\n", pos)
            if nl < 0:
                break
            line = buf[pos : nl + 1]
            pos = nl + 1
            if want_rc:
                return buf[:pos], False
            if line == DELIM_LINE:
                if not with_rc:
                    return buf[:pos], False
                want_rc = True
        chunk = os.read(fd, 65536)
        if not chunk:
            return buf, True
        buf += chunk


class RawLegacy:
    """One legacy bootstrap with `n` workers, driven over raw FIFOs."""

    def __init__(self, worker, n=1, env=None, tmpdir=None):
        self.tmpdir = tempfile.mkdtemp(prefix="raw-", dir=tmpdir)
        names = ("stdin", "stdout", "stderr")
        self.paths = {
            k: [os.path.join(self.tmpdir, f"brish_{i}_{k}") for i in range(n)]
            for k in names
        }
        for k in names:
            for p in self.paths[k]:
                os.mkfifo(p)
        self.p = subprocess.Popen(
            [worker, "--", ARGV_RESERVATION],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
        )
        boot = b"".join(
            "\n".join(self.paths[k]).encode() + b"\0" for k in names
        ) + b"\n"
        self.p.stdin.write(boot)
        self.p.stdin.flush()
        #: The same open order as the legacy Python: all request FIFOs, then
        #: all stdout FIFOs, then all stderr FIFOs.
        self.req = [os.open(p, os.O_WRONLY) for p in self.paths["stdin"]]
        self.out = [os.open(p, os.O_RDONLY) for p in self.paths["stdout"]]
        self.err = [os.open(p, os.O_RDONLY) for p in self.paths["stderr"]]

    @staticmethod
    def frame(cmd, stdin=b"", fork=False):
        return cmd + b"\0" + stdin + b"\0" + (b"y" if fork else b"") + b"\0" + b"\n"

    def send(self, cmd, stdin=b"", fork=False, index=0):
        """Returns (stdout_raw, stderr_raw, eof_out, eof_err)."""
        frame = self.frame(cmd, stdin, fork)
        result = {}

        def read_err():
            result["err"] = _read_reply(self.err[index], False)

        t = threading.Thread(target=read_err, daemon=True)
        t.start()
        #: Write in a thread too: a large stdin must not block the reads.
        def write():
            try:
                view = memoryview(frame)
                while view:
                    n = os.write(self.req[index], view)
                    view = view[n:]
            except BrokenPipeError:
                result["epipe"] = True

        w = threading.Thread(target=write, daemon=True)
        w.start()
        out, eof_out = _read_reply(self.out[index], True)
        t.join(10 if eof_out else None)
        w.join(10)
        err, eof_err = result.get("err", (b"", True))
        return out, err, eof_out, eof_err

    def close(self):
        for fd in self.req + self.out + self.err:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            self.p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        return self.p.stderr.read()


def parse(out_raw, err_raw):
    """(retcode, out, err) as the legacy Python derives them, in bytes."""
    marker = b"\n" + DELIM_LINE
    i = out_raw.rfind(marker)
    j = err_raw.rfind(marker)
    rc = int(out_raw[i + len(marker) :]) if i >= 0 else None
    return rc, out_raw[: max(i, 0)], err_raw[: max(j, 0)]
