# Installation

`pip install -U brish`

Or to install the latest version:

`pip install git+https://github.com/NightMachinary/brish`


# Usage

I plan to update this README with more examples and details, but the library is easy to use with the following examples in the meantime:

```
from brish import z, zp

z('echo {python_command_here_that_gets_quoted_automatically(...)}')
zp("echo zp is like z but prints the result's stdout in addition to returing a CmdResult")
zp("echo :e disables quoting {'$test1'} {'$test2':e}")
```

They accept `fork=True` to fork, but by default don't, so variables persist.
