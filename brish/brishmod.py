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
import collections
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
    """A worker could not run the command: it died before the command
    started, and its replacement could not run it either; or the calling
    thread holds that worker's lock (from acquire_lock), so the worker cannot
    be replaced until the thread releases it; or a new worker failed to
    start."""

    pass


class BrishWorkerBusyException(RuntimeError):
    """A call from a thread needs a worker that a BrishPopen of the same
    thread is still streaming from (inside its `for` loop, say). The worker
    lock would let that thread in, since it is an RLock, but the worker is
    busy: read the BrishPopen to its end or close it first, or leave
    server_index=None so that the call picks another worker. Raised at once;
    no worker is replaced."""

    pass


class BrishCancelledException(RuntimeError):
    """The `cancelled` callable given to popen, zpopen, send_cmd or z
    returned true, while the call waited for a worker or just before it
    would have sent the command: nothing ran, and the worker is free again
    (a replacement that the call started stays in its slot)."""

    pass


def _check_cancelled(cancelled):
    """Raise BrishCancelledException if `cancelled()` is true; an exception
    from it propagates as it is."""
    if cancelled():
        raise BrishCancelledException("cancelled before the command was sent; nothing ran")


#: Return code of a command whose worker died before reporting a status.
RETCODE_WORKER_DIED = 9001
WORKER_DIED_NOTE = "brish: worker died during this command"

_NEVER_RAN = object()


def _with_note(err, note):
    if err and not err.endswith("\n"):
        err += "\n"
    return err + note + "\n"


def _descendants(root):
    """PIDs of every descendant of `root`, from one `ps` snapshot."""
    return _trees([root])[1:]


def _trees(roots):
    """`roots` and every descendant of each, from one `ps` snapshot (only
    `roots` when `ps` fails)."""
    roots = [r for r in roots if r]
    if not roots:
        return []
    try:
        out = subprocess.run(
            ["ps", "-Ao", "pid=,ppid="], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        return list(roots)
    children = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    found, stack, seen = list(roots), list(roots), set(roots)
    while stack:
        for kid in children.get(stack.pop(), ()):
            if kid not in seen:
                seen.add(kid)
                found.append(kid)
                stack.append(kid)
    return found


def _parent_pid(pid):
    """The parent PID of `pid`, from `ps -o ppid= -p PID`, or None. One `ps`
    run, about half the cost of listing every process."""
    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(int(pid))],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception:
        return None
    return int(out) if out.isdigit() else None


def _child_pids(pid):
    """PIDs whose parent is `pid`, from `ps -A` (used on shutdown paths)."""
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


def _acquire_all(locks, skip=None):
    """Acquire every lock in `locks`, except those at an index `i` where
    `skip(i)` is true when it comes up, without ever blocking on one while
    holding another. A thread that holds one worker lock and waits for a
    second (both at the same time are fine) can then never deadlock with a
    cleanup that waits for all of them. Returns the locks taken."""
    while True:
        taken = []
        busy = None
        for i, lock in enumerate(locks):
            if lock.acquire(blocking=False):
                taken.append(lock)
            elif skip is not None and skip(i):
                continue
            else:
                busy = lock
                break
        if busy is None:
            return taken
        for lock in reversed(taken):
            lock.release()
        #: Poll, since whether a lock may be skipped can change meanwhile.
        if busy.acquire(timeout=_POLL):
            busy.release()


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

    def __init__(self, f):
        self.f = f
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
            self.data, self.eof, _ = _legacy_read_reply(self.f)
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


#: Read by the worker scripts and removed before any command runs: the
#: 0-based slot index that a bootstrap's first worker serves, added to each
#: worker's 1-based $brish_server_index. Set only for a bootstrap that
#: replaces one worker of a larger pool (see Brish._spawn).
SERVER_INDEX_OFFSET_VAR = "BRISH_SERVER_INDEX_OFFSET"


def _worker_env(offset=0):
    """The environment of a bootstrap: ours, plus BRISH_SESSION=1, which
    tells the worker scripts that they run in a session of their own (see
    Brish._spawn_legacy), and the index offset of a replacement bootstrap.
    The scripts remove both before any command runs."""
    env = dict(os.environ, BRISH_SESSION="1")
    env.pop(SERVER_INDEX_OFFSET_VAR, None)
    if offset:
        env[SERVER_INDEX_OFFSET_VAR] = str(int(offset))
    return env


def _in_group(pid, pgid):
    """Whether `pid` is alive in process group `pgid`. A worker is in its
    bootstrap's group (the session Brish started), so a worker PID that
    has been reused by another process since is never signalled."""
    try:
        return os.getpgid(pid) == pgid
    except OSError:
        return False


class _Refused(Exception):
    """A bootstrap was started for an instance that has moved on (cleaned
    up, or re-initialized) meanwhile."""


class _Slot:
    """One worker index of a Brish instance. Its lock is Brish.locks[i]; the
    worker that serves it is worker i of the bootstrap `p`, and can be
    swapped for another bootstrap's, at a fresh acquire of the slot (see
    Brish._acquire and Brish._service)."""

    __slots__ = ("p", "pending", "attn", "owner", "warming")

    def __init__(self, p):
        self.p = p
        #: A ready replacement: a bootstrap whose worker i has booted, to be
        #: swapped in at the slot's next fresh acquire.
        self.pending = None
        #: The next fresh acquire must look at this slot: its worker takes
        #: no request again (p.dead[i]), or a replacement is pending.
        self.attn = False
        #: The thread of the last fresh acquire (see Brish._owner_ended).
        self.owner = None
        #: An eager replacement that a helper thread is booting (see
        #: Brish._warm), or None.
        self.warming = None


class _Warm:
    """An eager replacement in the making (see Brish._warm): `done` is set
    once its thread has parked the new worker or given up."""

    __slots__ = ("done",)

    def __init__(self):
        self.done = threading.Event()


#: Protocol BRISH3 (binary mode). See docs/protocol.org.
BRISH3_FDS_ARG = "BRISH3-FDS"
_HELLO = b"\0BRISH3-HELLO:"
_READ_CHUNK = 65536
#: Seconds between liveness checks while a worker is silent.
_POLL = 0.5
#: Seconds between calls of a `cancelled` callable while a call waits for a
#: worker (see Brish.popen).
_CANCEL_POLL = 0.05
_END_TRAILER = re.compile(rb"(\d+)(:exit)?\Z")


class _StreamParser:
    """Splits one response stream into the bytes between START and END.

    Before START it keeps only the last len(START)-1 bytes. After START,
    `feed()` returns the payload bytes that are certain, as soon as they
    arrive: it holds back only a suffix that could be the start of END. END
    starts with NUL and has no other NUL, so only a suffix that starts at the
    last NUL can be such a start, and a chunk without NUL (with nothing held)
    is passed through without a search. Once END's prefix is found it waits
    for the newline that ends the END line. Anything after the END line is
    dropped.

    With `collect` (the default), the payload is also kept, in a list joined
    once by `payload()`.
    """

    __slots__ = (
        "start", "end", "keep", "pre", "started", "chunks", "held", "after", "trailer", "done",
    )

    def __init__(self, start, end_prefix, collect=True):
        self.start = start
        self.end = end_prefix
        self.keep = len(end_prefix) - 1
        self.pre = b""
        self.started = False
        self.chunks = [] if collect else None
        self.held = b""  # payload bytes that could be the start of END
        self.after = None  # the bytes after the END prefix, until its newline
        self.trailer = None
        self.done = False

    def feed(self, chunk):
        """Take the next bytes of the stream; return the payload bytes that
        are now certain (possibly b"")."""
        if self.done:
            return b""
        if self.after is not None:
            self._finish(self.after + chunk)
            return b""
        if not self.started:
            data = self.pre + chunk
            i = data.find(self.start)
            if i < 0:
                keep = len(self.start) - 1
                self.pre = data[-keep:] if len(data) > keep else data
                return b""
            self.pre = b""
            self.started = True
            chunk = data[i + len(self.start) :]
            if not chunk:
                return b""
        held = self.held
        if not held and b"\0" not in chunk:
            out = chunk
        else:
            window = held + chunk if held else chunk
            j = window.find(self.end)
            if j >= 0:
                out = window[:j]
                self.held = b""
                self._finish(window[j + len(self.end) :])
            else:
                i = window.rfind(b"\0", max(0, len(window) - self.keep))
                if i >= 0 and self.end.startswith(window[i:]):
                    out = window[:i]
                    self.held = window[i:]
                else:
                    out = window
                    self.held = b""
        if out and self.chunks is not None:
            self.chunks.append(out)
        return out

    def _finish(self, rest):
        k = rest.find(b"\n")
        if k < 0:
            self.after = rest
            return
        self.after = None
        self.trailer = rest[:k]
        self.done = True

    def flush(self):
        """The stream ended without END (the worker died): release the
        bytes held back as a possible start of END."""
        out, self.held = self.held, b""
        if out and self.chunks is not None:
            self.chunks.append(out)
        return out

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

    __slots__ = ("index", "req", "out", "err", "pid", "sel", "stale", "broken")

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
        #: A request frame was cut short, so the worker may hold part of one
        #: and would read the next request as its rest: it takes no request
        #: again (a lock holder gets BrishWorkerDiedException), and its slot
        #: gets a new worker.
        self.broken = False

    def close(self):
        if self.sel is not None:
            try:
                self.sel.close()
            except Exception:
                pass
            self.sel = None
        for fd in (self.req, self.out, self.err):
            if fd < 0:
                continue
            try:
                os.close(fd)
            except OSError:
                pass
        #: Closed: a second close() must not close numbers reused since.
        self.req = self.out = self.err = -1


#: The retcode line the bootstrap writes for a worker that died without
#: answering. Every Python parses it as 9001; no command's status prints so.
_LEGACY_DEATH_LINE = b"09001\n"


def _legacy_read_reply(f, with_rc=False):
    """Read one legacy reply from the binary file `f` (a reply FIFO), with
    _LegacyReplyParser, consuming nothing after it.

    Returns (payload, eof, rc_line): `eof` when the file ended before the
    delimiter, and with `with_rc` the retcode line (b"" or a partial line
    when the file ended inside it, as readline() would return it).
    """
    parser = _LegacyReplyParser(with_rc)
    chunks = []
    peek, read = f.peek, f.read
    while not parser.done:
        #: peek() returns the buffered bytes, after one raw read when the
        #: buffer is empty; only the bytes of this reply are consumed.
        buf = peek(1)
        if not buf:
            tail = parser.flush()
            if tail:
                chunks.append(tail)
            return b"".join(chunks), not parser.in_rc, parser.rc_buf
        payload, used = parser.feed(buf)
        read(used)
        if payload:
            chunks.append(payload)
    return b"".join(chunks), False, parser.rc


#: A legacy reply ends at the first line that is just NUL; the worker writes
#: a newline before it, which is not output.
_LEGACY_END = b"\n\0\n"


class _LegacyReplyParser:
    """Splits one legacy reply stream as it arrives. Both send_cmd (through
    _legacy_read_reply) and BrishPopen use it.

    A reply is the output, the newline the worker writes, a line holding only
    NUL, and on stdout the retcode line. The first line that is just NUL ends
    the output, as in the original line-by-line reader: the output ends at
    the first newline-NUL-newline, or at a NUL-newline at its very start
    (a virtual newline in `held` stands for the line start there).

    `feed(buf)` returns (payload, used): the output bytes that are now
    certain, and how many bytes of `buf` belong to this reply, so that the
    rest stays in the file's buffer for the next reply. Only a possible start
    of the end is held back: a trailing newline or newline-NUL.
    """

    __slots__ = ("with_rc", "held", "skip", "in_rc", "rc_buf", "rc", "done")

    def __init__(self, with_rc):
        self.with_rc = with_rc
        self.held = b"\n"
        self.skip = 1  # held[0] is the virtual newline, not output
        self.in_rc = False
        self.rc_buf = b""
        self.rc = None  # the retcode line, with its newline
        self.done = False

    def feed(self, buf):
        if self.in_rc:
            k = buf.find(b"\n")
            if k < 0:
                self.rc_buf += buf
                return b"", len(buf)
            self.rc = self.rc_buf + buf[: k + 1]
            self.done = True
            return b"", k + 1
        held, skip = self.held, self.skip
        window = held + buf
        j = window.find(_LEGACY_END)
        if j >= 0:
            payload = window[skip:j] if j > skip else b""
            used = j + len(_LEGACY_END) - len(held)
            self.held, self.skip = b"", 0
            if not self.with_rc:
                self.done = True
                return payload, used
            self.in_rc = True
            _, more = self.feed(buf[used:])
            return payload, used + more
        if window.endswith(b"\n\0"):
            cut = len(window) - 2
        elif window.endswith(b"\n"):
            cut = len(window) - 1
        else:
            cut = len(window)
        payload = window[skip:cut] if cut > skip else b""
        self.held = window[cut:]
        self.skip = 1 if (skip and cut == 0) else 0
        return payload, len(buf)

    def flush(self):
        """At EOF: the held bytes are output (the original reader kept every
        line it read before EOF)."""
        out = self.held[self.skip :]
        self.held, self.skip = b"", 0
        return out


class _Abandoned(Exception):
    pass


#: How far the legacy reader threads may run ahead of the caller: at most
#: _LEGACY_QUEUE_BYTES are queued, for both streams together. Once
#: _QUEUE_CHUNKS chunks are queued, a payload joins the last queued chunk of
#: its own stream, up to _MERGE_MAX bytes, so a caller that falls behind gets
#: fewer, larger chunks (as a pipe would give them). Only the byte bound
#: makes a reader wait. Once BrishPopen holds _QUEUE_CHUNKS chunks for the
#: caller, a new chunk joins the last one it holds of its stream the same way.
_QUEUE_CHUNKS = 4
_LEGACY_QUEUE_BYTES = 65536
_MERGE_MAX = 65536
#: Once kill() has sent its first signal, Brish reads up to this many bytes
#: more ahead of the caller, in both modes, so that it sees the command end
#: however slowly the caller reads (see BrishPopen._kill_step).
_KILL_READ_AHEAD = 256 * 1024
#: More than the pipes can hold: output read after a step's signal beyond
#: this was written after the signal.
_PIPE_SLACK = 128 * 1024
#: Binary mode, when a kill step is due: Brish reads ahead while the command
#: writes, until it has been quiet for _SETTLE seconds, for at most
#: _SETTLE_MAX seconds (see BrishPopen._kill_step).
_SETTLE = 0.1
_SETTLE_MAX = 0.5


class _ChunkQueue:
    """The queue between the legacy reader threads and BrishPopen: when the
    caller stops reading, the readers stop, the FIFOs fill up and the
    command blocks. Bounded in bytes only (see _QUEUE_CHUNKS); kill() raises
    the bound (see BrishPopen._kill_step)."""

    def __init__(self, chunks, limit):
        self.chunks = chunks
        self.limit = limit
        self.size = 0
        #: [stream, [parts], nbytes], or [None, None, 0] at the end.
        self.items = collections.deque()
        self.cond = threading.Condition(Lock())
        #: Readers waiting for room, that is with `limit` bytes queued.
        self.waiting = 0

    def put(self, stream, payload, abandoned):
        """Queue `payload`, waiting while the queue holds too many bytes. An
        empty queue takes any payload. Returns False, without queueing, once
        `abandoned()` is true."""
        n = len(payload)
        items = self.items
        with self.cond:
            while items and self.size + n > self.limit:
                if abandoned():
                    return False
                self.waiting += 1
                try:
                    self.cond.wait(_POLL)
                finally:
                    self.waiting -= 1
            last = None
            if len(items) >= self.chunks:
                #: The last chunk of this stream, so the stream keeps its
                #: order; the two streams have no order between them.
                for item in reversed(items):
                    if item[0] == stream:
                        last = item
                        break
            if last is not None and last[2] + n <= _MERGE_MAX:
                last[1].append(payload)
                last[2] += n
            else:
                items.append([stream, [payload], n])
            self.size += n
            self.cond.notify_all()
        return True

    def put_end(self):
        """Queue the end-of-stream item (None, None), which never waits."""
        with self.cond:
            self.items.append([None, None, 0])
            self.cond.notify_all()

    def get(self, timeout=0):
        """The next (stream, chunk), or None after `timeout` seconds without
        one."""
        with self.cond:
            if not self.items and timeout > 0:
                self.cond.wait_for(lambda: self.items, timeout)
            if not self.items:
                return None
            stream, parts, n = self.items.popleft()
            self.size -= n
            self.cond.notify_all()
        if stream is None:
            return (None, None)
        return (stream, parts[0] if len(parts) == 1 else b"".join(parts))

    def raise_limit(self, limit):
        with self.cond:
            if limit > self.limit:
                self.limit = limit
                self.cond.notify_all()


class _LegacyStreamReader:
    """Reads one legacy reply stream for BrishPopen in a helper thread and
    puts (stream, payload) items on a _ChunkQueue, so a reader that stops
    reading stops the command too. kqueue and poll are unreliable on FIFOs
    on macOS, hence the thread. Like _ErrReader, it owns the file while it
    runs: cleanup() hands the file over instead of closing it under a
    blocked read.
    """

    def __init__(self, f, stream, with_rc, q):
        self.f = f
        self.stream = stream
        self.q = q
        self.parser = _LegacyReplyParser(with_rc)
        self.eof = False
        self.exc = None
        self.done = False
        self.abandoned = False
        self.close_when_done = False
        #: When the FIFO last gave bytes, and how many it gave in all (see
        #: BrishPopen._kill_step).
        self.last_read = time.monotonic()
        self.nread = 0
        self._lock = Lock()
        self.thread = threading.Thread(
            target=self._run, daemon=True, name=f"brish-popen-{stream}"
        )
        self.thread.start()

    def _put(self, item):
        if not self.q.put(item[0], item[1], lambda: self.abandoned):
            raise _Abandoned

    def _run(self):
        try:
            f, parser = self.f, self.parser
            while not parser.done:
                #: peek() returns what the buffer holds (one raw read when it
                #: is empty); only the bytes of this reply are consumed.
                buf = f.peek(1)
                if not buf:
                    self.eof = True
                    tail = parser.flush()
                    if tail:
                        self._put((self.stream, tail))
                    break
                self.last_read = time.monotonic()
                payload, used = parser.feed(buf)
                f.read(used)
                self.nread += used
                if payload:
                    self._put((self.stream, payload))
        except _Abandoned:
            pass
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
            self.q.put_end()  # wakes the reader up

    def close_or_hand_over(self):
        self.abandoned = True
        with self._lock:
            if not self.done:
                self.close_when_done = True
                return
        try:
            self.f.close()
        except Exception:
            pass


def _legacy_busy(p, index):
    """Whether legacy worker `index` cannot take a request: a reply on it was
    abandoned (see Brish._legacy_abandon), or a helper thread of such a reply
    still reads one of its reply FIFOs."""
    stale = getattr(p, "legacy_stale", None)
    if stale and stale[index]:
        return True
    for readers in (p.err_readers, getattr(p, "out_readers", None)):
        if readers:
            r = readers[index]
            if r is not None and not r.done:
                return True
    return False


#: The legacy bootstrap (brish2.zsh) reports its workers' PIDs on its
#: stdout, after whatever the startup files printed there.
_LEGACY_PIDS_RE = re.compile(rb"\0BRISH2-PIDS:([0-9 ]*)\n")
#: How long the first popen of a generation waits for that report. The
#: bootstrap writes it right after forking the workers, before init returns.
_LEGACY_PIDS_WAIT = 5.0


def _legacy_read_pids(p, timeout):
    """Read the bootstrap's PID report (see _LEGACY_PIDS_RE) into
    p.legacy_pids, waiting up to `timeout` seconds for it. Returns whether
    it was read. A shell that is not brish2.zsh is not asked, and one that
    has not reported after a full _LEGACY_PIDS_WAIT is not asked again. Each
    PID must be in the bootstrap's process group, the session Brish started
    it in."""
    if p.pid_report is not None:
        return p.pid_report
    with p.pid_lock:
        if p.pid_report is not None:
            return p.pid_report
        try:
            fd = p.stdout.fileno()
        except (ValueError, OSError):
            return False
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as sel:
            sel.register(fd, selectors.EVENT_READ)
            while True:
                m = _LEGACY_PIDS_RE.search(p.boot_out)
                if m:
                    p.boot_out = b""
                    pids = [int(x) for x in m.group(1).split()]
                    #: The report lists the workers of this bootstrap, in
                    #: slot order: every slot for a full bootstrap, one for
                    #: a replacement (see Brish._spawn).
                    ok = len(pids) == len(p.slot_ids)
                    for i, pid in zip(p.slot_ids, pids if ok else ()):
                        try:
                            if os.getpgid(pid) == p.pid and p.legacy_pids[i] is None:
                                p.legacy_pids[i] = pid
                        except OSError:
                            pass  # gone already: the worker is replaced
                    p.pid_report = True if ok else False
                    return ok
                left = deadline - time.monotonic()
                if left <= 0 or not sel.select(left):
                    if timeout >= _LEGACY_PIDS_WAIT:
                        p.pid_report = False
                    return False
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    p.pid_report = False
                    return False
                #: Keep only what could hold the report: startup files may
                #: print plenty.
                p.boot_out = (p.boot_out + chunk)[-65536:]


#: Internal request that prints a legacy worker's PID, for a shell that does
#: not report them (see _legacy_read_pids): the parent of a command
#: substitution. The worker itself does not load zsh/system. The PID is
#: bracketed by a marker, because a background job of an earlier command may
#: write into the same reply (see docs/protocol.org); the last match wins.
#: The marker is printed together with the PID, inside a quoted
#: substitution, so that worker state (a DEBUG trap that prints, digits in
#: IFS) cannot separate them.
_LEGACY_PID_CMD = (
    b'builtin print -r -- "$(builtin zmodload zsh/system && builtin print -r -- brish-pid:${sysparams[ppid]}:)"'
)
_LEGACY_PID_RE = re.compile(rb"brish-pid:(\d+):")
#: kill() sends its first SIGINT no sooner than this many seconds after the
#: request was written, so that it reaches a command that has started.
_KILL_SETTLE = 0.05


_RLOCK_STATE = re.compile(r"owner=(\d+) count=(\d+)")


def _rlock_levels(lock, ident):
    """How many levels of the RLock `lock` the thread `ident` holds, or None
    if unknown. CPython's RLock shows its owner and count only in its repr;
    without them, a caller must assume the worst."""
    m = _RLOCK_STATE.search(repr(lock))
    if m is None:
        return None
    return int(m.group(2)) if int(m.group(1)) == ident else 0


class BrishPopen:
    """A command whose output streams while it runs. Made by `Brish.popen`,
    which documents the API.

    It holds its worker's lock from creation until the command has ended and
    its output has been read (or drained), so it is created, read, waited for
    and closed in one thread; `kill()` and `terminate()` work from any thread.
    """

    #: Seconds between the steps of kill()'s escalation.
    kill_grace = 2.0

    def __init__(self, brish, cmd, cmd_stdin="", fork=False, server_index=None,
                 lock_sleep=1, buffer=False, cancelled=None):
        self.cmd = cmd
        self.cmd_stdin = brish._stored_stdin(cmd_stdin)
        self.fork = bool(fork)
        #: None until the command has ended, then its status.
        self.retcode = None
        #: The worker that runs it.
        self.server_index = None
        self._brish = brish
        self._owner = threading.get_ident()
        self._owner_thread = threading.current_thread()
        self._mu = Lock()  # guards _finished, _stage and signal sending
        self._finished = False
        self._released = True  # until a worker lock is taken
        self._stage = 0  # 0: not killed; 1: SIGINT; 2: SIGTERM; 3, 4: SIGKILL
        self._stage_t = 0.0
        self._pending = collections.deque()
        self._pending_bytes = 0
        #: Set once kill() has sent its first signal; then Brish reads
        #: ahead. _step_nread: the bytes read when the last step was taken.
        self._ahead = False
        #: Set when kill()'s first signal is out, or its helper thread found
        #: the command ended (see signalled). _kill_t: when kill() was
        #: called.
        self._signal_done = threading.Event()
        self._kill_t = None
        self._nread = 0  # binary mode; legacy readers count their own
        self._step_nread = 0
        self._buffer = ([], []) if buffer else None
        self._result = None
        self._err_nl = True  # the err stream so far is empty or ends in a newline
        self._worker_pid = None
        self._last_io = time.monotonic()
        self._dead_since = None
        #: `cancelled` is passed down, never kept: a callable that refers to
        #: this object would make a reference cycle.
        self._start(cmd, cmd_stdin, fork, server_index, lock_sleep, cancelled)
        self._started_at = time.monotonic()

    def __repr__(self):
        state = "running" if self.retcode is None else f"retcode={self.retcode}"
        return f"<BrishPopen {state} server_index={self.server_index} cmd={_text_view(self.cmd)!r}>"

    # Starting

    def _start(self, cmd, cmd_stdin, fork, server_index, lock_sleep, cancelled):
        b = self._brish
        restart_cmd = cmd
        if isinstance(cmd, _BYTES_LIKE):
            restart_cmd = bytes(cmd).decode("utf-8", "surrogateescape")
        if restart_cmd == "%BRISH_RESTART":
            res = b.send_cmd(cmd, cmd_stdin=cmd_stdin)
            self._finish_without_worker(res.retcode, res.outb, res.errb)
            return
        #: Encode everything first: an encoding error must leave the worker
        #: untouched.
        cmd_b = b._to_bytes(cmd, "cmd")
        if b.binary:
            stdin_b = None if cmd_stdin is None else b._to_bytes(cmd_stdin, "cmd_stdin")
            start = self._start_binary
        else:
            stdin_b = b"" if cmd_stdin is None else b._to_bytes(cmd_stdin, "cmd_stdin")
            if b"\0" in cmd_b or b"\0" in stdin_b:
                self._finish_without_worker(
                    9000, b"",
                    b"Illegal input: Input contained the Brish marker (currently the NUL character).",
                )
                return
            start = self._start_legacy
        for attempt in range(2):
            #: One window is left, as in send_cmd: a KeyboardInterrupt as
            #: _acquire() returns, before the `try`, keeps the lock for
            #: good. Closing it would need _acquire to record the lock where
            #: the except clause reads it, which still leaves a gap of a few
            #: bytecodes inside _acquire.
            lock, index, p = b._acquire(server_index, lock_sleep, cancelled)
            try:
                #: No call before _released is cleared: an interrupt there
                #: (a KeyboardInterrupt comes at calls and loops) would reach
                #: the except clause with the lock taken but not recorded.
                self._lock, self.server_index, self._p = lock, index, p
                self._released = False
                p.free_server_count -= 1
                p.popen_owner[index] = self._owner_thread
                outcome = start(p, index, cmd_b, stdin_b, fork, cancelled)
            except BaseException:
                #: An interrupt while starting (the start methods leave the
                #: worker resynchronisable or due for replacement) must not
                #: leak the worker lock.
                self._release()
                raise
            if outcome is not _NEVER_RAN:
                return
            self._release()
            b._never_ran(p, index, lock)
        raise BrishWorkerDiedException("a worker died before running the command, twice")

    def _finish_without_worker(self, retcode, outb, errb):
        if outb:
            self._push("out", outb)
        if errb:
            self._push("err", errb)
        self._finished = True
        self.retcode = retcode

    def _start_binary(self, p, index, cmd_b, stdin_b, fork, cancelled):
        """Write the frame and wait for START. Returns _NEVER_RAN if the
        worker died before START, or is broken; then the lock is still
        held."""
        w = p.workers[index]
        if w.broken or p.dead[index]:
            return _NEVER_RAN
        self._w = w
        self._worker_pid = w.pid
        nonce = secrets.token_hex(16).encode()
        stdin_len = b"-" if stdin_b is None else b"%d" % len(stdin_b)
        header = b"BRISH3 %s %d %s %d\n" % (nonce, len(cmd_b), stdin_len, 1 if fork else 0)
        frame = b"".join((header, cmd_b, stdin_b or b""))
        self._frame = memoryview(frame)
        self._total = len(frame)
        self._sent = 0
        self._wreg = False
        start = b"\0BRISH3-START:" + nonce + b"\n"
        end = b"\0BRISH3-END:" + nonce + b":"
        self._so = _StreamParser(start, end, collect=False)
        self._se = _StreamParser(start, end, collect=False)
        self._streams = {w.out: ("out", self._so), w.err: ("err", self._se)}
        self._died = False
        if cancelled is not None:
            _check_cancelled(cancelled)  # nothing written yet
        try:
            try:
                self._sent = os.write(w.req, frame)
            except BlockingIOError:
                pass
            except BrokenPipeError:
                self._died = True
            while not self._died and not (self._so.started or self._se.started):
                self._pump_binary(_POLL)
        except BaseException:
            self._unregister_write()
            if self._sent >= self._total:
                w.stale = True
            else:
                #: The worker may hold part of a frame.
                w.broken = True
                self._brish._worker_died(p, index)
            self._release()
            raise
        if self._died and not (self._so.started or self._se.started):
            self._unregister_write()
            self._pending.clear()
            self._pending_bytes = 0
            return _NEVER_RAN
        return None

    def _start_legacy(self, p, index, cmd_b, stdin_b, fork, cancelled):
        """Learn the worker's PID if needed, write the request and start the
        reader threads. Returns _NEVER_RAN if the worker could not take it;
        then the lock is still held."""
        b = self._brish
        if p.dead[index]:
            return _NEVER_RAN
        if p.legacy_pids[index] is None:
            if self._learn_legacy_pid(p, index) is _NEVER_RAN:
                return _NEVER_RAN
        self._worker_pid = p.legacy_pids[index]
        if _legacy_busy(p, index):
            return _NEVER_RAN
        frame = b"".join((cmd_b, b"\0", stdin_b, b"\0", b"y" if fork else b"", b"\0\n"))
        if cancelled is not None:
            _check_cancelled(cancelled)  # nothing written yet
        try:
            f = p.brish_stdins[index]
            f.write(frame)
            f.flush()
        except BrokenPipeError:
            return _NEVER_RAN
        except BaseException:
            b._legacy_abandon(p, index)
            raise
        try:
            self._q = _ChunkQueue(_QUEUE_CHUNKS, _LEGACY_QUEUE_BYTES)
            self._rout = _LegacyStreamReader(p.brish_stdouts[index], "out", True, self._q)
            self._rerr = _LegacyStreamReader(p.brish_stderrs[index], "err", False, self._q)
            p.out_readers[index] = self._rout
            p.err_readers[index] = self._rerr
        except BaseException:
            b._legacy_abandon(p, index)
            raise
        return None

    def _learn_legacy_pid(self, p, index):
        """Learn legacy worker `index`'s PID, once per generation: from the
        bootstrap's report (see _legacy_read_pids), or by asking the worker
        (see _LEGACY_PID_CMD). Returns _NEVER_RAN if the worker could not
        answer. An interrupt while the request is in flight leaves its reply
        unread, so the worker is given up (Brish._legacy_abandon)."""
        b = self._brish
        if _legacy_read_pids(p, _LEGACY_PIDS_WAIT) and p.legacy_pids[index] is not None:
            return None
        complete = False
        try:
            outcome = b._legacy_transact(
                p, index, _LEGACY_PID_CMD + b"\0\0\0\n", _LEGACY_PID_CMD, ""
            )
            complete = True
            if outcome is _NEVER_RAN:
                return _NEVER_RAN
            res, restart = outcome
            if restart:
                b._worker_died(p, index)
                return _NEVER_RAN
            found = _LEGACY_PID_RE.findall(res.outb)
            #: Only a child of this instance's bootstrap is ever signalled.
            if found and _parent_pid(int(found[-1])) == p.pid:
                p.legacy_pids[index] = int(found[-1])
        except BaseException:
            if not complete:
                b._legacy_abandon(p, index)
            raise
        return None

    # Reading

    def _pump_binary(self, timeout):
        """One select round on the worker's pipes. Returns whether anything
        was read."""
        w = self._w
        sel = w.sel
        if self._sent < self._total and not self._wreg:
            sel.register(w.req, selectors.EVENT_WRITE)
            self._wreg = True
        events = sel.select(timeout)
        if not events:
            if time.monotonic() - self._last_io >= _POLL and not _alive(w.pid):
                self._binary_died()
            return False
        self._last_io = time.monotonic()
        for key, _ in events:
            fd = key.fd
            if fd == w.req:
                try:
                    self._sent += os.write(fd, self._frame[self._sent : self._sent + _READ_CHUNK])
                except BlockingIOError:
                    continue
                except BrokenPipeError:
                    self._binary_died()
                    return True
                if self._sent >= self._total:
                    self._unregister_write()
                continue
            name, st = self._streams[fd]
            try:
                chunk = os.read(fd, _READ_CHUNK)
            except BlockingIOError:
                continue
            if not chunk:
                self._binary_died()
                return True
            self._nread += len(chunk)
            out = st.feed(chunk)
            if out:
                self._push(name, out)
        if self._so.done and self._se.done and not self._finished:
            retcode, exited = _parse_trailer(self._so.trailer)
            self._w.stale = False
            self._finish(retcode, restart=exited)
        return True

    def _unregister_write(self):
        if self._wreg:
            try:
                self._w.sel.unregister(self._w.req)
            except (KeyError, ValueError, OSError, AttributeError):
                pass
            self._wreg = False

    def _binary_died(self):
        """The worker is gone: collect what it wrote (START and END may still
        be in the pipes), then finish."""
        self._died = True
        self._unregister_write()
        for fd, (name, st) in self._streams.items():
            for _ in range(64):  # a background job may keep writing
                if st.done:
                    break
                try:
                    chunk = os.read(fd, _READ_CHUNK)
                except (BlockingIOError, OSError):
                    break
                if not chunk:
                    break
                out = st.feed(chunk)
                if out:
                    self._push(name, out)
            out = st.flush()
            if out:
                self._push(name, out)
        if not (self._so.started or self._se.started):
            return  # it never ran: _start_binary handles that
        if self._so.done:
            retcode, _ = _parse_trailer(self._so.trailer)
            note = False
        elif self._se.done:
            retcode, _ = _parse_trailer(self._se.trailer)
            note = False
        else:
            retcode, note = RETCODE_WORKER_DIED, True
        self._finish(retcode, restart=True, note=note)

    def _pump_legacy(self, timeout):
        """Take what the reader threads have read. Returns whether anything
        was read."""
        item = self._q.get(timeout)
        if item is not None and item[0] is not None:
            self._push(*item)
            self._last_io = time.monotonic()
            return True
        rout, rerr = self._rout, self._rerr
        if rout.done and rerr.done:
            self._legacy_drain_queue()
            self._legacy_complete()
            return item is not None
        now = time.monotonic()
        if rout.done and self._legacy_rc()[1]:
            #: The worker is gone. Its stderr ends soon, unless a background
            #: job holds the FIFO open: then stop waiting after 2 s.
            dead = False
        elif now - self._last_io >= _POLL and self._worker_pid and not _alive(self._worker_pid):
            #: No reply from the bootstrap (it is gone too, and a background
            #: job holds the FIFOs open): the same 2 s.
            dead = not rout.done
        else:
            self._dead_since = None
            return item is not None
        if self._dead_since is None:
            self._dead_since = now
        elif now - self._dead_since > 2:
            self._legacy_drain_queue()
            self._legacy_complete(dead=dead)
        return item is not None

    def _legacy_drain_queue(self):
        while True:
            item = self._q.get()
            if item is None:
                return
            if item[0] is not None:
                self._push(*item)

    def _legacy_rc(self):
        """(retcode, died, exited) from the stdout reader, which is done."""
        rout = self._rout
        if rout.exc is not None or rout.eof or rout.parser.rc is None:
            return None, True, False
        line = rout.parser.rc
        try:
            retcode = int(line)
        except ValueError:
            #: The output forged the end of the reply (see docs/protocol.org).
            return None, True, False
        #: "+N": the command exited the worker; "09001": the bootstrap
        #: answered for a worker that died (see _legacy_transact).
        return retcode, line == _LEGACY_DEATH_LINE, line.startswith(b"+")

    def _legacy_complete(self, dead=False):
        rout, rerr = self._rout, self._rerr
        p, index = self._p, self.server_index
        if dead:
            retcode, died, exited = None, True, False
        else:
            retcode, died, exited = self._legacy_rc()
        if not rerr.done or rerr.exc is not None or rerr.eof:
            died = True
        if rout.done and not died:
            p.out_readers[index] = None
        if rerr.done and not died:
            p.err_readers[index] = None
        if died:
            #: Readers that are still running keep their files (cleanup hands
            #: them over). The worker is gone, or out of sync after a forged
            #: delimiter, so it takes no more requests, and its slot gets a
            #: new worker.
            self._brish._legacy_abandon(p, index)
            for r in (rout, rerr):
                if not r.done:
                    r.abandoned = True
                else:
                    if p.out_readers[index] is r:
                        p.out_readers[index] = None
                    if p.err_readers[index] is r:
                        p.err_readers[index] = None
            if rout.exc is not None and not isinstance(rout.exc, OSError):
                p.interrupted = True
        if retcode is None:
            retcode = RETCODE_WORKER_DIED
            note = True
        else:
            note = died
        self._finish(retcode, restart=died or exited, note=note)

    # Finishing

    def _push(self, name, chunk):
        pending = self._pending
        self._pending_bytes += len(chunk)
        if len(pending) >= _QUEUE_CHUNKS:
            #: Into the last held chunk of this stream, as in _ChunkQueue.put:
            #: the stream keeps its order, and the two streams have none
            #: between them. Joining only a last chunk of the same stream
            #: would let two streams in turn pile up one small chunk per
            #: read, and after kill() a slow caller would have to take
            #: _PIPE_SLACK bytes of them before the next step (see
            #: _kill_step).
            for i in range(len(pending) - 1, -1, -1):
                if pending[i][0] != name:
                    continue
                last = pending[i][1]
                if len(last) + len(chunk) <= _MERGE_MAX:
                    if not isinstance(last, bytearray):
                        last = bytearray(last)
                        pending[i] = (name, last)
                    last += chunk
                    return
                break
        pending.append((name, chunk))

    def _finish(self, retcode, restart=False, note=False):
        with self._mu:
            self._finished = True
            #: Step 4 SIGKILLed the worker (perhaps only after the command
            #: had ended): its state is gone, whatever the reply says.
            killed = self._stage >= 4
            if killed:
                retcode, restart, note = RETCODE_WORKER_DIED, True, True
            if note:
                #: The note is a chunk of its own, never merged into output
                #: (with a newline chunk before it if the err stream so far
                #: does not end in one).
                pending = self._pending
                if not self._err_nl_after_pending():
                    pending.append(("err", b"\n"))
                    self._pending_bytes += 1
                msg = (WORKER_DIED_NOTE + "\n").encode()
                pending.append(("err", msg))
                self._pending_bytes += len(msg)
            #: Set last, under _mu: `result` from another thread then sees
            #: every chunk.
            self.retcode = retcode
        if killed and not self._p.binary:
            self._brish._legacy_abandon(self._p, self.server_index)
        self._release(restart)

    def _err_nl_after_pending(self):
        nl = self._err_nl
        for name, chunk in self._pending:
            if name == "err":
                nl = chunk.endswith(b"\n")
        return nl

    def _release(self, restart=False):
        """Free the worker; with `restart`, it takes no request again, and
        its slot gets a new worker (see Brish._worker_died). Its lock is an
        RLock of the owner thread, so only the owner can release it: called
        in another thread (an orphan's helper thread, see _orphan), this
        only marks the worker, and the owner releases the lock at its next
        call (Brish._reap_orphans)."""
        if self._released:
            return
        p = self._p
        if restart:
            self._brish._worker_died(p, self.server_index)
        if threading.get_ident() != self._owner:
            return
        self._released = True
        p.free_server_count += 1
        p.popen_owner[self.server_index] = None
        self._lock.release()

    def _legacy_reply_read(self):
        """Whether both legacy reader threads have read their whole reply
        (or met the end of their FIFO)."""
        rout, rerr = getattr(self, "_rout", None), getattr(self, "_rerr", None)
        return rout is not None and rerr is not None and rout.done and rerr.done

    def _abandon(self):
        """Give the worker up without waiting for the reply: after an
        exception while reading, or when the object is collected unclosed.
        The command is interrupted; binary mode skips the rest of its reply
        on the next request (a stale worker), legacy mode replaces the
        worker, unless its readers already have the whole reply. The lock is
        released even if this is interrupted itself."""
        if self._released:
            return
        p = self._p
        if not p.binary and not self._finished and self._legacy_reply_read():
            #: Nothing is left unread, so the worker is in sync: finish as
            #: usual, and it keeps its state.
            try:
                self._legacy_drain_queue()
                self._legacy_complete()
            except BaseException:
                pass
            if self._finished:
                self._release()  # in case the exception came before it
                return
        already = self._finished
        try:
            pid = self._worker_pid
            try:
                pids = _descendants(pid) if pid else []
            except BaseException:
                pids = []
            with self._mu:
                already = self._finished
                self._finished = True
                if not already:
                    #: The worker first: see _interrupt.
                    if pid:
                        _signal_pids([pid], signal.SIGINT)
                    _signal_pids(pids, signal.SIGINT)
        finally:
            restart = False
            if p.binary:
                if getattr(self, "_w", None) is not None:
                    self._unregister_write()
                    if already:
                        pass
                    elif self._sent >= self._total:
                        self._w.stale = True
                    else:
                        self._w.broken = True
                        restart = True
            elif not already:
                for r in (getattr(self, "_rout", None), getattr(self, "_rerr", None)):
                    if r is not None and not r.done:
                        r.abandoned = True
                self._brish._legacy_abandon(p, self.server_index)
            self._release(restart)

    def _check_owner(self):
        if threading.get_ident() != self._owner:
            raise RuntimeError(
                "a BrishPopen is read, waited for and closed in the thread that created it "
                "(it holds that thread's worker lock); kill() works from any thread"
            )

    def _next_event(self):
        """The next (stream, chunk), or None once the output is complete."""
        self._check_owner()
        return self._read_event()

    def _read_event(self):
        """_next_event without the owner check: also an orphan's helper
        thread reads this way (see _orphan)."""
        while True:
            if self._stage and not self._finished:
                try:
                    if not self._ahead:
                        #: kill() was called, and its first signal is not
                        #: out yet. Reading now would drain the pipes, so a
                        #: command held up by backpressure would run on (and
                        #: a command after it in the same line too) before
                        #: the signal lands. Wait for it, at most a grace.
                        self._wait_first_signal(None)
                    self._kill_step()
                except BaseException:
                    self._abandon()
                    raise
            if self._pending:
                with self._mu:  # see `result`
                    ev = self._pending.popleft()
                    self._pending_bytes -= len(ev[1])
                    if isinstance(ev[1], bytearray):
                        ev = (ev[0], bytes(ev[1]))
                    if self._buffer is not None:
                        self._buffer[0 if ev[0] == "out" else 1].append(ev[1])
                if ev[0] == "err":
                    self._err_nl = ev[1].endswith(b"\n")
                return ev
            if self._finished:
                return None
            try:
                self._step()
            except BaseException:
                self._abandon()
                raise

    def _step(self):
        timeout = _POLL
        if self._stage:
            #: Wake up for the next step; past it, while output still
            #: flows, check again every 50 ms.
            left = self._stage_t + self.kill_grace - time.monotonic()
            timeout = min(timeout, left if left > 0 else 0.05)
        #: Dispatched here rather than through a bound method kept on the
        #: object, which would make a reference cycle: a BrishPopen dropped
        #: unclosed would then wait for the cyclic garbage collector, in
        #: whatever thread it runs.
        if self._p.binary:
            self._pump_binary(timeout)
        else:
            self._pump_legacy(timeout)

    def _kill_step(self):
        """After kill(), on every read: notice that the command has ended, or
        take the next step when it is due. Both go by what the command does,
        not by how fast the caller reads: once the first signal is out, Brish
        reads up to _KILL_READ_AHEAD bytes ahead of the caller, so it sees
        the end of the reply early. The legacy reader threads read ahead by
        themselves; in binary mode Brish reads ahead only inside the
        caller's reads, so when a step is due it first reads for as long as
        the command keeps writing (see _read_ahead)."""
        binary = self._p.binary
        if binary:
            if self._ahead:
                self._read_ahead(0)
        elif self._rout.done and self._rerr.done:
            self._legacy_drain_queue()
            self._legacy_complete()
        if self._finished:
            return
        with self._mu:
            if not self._ahead:
                return  # the first signal is not out yet
            due = self._stage_t + self.kill_grace
        now = time.monotonic()
        if now < due:
            return
        if binary:
            #: A command that was blocked on a full pipe while the caller
            #: did not read goes on writing only now.
            self._read_ahead(_SETTLE)
            if self._finished:
                return
            now = time.monotonic()
        with self._mu:
            since = self._nread_total() - self._step_nread
        if self._blocked():
            #: Brish is as far ahead of the caller as it reads, so the end of
            #: the reply may be waiting behind the caller. Go on only once
            #: the command has visibly written since the last step.
            if since <= _PIPE_SLACK:
                return
            flows = True
        else:
            flows = self._output_flows(now)
        #: The next step comes once the command is quiet, or a grace later if
        #: its output keeps coming (it ignores the signal and prints).
        if not flows or now >= due + self.kill_grace:
            self._escalate()

    def _read_ahead(self, quiet):
        """Binary mode, once the first signal is out: read while the pipes
        give output within `quiet` seconds (at most _SETTLE_MAX seconds in
        all), until Brish holds _KILL_READ_AHEAD bytes or the reply ends."""
        deadline = time.monotonic() + _SETTLE_MAX
        for _ in range(256):
            if self._finished or self._pending_bytes >= _KILL_READ_AHEAD:
                return
            if not self._pump_binary(quiet) or time.monotonic() > deadline:
                return

    def _nread_total(self):
        if self._p.binary:
            return self._nread
        return self._rout.nread + self._rerr.nread

    def _blocked(self):
        """Whether reading ahead has stopped because the caller is behind:
        Brish holds as many bytes as it reads ahead."""
        if self._p.binary:
            return self._pending_bytes >= _KILL_READ_AHEAD
        return self._q.waiting > 0  # the queue's byte bound is reached

    def _output_flows(self, now):
        """Whether the command wrote within the last _POLL seconds."""
        if self._p.binary:
            return now - self._last_io < _POLL
        return any(now - r.last_read < _POLL for r in (self._rout, self._rerr) if not r.done)

    def _signalled(self):
        """A step's signal is out (under _mu). The next step is due a grace
        from now: listing the processes to signal (one `ps`) can take
        seconds on a loaded machine, and the command needs the whole grace
        to react."""
        self._stage_t = time.monotonic()
        self._step_nread = self._nread_total()
        if not self._ahead:
            self._ahead = True
            if not self._p.binary:
                self._q.raise_limit(_LEGACY_QUEUE_BYTES + _KILL_READ_AHEAD)
            self._signal_done.set()

    def _peek_end(self):
        """Before SIGKILL to the worker: look for the end of the reply in
        what the pipes already hold beyond the read-ahead (up to _PIPE_SLACK
        more bytes, for up to _SETTLE_MAX seconds). A command that ended
        just after the last step can have its end waiting there, behind
        output the caller has not read yet. Returns whether the command has
        ended."""
        deadline = time.monotonic() + _SETTLE_MAX
        if self._p.binary:
            limit = self._nread + _PIPE_SLACK
            while not self._finished and self._nread < limit and time.monotonic() < deadline:
                if not self._pump_binary(_SETTLE):
                    break
        else:
            rout, rerr = self._rout, self._rerr
            self._q.raise_limit(self._q.limit + _PIPE_SLACK)
            for r in (rout, rerr):
                r.thread.join(max(0.0, deadline - time.monotonic()))
            if rout.done and rerr.done and not self._finished:
                self._legacy_drain_queue()
                self._legacy_complete()
        return self._finished

    def _escalate(self):
        pid = self._worker_pid
        stage = self._stage + 1
        if stage <= 4:
            pids = _descendants(pid) if pid else []
            if not pids and (stage == 3 or stage == 4):
                #: Next is SIGKILL to the worker (step 4). Without processes
                #: below it, the command may have ended with its end held
                #: behind the caller; then there is nothing to kill.
                if self._peek_end():
                    return
                if stage == 3 and self.fork:
                    #: A fork command's subshell has exited: the worker has
                    #: only the end to write, and runs no loop that ignores
                    #: the signals. Step 4 only if it has not, a grace later.
                    with self._mu:
                        if self._finished:
                            return
                        self._stage = 3
                        self._signalled()
                    return
            elif stage == 4 and self._peek_end():
                return
            with self._mu:
                if self._finished:
                    return
                self._stage = stage
                self._stage_t = time.monotonic()
                if stage == 2:
                    #: Again, in case the first one landed before the
                    #: command started; the worker first (see _interrupt).
                    if pid:
                        _signal_pids([pid], signal.SIGINT)
                    _signal_pids(pids, signal.SIGTERM)
                elif stage == 3 and pids:
                    _signal_pids(pids, signal.SIGKILL)
                else:
                    #: Last resort, also at stage 3 when nothing runs below
                    #: the worker (the worker itself ignores the signals).
                    self._stage = 4
                    _signal_pids(pids + ([pid] if pid else []), signal.SIGKILL)
                self._signalled()
            return
        #: The worker was killed a grace ago and its death went unnoticed
        #: (or its PID is unknown): stop waiting for it.
        with self._mu:
            self._stage = 5
        if self._p.binary:
            self._binary_died()
        else:
            self._legacy_drain_queue()
            self._p.interrupted = True
            self._legacy_complete(dead=True)

    # Public API

    def kill(self):
        """Interrupt the command (not the worker). Thread-safe and idempotent;
        returns at once. See Brish.popen for the escalation."""
        with self._mu:
            if self._finished or self._stage:
                return
            self._stage_t = self._kill_t = time.monotonic()
            self._stage = 1
            delay = max(0.0, getattr(self, "_started_at", 0.0) + _KILL_SETTLE - self._stage_t)
        threading.Thread(
            target=self._interrupt, args=(delay,), daemon=True, name="brish-popen-kill"
        ).start()

    terminate = kill

    @property
    def signalled(self):
        """Whether kill()'s first signal (SIGINT to the worker and the
        processes below it) is out. False before kill(), and when the
        command had ended before the signal was due. Any thread may read
        it."""
        return self._ahead

    def wait_signalled(self, timeout=None):
        """Wait until kill()'s first signal is out, and return `signalled`.

        Returns at once when there is nothing to wait for: kill() was not
        called, or the command had ended. Waits at most kill_grace seconds
        after kill() (and at most `timeout` seconds), which is also how
        long a read after kill() waits for the signal: until it is out, the
        output is not read, so that a command held up by a full pipe does
        not run on meanwhile. Any thread may call it."""
        if self._stage and not self._ahead and not self._finished:
            self._wait_first_signal(timeout)
        return self._ahead

    def _wait_first_signal(self, timeout):
        kill_t = self._kill_t
        left = (kill_t if kill_t is not None else time.monotonic()) + self.kill_grace - time.monotonic()
        if timeout is not None:
            left = min(left, timeout)
        if left > 0:
            self._signal_done.wait(left)

    def _interrupt(self, delay):
        if delay:
            time.sleep(delay)
        pid = self._worker_pid
        pids = _descendants(pid) if pid else []
        with self._mu:
            if self._finished:
                self._signal_done.set()
                return
            #: The worker before its descendants. zsh runs the worker's trap
            #: once the foreground child it waits for has exited, so the trap
            #: then sees the child die of the signal. The other way round,
            #: if this thread is held up between the two (the GIL, the OS),
            #: the worker may reap the child and go on before its own SIGINT
            #: arrives: the command then runs on, or under set -e the
            #: child's 130 exits the worker.
            if pid:
                _signal_pids([pid], signal.SIGINT)
            _signal_pids(pids, signal.SIGINT)
            self._signalled()

    def __iter__(self):
        return _PopenIterator(self)

    def _iterate(self):
        """The generator behind _PopenIterator. Only the owner thread
        advances it; when it is closed early (a `break`, or the garbage
        collector), the command is killed: by close() in the owner thread,
        by kill() elsewhere."""
        complete = False
        try:
            while True:
                ev = self._next_event()
                if ev is None:
                    complete = True
                    return
                yield ev
        finally:
            if not complete and not self._released:
                if threading.get_ident() == self._owner:
                    self.close()
                else:
                    self.kill()

    def __next__(self):
        ev = self._next_event()
        if ev is None:
            raise StopIteration
        return ev

    def wait(self):
        """Read the rest of the output (kept only with buffer=True) and
        return the retcode."""
        while self._next_event() is not None:
            pass
        return self.retcode

    def close(self):
        """Kill the command if it is still running, drain its output and free
        the worker. Idempotent. Only in the thread that created it: from
        another thread it raises RuntimeError and changes nothing."""
        if self._released and not self._pending:
            return
        self._check_owner()
        self.kill()
        self.wait()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __del__(self):
        try:
            if self._released:
                return
            if threading.get_ident() == self._owner:
                self._abandon()
            else:
                self._orphan()
        except Exception:
            pass

    def _orphan(self):
        """Collected in another thread than its owner, which alone can
        release the worker lock (an RLock); it does so at its next call to
        the instance (Brish._reap_orphans). Meanwhile a helper thread does
        what close() would: it kills the command, with every step, and reads
        the reply to its end. So the command ends, the worker is idle and in
        sync once the owner frees it, and cleanup() does not wait for it
        (see Brish.cleanup), while restart() gives its slot a new lock and
        worker at once (see Brish._swap_now)."""
        if sys.is_finalizing():
            #: Python is ending, and no thread can start any more: on
            #: Python 3.10, Thread.start() then waits for good, so Python
            #: never exited. Nothing is needed: Python's end closes the
            #: bootstrap's stdin, which stops a busy worker with every
            #: process below it.
            return
        self._buffer = None  # nobody reads the result
        self._drained = threading.Event()
        helper = threading.Thread(
            target=self._drain, daemon=True, name="brish-popen-orphan"
        )
        self._brish._orphans.append(self)
        try:
            helper.start()
        except BaseException:
            #: No helper (newer Pythons raise at shutdown, or a thread
            #: limit): the owner's next call abandons it instead (_reap),
            #: and must not wait for a drain that never comes.
            self._drained.set()
            self._brish._orphan_drained(self)
            raise

    def _drain(self):
        try:
            self.kill()
            while self._read_event() is not None:
                pass
        except BaseException:
            pass
        finally:
            self._drained.set()
            #: An owner that has ended will never free the worker: its slot
            #: gets a new lock and a new worker (Brish._orphan_drained). An
            #: owner that ends later is caught by Brish._reap_orphans.
            self._brish._orphan_drained(self)

    def _reap(self):
        """In the owner thread: free the worker of an orphan, once its
        helper thread is done (the kill escalates, so it ends)."""
        drained = getattr(self, "_drained", None)
        if drained is not None:
            drained.wait()
        if self._finished:
            self._release()
        else:
            self._abandon()

    def _orphan_holds(self, locks):
        """The slot whose lock (in `locks`, the instance's slot locks) this
        orphan holds with its worker idle, once its reply has been read to
        its end, if nothing else holds it: the owner thread has ended, or the
        level of the lock that this orphan took is the only one left (else
        the owner holds that lock for its own reasons too, with acquire_lock,
        say). Else None."""
        drained = getattr(self, "_drained", None)
        if (drained is None or not drained.is_set() or not self._finished
                or self._released):
            return None
        i = self.server_index
        if i is None or not 0 <= i < len(locks) or locks[i] is not self._lock:
            return None
        if self._owner_thread.is_alive() and _rlock_levels(self._lock, self._owner) != 1:
            return None
        return i

    @property
    def result(self):
        """With buffer=True: the CmdResult (from_bytes, as send_cmd returns
        it) once the command has ended, else None. Any thread may read it:
        it is built under _mu, which the owner holds while it moves a chunk
        from the pending chunks to the buffer, and the last chunk is pending
        before retcode is set."""
        if self._buffer is None:
            raise ValueError("BrishPopen.result needs popen(..., buffer=True)")
        with self._mu:
            if self.retcode is None:
                return None
            if self._result is None:
                outs, errs = list(self._buffer[0]), list(self._buffer[1])
                for name, chunk in self._pending:
                    (outs if name == "out" else errs).append(bytes(chunk))
                b = self._brish
                self._result = CmdResult.from_bytes(
                    self.retcode, b"".join(outs), b"".join(errs), self.cmd, self.cmd_stdin,
                    encoding=b.encoding, errors=b.decoding_errors,
                )
            return self._result


class _PopenIterator:
    """What iter(BrishPopen) returns. A `next()` from a thread other than
    the owner raises RuntimeError before it reaches the generator, which an
    exception would end, so the owner's iteration goes on. Closing it (or
    dropping it, as a `break` does) closes the generator."""

    __slots__ = ("_popen", "_gen")

    def __init__(self, popen):
        self._popen = popen
        self._gen = popen._iterate()

    def __iter__(self):
        return self

    def __next__(self):
        self._popen._check_owner()
        return next(self._gen)

    def close(self):
        self._gen.close()


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

    #: Whether a dead worker is replaced in the background as soon as its
    #: death is seen (an eager replacement), so that the next call that
    #: takes its slot need not wait for a new worker to start (about as long
    #: as a new Brish takes). The replacement runs boot_cmd when it starts,
    #: not when it is first used, and it costs a process even if the slot is
    #: not used again. With False, or whenever no replacement is ready, the
    #: call that takes the slot starts one itself.
    eager_replacement = True

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
        #: The newest full bootstrap (see init and restart), or None.
        self.p = None
        #: One RLock per slot (worker index). The list is replaced by init
        #: and cleanup; one lock is replaced when its holder has ended (see
        #: _recover_slot).
        self.locks = []
        #: One _Slot per worker index, or None while the instance has no
        #: workers (before init, after cleanup).
        self._slots = None
        #: Every bootstrap not closed yet: the slots' own ones, pending
        #: replacements, and those still starting.
        self._boots = []
        #: Held by init, restart and the end of cleanup, one at a time.
        #: Never waited for under the instance lock; its holder never waits
        #: for a worker lock.
        self._boot_mu = Lock()
        #: cleanup() calls under way. Meanwhile a thread that holds no
        #: worker lock waits (on _settled) before it takes one.
        self._closing = 0
        self._settled = threading.Condition(self.lock)
        #: Finished restarts, and the outcome of the last one: a restart
        #: that had to wait for another returns that one's outcome.
        self._restart_commits = 0
        self._restart_result = (True, [])
        #: Threads that boot an eager replacement (see _warm).
        self._warmers = set()
        #: BrishPopen objects collected unclosed outside their own thread,
        #: which still hold that thread's worker lock (see _reap_orphans).
        self._orphans = collections.deque()
        self._orphans_mu = Lock()  # one _reap_orphans at a time
        #: Bumped by init() and restart().
        self._gen = 0
        #: A failed init leaves the instance without workers; the next use
        #: tries again instead of raising UninitializedBrishException.
        self._init_on_use = False
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
        """Start the workers, stopping any first as cleanup() does. Returns
        the boot command's results, one per worker, or None without one."""
        self._cleanup(keep_boot_mu=True)
        try:
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
            return self._init_locked()
        finally:
            self._boot_mu.release()

    def _init_locked(self):
        """Start a full set of workers for an instance that has none. The
        caller holds _boot_mu. A failure leaves the instance without workers,
        and its next use tries again."""
        n = self.last_server_count
        with self.lock:
            self._gen += 1
        try:
            p = self._spawn(range(n), n, None, False)
            try:
                results = self._run_boot_cmd(p, range(n))
            except BaseException:
                self._close_boot(p)
                raise
        except BaseException:
            self._init_on_use = True
            raise
        with self.lock:
            slots = [_Slot(p) for _ in range(n)]
            for i, s in enumerate(slots):
                s.attn = p.dead[i]  # the boot command ended it
            self.locks = [RLock() for _ in range(n)]
            self._slots = slots
            self.p = p
            self._init_on_use = False
        return results

    # Bootstraps

    def _spawn(self, ids, n, slots, eager):
        """Start a bootstrap whose workers serve the slots `ids` of an
        instance of `n` slots (all of them, or one for a replacement), with
        the instance's shell, mode, encoding and environment, and wait until
        they are up. It is registered in self._boots as soon as it runs, so
        that cleanup() finds it; see _register for when it is refused."""
        ids = list(ids)
        if self.binary:
            return self._spawn_binary(self.lastShell, ids, n, slots, eager)
        return self._spawn_legacy(self.lastShell, ids, n, slots, eager)

    def _register(self, p, slots, eager):
        """Record the new bootstrap `p`. A replacement (`slots` is not None)
        is refused once the instance has other slots (it was cleaned up or
        re-initialized), and an eager one also while a cleanup is under way:
        cleanup() waits for the thread that boots it, and must not wait for a
        new one. Init and restart hold _boot_mu, so no cleanup runs."""
        with self.lock:
            if slots is not None and (self._slots is not slots or (eager and self._closing)):
                raise _Refused("the instance moved on while a worker was starting")
            self._boots.append(p)

    def _new_boot(self, p, ids, n):
        """The bookkeeping of a new bootstrap. Per-worker lists have one
        entry per slot of the instance, so that a worker is found by its slot
        index whichever bootstrap runs it; a bootstrap that serves one slot
        has None (or False) at the others."""
        p.slot_ids = ids
        p.gen = self._gen
        p.server_count = n
        p.free_server_count = len(ids)
        #: The thread (a Thread: idents are reused) whose running BrishPopen
        #: holds each worker, or None.
        p.popen_owner = [None] * n
        #: The worker takes no request again (it died, or its stream is out
        #: of sync): its slot gets a new one at its next fresh acquire.
        p.dead = [False] * n
        #: Out of use for good (see _retire); `refs` counts the others.
        p.retired = [False] * n
        p.refs = len(ids)
        #: Stopped, or claimed by the thread that stops it.
        p.stopped = [False] * n
        #: Binary mode: a retired worker whose pipes a BrishPopen may still
        #: read; that BrishPopen closes them (see _retire_locked).
        p.parked = [False] * n
        p.closed = False
        #: Set by cleanup(): a bootstrap that is still starting gives up.
        p.abort = False
        p.interrupted = False

    def _spawn_legacy(self, shell, ids, n, slots, eager):
        encoding = self.encoding
        decoding_errors = self.decoding_errors
        tmpdir = tempfile.mkdtemp()
        paths = {
            kind: [os.path.join(tmpdir, f"brish_{i}_{kind}") for i in ids]
            for kind in ("stdin", "stdout", "stderr")
        }
        try:
            for kind in paths:
                for path in paths[kind]:
                    os.mkfifo(path)

            #: A session of its own (setsid): no terminal signal (Ctrl-C,
            #: Ctrl-\, Ctrl-Z, the SIGHUP of a closing terminal) and no signal
            #: to our process group reaches the bootstrap, its workers or
            #: their commands, which have no controlling terminal either.
            #: Interrupts come from Brish alone (BrishPopen.kill(), or the one
            #: SIGINT for an abandoned BrishPopen). The workers stop with us
            #: through their pipes (see docs/protocol.org, Processes). The
            #: bootstrap's stdin stays open until it is closed (or our end):
            #: its EOF tells the bootstrap to stop what is left (brish2.zsh).
            p = Popen(
                shell,
                stdin=PIPE,
                stdout=PIPE,
                stderr=PIPE,
                env=_worker_env(ids[0]),
                text=True,
                errors=decoding_errors,  # escape invalid utf-8 bytes
                encoding=encoding,
                start_new_session=True,
            )
        except BaseException:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise
        self._new_boot(p, ids, n)
        p.tmpdir = tmpdir
        p.binary = False

        def spread(values):
            out = [None] * n
            for i, v in zip(ids, values):
                out[i] = v
            return out

        p.brish_stdin_paths = spread(paths["stdin"])
        p.brish_stdout_paths = spread(paths["stdout"])
        p.brish_stderr_paths = spread(paths["stderr"])
        p.brish_stdins = [None] * n
        p.brish_stdouts = [None] * n
        p.brish_stderrs = [None] * n
        p.err_readers = [None] * n
        #: Helper threads of BrishPopen that own a stdout FIFO (see
        #: _LegacyStreamReader), and each worker's PID, learned on demand.
        p.out_readers = [None] * n
        p.legacy_pids = [None] * n
        #: brish2.zsh reports the PIDs on its stdout (see _legacy_read_pids):
        #: None until read, then whether it was.
        p.pid_report = None if os.path.basename(shell[0]) == "brish2.zsh" else False
        p.pid_lock = Lock()
        p.boot_out = b""
        #: Workers whose reply was abandoned (see _legacy_abandon).
        p.legacy_stale = [False] * n
        try:
            self._register(p, slots, eager)
            try:
                print(
                    "\n".join(paths["stdin"])
                    + self.MARKER
                    + "\n".join(paths["stdout"])
                    + self.MARKER
                    + "\n".join(paths["stderr"])
                    + self.MARKER,
                    file=p.stdin,
                    flush=True,
                )
            except BrokenPipeError:
                raise BrishWorkerDiedException(
                    f"the shell exited during startup (status {p.wait()}): {shell[0]!r}"
                )
            #: Open each request FIFO without blocking, so that a shell that
            #: dies before opening its end is noticed instead of hanging.
            #: Requests are encoded before they are written; see _send_legacy.
            for i in ids:
                p.brish_stdins[i] = open(
                    self._legacy_open_request_fifo(p.brish_stdin_paths[i], p, shell), "wb"
                )
            #: Replies are read as bytes and decoded once, in CmdResult.from_bytes.
            for i in ids:
                p.brish_stdouts[i] = open(p.brish_stdout_paths[i], "rb")
            for i in ids:
                p.brish_stderrs[i] = open(p.brish_stderr_paths[i], "rb")
        except BaseException:
            self._close_boot(p)
            raise
        return p

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
                if p.abort:
                    raise _Refused("the instance is being cleaned up")
                time.sleep(0.002)
        os.set_blocking(fd, True)
        return fd

    def _spawn_binary(self, shell, ids, n, slots, eager):
        workers, made, child_fds = [None] * n, [], []
        try:
            for i in ids:
                req_r, req_w = os.pipe()
                out_r, out_w = os.pipe()
                err_r, err_w = os.pipe()
                child_fds += [req_r, out_w, err_w]
                w = _Worker(i, req_w, out_r, err_r)
                workers[i] = w
                made.append(w)
            argv = list(shell) + [BRISH3_FDS_ARG] + [
                f"{child_fds[3 * k]},{child_fds[3 * k + 1]},{child_fds[3 * k + 2]}"
                for k in range(len(ids))
            ]
            #: A session of its own, as in _spawn_legacy.
            p = Popen(
                argv,
                stdin=PIPE,
                stdout=subprocess.DEVNULL,
                stderr=None,  # startup errors stay visible
                pass_fds=child_fds,
                env=_worker_env(ids[0]),
                start_new_session=True,
            )
        except BaseException:
            for w in made:
                w.close()
            for fd in child_fds:
                os.close(fd)
            raise
        for fd in child_fds:
            os.close(fd)

        self._new_boot(p, ids, n)
        p.binary = True
        p.workers = workers
        try:
            self._register(p, slots, eager)
            for w in made:
                os.set_blocking(w.req, False)
                os.set_blocking(w.out, False)
                os.set_blocking(w.err, False)
            self._await_hellos(p, shell)
            for w in made:
                w.sel = selectors.DefaultSelector()
                w.sel.register(w.out, selectors.EVENT_READ)
                w.sel.register(w.err, selectors.EVENT_READ)
        except BaseException:
            self._close_boot(p)
            raise
        return p

    def _await_hellos(self, p, shell):
        """Wait until every worker has said HELLO, and record its PID."""
        deadline = time.monotonic() + self.startup_timeout
        pending = {w.out: w for w in p.workers if w is not None}
        bufs = {fd: b"" for fd in pending}
        sel = selectors.DefaultSelector()
        try:
            for fd in pending:
                sel.register(fd, selectors.EVENT_READ)
            while pending:
                if p.abort:
                    raise _Refused("the instance is being cleaned up")
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

    def _run_boot_cmd(self, p, ids):
        """Run boot_cmd on the workers `ids` of bootstrap `p`, which no slot
        serves yet. Returns the results (None without a boot_cmd). A worker
        that the command ends is marked dead."""
        if self.boot_cmd is None:
            return None
        results = []
        for i in ids:
            res, gone = self._exchange(p, i, self.boot_cmd)
            if gone:
                p.dead[i] = True
            results.append(res)
        return results

    def _exchange(self, p, i, cmd):
        """Run `cmd` (with empty stdin, not forked) on worker i of bootstrap
        `p`, which no slot serves yet, so no lock is needed. Returns
        (CmdResult, gone): `gone` if the worker died or exited."""
        cmd_b = self._to_bytes(cmd, "cmd")
        if self.binary:
            nonce = secrets.token_hex(16).encode()
            frame = b"".join((b"BRISH3 %s %d 0 0\n" % (nonce, len(cmd_b)), cmd_b))
            outcome = self._binary_transact(p, p.workers[i], frame, nonce)
            if outcome is _NEVER_RAN:
                raise BrishWorkerDiedException("a new worker died before it ran the boot command")
            retcode, outb, errb, gone = outcome
            res = CmdResult.from_bytes(
                retcode, outb, errb, cmd, "",
                encoding=self.encoding, errors=self.decoding_errors,
            )
            return res, gone
        if b"\0" in cmd_b:
            return CmdResult(
                9000, "",
                "Illegal input: Input contained the Brish marker (currently the NUL character).",
                cmd, "",
            ), False
        try:
            outcome = self._legacy_transact(p, i, cmd_b + b"\0\0\0\n", cmd, "")
        except BaseException:
            self._legacy_abandon(p, i)
            raise
        if outcome is _NEVER_RAN:
            raise BrishWorkerDiedException("a new worker died before it ran the boot command")
        return outcome

    def _close_boot(self, p):
        """Close bootstrap `p`: stop the workers it still has (see
        _cleanup_binary and _cleanup_legacy), close its stdin and reap it.
        Idempotent."""
        with self.lock:
            if p.closed:
                return
            p.closed = True
            try:
                self._boots.remove(p)
            except ValueError:
                pass
            todo = []
            for i in p.slot_ids:
                self._retire_locked(p, i)
                if not p.stopped[i]:
                    p.stopped[i] = True
                    todo.append(i)
        if p.binary:
            self._cleanup_binary(p, todo, whole=True)
        else:
            self._cleanup_legacy(p, todo, whole=True)

    def _retire(self, items):
        """Take the workers `items`, (bootstrap, slot index) pairs, out of
        use for good: stop each (with every process below it, if it may run
        a command), and close a bootstrap that has no worker in use left.
        The caller makes sure that no thread uses them: it holds the slot's
        lock, or the slot no longer has them."""
        stop, close = {}, []
        with self.lock:
            done = [(p, i) for p, i in items if self._retire_locked(p, i) and not p.closed]
            #: Decided after the whole batch: a bootstrap whose last worker
            #: in use is among `items` is closed whole, which stops all of
            #: its workers at once, and the others' workers are stopped
            #: together, one call per bootstrap.
            for p, i in done:
                if p.refs == 0:
                    if p not in close:
                        close.append(p)
                elif not p.stopped[i]:
                    p.stopped[i] = True
                    stop.setdefault(id(p), (p, []))[1].append(i)
        for p, todo in stop.values():
            if p.binary:
                self._cleanup_binary(p, todo, whole=False)
            else:
                self._cleanup_legacy(p, todo, whole=False)
        for p in close:
            self._close_boot(p)

    def _retire_locked(self, p, i):
        """Under the instance lock: mark worker i of `p` retired. Returns
        whether it was not already."""
        if p.retired[i]:
            return False
        p.retired[i] = True
        p.dead[i] = True
        p.refs -= 1
        if p.binary and p.popen_owner[i] is not None and not self._drained_on(p, i):
            #: A BrishPopen still holds the worker (its owner thread ended
            #: without freeing it), and its helper thread reads the pipes, or
            #: will once the object is collected (see BrishPopen._orphan):
            #: closing them could hand their numbers to new pipes under that
            #: reader. It closes them itself (see _orphan_drained).
            p.parked[i] = True
        return True

    def _drained_on(self, p, i):
        """Whether an orphan holds worker i of `p` and its helper thread is
        done reading it."""
        for popen in self._orphan_list():
            if getattr(popen, "_p", None) is p and popen.server_index == i:
                drained = getattr(popen, "_drained", None)
                return drained is not None and drained.is_set()
        return False

    # Slots

    def _worker_died(self, p, i):
        """Worker i of bootstrap `p` takes no request again. If it serves its
        slot, the slot gets a new worker: at the slot's next fresh acquire,
        or before it in the background (see eager_replacement)."""
        with self.lock:
            self._dead_locked(p, i)

    def _dead_locked(self, p, i):
        p.dead[i] = True
        slots = self._slots
        if slots is None or not 0 <= i < len(slots):
            return
        s = slots[i]
        if s.p is not p:
            return
        s.attn = True
        if (self.eager_replacement and s.pending is None and s.warming is None
                and not self._closing and not sys.is_finalizing()):
            warm = _Warm()
            t = threading.Thread(
                target=self._warm, args=(i, slots, warm), daemon=True, name="brish-replace"
            )
            s.warming = warm
            self._warmers.add(t)
            try:
                t.start()
            except BaseException as e:
                #: No thread (a thread limit, or an interrupt): the next
                #: fresh acquire replaces the worker itself.
                s.warming = None
                self._warmers.discard(t)
                warm.done.set()
                if not isinstance(e, Exception):
                    raise

    def _warm(self, i, slots, warm):
        """The thread of an eager replacement: boot a worker for slot i, and
        park it as the slot's pending replacement if the slot still needs
        one; else stop it. A failure is left to the next fresh acquire, which
        tries again and reports it."""
        p = None
        try:
            try:
                p = self._spawn([i], len(slots), slots, True)
                self._run_boot_cmd(p, [i])
            except BaseException:
                if p is not None:
                    self._close_boot(p)
                p = None
            with self.lock:
                s = slots[i]
                if (p is not None and not p.dead[i] and self._slots is slots
                        and not self._closing and s.pending is None and s.p.dead[i]):
                    s.pending = p
                    s.attn = True
                    p = None
            if p is not None:
                self._close_boot(p)
        finally:
            with self.lock:
                if slots[i].warming is warm:
                    slots[i].warming = None
                self._warmers.discard(threading.current_thread())
            warm.done.set()

    def _service(self, i, slots, s, cancelled=None):
        """At a fresh acquire of slot i, whose lock this thread now holds:
        swap in the pending replacement, or replace a dead worker, waiting
        for an eager replacement under way or booting one. Boots at most one
        worker, and raises BrishWorkerDiedException if that one is dead too
        (its boot command ended it); the slot is then tried again at its
        next fresh acquire."""
        booted = False
        while True:
            retire = None
            warm = None
            with self.lock:
                if self._slots is not slots or not s.attn:
                    return
                if s.pending is not None:
                    retire = self._swap_locked(i, s)
                elif s.warming is not None:
                    warm = s.warming
                elif not s.p.dead[i]:
                    s.attn = False
                    return
                elif booted:
                    raise BrishWorkerDiedException(
                        f"worker {i} was replaced, and the new worker died before it took "
                        "a command (did the boot command end it?)"
                    )
            if retire is not None:
                self._retire(retire)
                continue
            if warm is not None:
                if cancelled is None:
                    warm.done.wait()
                else:
                    #: The eager replacement goes on, and stays pending for
                    #: the slot's next user.
                    while not warm.done.wait(_CANCEL_POLL):
                        _check_cancelled(cancelled)
                continue
            p = self._spawn([i], len(slots), slots, False)
            try:
                self._run_boot_cmd(p, [i])
            except BaseException:
                self._close_boot(p)
                raise
            booted = True
            with self.lock:
                if self._slots is slots and s.pending is None:
                    s.pending = p
                    p = None
            if p is not None:
                self._close_boot(p)

    def _swap_locked(self, i, s):
        """Under the instance lock: put slot i's pending replacement in.
        Returns the worker to retire."""
        old = s.p
        s.p = s.pending
        s.pending = None
        s.attn = s.p.dead[i]
        return [(old, i)]

    def _swap_now(self, i, retire):
        """Put slot i's pending replacement in now, if no thread uses the
        slot: its lock is free, or held only by an ended thread or a drained
        orphan (see _recover_slot). Never waits. Returns whether it did.
        The worker it took out of a free slot is appended to `retire`, for
        the caller to retire with the others, so that a restart stops each
        old bootstrap in one go (see _retire)."""
        slots, locks = self._slots, self.locks
        if slots is None or not 0 <= i < len(locks):
            return True
        lock = locks[i]
        if lock._is_owned():
            if slots[i].owner is threading.current_thread():
                return False  # this thread's: its worker stays until the release
            return self._recover_slot(i, lock)  # an ended thread's, by its ident
        if lock.acquire(blocking=False):
            try:
                with self.lock:
                    s = slots[i]
                    if (self._slots is slots and self.locks is locks and locks[i] is lock
                            and s.pending is not None):
                        retire.extend(self._swap_locked(i, s))
            finally:
                lock.release()
            return True
        return self._recover_slot(i, lock)

    def _owner_ended(self, i, lock):
        """Whether slot i's lock `lock` is held by the thread of its last
        fresh acquire, which has ended: nobody will ever release it."""
        slots = self._slots
        if slots is None or not 0 <= i < len(slots):
            return False
        t = slots[i].owner
        return t is not None and not t.is_alive() and bool(_rlock_levels(lock, t.ident))

    def _recover_slot(self, i, lock):
        """Slot i's lock `lock` is held by a thread that has ended, or only
        by a drained orphan (see BrishPopen._orphan_holds): nobody will
        release it soon. Give the slot a new lock, and a new worker: the
        pending replacement, or one made at the next fresh acquire (or
        eagerly). Rechecked under the instance lock, which a thread that has
        just taken `lock` needs to keep it. Returns whether it did."""
        with self.lock:
            slots = self._slots
            if (slots is None or self._closing or i is None or not 0 <= i < len(slots)
                    or self.locks[i] is not lock):
                return False
            s = slots[i]
            t = s.owner
            ended = t is not None and not t.is_alive() and bool(_rlock_levels(lock, t.ident))
            if not ended and i not in self._orphan_held():
                return False
            self.locks[i] = RLock()
            s.owner = None
            if s.pending is not None:
                retire = self._swap_locked(i, s)
            else:
                retire = [(s.p, i)]
                self._dead_locked(s.p, i)
        self._retire(retire)
        return True

    def restart(self):
        """Replace every worker, without waiting for one in use.

        A new set of workers is started (each runs boot_cmd), outside the
        instance lock; then each slot whose lock is free gets its new worker
        at once, and its old one is stopped. A slot that a thread holds (with
        acquire_lock, or a running BrishPopen; the caller may be that
        thread) keeps its old worker for that thread, and gets the new one at
        its next fresh acquire after the release. Returns once the new
        workers are up and the free slots have them: True if every slot has
        its new worker, False if some keep the old one until their lock is
        released. Concurrent calls share one restart."""
        return self._restart()[0]

    def _restart(self):
        """restart(): (done, the slots that wait for a release)."""
        self._reap_orphans()
        seen = self._restart_commits
        with self._boot_mu:
            if self._slots is None:
                self.delayed_init = False
                self._init_locked()
                return True, []
            if self._restart_commits != seen:
                #: Another restart finished while this one waited for it: it
                #: started its workers after this call began waiting.
                return self._restart_result
            n = len(self._slots)
            p = self._spawn(range(n), n, None, False)
            try:
                self._run_boot_cmd(p, range(n))
            except BaseException:
                self._close_boot(p)
                raise
            retire = []
            with self.lock:
                slots = self._slots
                self._gen += 1
                p.gen = self._gen
                self.p = p
                for i, s in enumerate(slots):
                    if s.pending is not None:
                        retire.append((s.pending, i))  # an unused older one
                    s.pending = p
                    s.attn = True
            try:
                busy = [i for i in range(n) if not self._swap_now(i, retire)]
            finally:
                self._retire(retire)
            result = (not busy, busy)
            with self.lock:
                self._restart_commits += 1
                self._restart_result = result
            return result

    def _orphan_list(self):
        """A snapshot of self._orphans. BrishPopen._orphan appends to it
        without a lock (it runs in __del__, possibly inside this very
        thread's own critical section)."""
        while True:
            try:
                return list(self._orphans)
            except RuntimeError:  # appended to while it was copied
                continue

    def _reap_orphans(self):
        """Free the workers of this thread's BrishPopen objects that were
        collected unclosed in another thread: they still hold this thread's
        worker locks, which no other thread can release (see
        BrishPopen._orphan). Called at the start of every call that takes a
        worker; it waits for an orphan's helper thread to finish.

        It also looks after the drained orphans of owner threads that have
        ended, whose locks nobody will ever release (see _orphan_drained),
        and then forgets them."""
        if not self._orphans:
            return
        me = threading.current_thread()
        mine, ended = [], []
        with self._orphans_mu:
            for popen in self._orphan_list():
                if popen._owner_thread is me:
                    mine.append(popen)
                elif (popen._drained.is_set() and not popen._owner_thread.is_alive()
                        and not popen._released):
                    ended.append(popen)
            for popen in mine:
                self._orphans.remove(popen)
        for popen in ended:
            #: Still listed meanwhile, so that its worker counts as drained.
            self._orphan_drained(popen)
        if ended:
            with self._orphans_mu:
                for popen in ended:
                    try:
                        self._orphans.remove(popen)
                    except ValueError:
                        pass
        for popen in mine:
            popen._reap()

    def _orphan_drained(self, popen):
        """An orphan's helper thread is done reading (see BrishPopen._drain).
        If the owner thread has ended, nobody will release the worker lock:
        close the pipes that were left to the orphan (see _retire_locked), or
        give the slot a new lock and worker (see _recover_slot)."""
        if popen._owner_thread.is_alive():
            return
        p, i = getattr(popen, "_p", None), popen.server_index
        if p is None or i is None:
            return
        with self.lock:
            parked = p.parked[i]
            p.parked[i] = False
        if parked:
            p.workers[i].close()
            return
        self._recover_slot(i, popen._lock)

    def _orphan_held(self):
        """The slots whose locks drained orphans hold (see
        BrishPopen._orphan_holds): cleanup() does not wait for them, and
        restart() gives them a new lock and worker at once."""
        locks = self.locks
        held = set()
        for popen in self._orphan_list():
            i = popen._orphan_holds(locks)
            if i is not None:
                held.add(i)
        return held

    def _holds_worker_lock(self):
        """Whether this thread holds a worker lock of the instance (not
        merely the ident of a thread that ended holding one)."""
        slots = self._slots
        me = None
        for i, lock in enumerate(self.locks):
            if lock._is_owned():
                if me is None:
                    me = threading.current_thread()
                if slots is None or slots[i].owner is me:
                    return True
        return False

    def _never_ran(self, p, index, lock):
        """Worker `index` of bootstrap `p` could not run a command, and the
        caller has released the level of `lock` it took. Its slot gets a new
        worker at the next fresh acquire, so the caller tries again; unless
        this thread still holds the lock (acquire_lock): then the worker
        cannot be replaced until the release, and the call fails fast."""
        self._worker_died(p, index)
        if lock._is_owned():
            raise BrishWorkerDiedException(
                f"worker {index} cannot take a command (it died, or a request or reply "
                "on it was abandoned), and this thread holds a worker lock on it "
                "(acquire_lock), so it cannot be replaced now; release the lock, and "
                "the next call that takes the worker replaces it"
            )

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

    def acquire_lock(self, server_index=None, lock_sleep=1, cancelled=None):
        """Take a worker's lock, and return (lock, server_index); release
        it with lock.release(). `cancelled` works as for popen: called while
        the call waits, and once more when it has the worker; when it
        returns true, the lock is released again and
        BrishCancelledException is raised."""
        lock, server_index, _ = self._acquire(server_index, lock_sleep, cancelled)
        if cancelled is not None:
            try:
                _check_cancelled(cancelled)
            except BaseException:
                lock.release()
                raise
        return lock, server_index

    def _acquire(self, server_index=None, lock_sleep=1, cancelled=None):
        """Lock one slot. Returns (lock, server_index, p): worker
        `server_index` of bootstrap `p` serves the slot.

        `cancelled` (a callable, or None) is called every _CANCEL_POLL
        seconds while the call waits (for a slot's lock, for a cleanup to
        finish, for an eager replacement to start); when it returns true,
        the call raises BrishCancelledException, without the lock.

        A fresh acquire (this thread did not hold the slot's lock) first
        swaps in the slot's pending replacement, or replaces its dead worker
        (see _service), so a thread never sees its slot's worker change
        while it holds the lock. The instance lock is taken briefly, after
        the slot's lock, to check that the lock is still the slot's and to
        record the thread (see _owner_ended); nothing waits under it.
        """
        self._reap_orphans()
        while True:
            slots, locks = self._slots, self.locks
            if slots is None or (self._closing and not self._holds_worker_lock()):
                self._wait_usable(cancelled)
                continue
            me = threading.current_thread()
            if server_index is None:
                got = self._pick(slots, locks, me, lock_sleep, cancelled)
                if got is None:
                    continue
                index, lock, fresh = got
            else:
                n = len(locks)
                index = server_index
                if -n <= index < 0:
                    index += n
                if not 0 <= index < n:
                    ic(n, server_index)
                    time.sleep(1)
                    continue
                if slots[index].p.popen_owner[index] is me:
                    raise BrishWorkerBusyException(
                        f"worker {index} is streaming a BrishPopen of this thread; read it "
                        "to its end or close it first, or use server_index=None"
                    )
                lock = locks[index]
                fresh = not lock._is_owned()
                if fresh:
                    if not self._wait_slot_lock(index, lock, locks, cancelled):
                        continue
                else:
                    lock.acquire()

            s = slots[index]
            if not fresh:
                if s.owner is not me:
                    #: The RLock knows its owner by ident only, and this
                    #: thread has the ident of a thread that ended holding
                    #: it: the slot gets a new lock (see _recover_slot).
                    lock.release()
                    if self._recover_slot(index, lock):
                        continue
                    lock.acquire()
                #: This thread already holds the slot: it keeps its worker.
                return lock, index, s.p
            with self.lock:
                ok = self.locks is locks and locks[index] is lock
                if ok:
                    s.owner = me
                    attn = s.attn
            if not ok:
                #: Cleaned up, or the lock was replaced (see _recover_slot).
                lock.release()
                continue
            if attn:
                try:
                    self._service(index, slots, s, cancelled)
                except BaseException:
                    lock.release()
                    raise
            return lock, index, s.p

    def _wait_usable(self, cancelled=None):
        """The slow path of _acquire: the instance has no workers, or a
        cleanup is under way. A thread that holds no worker lock waits for
        the cleanup to finish (one that holds one goes on: the cleanup waits
        for it). Starts the workers of a delayed or failed init."""
        holder = self._holds_worker_lock()
        while True:
            with self.lock:
                if not (self._closing and not holder):
                    if self._slots is not None:
                        return
                    break
                self._settled.wait(None if cancelled is None else _CANCEL_POLL)
            #: Outside the instance lock: the caller's callable may block,
            #: and nothing may wait while holding that lock.
            if cancelled is not None:
                _check_cancelled(cancelled)
        with self._boot_mu:
            if self._slots is not None:
                return
            if not (self.delayed_init or self._init_on_use):
                raise UninitializedBrishException(
                    "acquire_lock called with an uninitialized Brish"
                )
            self.delayed_init = False
            self._init_locked()

    def _pick(self, slots, locks, me, lock_sleep, cancelled=None):
        """server_index=None: take a free slot, a healthy one first, then one
        with a pending replacement (a swap), a stale one (binary mode: its
        next request waits for an abandoned command) and last a dead one (a
        boot). Returns (index, lock, fresh), or None to start over (after
        sleeping lock_sleep seconds while every slot is taken)."""
        n = len(locks)
        binary = self.binary
        later = None
        for i in range(n):
            s = slots[i]
            p = s.p
            if p.popen_owner[i] is me:
                #: Its lock would let this thread in (an RLock), but its
                #: command is still streaming.
                continue
            if s.attn:
                rank = 1 if s.pending is not None else 3
            elif binary and p.workers[i].stale:
                rank = 2
            else:
                lock = locks[i]
                fresh = not lock._is_owned()
                # https://docs.python.org/3/library/threading.html#threading.Lock.acquire
                if lock.acquire(blocking=False):
                    return i, lock, fresh
                continue
            if later is None:
                later = []
            later.append((rank, i))
        if later is not None:
            later.sort()
            for _, i in later:
                lock = locks[i]
                fresh = not lock._is_owned()
                if lock.acquire(blocking=False):
                    return i, lock, fresh

        mine = [i for i in range(n) if slots[i].p.popen_owner[i] is me]
        if len(mine) == n:
            raise BrishWorkerBusyException(
                "every worker is streaming a BrishPopen of this thread; "
                "read one to its end or close it first"
            )
        if mine:
            #: Waiting here would hold the streaming worker while waiting
            #: for another one: two threads that do this on each other's
            #: workers would wait forever.
            raise BrishWorkerBusyException(
                f"worker {mine[0]} is streaming a BrishPopen of this thread, "
                "and every other worker is taken; this thread cannot wait for "
                "one, since another thread that streams and waits the same way "
                "would deadlock with it: make the call after the BrishPopen "
                "has been read to its end or closed"
            )
        for i in range(n):
            if self._owner_ended(i, locks[i]) and self._recover_slot(i, locks[i]):
                return None
        if lock_sleep is not None:
            if cancelled is None:
                time.sleep(lock_sleep)
            else:
                deadline = time.monotonic() + lock_sleep
                while True:
                    _check_cancelled(cancelled)
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    time.sleep(min(left, _CANCEL_POLL))
            return None
        i = random.randrange(n)
        lock = locks[i]
        if lock._is_owned():
            lock.acquire()
            return i, lock, False
        if not self._wait_slot_lock(i, lock, locks, cancelled):
            return None
        return i, lock, True

    def _wait_slot_lock(self, i, lock, locks, cancelled=None):
        """Take slot i's lock `lock`, which this thread does not hold.
        Returns False, without it, if it stops being the slot's lock: the
        instance was cleaned up, or the lock was replaced because its holder
        has ended (see _recover_slot)."""
        poll = _POLL if cancelled is None else _CANCEL_POLL
        while not lock.acquire(timeout=poll):
            if cancelled is not None:
                _check_cancelled(cancelled)
            if self.locks is not locks or locks[i] is not lock:
                return False
            if self._owner_ended(i, lock):
                self._recover_slot(i, lock)
                return False
        return True

    def send_cmd(
        self, cmd, cmd_stdin="", fork=False, server_index=None, lock_sleep=1,
        cancelled=None,
    ):
        """Run `cmd` in a worker and return its CmdResult.

        `cmd` and `cmd_stdin` may be bytes-like; `str` is encoded with the
        instance encoding and surrogateescape. `cmd_stdin=None` means
        /dev/null in binary mode, and empty stdin in legacy mode, where a
        NUL in `cmd` or `cmd_stdin` gives the retcode 9000 instead.

        `cancelled`, a callable without arguments, is called while the call
        waits for a worker (every 0.05 s) and once just before the command
        would be sent; when it returns true, the call raises
        BrishCancelledException and nothing runs (see popen).
        """
        restart_cmd = cmd
        if isinstance(cmd, _BYTES_LIKE):
            restart_cmd = bytes(cmd).decode("utf-8", "surrogateescape")
        if restart_cmd == "%BRISH_RESTART":
            #: Handled before any worker lock is taken: it restarts every
            #: worker (see restart), and runs on none.
            done, busy = self._restart()
            if done:
                msg = "Restarted succesfully."
            else:
                msg = (
                    "Restarted; in use, and replaced when their locks are released: "
                    + ", ".join(f"worker {i}" for i in busy) + "."
                )
            return CmdResult(0, msg, "", cmd, self._stored_stdin(cmd_stdin))
        if self.binary:
            return self._send_binary(cmd, cmd_stdin, fork, server_index, lock_sleep, cancelled)
        return self._send_legacy(cmd, cmd_stdin, fork, server_index, lock_sleep, cancelled)

    def popen(
        self, cmd, cmd_stdin="", fork=False, server_index=None, lock_sleep=1, buffer=False,
        cancelled=None,
    ):
        """Run `cmd` in a worker and stream its output while it runs.

        Returns a BrishPopen: in binary mode once the worker has read the
        whole request and is starting the command, in legacy mode once the
        request is written (the first popen after a legacy start reads the
        workers' PIDs from the bootstrap, or asks a custom shell's worker).
        Iterating it yields (stream, chunk) pairs: `stream` is "out" or
        "err", `chunk` is non-empty bytes, as read. Only the bytes that
        could start the end of the reply are held back until the next read:
        in binary mode a suffix that starts at a NUL, in legacy mode a
        trailing newline (or newline and NUL). A caller that stops reading
        blocks the command once Brish and the pipes hold what it wrote
        (binary: one read of up to 64 KiB per stream; legacy: a queue of 64
        KiB for both streams); one that falls behind gets fewer, larger
        chunks (up to 64 KiB). `retcode` is None until the command has
        ended.

        The arguments are those of send_cmd. With buffer=True, the chunks
        that iteration or wait() consumed are also kept, and `result` gives
        the CmdResult that send_cmd would have returned.

        `cancelled` is a callable without arguments (or None), for a caller
        whose client may go away while the call waits for a worker: for a
        free one, for a slot's lock, or for a dead worker's replacement to
        start. Brish calls it every 0.05 s while it waits for a lock or an
        eager replacement (also within lock_sleep), and once more after it
        holds the worker and any replacement of it is done, just before it
        writes the request. When it returns true, popen frees the worker (a
        replacement it started stays in the slot) and raises
        BrishCancelledException; nothing ran. An exception that the callable
        raises propagates the same way, with nothing run. A replacement that
        the call starts itself (eager_replacement off) is not interrupted:
        the check after it decides. In binary mode a stale worker (see
        below) takes the request at once and runs it when its abandoned
        command ends; `cancelled` is not called after the request is sent.

        The worker's lock is held from the call until the reply has been read,
        so the object is read, waited for and closed in the thread that made
        it (from another thread, these raise RuntimeError and change nothing);
        a thread that holds the lock (acquire_lock) can pass its
        `server_index`. Calls from the same thread meanwhile skip that busy
        worker: they raise BrishWorkerBusyException when they name it, or,
        with server_index=None, when no other worker is free at once. A call
        that names another worker waits for its lock as usual (two streaming
        threads that name each other's workers deadlock), and in binary mode a
        free but stale worker makes any call wait for its abandoned command.
        Use it as a context manager: leaving the block early, by break or by
        an exception, kills the command, drains its output and frees the
        worker. An unclosed object collected in another thread is killed with
        every step and drained in a helper thread, and its worker is freed at
        the creating thread's next call to this instance. Once it is drained,
        cleanup() does not wait for it and restart() replaces its worker at
        once, unless that thread holds the worker's lock for its own reasons
        too; if that thread has ended, the slot gets a new lock and a new
        worker.

        `kill()` (alias `terminate()`) is the way to interrupt the command:
        workers and their commands run in a session of their own, which no
        terminal signal (Ctrl-C, Ctrl-Z, the SIGHUP of a closing terminal) and
        no signal to the caller's process group reaches. A KeyboardInterrupt
        interrupts Python alone, and the command runs on, unless the exception
        comes inside a read of the object, which abandons it and sends the
        command one SIGINT, or leaves a `with` block that holds the object,
        which closes it. kill() works from any thread, is idempotent, and
        interrupts the command, not the worker. Step 1: SIGINT to the worker,
        then its descendants (the worker aborts the command as Ctrl-C does in
        an interactive shell, and its retcode is 130 unless the command traps
        INT). Step 2: SIGINT to the worker again, then SIGTERM to the
        descendants. Step 3: SIGKILL to the descendants, or step 4 at once if
        there are none (for a fork command, whose subshell has then exited, a
        grace later and only if its end has not shown up). Step 4: SIGKILL to
        the worker, which gives the retcode 9001 with WORKER_DIED_NOTE as the
        last chunk, and has the worker replaced (its slot alone); before it,
        Brish reads up to 128 KiB more of what the pipes hold, looking for the
        end of the reply. The steps stop once the command has ended; each
        comes `kill_grace` seconds (default 2) after the previous step's
        signals went out when the command has gone quiet, or two graces after
        them while its output keeps coming. After the first signal Brish reads
        up to 256 KiB ahead of the caller, so a command that ends at the
        signal and writes less than that meanwhile is not escalated, however
        slowly the caller reads. Background jobs of earlier commands are
        descendants of the worker too, and are stopped by steps 2 to 4.
        Step 1 goes out from a helper thread after one `ps` run; until then
        a read after kill() waits for it (at most `kill_grace` seconds) and
        reads nothing, so a command that a full pipe holds up does not run
        on meanwhile. `signalled` tells whether it is out, and
        `wait_signalled()` waits for it.
        """
        return BrishPopen(
            self, cmd, cmd_stdin=cmd_stdin, fork=fork, server_index=server_index,
            lock_sleep=lock_sleep, buffer=buffer, cancelled=cancelled,
        )

    def zpopen(self, template, locals_=None, getframe=2, **kwargs):
        """popen() of `zstring(template)`, as z() is send_cmd() of it."""
        return self.popen(
            self.zstring(template, locals_=locals_, getframe=getframe), **kwargs
        )

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

    def _send_binary(self, cmd, cmd_stdin, fork, server_index, lock_sleep, cancelled=None):
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
            lock, index, p = self._acquire(server_index, lock_sleep, cancelled)
            if cancelled is not None:
                try:
                    _check_cancelled(cancelled)
                except BaseException:
                    lock.release()
                    raise
            try:
                p.free_server_count -= 1
                outcome = self._binary_transact(p, p.workers[index], frame, nonce)
            finally:
                p.free_server_count += 1
                lock.release()

            if outcome is _NEVER_RAN:
                self._never_ran(p, index, lock)
                continue
            retcode, outb, errb, restart = outcome
            if restart:
                self._worker_died(p, index)
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

        Returns _NEVER_RAN if the worker died before START or is broken, or
        (retcode, outb, errb, restart). The frame is written non-blocking
        inside the loop that drains both response pipes, so neither side can
        block the other.
        """
        if w.broken or p.dead[w.index]:
            return _NEVER_RAN
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
                #: only a new worker recovers from that. Until then nothing
                #: may reach it, also not from a thread that holds its lock.
                w.broken = True
                self._worker_died(p, w.index)
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
            so.flush()
            se.flush()
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

    def _send_legacy(self, cmd, cmd_stdin, fork, server_index, lock_sleep, cancelled=None):
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
            lock, index, p = self._acquire(server_index, lock_sleep, cancelled)
            if cancelled is not None:
                try:
                    _check_cancelled(cancelled)
                except BaseException:
                    lock.release()
                    raise
            outcome = None
            try:
                p.free_server_count -= 1
                outcome = self._legacy_transact(p, index, frame, cmd, stored_stdin)
            except BaseException:
                #: An interrupt leaves a half-written request or a half-read
                #: reply, and possibly a helper thread that would consume the
                #: next reply's stderr. The worker is replaced.
                self._legacy_abandon(p, index)
                raise
            finally:
                p.free_server_count += 1
                lock.release()

            if outcome is _NEVER_RAN:
                self._never_ran(p, index, lock)
                continue
            result, restart = outcome
            if restart:
                self._worker_died(p, index)
            return result

        raise BrishWorkerDiedException(
            "a worker died before running the command, twice"
        )

    def _legacy_abandon(self, p, index):
        """A request to legacy worker `index` was cut short, or its reply was
        left half-read. The frozen wire has no marker to resynchronise on, so
        no request may reach that worker again (even from a thread that holds
        its lock: it gets BrishWorkerDiedException), and its slot gets a new
        worker."""
        p.interrupted = True
        p.legacy_stale[index] = True
        self._worker_died(p, index)

    def _legacy_transact(self, p, index, frame, cmd, cmd_stdin):
        """One request/reply exchange with legacy worker `index`.

        Returns _NEVER_RAN if the request could not be written, or
        (CmdResult, restart). See docs/protocol.org, "Legacy mode".
        """
        if p.dead[index] or _legacy_busy(p, index):
            #: The worker takes no request (an abandoned reply may still have
            #: a reader on its FIFOs). Its slot gets a new worker, but this
            #: thread holds its lock and so got here first.
            return _NEVER_RAN
        try:
            f = p.brish_stdins[index]
            f.write(frame)
            f.flush()
        except BrokenPipeError:
            #: The worker is gone; it cannot have read the whole request.
            return _NEVER_RAN

        #: Read stderr concurrently: a command that fills the stderr FIFO
        #: before finishing its stdout would otherwise deadlock.
        err_reader = _ErrReader(p.brish_stderrs[index])
        p.err_readers[index] = err_reader
        outb, died, rc_line = _legacy_read_reply(p.brish_stdouts[index], with_rc=True)
        return_code = None
        exited = False
        if not died:
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
            #: The caller has the worker replaced.
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
        """Stop every worker and close every bootstrap of the instance: the
        slots' workers, pending replacements, and those still starting
        (cleanup waits for them to start, then stops them). Waits for each
        worker lock that a live thread holds, as a command in progress ends
        first; not for one whose thread has ended, nor for one that only a
        drained orphan holds (see BrishPopen._orphan)."""
        self._cleanup(keep_boot_mu=False)

    def _cleanup(self, keep_boot_mu):
        """cleanup(); with `keep_boot_mu`, return holding _boot_mu, so that
        init() starts the new workers before any other init or restart."""
        with self.lock:
            self._closing += 1
        taken = []
        boot_mu = False
        try:
            locks = self.locks

            def skip(i):
                return self._owner_ended(i, locks[i]) or i in self._orphan_held()

            taken = _acquire_all(locks, skip)
            self._boot_mu.acquire()
            boot_mu = True
            self._teardown()
        except BaseException:
            if boot_mu:
                self._boot_mu.release()
                boot_mu = False
            raise
        finally:
            for lock in reversed(taken):
                lock.release()
            with self.lock:
                self._closing -= 1
                self._settled.notify_all()
            if boot_mu and not keep_boot_mu:
                self._boot_mu.release()

    def _teardown(self):
        """Under _boot_mu, with every slot lock taken (or skipped): close
        every bootstrap. Eager replacements still starting are told to give
        up, and waited for: they close their own bootstraps."""
        with self.lock:
            self._slots = None
            self.p = None
            self.locks = []
            warmers = list(self._warmers)
            for p in self._boots:
                p.abort = True
        for t in warmers:
            t.join()
        with self.lock:
            boots = list(self._boots)
            for p in boots:
                for i in p.slot_ids:
                    self._retire_locked(p, i)
        for p in boots:
            self._close_boot(p)

    @staticmethod
    def _cleanup_binary(p, todo, whole):
        """Stop the binary workers `todo` of bootstrap `p`; with `whole`, it
        is closing, and these are all that it has left. Stop the workers
        first, so this never waits for a user command. A worker that may
        still run one (its reply was abandoned, it is broken, or a
        BrishPopen holds it) is stopped with every process below it, so that
        no command outlives it; an idle worker just exits. A worker's PID
        is signalled only while it is in the bootstrap's process group (a
        dead worker's PID may be another process's by now). The pipes of a
        parked worker are left to its BrishPopen (see _retire_locked)."""
        ws = [p.workers[i] for i in todo if p.workers[i] is not None]
        busy, idle = [], []
        for w in ws:
            if not w.pid or not _in_group(w.pid, p.pid):
                continue
            if w.stale or w.broken or p.popen_owner[w.index] is not None or p.parked[w.index]:
                busy.append(w.pid)
            else:
                idle.append(w.pid)
        pids = idle + _trees(busy)
        _signal_pids(pids, signal.SIGTERM)
        for w in ws:
            if not p.parked[w.index]:
                w.close()
        if whole and p.stdin is not None:
            try:
                p.stdin.close()  # the bootstrap exits when its stdin closes
            except Exception:
                pass
        deadline = time.time() + 1
        while time.time() < deadline and any(_alive(pid) for pid in pids):
            time.sleep(0.005)
        _signal_pids([pid for pid in pids if _alive(pid)], signal.SIGKILL)
        if whole:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

    @staticmethod
    def _cleanup_legacy(p, todo, whole):
        """Stop the legacy workers `todo` of bootstrap `p` (see
        _cleanup_binary). A worker that may still run a command (its reply
        was abandoned, a helper thread still reads it, or a BrishPopen holds
        it) is stopped first, with every process below it, so that no
        command outlives it. One that may hold a truncated request must not
        run it once its FIFO closes either. Without its PID, the bootstrap
        stops it when it closes: every worker it has left, when Brish closes
        it, or itself, a second after its stdin closes."""
        def close(f):
            if f is None:
                return
            try:
                f.close()
            except Exception:
                pass

        busy = [i for i in todo if _legacy_busy(p, i) or p.popen_owner[i] is not None]
        if busy:
            _legacy_read_pids(p, 1.0)
            roots = [p.legacy_pids[i] for i in busy]
            if whole and None in roots:
                roots = _child_pids(p.pid)
            else:
                if None in roots:
                    p.interrupted = True
                roots = [r for r in roots if r is not None and _in_group(r, p.pid)]
            _stop_pids(_trees(roots))
        elif whole and p.interrupted:
            _stop_pids(_child_pids(p.pid))

        if whole:
            for f in (p.stdout, p.stderr, p.stdin):
                close(f)
        for i in todo:
            close(p.brish_stdins[i])
        for i in todo:
            for readers, files in ((p.out_readers, p.brish_stdouts), (p.err_readers, p.brish_stderrs)):
                reader = readers[i]
                if reader is not None:
                    reader.close_or_hand_over()
                else:
                    close(files[i])
        if not whole:
            return
        shutil.rmtree(p.tmpdir, ignore_errors=True)
        #: Workers exit once their request FIFO closes, and the bootstrap
        #: once its stdin has, stopping any worker that is still busy a
        #: second later (see brish2.zsh). Do not wait for it for long.
        try:
            p.wait(timeout=4)
        except subprocess.TimeoutExpired:
            _stop_pids(_trees(_child_pids(p.pid)))
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
            if typ is ast.Constant:
                #: Python 3.14 removed `ast.Str` and `Constant.s`.
                result.append(part.value)
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
