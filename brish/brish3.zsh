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

typeset -g BRISH3_NONCE= BRISH3_FORK=0 BRISH3_STDIN_MODE= BRISH3_INREQ= BRISH3_EOF=
typeset -g BRISH3_CMD= BRISH3_RET=0 brish_stdin= cmd=
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

#: Run the request. No locals and no emulate here: user code runs inside and
#: must see the user's options. The nonce is kept only in $1 while user code
#: runs; `always` restores it from there.
function brish3_run {  # $1 nonce, $2 fork (0|1), $3 stdin mode (empty|null|data)
  BRISH3_NONCE=
  {
    repeat 1 do  # absorbs a bare break or continue
      if [[ $2 == 1 ]]; then
        if [[ $3 == data ]]; then
          ( builtin set --; { builtin trap '' PIPE; builtin print -rn -- "$brish_stdin"; builtin true } 2>/dev/null | builtin eval "$BRISH3_CMD" ) >&$__brish3_out 2>&$__brish3_err
        elif [[ $3 == null ]]; then
          ( builtin set --; builtin eval "$BRISH3_CMD" ) </dev/null >&$__brish3_out 2>&$__brish3_err
        else
          ( builtin set --; builtin eval "$BRISH3_CMD" ) <&$__brish3_empty >&$__brish3_out 2>&$__brish3_err
        fi
      else
        #: A function body lets the command `return`. `functions[...]=` would
        #: corrupt NUL and the bytes 0x83 to 0x9D, so the body goes through
        #: eval. `&&`: after a syntax error the old body must not run again.
        if [[ $3 == data ]]; then
          builtin eval "function tmp_block_8182782 {${__brish3_nl}${BRISH3_CMD}${__brish3_nl}}" &&
            { builtin trap '' PIPE; builtin print -rn -- "$brish_stdin"; builtin true } 2>/dev/null | tmp_block_8182782 >&$__brish3_out 2>&$__brish3_err
        elif [[ $3 == null ]]; then
          builtin eval "function tmp_block_8182782 {${__brish3_nl}${BRISH3_CMD}${__brish3_nl}}" &&
            tmp_block_8182782 </dev/null >&$__brish3_out 2>&$__brish3_err
        else
          builtin eval "function tmp_block_8182782 {${__brish3_nl}${BRISH3_CMD}${__brish3_nl}}" &&
            tmp_block_8182782 <&$__brish3_empty >&$__brish3_out 2>&$__brish3_err
        fi
      fi
    done
  } always {
    #: Runs after normal completion, shell errors (NOMATCH and the like) and
    #: err_return unwinding. While `exit` unwinds, $? is 0 here and the EXIT
    #: trap reports the status instead.
    BRISH3_RET=$? BRISH3_NONCE=$1
    TRY_BLOCK_ERROR=0
    builtin unsetopt err_exit err_return
  }
  builtin true  # a failing status must not reach the user's ZERR trap here
}

function brish3_on_exit {  # $1: exit status
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
    brish3_setup $brish_server_index
    brish3_serve
  ) &
  __brish3_pids+=( $! )
done
brish3_close_specs 0

function brish3_bootstrap_wait {
  builtin emulate -LR zsh
  local x
  #: Stay until Python goes away (it holds our stdin), then stop the workers.
  #: `read`, not `sysread`: zsh reaps dead workers while blocked in `read`,
  #: but not in `sysread`, and Python's liveness check cannot see zombies.
  while IFS= builtin read -r x; do
    builtin true
  done
  builtin kill -TERM $__brish3_pids 2>/dev/null
  builtin wait
}
brish3_bootstrap_wait
