"""Pure-Python quoting of arbitrary bytes as one zsh word.

`zsh_quote_bytes` replaces the round trip through `${(q+)...}` that Brish
used before. That flag mis-escapes some bytes and code points (0x1C, 0x9C,
0xA7, 0xDC; U+001C, U+0080 to U+009F, U+00AD), and the broken escape can
swallow or supply the closing quote, so text after it was parsed as shell code.

The rules, in order:

- An empty value becomes `''`.
- A value that contains a control character, DEL, invalid UTF-8 or a
  non-printable code point becomes one `$'...'` word. Inside it `\\`, `'`,
  newline and tab are backslash-escaped, printable characters stay literal and
  every other byte is written as `\\xHH`.
- Otherwise a value that contains a character special to zsh is single-quoted
  in runs, with each `'` written as `\\'`.
- Otherwise the value is emitted as is.

For printable valid UTF-8 this is the same text `(q+)` produces, so existing
command strings do not change. The result is always a surrogate-free `str`.
With `ascii_only=True` it is pure ASCII; use that whenever the command text
will not be encoded as UTF-8.

The value survives zsh's default options, `rc_quotes`, `no_multibyte`,
`extended_glob`, `glob_subst` and the C locale. NUL does not survive under
`emulate sh`: its `posix_strings` option ends a `$'...'` string at a NUL.
"""

import re

__all__ = ["zsh_quote_bytes"]

#: zsh's special characters (utils.c), minus the control characters, which
#: already force the `$'...'` form.
_SPECIAL = re.compile(r"""[#$^*()=|{}\[\]`<>?~;& \\'"]""")
#: Fast path: a value made only of these is always emitted bare.
_BARE = re.compile(r"[A-Za-z0-9_./,:@%+!-]+")


def _dollar_escape(s, ascii_only):
    out = []
    append = out.append
    for ch in s:
        o = ord(ch)
        if 0x20 <= o < 0x7F:
            if ch == "\\":
                append("\\\\")
            elif ch == "'":
                append("\\'")
            else:
                append(ch)
        elif ch == "\n":
            append("\\n")
        elif ch == "\t":
            append("\\t")
        elif 0xDC80 <= o <= 0xDCFF:
            #: A raw byte that was not valid UTF-8 (surrogateescape).
            append("\\x%02x" % (o - 0xDC00))
        elif o < 0x80:
            append("\\x%02x" % o)
        elif not ascii_only and ch.isprintable():
            append(ch)
        else:
            append("".join("\\x%02x" % c for c in ch.encode("utf-8")))
    return "$'" + "".join(out) + "'"


def zsh_quote_bytes(value, ascii_only=False):
    """Quote the bytes `value` as a single zsh word and return it as a `str`.

    zsh reads the word back as exactly `value`. A `str` argument is taken as
    UTF-8 with surrogateescape.
    """
    if isinstance(value, str):
        value = value.encode("utf-8", "surrogateescape")
    elif not isinstance(value, bytes):
        value = bytes(value)
    if not value:
        return "''"
    s = value.decode("utf-8", "surrogateescape")
    if _BARE.fullmatch(s):
        return s
    #: `isprintable` is False for control characters, DEL, lone surrogates
    #: (the undecodable bytes) and every other non-printable code point.
    if not s.isprintable() or (ascii_only and not s.isascii()):
        return _dollar_escape(s, ascii_only)
    if not _SPECIAL.search(s):
        return s
    return "\\'".join("'" + part + "'" if part else "" for part in s.split("'"))
