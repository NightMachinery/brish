__version__ = '0.1.5'

import sys
import ast
from string import Formatter
import uuid
import random
from subprocess import Popen, PIPE, STDOUT
from plumbum import local
from IPython import embed
import pathlib
from dataclasses import dataclass

def idem(x):
    return x

def boolsh(some_bool):
    if some_bool:
        return 'y'
    else:
        return ''

@dataclass(frozen=True)
class CmdResult:
    retcode: int
    out: str
    err: str

    def toTuple(self):
        return self.retcode, self.out, self.err

    def __iter__(self):
        return iter(self.toTuple())

    def __getitem__(self, index):
        return self.toTuple()[index]

    def __str__(self):
        return self.out


class Brish:

    # self.MARKER = str(uuid.uuid4())
    MARKER = '\x00'
    MLEN = len(MARKER)

    def __init__(self, defaultShell=None):
        self.defaultShell = defaultShell or str(pathlib.Path(__file__).parent / 'brish.zsh')
        self.p = None

    def init(self, shell=None):
        if shell is None:
            shell = self.defaultShell
        self.p = Popen(shell, stdin=PIPE, stdout=PIPE, stderr=PIPE,
                universal_newlines=True) # decode output as utf-8, newline is '\n'

    def zsh_quote(self, str):
        e, out, err = self.send_cmd('print -rn -- "${(q+@)brish_stdin}"', cmd_stdin=str)
        return out

    def send_cmd(self, cmd, cmd_stdin="", fork=False):
        delim = self.MARKER + '\n'
        if self.p is None:
            self.init()
        print(cmd + self.MARKER + cmd_stdin + self.MARKER + boolsh(fork) + self.MARKER, file=self.p.stdin, flush=True)
        stdout = ""
        # embed()
        for line in iter(self.p.stdout.readline, delim):
            # if line.endswith(delim):
                # line = line[:-self.MLEN-1]
            stdout += line
        stdout = stdout[:-1]
        return_code = int(self.p.stdout.readline())
        stderr = ""
        for line in iter(self.p.stderr.readline, delim):
            # if line.endswith(delim):
                # line = line[:-self.MLEN-1]
            stderr += line
        stderr = stderr[:-1]
        return CmdResult(return_code, stdout, stderr)

    def cleanup(self):
        if self.p is None:
            return
        self.p.stdout.close()
        if self.p.stderr:
            self.p.stderr.close()
        self.p.stdin.close()
        self.p.wait()
        self.p = None

    

    _conversions = {'a': ascii, 'r': repr, 's': str, 'e': idem, 'b': boolsh}

    def zstring_old(self, template, locals_=None):
        # DEPRECATED
        if locals_ is None:
            previous_frame = sys._getframe(1)
            previous_frame_locals = previous_frame.f_locals
            locals_ = previous_frame_locals
            # locals_ = globals()
        result = []
        parts = Formatter().parse(template)
        for part in parts:
            literal_text, field_name, format_spec, conversion = part
            # print(part)
            if literal_text:
                result.append(literal_text)
            if not field_name:
                continue
            value = eval(field_name, locals_) #.__format__()
            if conversion:
                value = self._conversions[conversion](value)
            if format_spec:
                value = format(value, format_spec)
            else:
                value = str(value)
            if conversion != 'e':
                value = self.zsh_quote(value)
            result.append(value)
        cmd = ''.join(result)
        return cmd

    def zstring(self, template, locals_=None, getframe=1):
        if locals_ is None:
            previous_frame = sys._getframe(getframe)
            previous_frame_locals = previous_frame.f_locals
            locals_ = previous_frame_locals

        def asteval(astNode):
            if astNode is not None:
                return eval(compile(ast.Expression(astNode), filename='<string>', mode='eval'), locals_)
            else:
                return None

        def eatFormat(format_spec, code):
            res = False
            if format_spec:
                flags = format_spec.split(':')
                res = code in flags
                format_spec = list(filter(lambda a: a != code,flags))
            return ':'.join(format_spec), res


        p = ast.parse(f"f''' {template} '''")
        result = []
        parts = p.body[0].value.values
        for part in parts:
            typ = type(part)
            if typ is ast.Str:
                result.append(part.s)
            elif typ is ast.FormattedValue:
                # print(part.__dict__)

                value = asteval(part.value)
                conversion = part.conversion
                if conversion >= 0:
                    # parser doesn't support custom conversions, but this code works:
                    conversion = chr(conversion)
                    value = self._conversions[conversion](value)
                # if part.format_spec:
                #     value = format(value, asteval(part.format_spec))
                # else:
                #     value = str(value)
                # if conversion != 'e':
                #     value = self.zsh_quote(value)
                # embed()
                
                format_spec = asteval(part.format_spec) or ''
                # print(f"orig format: {format_spec}")
                format_spec, fmt_eval = eatFormat(format_spec, 'e')
                format_spec, fmt_bool = eatFormat(format_spec, 'bool')
                # print(f"format: {format_spec}")
                if format_spec:
                    value = format(value, format_spec)
                if fmt_bool:
                    value = boolsh(value)

                value = str(value)
                if not fmt_eval:
                    value = self.zsh_quote(value)
                result.append(value)
        cmd = ''.join(result)
        return cmd




    def z(self, template, locals_=None):
        return self.send_cmd(self.zstring(template, locals_=locals_, getframe=2))
    ## Aliases
    c = send_cmd
    q = zsh_quote


