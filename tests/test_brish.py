def test2():
    import threading

    def worker(c):
        zp('sleep {c} ; ec {c}')

    for i in range(10):
        t = threading.Thread(target=worker, args=(i,))
        t.start()

def test1():
    from brishmod import Brish
    bsh = Brish()
    w = 'World!'
    v = '$SOME_VAR'
    t = True
    f = False
    num = 12
    z = bsh.z
    res = z('''
    echo monkey$'\n\n'{z("curl -s https://www.poemist.com/api/v1/randompoems | jq --raw-output '.[0].content'")}$'\n'end | sed -e 's/monkey/start/'
    echo --------------
    echo {w} --- {w!r}
    echo {23:f}
    SOME_VAR='Hi!'
    echo {v}
    echo {v:e} # Not quoted
    echo This {'goes'} in stderr >&2
    test -n {t:bool} && echo 'true!'
    test -n {f:bool} && echo 'false!'
    (exit 12)
    ''')
    os.path.commonpath(z('realpath "${{(@f)$(exa)}}"'))
    list(z('''rpargs {iter(z('exa'))}''').iter0())
    # assert False
    return res

if __name__ == "__main__":
    import sys
    import os
    from IPython import embed
    sys.path.append(os.path.dirname(os.path.realpath(__file__)) + "/../brish/")
    res = test1()
    print(repr(res))
    print('')
    print(res.out)

