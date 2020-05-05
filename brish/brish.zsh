#!/usr/bin/env zsh
while IFS= read -d "" -r cmd
do
    IFS= read -d "" -r brish_stdin
    IFS= read -d "" -r brish_fork
    if test -n "$brish_fork" ; then
        (print -nr -- "$brish_stdin" | eval "$cmd")
    else
        print -nr -- "$brish_stdin" | eval "$cmd"
    fi
    # Note that exiting the shell can fail brish.py if not in the subshell.
    local ret=$?
    print -n $'\n\0\n'
    print -r -- $ret
    print -n $'\n\0\n' >&2
done
