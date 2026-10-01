#!/usr/bin/env zsh
# Brish worker for legacy mode (see docs/protocol.org, "Legacy mode").
#
# The wire format is frozen: Python processes running older Brish code spawn
# this file by path on every init() and restart(), so every reply must stay
# byte-compatible with what the original script wrote (checked by
# tests/test_wire_compat.py).
# MARKER=$'\0'"BRISH_MARKER"
MARKER=$'\0'

IFS= read -d "$MARKER" -r BRISH_STDIN
IFS= read -d "$MARKER" -r BRISH_STDOUT
IFS= read -d "$MARKER" -r BRISH_STDERR
stdins=(${(@f)BRISH_STDIN})
stdouts=(${(@f)BRISH_STDOUT})
stderrs=(${(@f)BRISH_STDERR})

#: The worker's EXIT trap. When a non-fork command exits the worker, this
#: writes the command's reply, so the caller gets the real status. The
#: retcode line is "+N", which old Python parses as N and new Python reads as
#: "this worker is gone". The stderr delimiter is written by a helper process
#: once this worker has fully exited (its parent PID changes then), so a
#: caller that has read the whole reply finds the request FIFO without a
#: reader (EPIPE) instead of racing the exit. errexit and `${x:?}` exit
#: without running EXIT traps; the bootstrap answers for those (see below).
function __brish2_on_exit {  # $1: exit status
  if [[ -n $__brish2_inreq ]] && (( ZSH_SUBSHELL == __brish2_level )); then
    __brish2_inreq=
    builtin print -rn -- $'\n\0\n'"+$1"$'\n'
    (
      if [[ -n $__brish2_pid ]] && builtin zmodload zsh/system zsh/zselect 2>/dev/null; then
        repeat 1000; do  # 10 ms steps; give up after 10 s
          [[ $sysparams[ppid] == $__brish2_pid ]] || builtin break
          builtin zselect -t 1  # status 1 is the timeout
        done
      fi
      builtin print -rn -- $'\n\0\n' >&2
    ) </dev/null &
  fi
}

typeset -ga __brish2_pids
local brish_server_index
for brish_server_index in {1..${#stdins}} ; do
    (
        typeset -g __brish2_inreq= __brish2_ret=0 __brish2_eof= __brish2_x=
        typeset -g __brish2_level=$ZSH_SUBSHELL
        #: This worker's PID, from a child, so the worker itself does not
        #: load zsh/system.
        typeset -g __brish2_pid=$(builtin zmodload zsh/system 2>/dev/null && builtin print -r -- $sysparams[ppid])
        trap '__brish2_on_exit $?' EXIT
        #: The reply is written at the top of the loop, so a command's
        #: `continue N` still gets one, and the outer loop absorbs `break N`.
        while [[ -z $__brish2_eof ]]; do
            while {
                if [[ -n $__brish2_inreq ]]; then
                    print -nr -- $'\n'"$MARKER"$'\n'
                    print -r -- $__brish2_ret
                    print -nr -- $'\n'"$MARKER"$'\n' >&2
                    __brish2_inreq=
                fi
                IFS= read -d "$MARKER" -r cmd
            }
            do
                IFS= read -d "$MARKER" -r brish_stdin
                IFS= read -d "$MARKER" -r brish_fork
                __brish2_inreq=1 __brish2_ret=
                {
                    repeat 1 do  # absorbs a bare break or continue
                        if test -n "$brish_fork" ; then
                            #: </dev/null: nothing in the subshell may hold the
                            #: request FIFO (see __brish2_on_exit).
                            ( { ( print -nr -- "$brish_stdin" ) || true }  | eval "$cmd" ) </dev/null
                        else
                            #: Running the code wrapped in a function block has a lot of benefits, e.g., we can use 'return' freely.
                            # functions[tmp_block_8182782]="$cmd"
                            #: This corrupts unicode characters! But using eval directly works.
                            #: @test typeset cmd=$'\nec \'HARRY: “Hermione,\' > ~/tmp/a'
                            #: `&&`: after a syntax error the previous command must not run again.
                            #: The stdin writer gets </dev/null, so it does not hold
                            #: the request FIFO (see __brish2_on_exit).
                            eval "function tmp_block_8182782 {"$'\n'"$cmd"$'\n'"}" &&
                                { ( print -nr -- "$brish_stdin" ) || true } </dev/null | tmp_block_8182782
                        fi
                    done
                    #: The status is taken here, so that the `always` construct
                    #: ends with status 0: otherwise a ZERR trap fires twice.
                    __brish2_ret=$?
                } always {
                    #: Runs after normal completion, after shell errors (NOMATCH
                    #: and the like), and when a `break N` or `continue N` from
                    #: the command unwinds. `exit` skips it.
                    __brish2_x=$?
                    if [[ -z $__brish2_ret ]]; then
                        __brish2_ret=$__brish2_x
                    fi
                    TRY_BLOCK_ERROR=0
                    unsetopt err_exit err_return
                }
            done
            if [[ -z $__brish2_inreq ]]; then
                __brish2_eof=1
            fi
        done
    ) < $stdins[$brish_server_index] >> $stdouts[$brish_server_index] 2>> $stderrs[$brish_server_index] &
    __brish2_pids[brish_server_index]=$!
done

#: A worker that dies without answering (errexit, `${x:?}`, a signal, `exec`)
#: would leave its caller waiting for as long as a background job holds the
#: reply FIFOs open, and an old Python spinning on EOF. So when a worker is
#: gone, the bootstrap writes the reply "retcode 9001" on its behalf. If the
#: worker answered, or was idle, nobody reads it: the caller's next request
#: finds the request FIFO without a reader and restarts the instance. The
#: FIFOs are opened non-blocking, so this never waits for a reader.
function __brish2_answer {  # $1: worker index
  local fd
  if builtin sysopen -w -o nonblock -u fd -- $stdouts[$1] 2>/dev/null; then
    builtin syswrite -o $fd -- $'\n\0\n9001\n' 2>/dev/null
    exec {fd}>&-
  fi
  if builtin sysopen -w -o nonblock -u fd -- $stderrs[$1] 2>/dev/null; then
    builtin syswrite -o $fd -- $'\n\0\n' 2>/dev/null
    exec {fd}>&-
  fi
}
function __brish2_reap {
  local i
  for (( i = 1; i <= $#__brish2_pids; i++ )); do
    if [[ -n $__brish2_pids[i] ]] && ! builtin kill -0 $__brish2_pids[i] 2>/dev/null; then
      __brish2_pids[i]=
      __brish2_answer $i
    fi
  done
}
#: Set after the workers are forked, so they do not see the module or the
#: traps. A reader that goes away must not kill the bootstrap with SIGPIPE.
trap '' PIPE
if builtin zmodload zsh/system 2>/dev/null; then
  trap __brish2_reap CHLD
fi

wait
__brish2_reap
