from brish import z, zp, Brish

NI = True

name="A$ron"
z("echo Hello {name}")

t1=""
def test1():
    assert t1 == "Hello A$ron"
    return True
NI or test1()

alist = ["# Fruits", "1. Orange", "2. Rambutan", "3. Strawberry"]
z("for i in {alist} ; do echo $i ; done")

t2="""Traceback (most recent call last):
  File \"<stdin>\", line 9, in <module>
  File \"<string>\", line 2, in <module>
NameError: name 'z' is not defined
"""
def test2():
    assert t2 == """# Fruits
1. Orange
2. Rambutan
3. Strawberry"""
NI or test2()

if z("test -e ~/"):
    print("HOME exists!")
else:
    print("We're homeless :(")

t3="""Traceback (most recent call last):
  File \"<stdin>\", line 1, in <module>
  File \"/var/folders/5v/g3zxt_7d64g3sd_56bzpqbvh0000gn/T/babel-JVsAap/python-BtgRvV\", line 1, in <module>
    if z(\"test -e ~/\"):
NameError: name 'z' is not defined"""
assert t3 == "HOME exists!"

for path in z("command ls ~/tmp/"): # `command` helps bypass potential aliases defined on `ls`
    zp("du -h ~/tmp/{path}") # zp prints the result
