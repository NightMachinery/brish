from concurrent.futures import ThreadPoolExecutor
import os
from .brishz import *
from .brishmod import *

bsh = Brish(delayed_init=True, server_count=4)
z = bsh.z
zp = bsh.zp
zpe = bsh.zpe
zq = bsh.zsh_quote
zs = bsh.zstring


##
def z_background(
    *args,
    background_p=True,
    getframe=1,
    locals_=None,
    **kwargs,
):
    locals_ = get_locals(
        getframe=getframe,
        locals_=locals_,
    )

    def run_command(locals_):
        bsh = Brish(delayed_init=True, server_count=1)
        return bsh.z(
            *args,
            locals_=locals_,
            **kwargs,
        )

    if background_p:
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(run_command, locals_)
        executor.shutdown(wait=False)
        return future

    else:
        return run_command(locals_)


zb = z_background


## @personal
def zn(*a, getframe=3, **kw):
    "Runs only if night.sh is installed"

    if isNight:
        return z(*a, **kw, getframe=getframe)

    else:
        # return None
        return CmdResult(127, "", "night.sh not found", "NA", "NA")


##
