"""
Every string the operator can be shown has to be ASCII.

The runtime check in test_end_to_end only sees strings on the paths a test
actually walks, which is how an em-dash survived in the reconnect message and
twelve more in preflight.py: one is an error branch, the other a script no test
invokes. This reads the source instead, so an unexercised line is covered too.

Comments and docstrings are deliberately left alone - they never reach a
console or a log file, and prose reads better with real punctuation.
"""
import ast

import pytest
from conftest import ROOT

# Characters a UTF-8 file picks up naturally but PowerShell's Get-Content,
# reading ANSI, renders as mojibake. The value is what to write instead.
SUBSTITUTES = {
    "\u2014": "-",
    "\u2013": "-",
    "\u2026": "...",
    "\u2018": "'",
    "\u2019": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u00a0": "a plain space",
}

MODULES = sorted(ROOT.glob("*.py"))

HAS_BODY = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _docstring_nodes(tree):
    """The Constant nodes that are docstrings, by identity."""
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, HAS_BODY):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            found.add(id(first.value))
    return found


def runtime_literals(path):
    """
    Yield (lineno, text) for every string literal that is not a docstring.

    This reads the AST rather than the token stream, which an earlier version
    did and got wrong twice. Tokens make three distinctions that have to be
    hand-rolled and are easy to get backwards:

      - A module docstring sits at token index 0, because generate_tokens()
        emits no ENCODING token.
      - PEP 701 (Python 3.12) splits an f-string into FSTRING_START / MIDDLE /
        END, so matching only STRING goes blind to every f-string on 3.12 while
        still passing on 3.11.
      - Implicit concatenation across lines inside parentheses puts an NL token
        before the second piece, which looks exactly like the start of a
        statement - so a "preceded by NL means docstring" rule silently skips
        the tail of a wrapped message.

    The AST knows a docstring exactly, and gives the pieces of an f-string as
    plain Constants on every supported version, so none of that applies.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    skip = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in skip):
            yield node.lineno, node.value


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_runtime_strings_are_ascii(module):
    offenders = []
    for lineno, value in sorted(runtime_literals(module)):
        for char in sorted({c for c in value if ord(c) > 127}):
            fix = SUBSTITUTES.get(char, "an ASCII equivalent")
            offenders.append(
                f"{module.name}:{lineno}: {char!r} (U+{ord(char):04X}) "
                f"-> write {fix!r}: {value.strip()[:70]!r}"
            )
    assert not offenders, (
        "non-ASCII in a string the operator may be shown; PowerShell renders "
        "these as mojibake:\n  " + "\n  ".join(offenders)
    )


def _scan(tmp_path, name, source):
    p = tmp_path / name
    p.write_text(source, encoding="utf-8")
    return [v for _, v in runtime_literals(p)]


def test_docstrings_are_skipped_and_messages_are_not(tmp_path):
    """
    The check is only worth having if it reads the source correctly: a
    docstring must be skipped and a message must not be. Without this, a
    scanner that silently matched nothing would look like a passing suite.
    """
    found = _scan(tmp_path, "sample.py",
                  '"""A module docstring \u2014 left alone."""\n'
                  "class C:\n"
                  '    """A class docstring \u2014 left alone."""\n'
                  "    def f(self):\n"
                  '        """A method docstring \u2014 left alone."""\n'
                  '        print("a message \u2014 flagged")\n'
                  '        return "plain ascii"\n')
    # ast.walk is breadth-first, so the order carries no meaning here.
    assert sorted(found) == sorted(["a message \u2014 flagged",
                                    "plain ascii"])


def test_f_strings_are_read(tmp_path):
    """
    An f-string is one STRING token up to 3.11 and three or more from 3.12
    (PEP 701). Reading the AST makes the versions agree; this pins it, because
    the token-based version passed on 3.11 and checked nothing on 3.12.
    """
    found = _scan(tmp_path, "f.py",
                  "def f(x):\n"
                  '    print(f"before \u2014 {x} after")\n')
    assert any("\u2014" in v for v in found)


def test_the_tail_of_a_wrapped_message_is_read(tmp_path):
    """
    Two em-dashes in preflight.py hid here: inside parentheses the line break
    before the second piece of an implicitly concatenated string emits an NL
    token, which the old token rule mistook for the start of a statement.
    """
    found = _scan(tmp_path, "wrapped.py",
                  "def f(x):\n"
                  '    print(f"first line {x} "\n'
                  '          f"second line \u2014 flagged")\n')
    assert any("\u2014" in v for v in found)
