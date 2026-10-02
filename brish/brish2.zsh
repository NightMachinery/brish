#!/usr/bin/env zsh
\builtin \typeset -g "__brish2_aliases=${options[aliases]}"; \builtin \setopt \no_aliases
# Brish worker for legacy mode (see docs/protocol.org, "Legacy mode").
#
# The wire format is frozen: Python processes running older Brish code spawn
# this file by path on every init() and restart(), so every reply must stay
# byte-compatible with what the original script wrote (checked by
# tests/test_wire_compat.py).
#
# Aliases are off while this file is parsed (the startup files may define
# global aliases) and every internal command is prefixed with `builtin`, so
# user functions that shadow builtins cannot reach worker internals. Framing
# uses the private constants below, set once before any command runs:
# $MARKER is kept only because commands may read it, and a `$'\0'` literal
# evaluates to the empty string while a command's POSIX_STRINGS is on (it is
# part of `emulate sh`).
# MARKER=$'\0'"BRISH_MARKER"
MARKER=$'\0'
builtin printf -v __brish2_nul '\0'
__brish2_dl=$'\n'"$__brish2_nul"$'\n'  # a reply delimiter, with the newline before it

IFS= builtin read -r -d "$__brish2_nul" BRISH_STDIN
IFS= builtin read -r -d "$__brish2_nul" BRISH_STDOUT
IFS= builtin read -r -d "$__brish2_nul" BRISH_STDERR
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
    builtin print -rn -- "$__brish2_dl+$1"$'\n'
    (
      if [[ -n $__brish2_pid ]] && builtin zmodload zsh/system zsh/zselect 2>/dev/null; then
        repeat 1000; do  # 10 ms steps; give up after 10 s
          [[ ${sysparams[ppid]} == $__brish2_pid ]] || builtin break
          builtin zselect -t 1 || builtin true  # status 1 is the timeout
        done
      fi
      builtin print -rn -- "$__brish2_dl" >&2
    ) </dev/null &
  fi
}

builtin typeset -ga __brish2_pids
builtin local brish_server_index
for brish_server_index in {1..${#stdins}} ; do
    (
        #: fd 0 is the request FIFO. Commands never inherit it: they run with
        #: stdin redirected at the call site, so zsh keeps fd 0 in a private
        #: copy that it closes in every child process.
        builtin typeset -g __brish2_inreq= __brish2_ret=0 __brish2_eof= __brish2_x= __brish2_int= __brish2_pb=
        builtin typeset -g __brish2_level=$ZSH_SUBSHELL
        #: This worker's PID, from a child, so the worker itself does not
        #: load zsh/system.
        builtin typeset -g __brish2_pid=$(builtin zmodload zsh/system 2>/dev/null && builtin print -r -- ${sysparams[ppid]})
        builtin trap '__brish2_on_exit $?' EXIT
        builtin trap '' INT  # until a command runs; see TRAPINT below
        #: An always-EOF pipe, opened once: the stdin of every command that
        #: gets no stdin, so such a command forks nothing.
        builtin typeset -g __brish2_empty=
        builtin exec {__brish2_empty}< <(builtin true)
        if [[ $__brish2_aliases == on ]]; then
            builtin setopt aliases  # user code keeps its aliases
        fi
        #: The reply is written at the top of the loop, so a command's
        #: `continue N` still gets one, and the outer loop absorbs `break N`.
        while [[ -z $__brish2_eof ]]; do
            while {
                if [[ -n $__brish2_inreq ]]; then
                    builtin print -rn -- "$__brish2_dl$__brish2_ret"$'\n'
                    builtin print -rn -- "$__brish2_dl" >&2
                    __brish2_inreq=
                fi
                IFS= builtin read -r -d "$__brish2_nul" cmd
            }
            do
                IFS= builtin read -r -d "$__brish2_nul" brish_stdin
                IFS= builtin read -r -d "$__brish2_nul" brish_fork
                #: Set again for every request: zsh skips an EXIT trap that was
                #: set while POSIX_TRAPS was off, if a command turned it on and a
                #: later command exits from inside a function.
                builtin trap '__brish2_on_exit $?' EXIT
                __brish2_inreq=1 __brish2_ret= __brish2_int= __brish2_pb=
                {
                    #: SIGINT is ignored while the worker is idle or framing,
                    #: so that it never interrupts the worker's own reads and
                    #: writes. While a command runs, a TRAPINT makes SIGINT act
                    #: like an interactive Ctrl-C: returning 128+signal unwinds
                    #: the command as interrupted. A fork command sets a trap in
                    #: its subshell that exits with 128+signal. A non-fork
                    #: command gets the worker's trap: `always` stops the
                    #: unwinding (TRY_BLOCK_INTERRUPT=0), and the reply carries
                    #: the status the trap records (returning through a
                    #: function turns it into 1). The worker's trap acts only
                    #: inside the command's function: at this level an
                    #: interrupt would break every loop of the worker, which
                    #: ends it, so here it is ignored. A command may set its
                    #: own INT trap, which lasts until the command ends.
                    repeat 1 do  # absorbs a bare break or continue
                        #: Call-site redirections: `>&1 2>&2` also undo a
                        #: command's `exec >file`, which would hide the replies.
                        #: The stdin writer ignores SIGPIPE and always succeeds,
                        #: so a command that does not read its stdin still
                        #: reports its own status, also under pipefail; it gets
                        #: </dev/null so that it never holds the request FIFO.
                        if [[ -n $brish_fork ]]; then
                            if [[ -n $brish_stdin ]]; then
                                ( function TRAPINT { builtin exit $(( 128 + $1 )) }; { builtin trap '' PIPE; builtin print -rn -- "$brish_stdin"; builtin true } 2>/dev/null | builtin eval "$cmd" ) </dev/null
                            else
                                #: `true` first: the command starts with $? = 0,
                                #: as it did in the original pipeline.
                                ( function TRAPINT { builtin exit $(( 128 + $1 )) }; builtin true; builtin eval "$cmd" ) <&$__brish2_empty
                            fi
                        else
                            #: The trap turns off the options that would make zsh
                            #: exit the worker instead of unwinding: err_exit
                            #: and err_return (at this top level err_return acts
                            #: as err_exit), which `always` turns off anyway, and
                            #: posix_builtins (`emulate sh`), under which an
                            #: interrupt inside a special builtin such as `:` or
                            #: `eval` is fatal; `always` turns that back on when
                            #: the change was global (no local_options where the
                            #: trap ran). Turning off local_options keeps these
                            #: changes when the trap returns into a function that
                            #: set it. `[@]`: the element count, also under
                            #: ksh_arrays.
                            function TRAPINT {
                                if (( ${#funcstack[@]} > 1 )); then
                                    __brish2_int=$(( 128 + $1 ))
                                    if [[ -o posix_builtins ]]; then
                                        [[ -o local_options ]] || __brish2_pb=1
                                        builtin unsetopt posix_builtins
                                    fi
                                    builtin unsetopt local_options err_exit err_return
                                    builtin return $__brish2_int
                                fi
                            }
                            #: Running the code wrapped in a function block lets it
                            #: use 'return'. `functions[tmp_block_8182782]="$cmd"`
                            #: would corrupt unicode characters (it does not
                            #: unmetafy), so the body goes through eval.
                            #: @test typeset cmd=$'\nec \'HARRY: “Hermione,\' > ~/tmp/a'
                            #: `&&`: after a syntax error the previous command must
                            #: not run again.
                            if [[ -n $brish_stdin ]]; then
                                builtin eval "function tmp_block_8182782 {"$'\n'"$cmd"$'\n'"}" &&
                                    { builtin trap '' PIPE; builtin print -rn -- "$brish_stdin"; builtin true } </dev/null 2>/dev/null | tmp_block_8182782 >&1 2>&2
                            else
                                builtin eval "function tmp_block_8182782 {"$'\n'"$cmd"$'\n'"}" &&
                                    tmp_block_8182782 <&$__brish2_empty >&1 2>&2
                            fi
                        fi
                    done
                    #: The status is taken here, so that the `always` construct
                    #: ends with status 0: otherwise a ZERR trap fires twice.
                    __brish2_ret=$?
                } always {
                    #: Runs after normal completion, after shell errors (NOMATCH
                    #: and the like), after an interrupt, and when a `break N`
                    #: or `continue N` from the command unwinds. `exit` skips it.
                    __brish2_x=${__brish2_int:-$?} TRY_BLOCK_ERROR=0 TRY_BLOCK_INTERRUPT=0
                    builtin trap '' INT
                    if [[ -z $__brish2_ret ]]; then
                        __brish2_ret=$__brish2_x
                    fi
                    builtin unsetopt err_exit err_return
                    if [[ -n $__brish2_pb ]]; then
                        builtin setopt posix_builtins
                    fi
                }
            done
            if [[ -z $__brish2_inreq ]]; then
                __brish2_eof=1
            fi
        done
    ) < $stdins[$brish_server_index] >> $stdouts[$brish_server_index] 2>> $stderrs[$brish_server_index] &
    #: Quoted: zsh 5.9 stores a subscripted assignment from a bare `$!` as
    #: the two characters `$!`, which made every worker look dead.
    __brish2_pids[brish_server_index]="$!"
done

#: A worker that dies without answering (errexit, `${x:?}`, a signal, `exec`)
#: would leave its caller waiting for as long as a background job holds the
#: reply FIFOs open, and an old Python spinning on EOF. So when a worker is
#: gone, the bootstrap writes the reply "retcode 9001" on its behalf, with the
#: retcode line "09001": every Python parses it as 9001, and new Python tells
#: it apart from a command's own `return 9001`. If the
#: worker answered, or was idle, nobody reads it: the caller's next request
#: finds the request FIFO without a reader and restarts the instance. The
#: FIFOs are opened non-blocking, so this never waits for a reader.
function __brish2_answer {  # $1: worker index
  builtin local fd
  if builtin sysopen -w -o nonblock -u fd -- $stdouts[$1] 2>/dev/null; then
    builtin syswrite -o $fd -- "${__brish2_dl}09001"$'\n' 2>/dev/null
    builtin exec {fd}>&-
  fi
  if builtin sysopen -w -o nonblock -u fd -- $stderrs[$1] 2>/dev/null; then
    builtin syswrite -o $fd -- "$__brish2_dl" 2>/dev/null
    builtin exec {fd}>&-
  fi
}
function __brish2_reap {
  builtin local i
  for (( i = 1; i <= $#__brish2_pids; i++ )); do
    if [[ -n $__brish2_pids[i] ]] && ! builtin kill -0 $__brish2_pids[i] 2>/dev/null; then
      __brish2_pids[i]=
      __brish2_answer $i
    fi
  done
}
#: Set after the workers are forked, so they do not see the module or the
#: traps. A reader that goes away must not kill the bootstrap with SIGPIPE,
#: and a terminal Ctrl-C, which reaches the whole process group, must not
#: kill it either (each worker aborts only its current command).
builtin trap '' PIPE INT
if builtin zmodload zsh/system 2>/dev/null; then
  builtin trap __brish2_reap CHLD
fi

builtin wait
__brish2_reap
