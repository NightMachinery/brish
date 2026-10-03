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

#: New Python starts this bootstrap in a session of its own, which no
#: terminal's signals reach, and says so with BRISH_SESSION=1. Its workers
#: then get SIGINT from Brish alone (BrishPopen.kill(), or the one SIGINT
#: for an abandoned BrishPopen), and handle it (see below). Older Python
#: does neither: its workers share its terminal's process group, and ignore
#: SIGINT as they always did. Commands do not inherit the variable.
builtin typeset -g __brish_session=${BRISH_SESSION-}
builtin unset BRISH_SESSION
#: The SIGINT trap's state (see trapint.zsh, and the worker below). TRAPINT
#: acts only deeper than itself, that is inside the command's function.
builtin typeset -g __brish_trapfile=${${(%):-%x}:A:h}/trapint.zsh
builtin typeset -g __brish_trap= __brish_trap_arg= __brish_int= __brish_pb= __brish_pb0= __brish_tb=
builtin typeset -g __brish_trapid= __brish_trapref= __brish_tbref= __brish_level=0 __brish_depth=1
#: The worker tells its own TRAPINT from one that a command defined by the
#: file the function came from, $functions_source[TRAPINT] (zsh 5.4 and
#: later). Reading $functions[TRAPINT] instead turns the body back into text
#: every time, which made each command about 4 us slower. Older zsh compare
#: the body. In the same way, __brish_tbref tells whether the command's
#: function still exists (a command may remove it itself); older zsh go by
#: __brish_tb, set when the function was defined.
if (( ${+functions_source} )); then
    __brish_trapref='functions_source[TRAPINT]'
    __brish_tbref='functions_source[tmp_block_8182782]'
else
    __brish_trapref='functions[TRAPINT]'
    __brish_tbref=__brish_tb
fi

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
    if (( ${+builtins[syswrite]} )); then  # see the worker's reply
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
#: this function. What tells this trap from a command's own is kept (see
#: __brish_trapref). Without syswrite (zsh/system) or the trap file, the
#: worker ignores SIGINT instead, as workers did before TRAPINT: it writes
#: its replies with `print`, which a SIGINT trap would cut short. A
#: command's own trap string is no function either, so it cannot be told
#: from that: the id is then NUL, which nothing matches, and `always`
#: ignores SIGINT again after every command. Only a worker in a session of
#: its own (see __brish_session) defines a trap at all.
#: @duplicateCode/9834d1f0406a4c3eb2f9b672e929d810 brish3_deftrap in brish3.zsh
function __brish2_deftrap {
  builtin emulate -L zsh
  builtin setopt no_aliases no_local_traps
  if [[ -n $__brish2_sw && -n $__brish_trapfile ]]; then
    builtin source "$__brish_trapfile"
    __brish_trapid=${(P)__brish_trapref-}
  else
    builtin trap '' INT
    __brish_trapid=$'\0'
  fi
}

#: Writes a command's cmd_stdin, in a child process of its own. It clears
#: __brish_trap for itself first: kill() sends SIGINT to every process below
#: the worker, this one included, and the trap would otherwise exit it with
#: 130 (it runs in a subshell), cutting the stdin short. The command decides
#: whether it is interrupted. It writes with syswrite, which goes on after a
#: SIGINT that the trap ignores (print can give up in the middle of a
#: write). It ignores SIGPIPE and always succeeds, so a command that does not
#: read its stdin still reports its own status, also under pipefail. It
#: replaces its stdin and stderr with /dev/null by `exec`: a redirection of a
#: group would keep a saved copy of the worker's fd 0, the request FIFO.
#: Without syswrite it uses print, as the worker's reply does (see there).
#: @duplicateCode/0a4f05830c0a4ae7ba6009411f60fdbe brish3_write_stdin in brish3.zsh
function __brish2_write_stdin {
  __brish_trap= __brish_trap_arg=
  builtin exec </dev/null 2>/dev/null
  builtin trap '' PIPE
  if (( ${+builtins[syswrite]} )); then
    builtin syswrite -- "$brish_stdin" || builtin true
  else
    builtin print -rn -- "$brish_stdin" || builtin true
  fi
}

#: A bootstrap that Python starts to replace one worker of a larger pool (see
#: docs/protocol.org, "Slots") gets BRISH_SERVER_INDEX_OFFSET, the 0-based
#: index of the first slot it serves: it is added to each worker's 1-based
#: index, so that $brish_server_index is the slot's, as in the pool's own
#: bootstrap. Unset (as older Python leaves it), it is 0 and nothing changes.
#: Commands do not inherit the variable.
#: @duplicateCode/21ca36344d874147ae4ab89e816af523 the offset in brish3.zsh
builtin typeset -g __brish2_offset=${BRISH_SERVER_INDEX_OFFSET:-0}
builtin unset BRISH_SERVER_INDEX_OFFSET
#: Checked under zsh's own options: the startup files may have left
#: sh_glob (or emulate sh) on, where <-> is no pattern.
if ! () { builtin emulate -LR zsh; [[ $1 == <-> ]] } "$__brish2_offset"; then
    builtin print -ru2 -- "brish2: bad BRISH_SERVER_INDEX_OFFSET: $__brish2_offset"
    builtin exit 64
fi

builtin typeset -ga __brish2_pids
#: __brish2_i is the worker's own index into the FIFO lists.
builtin local brish_server_index __brish2_i
for __brish2_i in {1..${#stdins}} ; do
    brish_server_index=$(( __brish2_i + __brish2_offset ))
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
        if builtin zmodload -F zsh/system b:syswrite 2>/dev/null; then
            __brish2_sw=1
        fi
        [[ -r $__brish_trapfile ]] || __brish_trapfile=
        if [[ -n $__brish_session ]]; then
            __brish2_deftrap  # the worker's TRAPINT, for good; see below
        else
            #: Older Python: SIGINT stays ignored (a background job ignores
            #: it), and the worker never sets an INT trap, as before
            #: TRAPINT: `always` finds nothing to put back.
            __brish_trapref=__brish_trapid
        fi
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
                    #: ignores; print gives up. A command may have unloaded
                    #: zsh/system or disabled syswrite: then print, with
                    #: which a SIGINT of kill() can cut the reply short (and
                    #: kill() then ends at step 4). Chosen by whether the
                    #: builtin exists, never by syswrite's failure, which
                    #: can follow a partial write.
                    if (( ${+builtins[syswrite]} )); then
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
                #: A request cut short (its writer closed the FIFO in the
                #: middle of it, as Python's end does while it writes a
                #: large stdin) is never run: `read` then fails, and the
                #: worker ends with no request open, as a binary worker
                #: does on EOF inside a payload. Every Python, old and new,
                #: writes all three fields.
                IFS= builtin read -r -d "$__brish2_nul" brish_stdin &&
                    IFS= builtin read -r -d "$__brish2_nul" brish_fork ||
                    builtin break
                #: Set again for every request: zsh skips an EXIT trap that was
                #: set while POSIX_TRAPS was off, if a command turned it on and a
                #: later command exits from inside a function.
                builtin trap '__brish2_on_exit $?' EXIT
                __brish2_inreq=1 __brish2_ret= __brish_int= __brish_pb= __brish_pb0= __brish_tb=
                [[ -o posix_builtins ]] && __brish_pb0=1
                {
                    #: SIGINT, which comes from Brish alone (the worker is
                    #: in a session of its own): BrishPopen.kill(), or the
                    #: one SIGINT for an abandoned BrishPopen. It reaches
                    #: the worker's TRAPINT at any time (see trapint.zsh). Under
                    #: older Python there is no trap, and the worker ignores
                    #: SIGINT throughout. Idle or framing, the trap returns
                    #: at once.
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
                        #: __brish2_write_stdin writes the stdin: in a fork
                        #: command's subshell through a pipeline, and for a
                        #: non-fork command from a process substitution. Not a
                        #: pipeline there: under `emulate sh`, which a command
                        #: can leave behind, zsh runs the last element of a
                        #: pipeline in a subshell, which loses the command's
                        #: state and its `exit`. A process substitution's body
                        #: is parsed when it runs, under the command's options
                        #: and aliases, hence one quoted word.
                        if [[ -n $brish_fork ]]; then
                            __brish_trap=unsetopt __brish_trap_arg=xtrace
                            if [[ -n $brish_stdin ]]; then
                                ( __brish2_write_stdin | builtin eval "$cmd" ) </dev/null
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
                            #: the eval would break the worker's loops.
                            if [[ -n $brish_stdin ]]; then
                                builtin eval "function tmp_block_8182782 {"$'\n'"$cmd"$'\n'"}" &&
                                    __brish_tb=1 __brish_trap=unsetopt __brish_trap_arg=xtrace &&
                                    tmp_block_8182782 < <(\__brish2_write_stdin) >&1 2>&2
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
                    [[ ${(P)__brish_trapref-} == "$__brish_trapid" ]] || __brish2_deftrap
                    #: `unfunction` frees the body with signals held back;
                    #: redefining it in the next eval would free it where a
                    #: SIGINT can run the trap, and zsh crashes when a trap
                    #: runs inside free() (see trapint.zsh). Only while it
                    #: exists: a command may have removed it (see
                    #: __brish_tbref).
                    [[ -z ${(P)__brish_tbref-} ]] || builtin unfunction tmp_block_8182782
                }
            done
            if [[ -z $__brish2_inreq ]]; then
                __brish2_eof=1
            fi
        done
    ) < $stdins[$__brish2_i] >> $stdouts[$__brish2_i] 2>> $stderrs[$__brish2_i] &
    #: Quoted: zsh 5.9 stores a subscripted assignment from a bare `$!` as
    #: the two characters `$!`, which made every worker look dead.
    __brish2_pids[__brish2_i]="$!"
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
#: goes away must not kill the bootstrap with SIGPIPE, and a SIGINT to the
#: whole process group must not kill it either: one that a command sends
#: (`kill -INT 0`), or under older Python a terminal Ctrl-C.
builtin trap '' PIPE INT
if builtin zmodload zsh/system 2>/dev/null; then
  builtin trap __brish2_reap CHLD
fi
#: The workers' PIDs, which BrishPopen.kill() signals, for new Python: it
#: reads them from the bootstrap's stdout, which nothing else writes after
#: the startup files, and which older Python never reads. The NUL sets the
#: line apart from what the startup files printed.
builtin print -rn -- "${__brish2_nul}BRISH2-PIDS:${(j: :)__brish2_pids}"$'\n' 2>/dev/null

#: Python has closed the bootstrap's stdin: it is cleaning up, or it has
#: ended, however it ended (normally, by an exception, a signal, SIGKILL, or
#: a closed terminal). An idle worker exits by itself, since nobody writes
#: its requests any more. A worker still alive a second later runs a
#: command (one whose reply was abandoned, or one whose caller died): it is
#: stopped with every process below it, background jobs of its earlier
#: commands included, as kill()'s steps 2 to 4 would stop them: SIGTERM,
#: and SIGKILL to whatever is left a second later. So no command outlives
#: Python. Background jobs that idle workers' commands left are not
#: touched, and neither is a process that left the worker's tree (a
#: double fork). The waits go by the clock: a timed wait can take several
#: times as long as asked on a loaded machine.
#: @duplicateCode/f2d464e1ad6f4e3eaedeeb994de39d42 brish3_stop_workers in brish3.zsh
function __brish2_stop_workers {  # $@: the workers' PIDs
  builtin emulate -LR zsh
  builtin local -a left todo f
  builtin local -A below
  builtin local p row zs= i round IFS=$' \t\n'
  builtin local -F end
  builtin zmodload zsh/zselect 2>/dev/null && zs=1
  builtin zmodload zsh/datetime 2>/dev/null
  left=( $@ )
  for round in 1 2; do
    #: Two rounds: first the workers, which idle ones leave by themselves;
    #: then every process left below them, after SIGTERM.
    end=$(( ${EPOCHREALTIME:-0} + 1 ))
    for i in {1..1000}; do
      todo=( $left ) left=()
      for p in $todo; do
        builtin kill -0 $p 2>/dev/null && left+=( $p )
      done
      (( $#left )) || builtin return 0
      if (( ${+EPOCHREALTIME} )); then
        (( EPOCHREALTIME < end )) || builtin break
      elif (( i >= 50 )); then
        builtin break
      fi
      if [[ -n $zs ]]; then builtin zselect -t 2; else command sleep 0.02; fi
    done
    (( round == 1 )) || builtin break
    for row in "${(@f)$(command ps -Ao pid=,ppid= 2>/dev/null)}"; do
      f=( ${=row} )
      (( $#f == 2 )) && below[$f[2]]+=" $f[1]"
    done
    todo=( $left ) left=()
    while (( $#todo )); do
      p=$todo[1]
      todo[1]=()
      left+=( $p )
      todo+=( ${=below[$p]} )
    done
    builtin kill -TERM $left 2>/dev/null
  done
  builtin kill -KILL $left 2>/dev/null
  builtin return 0
}

#: Stay until Python goes away: every Python, old and new, holds the
#: bootstrap's stdin open until cleanup() or its own end. `read`: zsh reaps
#: dead workers and runs the CHLD trap while it waits in `read`.
while IFS= builtin read -r __brish2_line; do
  builtin true
done
__brish2_stop_workers $__brish2_pids

builtin wait
__brish2_reap
