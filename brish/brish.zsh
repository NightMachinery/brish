#!/usr/bin/env zsh
# MARKER=$'\0'"BRISH_MARKER"
MARKER=$'\0'
while IFS= read -d "$MARKER" -r cmd
do
    IFS= read -d "$MARKER" -r brish_stdin
    IFS= read -d "$MARKER" -r brish_fork
    if test -n "$brish_fork" ; then
        (print -nr -- "$brish_stdin" | eval "$cmd")
    else
        print -nr -- "$brish_stdin" | eval "$cmd"
    fi
    # Note that exiting the shell can fail brish.py if not in the subshell.
    local ret=$?
    print -nr -- $'\n'"$MARKER"$'\n'
    print -r -- $ret
    print -nr -- $'\n'"$MARKER"$'\n' >&2
done
