# Installation

`pip install -U brish`

# Usage

```
from brish import Brish
bsh = Brish()
w = 'World!'
v = '$SOME_VAR'
t = True
f = False
return_code, stdout , stderr = bsh.z('''
echo monkey {z("curl -s https://www.poemist.com/api/v1/randompoems | jq --raw-output '.[0].content'")} end | sed -e 's/monkey/start/'
echo --------------
echo {w} --- {w!r}
echo {23:f}
SOME_VAR='Hi!'
echo {v}
echo {v!e} # Not quoted
test -z {t!b} && echo 'true!'
test -z {f!b} && echo 'false!'
''')
```
