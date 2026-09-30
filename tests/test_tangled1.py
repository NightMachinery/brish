from brish import z, zp, Brish

NI = True

name="A$ron"
z("echo Hello {name}")

def test1():
    assert z("echo Hello {name}").outrs == "Hello A$ron"
    return True
NI or test1()

alist = ["# Fruits", "1. Orange", "2. Rambutan", "3. Strawberry"]
z("for i in {alist} ; do echo $i ; done")

def test2():
    assert z("for i in {alist} ; do echo $i ; done").outrs == """# Fruits
1. Orange
2. Rambutan
3. Strawberry"""
NI or test2()

if z("test -e ~/"):
    print("HOME exists!")
else:
    print("We're homeless :(")

def test3():
    assert z("test -e ~/")
NI or test3()
