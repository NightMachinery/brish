#!/usr/bin/env zsh
# MARKER=$'\0'"BRISH_MARKER"
MARKER=$'\0'

IFS= read -d "$MARKER" -r BRISH_STDIN
IFS= read -d "$MARKER" -r BRISH_STDOUT
IFS= read -d "$MARKER" -r BRISH_STDERR
stdins=(${(@f)BRISH_STDIN})
stdouts=(${(@f)BRISH_STDOUT})
stderrs=(${(@f)BRISH_STDERR})

local brish_server_index
for brish_server_index in {1..${#stdins}} ; do
    (
        while IFS= read -d "$MARKER" -r cmd
        do
            IFS= read -d "$MARKER" -r brish_stdin
            IFS= read -d "$MARKER" -r brish_fork
            if test -n "$brish_fork" ; then
                ( { ( print -nr -- "$brish_stdin" ) || true }  | eval "$cmd" )
            else
                ##
                # { ( print -nr -- "$brish_stdin" ) || true } | eval "$cmd"
                ##
                # Running the code wrapped in a function block has a lot of benefits, e.g., we can use 'return' freely.
                functions[tmp_block_8182782]="$cmd"
                { ( print -nr -- "$brish_stdin" ) || true } | tmp_block_8182782
                ##
            fi
            # Note that exiting the shell can fail brish.py if not in the subshell.
            local ret=$?
            print -nr -- $'\n'"$MARKER"$'\n'
            print -r -- $ret
            print -nr -- $'\n'"$MARKER"$'\n' >&2
        done
    ) < $stdins[$brish_server_index] >> $stdouts[$brish_server_index] 2>> $stderrs[$brish_server_index] &
done

wait
