try:
    # Dev imports
    # from IPython import embed
    # importing this takes quite a bit of time
    #   `time2 python -c 'from IPython import embed'`
    from icecream import ic

    pass
except ImportError:

    def ic(*args, **kwargs):
        pass

    def embed(*args, **kwargs):
        print("IPython not available.")

    pass


import sys
import time
import os
import errno
import codecs
import signal
import shutil
import subprocess
import threading
import selectors
import secrets
import re
import tempfile
from collections.abc import Iterable
from typing import Union, Any
import ast
from string import Formatter
import uuid
import random
from subprocess import Popen, PIPE, STDOUT
import pathlib
from dataclasses import dataclass
import inspect
from icecream import ic

from .quoting import zsh_quote_bytes

# http://docs.python.org/library/threading.html#rlock-objects
from threading import RLock, Lock


def idem(x):
    return x


def boolsh(some_bool):
    if some_bool:
        return "y"
    else:
        return ""


def bool_from_str(some_bool):
    if isinstance(some_bool, str) and some_bool.lower() in (
        "",
        "0",
        "n",
        "no",
        "false",
    ):
        return False

    else:
        return bool(some_bool)


class NonzeroBrishException(Exception):
    pass


def get_locals(
    *,
    getframe=1,
    locals_=None,
):
    getframe += 1
    #: To adjust for this function itself

    if locals_ is None:
        try:
            ##
            #: @duplicateCode/c55d771bbc2dfcf7f154f78c6634b680
            previous_frame = sys._getframe(getframe)
            previous_frame_locals = previous_frame.f_locals
            locals_ = dict(previous_frame.f_globals, **previous_frame_locals)
            # https://stackoverflow.com/questions/1041639/get-a-dict-of-all-variables-currently-in-scope-and-their-values
            # We will still miss the closure variables.
            ##
        except:
            # Julia runs Python in an embedded mode with no stack frame.
            pass

    return locals_


_BYTES_LIKE = (bytes, bytearray, memoryview)


def _bytes_view(s):
    """Encode a text view back to bytes, never raising."""
    try:
        return s.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        return s.encode("utf-8", "backslashreplace")


def _text_view(x):
    """A printable form of a command or stdin that may be bytes."""
    if isinstance(x, _BYTES_LIKE):
        return bytes(x).decode("utf-8", "backslashreplace")
    return x


@dataclass(frozen=True)
class CmdResult:
    """The result of one command.

    The five fields are text. `outb` and `errb` are the byte views: the exact
    bytes the command wrote when the result came from `from_bytes` (as every
    result from a worker does, in both modes), and otherwise the text views
    encoded back as UTF-8.
    """

    retcode: int
    out: str
    err: str
    cmd: Any  # Union[str, bytes, Iterable[str]]
    cmd_stdin: Any  # Union[str, bytes, None]

    def __post_init__(self):
        #: Bytes passed positionally become the byte views; the fields keep
        #: their text.
        for name in ("out", "err"):
            value = getattr(self, name)
            if isinstance(value, _BYTES_LIKE):
                raw = bytes(value)
                object.__setattr__(self, "_" + name + "b", raw)
                object.__setattr__(self, name, raw.decode("utf-8", "backslashreplace"))
        if isinstance(self.cmd_stdin, (bytearray, memoryview)):
            object.__setattr__(self, "cmd_stdin", bytes(self.cmd_stdin))

    @classmethod
    def from_bytes(
        cls,
        retcode,
        outb,
        errb,
        cmd,
        cmd_stdin,
        *,
        encoding="utf-8",
        errors="backslashreplace",
    ):
        """Build a result from raw output. The text views are decoded now,
        with `encoding` and `errors`; the bytes are kept as they are."""
        outb = bytes(outb)
        errb = bytes(errb)
        res = cls(
            retcode,
            outb.decode(encoding, errors),
            errb.decode(encoding, errors),
            cmd,
            cmd_stdin,
        )
        object.__setattr__(res, "_outb", outb)
        object.__setattr__(res, "_errb", errb)
        return res

    @property
    def outb(self):
        """The stdout bytes."""
        b = self.__dict__.get("_outb")
        return _bytes_view(self.out) if b is None else b

    @property
    def errb(self):
        """The stderr bytes."""
        b = self.__dict__.get("_errb")
        return _bytes_view(self.err) if b is None else b

    @property
    def outrs(self):
        """out.rstrip('\\n')"""
        return self.out.rstrip("\n")

    @property
    def outrsb(self):
        """outb.rstrip(b'\\n')"""
        return self.outb.rstrip(b"\n")

    @property
    def summary(self):
        return self.retcode, self.out, self.err

    @property
    def outerr(self):
        return self.out + self.err

    @property
    def outerrb(self):
        return self.outb + self.errb

    @property
    def longstr(self):
        r = ""
        if self.cmd_stdin:
            r += f"""\ncmd_stdin:\n{_text_view(self.cmd_stdin)}"""
        r += f"""\ncmd: {_text_view(self.cmd)}"""
        if True or self.out:
            r += f"""\nstdout:\n{self.out}"""
        if self.err:
            r += f"""\nstderr:\n{self.err}"""
        if not self:
            r += f"""\nreturn code: {self.retcode}"""
        return r + "\n"

    def print(self, *args, **kwargs):
        print(self.longstr, *args, **kwargs, flush=True)

    def __iter__(self):
        # return iter(self.toTuple())
        return iter(self.outrs.split("\n"))

    def iterb(self):
        return iter(self.outrsb.split(b"\n"))

    def iter0(self):
        return iter(self.out.rstrip("\x00").split("\x00"))

    def iter0b(self):
        return iter(self.outb.rstrip(b"\x00").split(b"\x00"))

    # def __getitem__(self, index):
    #     return self.toTuple()[index]

    def __str__(self):
        return self.outrs

    def __bool__(self):
        return self.retcode == 0

    def __eq__(self, other):
        if other.__class__ is not self.__class__:
            return NotImplemented
        return (
            (self.retcode, self.out, self.err, self.cmd, self.cmd_stdin)
            == (other.retcode, other.out, other.err, other.cmd, other.cmd_stdin)
            and self.outb == other.outb
            and self.errb == other.errb
        )

    def __hash__(self):
        return hash((self.retcode, self.out, self.err, self.cmd, self.cmd_stdin))

    @property
    def assert_zero(self):
        if self.retcode == 0:
            return self
        else:
            raise NonzeroBrishException(f"retcode={self.retcode}, stderr:\n{self.err}")


class UninitializedBrishException(Exception):
    pass


class BrishWorkerDiedException(Exception):
    """A worker could not run the command: it died before the command started,
    and the restarted instance could not run it either."""

    pass


#: Return code of a command whose worker died before reporting a status.
RETCODE_WORKER_DIED = 9001
WORKER_DIED_NOTE = "brish: worker died during this command"

_NEVER_RAN = object()


def _with_note(err, note):
    if err and not err.endswith("\n"):
        err += "\n"
    return err + note + "\n"


def _child_pids(pid):
    """PIDs whose parent is `pid`, from `ps` (used only on slow shutdown paths)."""
    try:
        out = subprocess.run(
            ["ps", "-Ao", "pid=,ppid="], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        return []
    kids = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == str(pid):
            kids.append(int(parts[0]))
    return kids


def _signal_pids(pids, sig):
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _stop_pids(pids, grace=1.0):
    """SIGTERM `pids`, then SIGKILL whichever are still alive after `grace`."""
    if not pids:
        return
    _signal_pids(pids, signal.SIGTERM)
    deadline = time.time() + grace
    while time.time() < deadline and any(_alive(pid) for pid in pids):
        time.sleep(0.01)
    _signal_pids([pid for pid in pids if _alive(pid)], signal.SIGKILL)


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class _ErrReader:
    """Reads one legacy stderr reply (bytes) in a helper thread.

    If the reply never completes (an interrupt, or a dead worker whose stderr
    FIFO is still held open by a background job), the thread is left running
    and owns the file: cleanup() does not close a file that a live helper is
    blocked on, because closing a buffered file waits for its lock.
    """

    def __init__(self, f, delim):
        self.f = f
        self.delim = delim
        self.data = b""
        self.eof = False
        self.exc = None
        self.done = False
        self.close_when_done = False
        self._lock = Lock()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        try:
            self.data, self.eof = _legacy_read_reply(self.f, self.delim)
        except BaseException as e:
            self.exc = e
        finally:
            with self._lock:
                self.done = True
                close = self.close_when_done
            if close:
                try:
                    self.f.close()
                except Exception:
                    pass

    def close_or_hand_over(self):
        with self._lock:
            if not self.done:
                self.close_when_done = True
                return
        try:
            self.f.close()
        except Exception:
            pass


#: Protocol BRISH3 (binary mode). See docs/protocol.org.
BRISH3_FDS_ARG = "BRISH3-FDS"
_HELLO = b"\0BRISH3-HELLO:"
_READ_CHUNK = 65536
#: Seconds between liveness checks while a worker is silent.
_POLL = 0.5
_END_TRAILER = re.compile(rb"(\d+)(:exit)?\Z")


class _StreamParser:
    """Splits one response stream into the bytes between START and END.

    Before START it keeps only the last len(START)-1 bytes. After START it
    collects the chunks in a list, joined once at the end, and keeps the last
    len(END)-1 payload bytes as `tail`, so that an END prefix split across
    chunks is still found. END starts with NUL, so a chunk without NUL (and a
    tail without NUL) cannot contain it and is not searched. Once the prefix
    is found it waits for the newline that ends the END line. Anything after
    the END line is dropped.
    """

    __slots__ = ("start", "end", "keep", "pre", "chunks", "tail", "after", "trailer", "done")

    def __init__(self, start, end_prefix):
        self.start = start
        self.end = end_prefix
        self.keep = len(end_prefix) - 1
        self.pre = b""
        self.chunks = None  # a list once START has been seen
        self.tail = b""
        self.after = None  # the bytes after the END prefix, until its newline
        self.trailer = None
        self.done = False

    @property
    def started(self):
        return self.chunks is not None

    def feed(self, chunk):
        if self.done:
            return
        if self.after is not None:
            self._finish(self.after + chunk)
            return
        if self.chunks is None:
            data = self.pre + chunk
            i = data.find(self.start)
            if i < 0:
                keep = len(self.start) - 1
                self.pre = data[-keep:] if len(data) > keep else data
                return
            self.pre = b""
            self.chunks = []
            chunk = data[i + len(self.start) :]
            if not chunk:
                return
        tail = self.tail
        if b"\0" not in chunk and b"\0" not in tail:
            self.chunks.append(chunk)
            self.tail = chunk[-self.keep :] if len(chunk) >= self.keep else (tail + chunk)[-self.keep :]
            return
        window = tail + chunk
        j = window.find(self.end)
        if j < 0:
            self.chunks.append(chunk)
            self.tail = window[-self.keep :]
            return
        if j >= len(tail):
            self.chunks.append(chunk[: j - len(tail)])
        else:
            #: END began in bytes already collected: take them back.
            extra = len(tail) - j
            while extra:
                last = self.chunks.pop()
                if len(last) > extra:
                    self.chunks.append(last[:-extra])
                    break
                extra -= len(last)
        self._finish(window[j + len(self.end) :])

    def _finish(self, rest):
        k = rest.find(b"\n")
        if k < 0:
            self.after = rest
            return
        self.after = None
        self.trailer = rest[:k]
        self.done = True

    def payload(self):
        return b"" if self.chunks is None else b"".join(self.chunks)


def _parse_trailer(trailer):
    """(retcode, exited) from an END trailer such as b"0" or b"3:exit"."""
    m = _END_TRAILER.match(trailer or b"")
    if not m:
        return RETCODE_WORKER_DIED, True
    return int(m.group(1)), m.group(2) is not None


class _Worker:
    """Python's side of one BRISH3 worker."""

    __slots__ = ("index", "req", "out", "err", "pid", "sel", "stale")

    def __init__(self, index, req, out, err):
        self.index = index
        self.req = req
        self.out = out
        self.err = err
        self.pid = None
        self.sel = None
        #: The last reply was abandoned by an interrupt. The next request
        #: resynchronises through its START marker.
        self.stale = False

    def close(self):
        if self.sel is not None:
            try:
                self.sel.close()
            except Exception:
                pass
            self.sel = None
        for fd in (self.req, self.out, self.err):
            try:
                os.close(fd)
            except OSError:
                pass


#: The legacy reply delimiter: a line holding only NUL (see docs/protocol.org).
_LEGACY_DELIM = b"\0\n"
#: The retcode line the bootstrap writes for a worker that died without
#: answering. Every Python parses it as 9001; no command's status prints so.
_LEGACY_DEATH_LINE = b"09001\n"


def _legacy_read_reply(f, delim=_LEGACY_DELIM):
    """Read lines from the binary file `f` until the line `delim`, and drop
    the newline the worker writes before it. Lines end at b"\n" only, so CR
    and every other byte are kept. Returns (bytes, eof)."""
    lines = []
    readline = f.readline
    while True:
        line = readline()
        if line == delim:
            return b"".join(lines)[:-1], False
        if not line:
            return b"".join(lines), True
        lines.append(line)


_TEMPLATE_UNSAFE = re.compile("[\r\0\ud800-\udfff]")


def _escape_template_literals(template):
    """Rewrite CR, NUL and lone surrogates as Python escapes, so that
    zstring's `ast.parse` keeps them instead of translating or rejecting them;
    the escapes decode back to the same characters."""
    if not _TEMPLATE_UNSAFE.search(template):
        return template

    def esc(m):
        ch = m.group()
        if ch == "\r":
            return "\\r"
        if ch == "\0":
            return "\\x00"
        return "\\u%04x" % ord(ch)

    return _TEMPLATE_UNSAFE.sub(esc, template)


class Brish:
    """Brish is a bridge between Python and an interpreter. The interpreter needs to adhere to the Brish protocol. A zsh interpreter is provided, and is the default. Threadsafe."""

    # MARKER = '\x00BRISH_MARKER'
    MARKER = "\x00"

    #: Seconds to wait for every worker's HELLO in binary mode. Startup files
    #: can take seconds on a loaded machine.
    startup_timeout = 30

    def __init__(
        self,
        defaultShell=None,
        boot_cmd=None,
        server_count=1,
        delayed_init=False,
        binary=None,
        **kwargs,
    ):
        self.lock = RLock()
        #: Binary mode (protocol BRISH3, byte-exact) or legacy mode (the
        #: brish2.zsh transport). Read once; restarts keep it.
        if binary is None:
            binary = bool_from_str(os.environ.get("BRISH_BINARY", ""))
        self.binary = bool(binary)
        #: `init()` arguments are kept on the instance, so that `restart()`
        #: and a delayed first use start the same kind of worker.
        self.encoding = kwargs.get("encoding") or "utf-8"
        self.decoding_errors = kwargs.get("decoding_errors") or "backslashreplace"
        if kwargs.get("startup_timeout"):
            self.startup_timeout = kwargs["startup_timeout"]
        if boot_cmd:
            self.boot_cmd = self.zstring(boot_cmd, getframe=2)
        else:
            self.boot_cmd = boot_cmd

        self.defaultShell = defaultShell or [
            str(
                pathlib.Path(__file__).parent
                / ("brish3.zsh" if self.binary else "brish2.zsh")
            ),
            "--",
            "BR" + "I" * 2048 + "SH",
        ]  # Reserve big argv for `insubshell`
        self.lastShell = kwargs.get("shell") or self.defaultShell
        self.last_server_count = server_count
        self.p = None
        self.locks = []
        #: Bumped by every init(); a restart requested for an older
        #: generation is already done.
        self._gen = 0
        #: The generation that must restart before its next use.
        self._restart_gen = None
        #: A failed restart leaves the instance uninitialized; the next use
        #: tries again instead of raising UninitializedBrishException.
        self._init_on_use = False
        self._booting = False
        self.delayed_init = delayed_init
        if not self.delayed_init:
            self.init(**kwargs)

    def init(
        self,
        shell=None,
        server_count=None,
        decoding_errors=None,
        # https://docs.python.org/3/library/codecs.html#codec-base-classes
        encoding=None,
        startup_timeout=None,
    ):
        with self.lock:
            if self.p is not None:
                self.cleanup()

            if encoding is not None:
                self.encoding = encoding
            if decoding_errors is not None:
                self.decoding_errors = decoding_errors
            if startup_timeout:
                self.startup_timeout = startup_timeout
            if not server_count:
                server_count = self.last_server_count

            if shell is None:
                shell = self.lastShell or self.defaultShell

            self.lastShell = shell
            self.last_server_count = server_count
            assert server_count >= 1

            self._gen += 1
            self._restart_gen = None
            try:
                if self.binary:
                    self._init_binary(shell, server_count)
                else:
                    self._init_legacy(shell, server_count)
            except BaseException:
                self._init_on_use = True
                raise
            self._init_on_use = False

            if self.boot_cmd is not None:
                self._booting = True
                try:
                    return [
                        self.send_cmd(self.boot_cmd, fork=False, server_index=i)
                        for i in range(server_count)
                    ]
                finally:
                    self._booting = False

    def _init_legacy(self, shell, server_count):
        encoding = self.encoding
        decoding_errors = self.decoding_errors
        tmpdir = tempfile.mkdtemp()

        brish_stdin_paths = [
            os.path.join(tmpdir, f"brish_{i}_stdin") for i in range(server_count)
        ]
        brish_stdout_paths = [
            os.path.join(tmpdir, f"brish_{i}_stdout") for i in range(server_count)
        ]
        brish_stderr_paths = [
            os.path.join(tmpdir, f"brish_{i}_stderr") for i in range(server_count)
        ]
        for path in brish_stdin_paths + brish_stdout_paths + brish_stderr_paths:
            os.mkfifo(path)

        p = Popen(
            shell,
            stdin=PIPE,
            stdout=PIPE,
            stderr=PIPE,
            env=dict(
                os.environ,
            ),
            text=True,
            errors=decoding_errors,  # escape invalid utf-8 bytes
            encoding=encoding,
        )
        p.tmpdir = tmpdir
        p.gen = self._gen
        p.binary = False
        p.server_count = server_count
        p.free_server_count = server_count
        p.brish_stdin_paths = brish_stdin_paths
        p.brish_stdout_paths = brish_stdout_paths
        p.brish_stderr_paths = brish_stderr_paths
        p.brish_stdins = []
        p.brish_stdouts = []
        p.brish_stderrs = []
        p.err_readers = [None] * server_count
        try:
            BRISH_STDIN = "\n".join(brish_stdin_paths)
            BRISH_STDOUT = "\n".join(brish_stdout_paths)
            BRISH_STDERR = "\n".join(brish_stderr_paths)
            try:
                print(
                    BRISH_STDIN
                    + self.MARKER
                    + BRISH_STDOUT
                    + self.MARKER
                    + BRISH_STDERR
                    + self.MARKER,
                    file=p.stdin,
                    flush=True,
                )
            except BrokenPipeError:
                raise BrishWorkerDiedException(
                    f"the shell exited during startup (status {p.wait()}): {shell[0]!r}"
                )
            #: Open each request FIFO without blocking, so that a shell that
            #: dies before opening its end is noticed instead of hanging init.
            #: Requests are encoded before they are written; see _send_legacy.
            for path in brish_stdin_paths:
                p.brish_stdins.append(
                    open(self._legacy_open_request_fifo(path, p, shell), "wb")
                )
            #: Replies are read as bytes and decoded once, in CmdResult.from_bytes.
            for path in brish_stdout_paths:
                p.brish_stdouts.append(open(path, "rb"))
            for path in brish_stderr_paths:
                p.brish_stderrs.append(open(path, "rb"))
        except BaseException:
            self._cleanup_legacy(p)
            raise
        self.locks = [RLock() for i in range(server_count)]
        self.p = p

    @staticmethod
    def _legacy_open_request_fifo(path, p, shell):
        while True:
            try:
                fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
                break
            except OSError as e:
                if e.errno != errno.ENXIO:
                    raise
                if p.poll() is not None:
                    raise BrishWorkerDiedException(
                        f"the shell exited during startup (status {p.returncode}): {shell[0]!r}"
                    )
                time.sleep(0.002)
        os.set_blocking(fd, True)
        return fd

    def _init_binary(self, shell, server_count):
        workers, child_fds = [], []
        try:
            for i in range(server_count):
                req_r, req_w = os.pipe()
                out_r, out_w = os.pipe()
                err_r, err_w = os.pipe()
                child_fds += [req_r, out_w, err_w]
                workers.append(_Worker(i, req_w, out_r, err_r))
            argv = list(shell) + [BRISH3_FDS_ARG] + [
                f"{child_fds[3 * i]},{child_fds[3 * i + 1]},{child_fds[3 * i + 2]}"
                for i in range(server_count)
            ]
            p = Popen(
                argv,
                stdin=PIPE,
                stdout=subprocess.DEVNULL,
                stderr=None,  # startup errors stay visible
                pass_fds=child_fds,
                env=dict(os.environ),
            )
        except BaseException:
            for w in workers:
                w.close()
            for fd in child_fds:
                os.close(fd)
            raise
        for fd in child_fds:
            os.close(fd)

        p.gen = self._gen
        p.binary = True
        p.workers = workers
        p.server_count = server_count
        p.free_server_count = server_count
        try:
            for w in workers:
                os.set_blocking(w.req, False)
                os.set_blocking(w.out, False)
                os.set_blocking(w.err, False)
            self._await_hellos(p, shell)
            for w in workers:
                w.sel = selectors.DefaultSelector()
                w.sel.register(w.out, selectors.EVENT_READ)
                w.sel.register(w.err, selectors.EVENT_READ)
        except BaseException:
            self._cleanup_binary(p)
            raise
        self.locks = [RLock() for i in range(server_count)]
        self.p = p

    def _await_hellos(self, p, shell):
        """Wait until every worker has said HELLO, and record its PID."""
        deadline = time.monotonic() + self.startup_timeout
        pending = {w.out: w for w in p.workers}
        bufs = {fd: b"" for fd in pending}
        sel = selectors.DefaultSelector()
        try:
            for fd in pending:
                sel.register(fd, selectors.EVENT_READ)
            while pending:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise BrishWorkerDiedException(
                        f"no HELLO within {self.startup_timeout}s: {shell[0]!r} is not a BRISH3 worker"
                    )
                for key, _ in sel.select(min(left, _POLL)):
                    fd = key.fd
                    try:
                        chunk = os.read(fd, 4096)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        status = p.poll()
                        raise BrishWorkerDiedException(
                            f"a worker exited before its HELLO (shell status {status}): "
                            f"{shell[0]!r} is not a BRISH3 worker, or it failed to start"
                        )
                    buf = bufs[fd] + chunk
                    i = buf.find(_HELLO)
                    if i >= 0:
                        j = buf.find(b"\n", i)
                        if j >= 0:
                            pending.pop(fd).pid = int(buf[i + len(_HELLO) : j])
                            sel.unregister(fd)
                            continue
                        buf = buf[i:]
                    else:
                        buf = buf[-len(_HELLO) :]
                    bufs[fd] = buf
        finally:
            sel.close()

    def restart(self):
        with self.lock:
            self.cleanup()
            self.delayed_init = False
            self.init(shell=self.lastShell, server_count=self.last_server_count)

    def _request_restart(self, gen):
        """Restart generation `gen` before its next use."""
        if gen == self._gen:
            self._restart_gen = gen

    def _restart_now(self, gen):
        """Restart unless generation `gen` has already been replaced. Never
        call this while holding a worker lock."""
        with self.lock:
            if gen == self._gen or self.p is None:
                self.restart()

    def zsh_quote(self, obj, use_shared_instance=True, retry_count=0, retry_limit=10):
        """Quote `obj` as zsh words, in pure Python (no zsh process is used).

        `None` gives an empty expansion, a `CmdResult` quotes its `outrs`, and
        other iterables (except `str`) become one word per item. See
        `brish.quoting` for the rules. The remaining arguments are accepted
        for compatibility and ignored.
        """
        if obj is None:
            return ""

        typ = type(obj)
        if typ is CmdResult:
            return self._quote_bytes(obj.outrsb)
        #: Bytes-like values are quoted byte-exactly, before the Iterable
        #: branch would turn them into ints. The quoted text is ASCII for
        #: anything that is not printable UTF-8, so the legacy text wire
        #: carries it too.
        if isinstance(obj, os.PathLike):
            obj = os.fspath(obj)
        if isinstance(obj, _BYTES_LIKE):
            return self._quote_bytes(bytes(obj))
        if not isinstance(obj, str) and isinstance(obj, Iterable):
            # zsh doesn't support nested arrays, so we str the inner object.
            words = []
            for i in iter(obj):
                if isinstance(i, os.PathLike):
                    i = os.fspath(i)
                if isinstance(i, _BYTES_LIKE):
                    words.append(self._quote_bytes(bytes(i)))
                    continue
                words.append(self._quote_word(str(i)))
            return " ".join(words)
        else:
            return self._quote_word(str(obj))

    def _quote_ascii_only(self):
        return codecs.lookup(self.encoding).name != "utf-8"

    def _quote_word(self, s):
        return zsh_quote_bytes(
            s.encode(self.encoding, "surrogateescape"),
            ascii_only=self._quote_ascii_only(),
        )

    def _quote_bytes(self, b):
        return zsh_quote_bytes(b, ascii_only=self._quote_ascii_only())

    def acquire_lock(self, server_index=None, lock_sleep=1):
        lock, server_index, _ = self._acquire(server_index, lock_sleep)
        return lock, server_index

    def _acquire(self, server_index=None, lock_sleep=1):
        """Lock one worker. Returns (lock, server_index, p).

        A pending restart runs here, before the worker lock is taken, so a
        thread never restarts while holding a worker lock.
        """
        while True:
            with self.lock:
                if self.p is not None and self._restart_gen == self._gen:
                    self.restart()
                if self.p is None:
                    if self.delayed_init or self._init_on_use:
                        self.delayed_init = False
                        self.restart()
                    else:
                        raise UninitializedBrishException(
                            "acquire_lock called with an uninitialized Brish"
                        )
                current_p = self.p
                locks = self.locks

            assert len(locks) >= 1
            lock = None
            if server_index is None:
                for i in self._worker_order(current_p):
                    if locks[i].acquire(blocking=False):
                        # https://docs.python.org/3/library/threading.html#threading.Lock.acquire
                        lock, index = locks[i], i
                        break
                if lock is None:
                    if lock_sleep is not None:
                        time.sleep(lock_sleep)
                        continue
                    index = random.randrange(len(locks))
            else:
                index = server_index

            if lock is None:
                try:
                    lock = locks[index]
                except IndexError:
                    ic(len(locks), index)
                    time.sleep(1)
                    continue
                lock.acquire()

            #: Holding a worker lock, `self.p` can no longer be replaced.
            if self.p is current_p:
                return lock, index, current_p
            lock.release()

    def _worker_order(self, p):
        n = len(self.locks)
        if not getattr(p, "binary", False):
            return range(n)
        workers = p.workers
        return [i for i in range(n) if not workers[i].stale] + [
            i for i in range(n) if workers[i].stale
        ]

    def send_cmd(
        self, cmd, cmd_stdin="", fork=False, server_index=None, lock_sleep=1
    ):
        """Run `cmd` in a worker and return its CmdResult.

        `cmd` and `cmd_stdin` may be bytes-like; `str` is encoded with the
        instance encoding and surrogateescape. `cmd_stdin=None` means
        /dev/null in binary mode, and empty stdin in legacy mode, where a
        NUL in `cmd` or `cmd_stdin` gives the retcode 9000 instead.
        """
        restart_cmd = cmd
        if isinstance(cmd, _BYTES_LIKE):
            restart_cmd = bytes(cmd).decode("utf-8", "surrogateescape")
        if restart_cmd == "%BRISH_RESTART":
            #: Handled before any worker lock is taken: restarting needs every
            #: worker lock, so holding one here could deadlock with another
            #: thread's restart().
            self.restart()
            return CmdResult(0, "Restarted succesfully.", "", cmd, self._stored_stdin(cmd_stdin))
        if self.binary:
            return self._send_binary(cmd, cmd_stdin, fork, server_index, lock_sleep)
        return self._send_legacy(cmd, cmd_stdin, fork, server_index, lock_sleep)

    def _to_bytes(self, x, what="value"):
        """The encoding boundary of both modes: any value to bytes."""
        if isinstance(x, _BYTES_LIKE):
            return bytes(x)
        if isinstance(x, CmdResult):
            return x.outrsb
        if isinstance(x, os.PathLike):
            x = os.fspath(x)
            if isinstance(x, bytes):
                return x
        if not isinstance(x, str):
            x = str(x)
        try:
            return x.encode(self.encoding, "surrogateescape")
        except UnicodeEncodeError as e:
            raise UnicodeEncodeError(
                e.encoding,
                e.object,
                e.start,
                e.end,
                f"{what} is not encodable as {self.encoding} with surrogateescape "
                "(a lone surrogate or an unencodable character); pass bytes instead",
            ) from None

    @staticmethod
    def _stored_stdin(cmd_stdin):
        if cmd_stdin is None or isinstance(cmd_stdin, (str, bytes)):
            return cmd_stdin
        if isinstance(cmd_stdin, _BYTES_LIKE):
            return bytes(cmd_stdin)
        return str(cmd_stdin)

    def _send_binary(self, cmd, cmd_stdin, fork, server_index, lock_sleep):
        #: Encode everything first: an encoding error must leave the worker
        #: untouched.
        cmd_b = self._to_bytes(cmd, "cmd")
        stdin_b = None if cmd_stdin is None else self._to_bytes(cmd_stdin, "cmd_stdin")
        stored_stdin = self._stored_stdin(cmd_stdin)
        stdin_len = b"-" if stdin_b is None else b"%d" % len(stdin_b)

        for attempt in range(2):
            nonce = secrets.token_hex(16).encode()
            header = b"BRISH3 %s %d %s %d\n" % (nonce, len(cmd_b), stdin_len, 1 if fork else 0)
            frame = b"".join((header, cmd_b, stdin_b or b""))
            lock, index, p = self._acquire(server_index, lock_sleep)
            try:
                p.free_server_count -= 1
                outcome = self._binary_transact(p, p.workers[index], frame, nonce)
            finally:
                p.free_server_count += 1
                lock.release()

            if outcome is _NEVER_RAN:
                if self._booting:
                    raise BrishWorkerDiedException(
                        "a worker died before running the boot command"
                    )
                self._restart_now(p.gen)
                continue
            retcode, outb, errb, restart = outcome
            if restart:
                self._request_restart(p.gen)
            return CmdResult.from_bytes(
                retcode,
                outb,
                errb,
                cmd,
                stored_stdin,
                encoding=self.encoding,
                errors=self.decoding_errors,
            )

        raise BrishWorkerDiedException(
            "a worker died before running the command, twice"
        )

    def _binary_transact(self, p, w, frame, nonce):
        """One request/response exchange with worker `w`.

        Returns _NEVER_RAN if the worker died before START, or
        (retcode, outb, errb, restart). The frame is written non-blocking
        inside the loop that drains both response pipes, so neither side can
        block the other.
        """
        start = b"\0BRISH3-START:" + nonce + b"\n"
        end = b"\0BRISH3-END:" + nonce + b":"
        so, se = _StreamParser(start, end), _StreamParser(start, end)
        streams = {w.out: so, w.err: se}
        sel = w.sel
        total = len(frame)
        sent = 0
        attempted = False
        died = False
        writing_registered = False
        try:
            #: Most frames fit in the pipe buffer: try the write first.
            attempted = True
            try:
                sent = os.write(w.req, frame)
            except BlockingIOError:
                pass
            except BrokenPipeError:
                died = True
            mv = memoryview(frame)
            while not died and not (so.done and se.done):
                if sent < total and not writing_registered:
                    sel.register(w.req, selectors.EVENT_WRITE)
                    writing_registered = True
                events = sel.select(_POLL)
                if not events:
                    if not _alive(w.pid):
                        died = True
                    continue
                for key, _ in events:
                    fd = key.fd
                    if fd == w.req:
                        try:
                            sent += os.write(fd, mv[sent : sent + _READ_CHUNK])
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            died = True
                            break
                        if sent >= total:
                            sel.unregister(fd)
                            writing_registered = False
                        continue
                    st = streams[fd]
                    #: Drain the pipe before selecting again: one select per
                    #: pipe-buffer refill is a large share of the cost of a
                    #: big reply.
                    while True:
                        try:
                            chunk = os.read(fd, _READ_CHUNK)
                        except BlockingIOError:
                            break
                        if not chunk:
                            died = True
                            break
                        st.feed(chunk)
                        if st.done or len(chunk) < _READ_CHUNK:
                            break
                    if died:
                        break
            if died:
                #: Whatever the dead worker wrote is already buffered in the
                #: pipes; collect it, so that START and END are not missed on
                #: the stream that did not report EOF first.
                for fd, st in streams.items():
                    for _ in range(64):  # a background job may keep writing
                        if st.done:
                            break
                        try:
                            chunk = os.read(fd, _READ_CHUNK)
                        except BlockingIOError:
                            break
                        if not chunk:
                            break
                        st.feed(chunk)
            w.stale = False
        except BaseException:
            if sent >= total:
                #: The command runs on; the next request resyncs via START.
                w.stale = True
            elif attempted:
                #: The worker may hold part of a frame (an interrupt can land
                #: after a write returned but before `sent` was updated), and
                #: only a restart recovers from that.
                self._request_restart(p.gen)
            raise
        finally:
            if writing_registered:
                try:
                    sel.unregister(w.req)
                except (KeyError, ValueError, OSError):
                    pass

        if died:
            if not (so.started or se.started):
                return _NEVER_RAN
            if so.done:
                retcode, _ = _parse_trailer(so.trailer)
                errb = se.payload()
            elif se.done:
                retcode, _ = _parse_trailer(se.trailer)
                errb = se.payload()
            else:
                retcode = RETCODE_WORKER_DIED
                errb = _with_note(se.payload().decode("latin-1"), WORKER_DIED_NOTE).encode("latin-1")
            return retcode, so.payload(), errb, True
        retcode, exited = _parse_trailer(so.trailer)
        return retcode, so.payload(), se.payload(), exited

    def _send_legacy(self, cmd, cmd_stdin, fork, server_index, lock_sleep):
        #: Encode everything first, with the same rules as binary mode: an
        #: encoding error must leave the worker untouched.
        cmd_b = self._to_bytes(cmd, "cmd")
        stdin_b = b"" if cmd_stdin is None else self._to_bytes(cmd_stdin, "cmd_stdin")
        stored_stdin = self._stored_stdin(cmd_stdin)
        #: The legacy wire separates fields with NUL, so a NUL cannot travel.
        if b"\0" in cmd_b or b"\0" in stdin_b:
            return CmdResult(
                9000,
                "",
                "Illegal input: Input contained the Brish marker (currently the NUL character).",
                cmd,
                stored_stdin,
            )
        #: cmd NUL stdin NUL fork NUL, and the newline that print() used to
        #: add (it becomes a leading newline of the next command).
        frame = b"".join((cmd_b, b"\0", stdin_b, b"\0", b"y" if fork else b"", b"\0\n"))

        for attempt in range(2):
            lock, index, p = self._acquire(server_index, lock_sleep)
            outcome = None
            try:
                p.free_server_count -= 1
                outcome = self._legacy_transact(p, index, frame, cmd, stored_stdin)
            except BaseException:
                #: An interrupt leaves a half-written request or a half-read
                #: reply, and possibly a helper thread that would consume the
                #: next reply's stderr. Restart before the next use.
                p.interrupted = True
                self._request_restart(p.gen)
                raise
            finally:
                p.free_server_count += 1
                lock.release()

            if outcome is _NEVER_RAN:
                if self._booting:
                    raise BrishWorkerDiedException(
                        "a worker died before running the boot command"
                    )
                self._restart_now(p.gen)
                continue
            result, restart = outcome
            if restart:
                self._request_restart(p.gen)
            return result

        raise BrishWorkerDiedException(
            "a worker died before running the command, twice"
        )

    def _legacy_transact(self, p, index, frame, cmd, cmd_stdin):
        """One request/reply exchange with legacy worker `index`.

        Returns _NEVER_RAN if the request could not be written, or
        (CmdResult, restart). See docs/protocol.org, "Legacy mode".
        """
        try:
            f = p.brish_stdins[index]
            f.write(frame)
            f.flush()
        except BrokenPipeError:
            #: The worker is gone; it cannot have read the whole request.
            return _NEVER_RAN

        #: Read stderr concurrently: a command that fills the stderr FIFO
        #: before finishing its stdout would otherwise deadlock.
        err_reader = _ErrReader(p.brish_stderrs[index], _LEGACY_DELIM)
        p.err_readers[index] = err_reader
        outb, died = _legacy_read_reply(p.brish_stdouts[index])
        return_code = None
        exited = False
        if not died:
            rc_line = p.brish_stdouts[index].readline()
            if not rc_line:
                died = True
            else:
                #: "+N" comes from the worker's EXIT trap: the command exited
                #: the worker with status N. "09001" is the bootstrap
                #: answering for a worker that died silently; a plain 9001 is
                #: the command's own status (`return 9001`).
                exited = rc_line.startswith(b"+")
                return_code = int(rc_line)
                if rc_line == _LEGACY_DEATH_LINE:
                    died = True
        err_reader.thread.join(2 if died else None)
        if err_reader.exc is not None:
            raise err_reader.exc
        errb = err_reader.data
        if err_reader.done:
            p.err_readers[index] = None
            died = died or err_reader.eof
        else:
            #: A background job holds the dead worker's stderr open.
            died = True
        if died:
            #: The caller restarts the instance before its next use.
            if return_code is None:
                return_code = RETCODE_WORKER_DIED
            errb = _with_note(errb.decode("latin-1"), WORKER_DIED_NOTE).encode("latin-1")
        res = CmdResult.from_bytes(
            return_code,
            outb,
            errb,
            cmd,
            cmd_stdin,
            encoding=self.encoding,
            errors=self.decoding_errors,
        )
        return res, died or exited

    def cleanup(self):
        with self.lock:
            if self.p is None:
                return
            locks = self.locks
            for lock in locks:
                lock.acquire()
            try:
                p = self.p
                self.p = None
                self.locks = []
                if getattr(p, "binary", False):
                    self._cleanup_binary(p)
                else:
                    self._cleanup_legacy(p)
            finally:
                for lock in locks:
                    lock.release()

    @staticmethod
    def _cleanup_binary(p):
        #: Stop the workers first, so this never waits for a user command.
        pids = [w.pid for w in p.workers if w.pid]
        _signal_pids(pids, signal.SIGTERM)
        for w in p.workers:
            w.close()
        if p.stdin is not None:
            try:
                p.stdin.close()  # the bootstrap exits when its stdin closes
            except Exception:
                pass
        deadline = time.time() + 1
        while time.time() < deadline and any(_alive(pid) for pid in pids):
            time.sleep(0.005)
        _signal_pids([pid for pid in pids if _alive(pid)], signal.SIGKILL)
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()

    @staticmethod
    def _cleanup_legacy(p):
        def close(f):
            try:
                f.close()
            except Exception:
                pass

        if getattr(p, "interrupted", False):
            #: A worker may hold a truncated request. Closing its FIFO would
            #: let it run the command with truncated stdin, so stop it first.
            _stop_pids(_child_pids(p.pid))

        for f in (p.stdout, p.stderr, p.stdin):
            if f is not None:
                close(f)
        for f in p.brish_stdins:
            close(f)
        for f in p.brish_stdouts:
            close(f)
        readers = getattr(p, "err_readers", [])
        for i, f in enumerate(p.brish_stderrs):
            reader = readers[i] if i < len(readers) else None
            if reader is not None:
                reader.close_or_hand_over()
            else:
                close(f)
        shutil.rmtree(p.tmpdir, ignore_errors=True)
        #: Workers exit once their request FIFO closes, unless a command is
        #: still running (after an interrupt). Do not wait for it for long.
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _stop_pids(_child_pids(p.pid))
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

    _conversions = {"a": ascii, "r": repr, "s": str, "e": idem, "b": boolsh}

    def zstring_old(self, template, locals_=None):
        #: DEPRECATED
        ##
        if locals_ is None:
            previous_frame = sys._getframe(1)
            previous_frame_locals = previous_frame.f_locals
            locals_ = dict(previous_frame.f_globals, **previous_frame_locals)
            # https://stackoverflow.com/questions/1041639/get-a-dict-of-all-variables-currently-in-scope-and-their-values
            # We will still miss the closure variables.
        result = []
        parts = Formatter().parse(template)
        for part in parts:
            literal_text, field_name, format_spec, conversion = part
            # print(part)
            if literal_text:
                result.append(literal_text)
            if not field_name:
                continue
            value = eval(field_name, locals_)  # .__format__()
            if conversion:
                value = self._conversions[conversion](value)
            if format_spec:
                value = format(value, format_spec)
            else:
                value = str(value)
            if conversion != "e":
                value = self.zsh_quote(value)
            result.append(value)
        cmd = "".join(result)
        return cmd

    def zstring(
        self,
        template,
        locals_=None,
        getframe=1,
    ):
        locals_ = get_locals(
            getframe=getframe,
            locals_=locals_,
        )

        def asteval(astNode):
            if astNode is not None:
                return eval(
                    compile(ast.Expression(astNode), filename="<string>", mode="eval"),
                    locals_,
                )
            else:
                return None

        def eatFormat(format_spec, code):
            res = False
            if format_spec:
                flags = format_spec.split(":")
                res = code in flags
                format_spec = list(filter(lambda a: a != code, flags))
            return ":".join(format_spec), res

        template = _escape_template_literals(template)
        p = ast.parse(f"f''' {template} '''")  # The whitespace is necessary
        result = []
        parts = p.body[0].value.values
        for part in parts:
            typ = type(part)
            if typ is ast.Constant or typ is ast.Str:
                result.append(part.s)  # part.value can also work in Py3.8
            elif typ is ast.FormattedValue:
                # print(part.__dict__)

                value = asteval(part.value)
                conversion = part.conversion
                if conversion >= 0:
                    # parser doesn't support custom conversions, but this code works:
                    conversion = chr(conversion)
                    value = self._conversions[conversion](value)
                # if part.format_spec:
                #     value = format(value, asteval(part.format_spec))
                # else:
                #     value = str(value)
                # if conversion != 'e':
                #     value = self.zsh_quote(value)
                # embed()

                format_spec = asteval(part.format_spec) or ""
                # print(f"orig format: {format_spec}")
                format_spec, fmt_eval = eatFormat(format_spec, "e")
                format_spec, fmt_bool = eatFormat(format_spec, "bool")
                # print(f"format: {format_spec}")
                if format_spec:
                    value = format(value, format_spec)
                if fmt_bool:
                    value = boolsh(value)

                if not fmt_eval:
                    value = self.zsh_quote(value)
                else:
                    #: `:e` inserts the value itself; bytes decode losslessly
                    #: and are encoded back to the same bytes by send_cmd.
                    if isinstance(value, os.PathLike):
                        value = os.fspath(value)
                    if isinstance(value, _BYTES_LIKE):
                        value = bytes(value).decode(self.encoding, "surrogateescape")
                value = str(value)
                result.append(value)
        cmd = "".join(result)
        return cmd

    def z(self, template, locals_=None, getframe=2, *args, **kwargs):
        return self.send_cmd(
            self.zstring(template, locals_=locals_, getframe=getframe), *args, **kwargs
        )

    def z_print(self, *args, getframe=3, file=None, **kwargs):
        res = self.z(*args, getframe=getframe, **kwargs)

        #: Pass the bytes through when the target has a binary buffer; flush
        #: the text layer first so the output stays in order.
        target = sys.stdout if file is None else file
        buffer = getattr(target, "buffer", None)
        if buffer is not None:
            target.flush()
            buffer.write(res.outerrb)
            buffer.flush()
            return res

        print_opts = dict()
        if file is not None:
            print_opts["file"] = file

        print(res.outerr, end="", flush=True, **print_opts)
        return res

    def z_print_stderr(self, *args, getframe=4, **kwargs):
        ## tests:
        #: `rederr python -c 'from brish import * ; zpe("echo hi") ; zp("echo ic") ; zpe("echo woo")'`
        ##
        return self.z_print(*args, getframe=getframe, file=sys.stderr, **kwargs)

    # Aliases
    c = send_cmd
    zq = zsh_quote
    zp = z_print
    zpe = z_print_stderr


_shared_brish = Brish(
    delayed_init=True
)  # Any identifier of the form __spam (at least two leading underscores, at most one trailing underscore) is textually replaced with _classname__spam, so we can't use it in Brish if we use two underscores.
