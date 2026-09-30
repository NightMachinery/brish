import subprocess
from .brishmod import CmdResult, bool_from_str
import os


def brishz(cmd_array, stdin="", binary=None):
    """Uses (and needs) BrishGarden via 'brishzq.zsh', thus costing the caller possible startup costs.

    With `binary=True`, or the env var `brishz_binary` set, the client runs
    with `brishz_binary=y` and everything stays bytes: `stdin` may be bytes
    (a str is encoded as UTF-8 with surrogateescape, None is /dev/null), and
    the result carries the exact output in `outb` / `errb`. Otherwise this is
    the text interface it always was.
    """
    if binary is None:
        binary = bool_from_str(os.environ.get("brishz_binary", ""))
    if binary:
        return _brishz_bytes(cmd_array, stdin)

    sp = subprocess.run(
    ["/usr/bin/env", "brishz_in=MAGIC_READ_STDIN", "brishzq.zsh", *cmd_array],
    shell=False,
    cwd=os.getcwd(),
    text=True,
    executable=None,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    input=stdin
    )

    return CmdResult(sp.returncode, sp.stdout, sp.stderr, cmd_array, stdin)


def _brishz_bytes(cmd_array, stdin):
    if stdin is None:
        data, stdin_opt = None, subprocess.DEVNULL
    else:
        if isinstance(stdin, (bytes, bytearray, memoryview)):
            data = bytes(stdin)
        else:
            data = str(stdin).encode("utf-8", "surrogateescape")
        stdin_opt = None
    sp = subprocess.run(
        [
            "/usr/bin/env",
            "brishz_in=MAGIC_READ_STDIN",
            "brishz_binary=y",
            "brishzq.zsh",
            *cmd_array,
        ],
        shell=False,
        cwd=os.getcwd(),
        stdin=stdin_opt,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        input=data,
    )
    return CmdResult.from_bytes(sp.returncode, sp.stdout, sp.stderr, cmd_array, stdin)

## @personal
def isNight():
    return bool(os.environ.get("NIGHTDIR", False))

def brishzn(*args, **kwargs):
    if isNight():
        return brishz(*args, **kwargs)
    else:
        # return None
        return CmdResult(127, "", "night.sh not found", "NA", "NA")
##
