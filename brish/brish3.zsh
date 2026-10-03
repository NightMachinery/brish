#!/usr/bin/env zsh
\builtin \typeset \-g "__brish3_aliases=${options[aliases]}"; \builtin \setopt \no_aliases
# Brish worker for protocol BRISH3 (see docs/protocol.org).
#
# argv: [anything ...] BRISH3-FDS <req>,<out>,<err> [<req>,<out>,<err> ...]
# One triple of inherited pipe fds per worker: the request pipe it reads,
# and the stdout and stderr pipes it answers on.
#
# Aliases are off while this file is parsed (the startup files may define
# global aliases) and every internal command is prefixed with `builtin`, so
# user functions that shadow builtins cannot reach worker internals.
builtin zmodload zsh/system || builtin exit 70
#: New Python starts this bootstrap in a session of its own, which no
#: terminal's signals reach, and says so with BRISH_SESSION=1. Its workers
#: then get SIGINT from BrishPopen.kill() alone, and handle it (see
#: brish3_run). Older Python does neither: its workers share its terminal's
#: process group, and ignore SIGINT as they always did. Commands do not
#: inherit the variable.
typeset -g __brish_session=${BRISH_SESSION-}
builtin unset BRISH_SESSION

typeset -g BRISH3_NONCE= BRISH3_FORK=0 BRISH3_STDIN_MODE= BRISH3_INREQ= BRISH3_EOF=
typeset -g BRISH3_CMD= BRISH3_RET=0 brish_stdin= cmd=
#: The SIGINT trap's state (see trapint.zsh). TRAPINT acts only deeper than
#: itself, brish3_run and brish3_serve.
typeset -g __brish_trapfile=${${(%):-%x}:A:h}/trapint.zsh
[[ -r $__brish_trapfile ]] || { builtin print -ru2 -- "brish3: cannot read $__brish_trapfile"; builtin exit 70 }
typeset -g __brish_trap= __brish_trap_arg= __brish_int= __brish_pb= __brish_pb0= __brish_tb=
typeset -g __brish_trapid= __brish_trapref= __brish_level=0 __brish_depth=3
#: The worker tells its own TRAPINT from one that a command defined by the
#: file the function came from, $functions_source[TRAPINT] (zsh 5.4 and
#: later). Reading $functions[TRAPINT] instead turns the body back into text
#: every time, which made each command about 4 us slower. Older zsh compare
#: the body.
if (( ${+functions_source} )); then
  __brish_trapref='functions_source[TRAPINT]'
else
  __brish_trapref='functions[TRAPINT]'
fi
typeset -g __brish3_req= __brish3_out= __brish3_err= __brish3_empty=
typeset -g __brish3_nul= __brish3_nl= __brish3_startp= __brish3_endp=
typeset -ga __brish3_specs __brish3_pids

function brish3_parse_argv {
  builtin emulate -LR zsh
  local spec
  integer i=${argv[(I)BRISH3-FDS]}
  if (( ! i )); then
    builtin print -ru2 -- "brish3: no BRISH3-FDS in argv; this worker is started by Brish(binary=True)"
    return 1
  fi
  for spec in "${(@)argv[i+1,-1]}"; do
    if [[ $spec != <->,<->,<-> ]]; then
      builtin print -ru2 -- "brish3: bad fd triple: $spec"
      return 1
    fi
    __brish3_specs+=( "$spec" )
  done
  (( $#__brish3_specs )) || { builtin print -ru2 -- "brish3: no workers requested"; return 1 }
  __brish3_nul=$'\0' __brish3_nl=$'\n'
  __brish3_startp=$'\0'BRISH3-START: __brish3_endp=$'\0'BRISH3-END:
}

function brish3_close_specs {  # $1: index to keep (0 keeps none)
  builtin emulate -LR zsh
  integer i n
  local -a f
  for (( i = 1; i <= $#__brish3_specs; i++ )); do
    (( i == $1 )) && continue
    f=( ${(s:,:)__brish3_specs[i]} )
    for n in $f; do
      exec {n}<&-
    done
  done
}

function brish3_setup {  # $1: this worker's 1-based index
  builtin emulate -LR zsh
  local -a f
  integer n
  brish3_close_specs $1
  f=( ${(s:,:)__brish3_specs[$1]} )
  #: Move our fds above 9, where user `exec 3>...` style redirections do
  #: not reach, and close the originals. The request fd stays unique.
  n=$f[1]; exec {__brish3_req}<&$n; exec {n}<&-
  n=$f[2]; exec {__brish3_out}>&$n; exec {n}>&-
  n=$f[3]; exec {__brish3_err}>&$n; exec {n}>&-
  #: An always-EOF pipe: the default stdin of every command.
  exec {__brish3_empty}< <(builtin true)
  exec 0</dev/null 1>&$__brish3_out 2>&$__brish3_err
  builtin syswrite -o $__brish3_out -- "${__brish3_nul}BRISH3-HELLO:${sysparams[pid]}${__brish3_nl}"
}

#: Parse one request frame, read both payloads, then write START on both
#: response pipes. Returns 1 (and sets BRISH3_EOF) on EOF or a bad frame.
#: The header is ASCII: BRISH3 <32 hex nonce> <cmd_len> <stdin_len|-> <fork>
#: and it is validated before any arithmetic, because zsh arithmetic
#: expands command substitutions.
function brish3_recv {
  builtin emulate -LR zsh
  local h c
  local -a f a
  integer want got i
  IFS= builtin read -r -u $__brish3_req h || { BRISH3_EOF=1; return 1 }
  f=( ${(s: :)h} )
  if ! [[ $#f == 5 && $f[1] == BRISH3 && ${#f[2]} == 32 && $f[2] != *[^0-9a-f]* \
        && $f[3] == <-> && ${#f[3]} -le 15 \
        && ( $f[4] == - || ( $f[4] == <-> && ${#f[4]} -le 15 ) ) && $f[5] == [01] ]]; then
    builtin print -ru2 -- "brish3: bad request header; exiting"
    BRISH3_EOF=1
    return 1
  fi
  #: sysread -s 0 returns status 5, so empty payloads are never read.
  #: Each chunk is read straight into the next array element and the chunks
  #: are joined once: `+=` on a scalar is quadratic. Large values are always
  #: expanded inside double quotes, which is several times faster.
  want=$f[3] got=0 i=0 a=()
  while (( got < want )); do
    builtin sysread -i $__brish3_req -c c -s $(( want - got > 65536 ? 65536 : want - got )) 'a[++i]' || { BRISH3_EOF=1; return 1 }
    (( got += c ))
  done
  BRISH3_CMD="${(j::)a}"
  if [[ $f[4] == - ]]; then
    brish_stdin= BRISH3_STDIN_MODE=null
  elif [[ $f[4] == 0 ]]; then
    brish_stdin= BRISH3_STDIN_MODE=empty
  else
    want=$f[4] got=0 i=0 a=()
    while (( got < want )); do
      builtin sysread -i $__brish3_req -c c -s $(( want - got > 65536 ? 65536 : want - got )) 'a[++i]' || { BRISH3_EOF=1; return 1 }
      (( got += c ))
    done
    brish_stdin="${(j::)a}" BRISH3_STDIN_MODE=data
  fi
  cmd="$BRISH3_CMD" BRISH3_NONCE=$f[2] BRISH3_FORK=$f[5]
  builtin syswrite -o $__brish3_out -- "${__brish3_startp}${f[2]}${__brish3_nl}"
  builtin syswrite -o $__brish3_err -- "${__brish3_startp}${f[2]}${__brish3_nl}"
}

#: (Re)define the worker's TRAPINT from trapint.zsh: once when the worker
#: starts, and after a command replaced or removed it. Aliases and
#: local_traps are off, so that the definition parses as written and outlasts
#: this function. What tells this trap from a command's own is kept (see
#: __brish_trapref). Only a worker in a session of its own (see
#: __brish_session) defines a trap at all.
#: @duplicateCode/9834d1f0406a4c3eb2f9b672e929d810 __brish2_deftrap in brish2.zsh
function brish3_deftrap {
  builtin emulate -L zsh
  builtin setopt no_aliases no_local_traps
  builtin source "$__brish_trapfile"
  __brish_trapid=${(P)__brish_trapref-}
}

#: Writes a command's cmd_stdin, in a child process of its own. It clears
#: __brish_trap for itself first: kill() sends SIGINT to every process below
#: the worker, this one included, and the trap would otherwise exit it with
#: 130 (it runs in a subshell), cutting the stdin short. The command decides
#: whether it is interrupted. It writes with syswrite, which goes on after a
#: SIGINT that the trap ignores (print can give up in the middle of a
#: write). It ignores SIGPIPE and always succeeds, so a command that does not
#: read its stdin still reports its own status, also under pipefail. Its
#: stdin and stderr are /dev/null.
#: @duplicateCode/0a4f05830c0a4ae7ba6009411f60fdbe __brish2_write_stdin in brish2.zsh
function brish3_write_stdin {
  __brish_trap= __brish_trap_arg=
  builtin exec </dev/null 2>/dev/null
  builtin trap '' PIPE
  builtin syswrite -- "$brish_stdin" || builtin true
}

#: Run the request. No locals and no emulate here: user code runs inside and
#: must see the user's options. The nonce is kept only in $1 while user code
#: runs; `always` restores it from there.
#:
#: SIGINT, which only BrishPopen.kill() sends (the worker is in a session of
#: its own), reaches the worker's TRAPINT at any time (see trapint.zsh).
#: Under older Python there is no trap, and the worker ignores SIGINT
#: throughout. Idle or framing, the trap returns at once. While a command runs (from setting
#: __brish_trap to `always`), it makes SIGINT act like an interactive Ctrl-C.
#: A fork command's subshell exits with 128+signal; the worker itself goes on
#: waiting for it. A non-fork command is unwound: `always` stops the unwinding
#: (TRY_BLOCK_INTERRUPT=0), and END carries the status the trap records
#: (returning through a function turns it into 1). A command may set its own
#: INT trap, which lasts until the command ends.
function brish3_run {  # $1 nonce, $2 fork (0|1), $3 stdin mode (empty|null|data)
  BRISH3_NONCE= __brish_int= __brish_pb= __brish_pb0=
  [[ -o posix_builtins ]] && __brish_pb0=1
  {
    repeat 1 do  # absorbs a bare break or continue
      if [[ $2 == 1 ]]; then
        __brish_trap=unsetopt __brish_trap_arg=xtrace
        if [[ $3 == data ]]; then
          ( builtin set --; brish3_write_stdin | builtin eval "$BRISH3_CMD" ) >&$__brish3_out 2>&$__brish3_err
        elif [[ $3 == null ]]; then
          ( builtin set --; builtin eval "$BRISH3_CMD" ) </dev/null >&$__brish3_out 2>&$__brish3_err
        else
          ( builtin set --; builtin eval "$BRISH3_CMD" ) <&$__brish3_empty >&$__brish3_out 2>&$__brish3_err
        fi
      else
        #: A function body lets the command `return`. `functions[...]=` would
        #: corrupt NUL and the bytes 0x83 to 0x9D, so the body goes through
        #: eval. `&&`: after a syntax error the old body must not run again.
        #: __brish_trap is set once the function is defined, as in brish2.zsh,
        #: where a trap that acted inside that eval would break the worker's
        #: loops. brish3_write_stdin writes the stdin.
        if [[ $3 == data ]]; then
          builtin eval "function tmp_block_8182782 {${__brish3_nl}${BRISH3_CMD}${__brish3_nl}}" &&
            __brish_tb=1 __brish_trap=unsetopt __brish_trap_arg=xtrace &&
            brish3_write_stdin | tmp_block_8182782 >&$__brish3_out 2>&$__brish3_err
        elif [[ $3 == null ]]; then
          builtin eval "function tmp_block_8182782 {${__brish3_nl}${BRISH3_CMD}${__brish3_nl}}" &&
            __brish_tb=1 __brish_trap=unsetopt __brish_trap_arg=xtrace &&
            tmp_block_8182782 </dev/null >&$__brish3_out 2>&$__brish3_err
        else
          builtin eval "function tmp_block_8182782 {${__brish3_nl}${BRISH3_CMD}${__brish3_nl}}" &&
            __brish_tb=1 __brish_trap=unsetopt __brish_trap_arg=xtrace &&
            tmp_block_8182782 <&$__brish3_empty >&$__brish3_out 2>&$__brish3_err
        fi
      fi
    done
  } always {
    #: Runs after normal completion, shell errors (NOMATCH and the like),
    #: err_return unwinding and interrupts. While `exit` unwinds, $? is 0
    #: here and the EXIT trap reports the status instead.
    BRISH3_RET=${__brish_int:-$?} BRISH3_NONCE=$1 TRY_BLOCK_ERROR=0 TRY_BLOCK_INTERRUPT=0 __brish_trap= __brish_trap_arg=
    builtin unsetopt err_exit err_return
    #: The trap turned posix_builtins off where it ran. Whether that was the
    #: command's global setting or one that a scope (`emulate sh -c`) has
    #: undone since, it is put back as it was before the command.
    if [[ -n $__brish_pb ]]; then
      if [[ -n $__brish_pb0 ]]; then
        builtin setopt posix_builtins
      else
        builtin unsetopt posix_builtins
      fi
    fi
    [[ ${(P)__brish_trapref-} == "$__brish_trapid" ]] || brish3_deftrap
    #: `unfunction` frees the body with signals held back; redefining it
    #: in the next eval would free it where a SIGINT can run the trap, and
    #: zsh crashes when a trap runs inside free() (see trapint.zsh).
    if [[ -n $__brish_tb ]]; then
      __brish_tb=
      builtin unfunction tmp_block_8182782
    fi
  }
  builtin true  # a failing status must not reach the user's ZERR trap here
}

function brish3_on_exit {  # $1: exit status
  __brish_trap= __brish_trap_arg=  # a SIGINT must not cut this short
  builtin emulate -LR zsh
  if [[ -n $BRISH3_INREQ && -n $BRISH3_NONCE ]]; then
    builtin syswrite -o $__brish3_out -- "${__brish3_endp}${BRISH3_NONCE}:${1}:exit${__brish3_nl}"
    builtin syswrite -o $__brish3_err -- "${__brish3_endp}${BRISH3_NONCE}:${1}:exit${__brish3_nl}"
  fi
}

#: The serve loop. END is written here, after brish3_run returns: never in
#: `always`, and never with brish3_run inside `||` or `if`, which would
#: disable err_exit for the user's command.
function brish3_serve {
  if [[ $__brish3_aliases == on ]]; then
    builtin setopt aliases  # user code keeps its aliases
  fi
  while [[ -z $BRISH3_EOF ]]; do
    while brish3_recv; do
      BRISH3_INREQ=1
      brish3_run "$BRISH3_NONCE" "$BRISH3_FORK" "$BRISH3_STDIN_MODE"
      builtin syswrite -o $__brish3_out -- "${__brish3_endp}${BRISH3_NONCE}:${BRISH3_RET}${__brish3_nl}"
      builtin syswrite -o $__brish3_err -- "${__brish3_endp}${BRISH3_NONCE}:${BRISH3_RET}${__brish3_nl}"
      BRISH3_INREQ=
    done
    #: A user `break 2` lands here with the request still open.
    if [[ -n $BRISH3_INREQ ]]; then
      builtin syswrite -o $__brish3_out -- "${__brish3_endp}${BRISH3_NONCE}:${BRISH3_RET}${__brish3_nl}"
      builtin syswrite -o $__brish3_err -- "${__brish3_endp}${BRISH3_NONCE}:${BRISH3_RET}${__brish3_nl}"
      BRISH3_INREQ=
    fi
  done
}

brish3_parse_argv "$@" || builtin exit 64

#: Fork the workers at top level, so that each inherits the options and state
#: the startup files left behind. `$brish_server_index` is 1-based, as in
#: brish2.zsh.
typeset -g brish_server_index
for (( brish_server_index = 1; brish_server_index <= $#__brish3_specs; brish_server_index++ )); do
  (
    builtin trap 'brish3_on_exit $?' EXIT
    __brish_level=$ZSH_SUBSHELL
    if [[ -n $__brish_session ]]; then
      brish3_deftrap  # the worker's TRAPINT, for good; see brish3_run
    else
      #: Older Python: SIGINT stays ignored (a background job ignores it),
      #: and the worker never sets an INT trap: `always` finds nothing to
      #: put back.
      __brish_trapref=__brish_trapid
    fi
    brish3_setup $brish_server_index
    brish3_serve
  ) &
  __brish3_pids+=( $! )
done
brish3_close_specs 0
#: A SIGINT to the whole process group must not kill the bootstrap: one that
#: a command sends (`kill -INT 0`), or under older Python a terminal Ctrl-C.
builtin trap '' INT

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
#: @duplicateCode/f2d464e1ad6f4e3eaedeeb994de39d42 __brish2_stop_workers in brish2.zsh
function brish3_stop_workers {  # $@: the workers' PIDs
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

function brish3_bootstrap_wait {
  builtin emulate -LR zsh
  local x
  #: Stay until Python goes away (it holds our stdin), then stop the workers.
  #: `read`, not `sysread`: zsh reaps dead workers while blocked in `read`,
  #: but not in `sysread`, and Python's liveness check cannot see zombies.
  while IFS= builtin read -r x; do
    builtin true
  done
  brish3_stop_workers $__brish3_pids
  builtin wait
}
brish3_bootstrap_wait
