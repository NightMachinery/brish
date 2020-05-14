# Installation

`pip install -U brish`

Or to install the latest version:

`pip install git+https://github.com/NightMachinary/brish`


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
''')
```
