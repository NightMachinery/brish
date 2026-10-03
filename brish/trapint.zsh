# The SIGINT trap of both Brish workers (brish2.zsh and brish3.zsh). See
# docs/protocol.org, "Interrupts".
#
# A worker sources this file once when it starts, and again only after a
# command has replaced or removed the trap: every change of the INT trap
# passes through the default disposition for a moment, and a SIGINT that
# arrives then kills the worker. So the trap stays the same while the worker
# idles, frames and runs commands, and variables that the worker sets for
# the duration of each command decide what it does. The worker keeps the
# file the trap came from (`$functions_source[TRAPINT]`; with zsh before 5.4,
# the trap's body) from when it sourced this file, and tells its own trap
# from one that a command defined by comparing the two.
#
# The worker sets these before the first command:
#   __brish_level: its $ZSH_SUBSHELL;
#   __brish_depth: ${#funcstack} where it calls a non-fork command's
#     function, plus one for TRAPINT itself;
#   __brish_trap, __brish_trap_arg: empty while idle or framing, and
#     "unsetopt" and "xtrace" while a command runs.
# The trap sets __brish_int (128+signal), and __brish_pb when it turned
# posix_builtins off, for the worker's `always` block.
#
# Idle or framing, the first line is `builtin return 0`, and nothing else is
# evaluated: a trap that returns 0 leaves $?, `break` and `return` as they
# were, and the worker's reads (`read`, `sysread`) and writes (`syswrite`)
# retry after it. While a command runs, the first line turns xtrace off for
# the rest of the trap (zsh restores it when the trap returns), and then:
#   - in a subshell of the worker (a fork command, or a command
#     substitution or subshell of a non-fork command), the trap exits it
#     with 128+signal;
#   - inside a non-fork command's function, it records 128+signal and
#     returns it, which unwinds the command as an interactive Ctrl-C would.
#     It first turns off the options that would make zsh exit the worker
#     instead: err_exit and err_return (`always` turns them off after every
#     command anyway), and posix_builtins (`emulate sh`), under which an
#     interrupt inside a special builtin such as `eval` or `.` is fatal;
#     `always` then sets posix_builtins back to what it was before the
#     command. It also turns off local_options, which the trap inherits
#     from where it runs: otherwise its own return would undo these
#     changes. A scope that restores options (local_options, `emulate -L`,
#     `emulate -c`) still turns them back on when the interrupt unwinds out
#     of it; see docs/protocol.org for the cases that cost the worker;
#   - elsewhere (the worker's own code around the command, and the worker
#     waiting for a fork command's subshell), it does nothing.
# Assignments use `typeset -g`, which warn_nested_var does not report. `[@]`
# counts the elements also under ksh_arrays.
#
# zsh 5.9 runs a trap from its signal handler, unless it is waiting for a
# child, and the trap allocates memory. When the signal lands inside a
# free() of zsh's (which zsh does not guard), the allocator's lock is held,
# and the process crashes (macOS: SIGTRAP). The worker therefore keeps such
# frees out of its own code between commands where it can: it removes the
# command's function with `unfunction`, which holds signals back, instead
# of letting the next definition free it. A command's own shell code
# (defining a function, say) can still be hit.
function TRAPINT {
  builtin "${__brish_trap:-return}" "${__brish_trap_arg:-0}"
  if (( ZSH_SUBSHELL > __brish_level )); then
    builtin exit $(( 128 + $1 ))
  fi
  if (( ${#funcstack[@]} > __brish_depth )); then
    builtin typeset -g __brish_int=$(( 128 + $1 ))
    if [[ -o posix_builtins ]]; then
      builtin typeset -g __brish_pb=1
      builtin unsetopt posix_builtins
    fi
    builtin unsetopt local_options err_exit err_return
    builtin return $__brish_int
  fi
}
