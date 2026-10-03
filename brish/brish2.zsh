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

#: The SIGINT trap's state (see trapint.zsh, and the worker below). TRAPINT
#: acts only deeper than itself, that is inside the command's function.
builtin typeset -g __brish_trapfile=${${(%):-%x}:A:h}/trapint.zsh
builtin typeset -g __brish_trap= __brish_trap_arg= __brish_int= __brish_pb= __brish_pb0= __brish_tb=
builtin typeset -g __brish_trapbody= __brish_level=0 __brish_depth=1

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
  __brish_trap= __brish_trap_arg=  # a SIGINT must not cut this short
  if [[ -n $__brish2_inreq ]] && (( ZSH_SUBSHELL == __brish_level )); then
    __brish2_inreq=
    if [[ -n $__brish2_sw ]]; then
      builtin syswrite -- "$__brish2_dl+$1"$'\n'
    else
      builtin print -rn -- "$__brish2_dl+$1"$'\n'
    fi
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

#: (Re)define the worker's TRAPINT from trapint.zsh: once when the worker
#: starts, and after a command replaced or removed it. Aliases and
#: local_traps are off, so that the definition parses as written and outlasts
#: this function. The body is kept, to tell this trap from a command's own.
#: Without syswrite (zsh/system) or the trap file, the worker ignores SIGINT
#: instead, as workers did before TRAPINT: it writes its replies with
#: `print`, which a SIGINT trap would cut short.
#: @duplicateCode/9834d1f0406a4c3eb2f9b672e929d810 brish3_deftrap in brish3.zsh
function __brish2_deftrap {
  builtin emulate -L zsh
  builtin setopt no_aliases no_local_traps
  if [[ -n $__brish2_sw ]]; then
    builtin source "$__brish_trapfile"
    __brish_trapbody=${functions[TRAPINT]-}
  else
    builtin trap '' INT
  fi
}

builtin typeset -ga __brish2_pids
builtin local brish_server_index
for brish_server_index in {1..${#stdins}} ; do
    (
        #: fd 0 is the request FIFO. Commands never inherit it: they run with
        #: stdin redirected at the call site, so zsh keeps fd 0 in a private
        #: copy that it closes in every child process.
        builtin typeset -g __brish2_inreq= __brish2_ret=0 __brish2_eof= __brish2_x= __brish2_sw=
        builtin typeset -g __brish_level=$ZSH_SUBSHELL
        #: This worker's PID, from a child, so that the worker itself loads
        #: only syswrite from zsh/system.
        builtin typeset -g __brish2_pid=$(builtin zmodload zsh/system 2>/dev/null && builtin print -r -- ${sysparams[ppid]})
        builtin trap '__brish2_on_exit $?' EXIT
        if [[ -r $__brish_trapfile ]] && builtin zmodload -F zsh/system b:syswrite 2>/dev/null; then
            __brish2_sw=1
        else
            __brish_trapfile=
        fi
        __brish2_deftrap  # the worker's TRAPINT, for good; see below
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
                    #: syswrite, which goes on after a SIGINT that the trap
                    #: ignores; print gives up.
                    if [[ -n $__brish2_sw ]]; then
                        builtin syswrite -- "$__brish2_dl$__brish2_ret"$'\n'
                        builtin syswrite -o 2 -- "$__brish2_dl"
                    else
                        builtin print -rn -- "$__brish2_dl$__brish2_ret"$'\n'
                        builtin print -rn -- "$__brish2_dl" >&2
                    fi
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
                __brish2_inreq=1 __brish2_ret= __brish_int= __brish_pb= __brish_pb0=
                [[ -o posix_builtins ]] && __brish_pb0=1
                {
                    #: SIGINT reaches the worker's TRAPINT at any time (see
                    #: trapint.zsh). Idle or framing, it returns at once.
                    #: While a command runs (from setting __brish_trap to
                    #: `always`), it makes SIGINT act like an interactive
                    #: Ctrl-C. A fork command's subshell exits with
                    #: 128+signal; the worker itself goes on waiting for it.
                    #: A non-fork command is unwound: `always` stops the
                    #: unwinding (TRY_BLOCK_INTERRUPT=0), and the reply
                    #: carries the status the trap records (returning through
                    #: a function turns it into 1). The trap acts only inside
                    #: the command's function: at this level an interrupt
                    #: would break every loop of the worker, which ends it. A
                    #: command may set its own INT trap, which lasts until
                    #: the command ends.
                    repeat 1 do  # absorbs a bare break or continue
                        #: Call-site redirections: `>&1 2>&2` also undo a
                        #: command's `exec >file`, which would hide the replies.
                        #: The stdin writer ignores SIGPIPE and always succeeds,
                        #: so a command that does not read its stdin still
                        #: reports its own status, also under pipefail; it gets
                        #: </dev/null so that it never holds the request FIFO.
                        if [[ -n $brish_fork ]]; then
                            __brish_trap=unsetopt __brish_trap_arg=xtrace
                            if [[ -n $brish_stdin ]]; then
                                ( { builtin trap '' PIPE; builtin print -rn -- "$brish_stdin"; builtin true } 2>/dev/null | builtin eval "$cmd" ) </dev/null
                            else
                                #: `true` first: the command starts with $? = 0,
                                #: as it did in the original pipeline.
                                ( builtin true; builtin eval "$cmd" ) <&$__brish2_empty
                            fi
                        else
                            #: Running the code wrapped in a function block lets it
                            #: use 'return'. `functions[tmp_block_8182782]="$cmd"`
                            #: would corrupt unicode characters (it does not
                            #: unmetafy), so the body goes through eval.
                            #: @test typeset cmd=$'\nec \'HARRY: “Hermione,\' > ~/tmp/a'
                            #: `&&`: after a syntax error the previous command must
                            #: not run again. __brish_trap is set once the
                            #: function is defined: a trap that acted inside
                            #: the eval would break the worker's loops. The
                            #: stdin writer clears it, so that a SIGINT does
                            #: not stop it: the command decides whether it is
                            #: interrupted.
                            if [[ -n $brish_stdin ]]; then
                                builtin eval "function tmp_block_8182782 {"$'\n'"$cmd"$'\n'"}" &&
                                    __brish_tb=1 __brish_trap=unsetopt __brish_trap_arg=xtrace &&
                                    { __brish_trap=; builtin trap '' PIPE; builtin print -rn -- "$brish_stdin"; builtin true } </dev/null 2>/dev/null | tmp_block_8182782 >&1 2>&2
                            else
                                builtin eval "function tmp_block_8182782 {"$'\n'"$cmd"$'\n'"}" &&
                                    __brish_tb=1 __brish_trap=unsetopt __brish_trap_arg=xtrace &&
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
                    __brish2_x=${__brish_int:-$?} TRY_BLOCK_ERROR=0 TRY_BLOCK_INTERRUPT=0 __brish_trap= __brish_trap_arg=
                    if [[ -z $__brish2_ret ]]; then
                        __brish2_ret=$__brish2_x
                    fi
                    builtin unsetopt err_exit err_return
                    #: The trap turned posix_builtins off where it ran.
                    #: Whether that was the command's global setting or one
                    #: that a scope (`emulate sh -c`) has undone since, it is
                    #: put back as it was before the command.
                    if [[ -n $__brish_pb ]]; then
                        if [[ -n $__brish_pb0 ]]; then
                            builtin setopt posix_builtins
                        else
                            builtin unsetopt posix_builtins
                        fi
                    fi
                    [[ ${functions[TRAPINT]-} == "$__brish_trapbody" ]] || __brish2_deftrap
                    #: `unfunction` frees the body with signals held back;
                    #: redefining it in the next eval would free it where a
                    #: SIGINT can run the trap, and zsh crashes when a trap
                    #: runs inside free() (see trapint.zsh).
                    if [[ -n $__brish_tb ]]; then
                        __brish_tb=
                        builtin unfunction tmp_block_8182782
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
#: Set after the workers are forked, so they do not inherit these traps or
#: the whole module (a worker loads only syswrite from it). A reader that
#: goes away must not kill the bootstrap with SIGPIPE, and a terminal Ctrl-C,
#: which reaches the whole process group, must not kill it either (each
#: worker aborts only its current command).
builtin trap '' PIPE INT
if builtin zmodload zsh/system 2>/dev/null; then
  builtin trap __brish2_reap CHLD
fi

builtin wait
__brish2_reap
