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
    bytes the command wrote when the result came from `from_bytes` (as in
    binary mode), and otherwise the text views encoded back as UTF-8.
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
    """Reads one legacy stderr reply in a helper thread.

    If the reply never completes (an interrupt, or a dead worker whose stderr
    FIFO is still held open by a background job), the thread is left running
    and owns the file: cleanup() does not close a file that a live helper is
    blocked on, because closing a buffered file waits for its lock.
    """

    def __init__(self, f, delim):
        self.f = f
        self.delim = delim
        self.text = ""
        self.eof = False
        self.exc = None
        self.done = False
        self.close_when_done = False
        self._lock = Lock()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        try:
            self.text, self.eof = _legacy_read_reply(self.f, self.delim)
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


def _legacy_read_reply(f, delim):
    """Read lines until `delim`. Returns (text, eof)."""
    lines = []
    while True:
        line = f.readline()
        if line == delim:
            return "".join(lines)[:-1], False
        if line == "":
            return "".join(lines), True
        lines.append(line)


class Brish:
    """Brish is a bridge between Python and an interpreter. The interpreter needs to adhere to the Brish protocol. A zsh interpreter is provided, and is the default. Threadsafe."""

    # MARKER = '\x00BRISH_MARKER'
    MARKER = "\x00"

    def __init__(
        self,
        defaultShell=None,
        boot_cmd=None,
        server_count=1,
        delayed_init=False,
        **kwargs,
    ):
        self.lock = RLock()
        #: `init()` arguments are kept on the instance, so that `restart()`
        #: and a delayed first use start the same kind of worker.
        self.encoding = kwargs.get("encoding") or "utf-8"
        self.decoding_errors = kwargs.get("decoding_errors") or "backslashreplace"
        if boot_cmd:
            self.boot_cmd = _shared_brish.zstring(boot_cmd, getframe=2)
        else:
            self.boot_cmd = boot_cmd

        self.defaultShell = defaultShell or [
            str(pathlib.Path(__file__).parent / "brish2.zsh"),
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
    ):
        with self.lock:
            if self.p is not None:
                self.cleanup()

            if encoding is not None:
                self.encoding = encoding
            if decoding_errors is not None:
                self.decoding_errors = decoding_errors
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
            for path in brish_stdin_paths:
                p.brish_stdins.append(
                    open(
                        self._legacy_open_request_fifo(path, p, shell),
                        "w",
                        errors="strict",
                        encoding=encoding,
                    )
                )
            for path in brish_stdout_paths:
                p.brish_stdouts.append(
                    open(path, "r", errors=decoding_errors, encoding=encoding)
                )
            for path in brish_stderr_paths:
                p.brish_stderrs.append(
                    open(path, "r", errors=decoding_errors, encoding=encoding)
                )
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
            return self._quote_word(obj.outrs)
        elif not isinstance(obj, str) and isinstance(obj, Iterable):
            # zsh doesn't support nested arrays, so we str the inner object.
            return " ".join(self._quote_word(str(i)) for i in iter(obj))
        else:
            return self._quote_word(str(obj))

    def _quote_word(self, s):
        encoding = self.encoding
        utf8 = codecs.lookup(encoding).name == "utf-8"
        return zsh_quote_bytes(s.encode(encoding, "surrogateescape"), ascii_only=not utf8)

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
        return range(len(self.locks))

    def send_cmd(
        self, cmd, cmd_stdin="", fork=False, server_index=None, lock_sleep=1
    ):
        if cmd == "%BRISH_RESTART":
            #: Handled before any worker lock is taken: restarting needs every
            #: worker lock, so holding one here could deadlock with another
            #: thread's restart().
            self.restart()
            return CmdResult(0, "Restarted succesfully.", "", cmd, str(cmd_stdin))
        return self._send_legacy(cmd, cmd_stdin, fork, server_index, lock_sleep)

    def _send_legacy(self, cmd, cmd_stdin, fork, server_index, lock_sleep):
        cmd_stdin = str(cmd_stdin)
        # assert  isinstance(cmd, str)
        if any(self.MARKER in input for input in (cmd, cmd_stdin)):
            return CmdResult(
                9000,
                "",
                "Illegal input: Input contained the Brish marker (currently the NUL character).",
                cmd,
                cmd_stdin,
            )
        cmd_processed = (
            cmd + self.MARKER + cmd_stdin + self.MARKER + boolsh(fork) + self.MARKER
        )
        #: Fail on unencodable input before anything reaches a worker.
        (cmd_processed + "\n").encode(self.encoding)

        for attempt in range(2):
            lock, index, p = self._acquire(server_index, lock_sleep)
            outcome = None
            try:
                p.free_server_count -= 1
                outcome = self._legacy_transact(p, index, cmd_processed, cmd, cmd_stdin)
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
            result, died = outcome
            if died:
                self._request_restart(p.gen)
            return result

        raise BrishWorkerDiedException(
            "a worker died before running the command, twice"
        )

    def _legacy_transact(self, p, index, cmd_processed, cmd, cmd_stdin):
        delim = self.MARKER + "\n"
        try:
            ##
            # trying to open the stdin as binary. It didn't work, idk why.
            # cmd_processed = cmd_processed.encode()
            # self.p.brish_stdins[server_index].write(cmd_processed)
            ##
            print(
                cmd_processed,
                file=p.brish_stdins[index],
                flush=True,
            )
        except BrokenPipeError:
            #: The worker is gone; it cannot have read the whole request.
            return _NEVER_RAN

        #: Read stderr concurrently: a command that fills the stderr FIFO
        #: before finishing its stdout would otherwise deadlock.
        err_reader = _ErrReader(p.brish_stderrs[index], delim)
        p.err_readers[index] = err_reader
        stdout, died = _legacy_read_reply(p.brish_stdouts[index], delim)
        return_code = None
        if not died:
            rc_line = p.brish_stdouts[index].readline()
            if rc_line == "":
                died = True
            else:
                return_code = int(rc_line)
        err_reader.thread.join(2 if died else None)
        if err_reader.exc is not None:
            raise err_reader.exc
        stderr = err_reader.text
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
            return (
                CmdResult(
                    return_code,
                    stdout,
                    _with_note(stderr, WORKER_DIED_NOTE),
                    cmd,
                    cmd_stdin,
                ),
                True,
            )
        return CmdResult(return_code, stdout, stderr, cmd, cmd_stdin), False

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
                self._cleanup_legacy(p)
            finally:
                for lock in locks:
                    lock.release()

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
