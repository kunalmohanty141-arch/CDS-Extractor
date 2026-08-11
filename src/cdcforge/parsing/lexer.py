"""Tokenizer for ABAP CDS DDL.

A real tokenizer, not regex. The specification is explicit about why: the
parser must correctly handle line and block comments, string literals
containing SQL keywords, CASE WHEN blocks, nested expressions, $projection
self-references, association path expressions and annotations at header and
element level. A regex that searches the raw text for ``GROUP BY`` will happily
fire on a comment that says "no GROUP BY here" or on the literal
``'GROUP BY clause'`` — and a false PASS is far worse than a false FAIL.

Comments and whitespace are dropped from the emitted stream but their positions
are preserved on the surrounding tokens, so every rule can still cite a line.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass
from enum import Enum, auto

from cdcforge.model import SourceRef


class TokenKind(Enum):
    IDENT = auto()
    """Identifier or keyword. Also ``$projection``, ``$session``, ``/NS/NAME``."""

    NUMBER = auto()
    STRING = auto()
    """A quoted literal. ``text`` holds the *decoded* value, without quotes."""

    ENUM = auto()
    """An annotation enum value such as ``#MAIN``. ``text`` excludes the ``#``."""

    AT = auto()
    """The ``@`` that introduces an annotation."""

    PUNCT = auto()
    EOF = auto()


_IDENT_START = frozenset(string.ascii_letters + "_$")
_IDENT_CONT = frozenset(string.ascii_letters + string.digits + "_")

# Longest-first so that '..' is not lexed as '.' '.'
_PUNCT_MULTI = ("<=", ">=", "<>", "!=", "||", "..", "->")
_PUNCT_SINGLE = frozenset("{}()[],.:;+-*/<>=|&?")

# A namespaced object name: /1DH/OBSERVE_LOGTAB, /BOBF/OBJ. Must be recognised
# before '/' is treated as division, and after '//' has been handled as comment.
_NAMESPACE_RE = re.compile(r"/[A-Za-z0-9_]+/[A-Za-z][A-Za-z0-9_]*")


class LexError(Exception):
    """Raised when the source cannot be tokenized at all.

    Unterminated literals and unterminated block comments land here. The caller
    turns this into an UNPARSEABLE verdict — never a PASS.
    """

    def __init__(self, message: str, line: int, column: int) -> None:
        super().__init__(f"{message} (line {line}, col {column})")
        self.message = message
        self.line = line
        self.column = column


@dataclass(frozen=True)
class Token:
    kind: TokenKind
    text: str
    line: int
    column: int

    @property
    def upper(self) -> str:
        return self.text.upper()

    def is_word(self, *words: str) -> bool:
        """Case-insensitive keyword test. ABAP CDS keywords are case-insensitive."""
        return self.kind is TokenKind.IDENT and self.upper in {w.upper() for w in words}

    def is_punct(self, *puncts: str) -> bool:
        return self.kind is TokenKind.PUNCT and self.text in puncts

    def ref(self, snippet: str = "") -> SourceRef:
        return SourceRef(line=self.line, column=self.column, snippet=snippet or self.text)

    def __str__(self) -> str:  # pragma: no cover - display helper
        return self.text


def tokenize(source: str) -> list[Token]:
    """Tokenize CDS DDL. Comments and whitespace are consumed, not emitted."""
    tokens: list[Token] = []
    # DDL exported from Eclipse or written on Windows frequently carries a UTF-8
    # BOM. Refusing to parse a real, valid view over an invisible leading byte
    # would be an UNPARSEABLE verdict the user could never explain.
    if source.startswith("﻿"):
        source = source[1:]
    i = 0
    n = len(source)
    line = 1
    line_start = 0

    def col(pos: int) -> int:
        return pos - line_start + 1

    while i < n:
        ch = source[i]

        # --- whitespace -------------------------------------------------
        if ch == "\n":
            line += 1
            i += 1
            line_start = i
            continue
        if ch in " \t\r\f\v":
            i += 1
            continue
        # Unicode spaces, chiefly U+00A0. SAP's delivered DDL is full of them —
        # 28 of the 34 views that failed to parse across the 887 that S/4HANA
        # ships with CDC delta declared died on a single non-breaking space,
        # which is an UNPARSEABLE verdict on a perfectly good view over a
        # character nobody can see. Newlines are handled above, so anything
        # still reported as space here is safe to skip.
        if ch.isspace():
            i += 1
            continue

        # --- line comment -----------------------------------------------
        #
        # ABAP CDS DDL accepts both '//' and '--'. SAP's own delivered content
        # uses '--' heavily, and missing it is not a cosmetic gap: a comment
        # containing an apostrophe ("we don't use any key fields") opens a
        # string literal that never closes, and the whole view becomes
        # UNPARSEABLE. That is how I_MaterialDocumentRecord failed.
        if source.startswith("//", i) or source.startswith("--", i):
            j = source.find("\n", i)
            i = n if j < 0 else j
            continue

        # --- block comment ----------------------------------------------
        if source.startswith("/*", i):
            start_line, start_col = line, col(i)
            j = source.find("*/", i + 2)
            if j < 0:
                raise LexError("unterminated block comment", start_line, start_col)
            # Keep the line counter honest across a multi-line comment.
            for k in range(i, j):
                if source[k] == "\n":
                    line += 1
                    line_start = k + 1
            i = j + 2
            continue

        # --- string literal ---------------------------------------------
        if ch == "'":
            start_line, start_col = line, col(i)
            j = i + 1
            buf: list[str] = []
            while True:
                if j >= n:
                    raise LexError("unterminated string literal", start_line, start_col)
                c = source[j]
                if c == "\n":
                    # CDS literals do not span lines; treating this as an error
                    # is what stops one stray quote swallowing the whole file
                    # and silently hiding a GROUP BY from the rule engine.
                    raise LexError("unterminated string literal", start_line, start_col)
                if c == "'":
                    if j + 1 < n and source[j + 1] == "'":  # '' escape
                        buf.append("'")
                        j += 2
                        continue
                    j += 1
                    break
                buf.append(c)
                j += 1
            tokens.append(Token(TokenKind.STRING, "".join(buf), start_line, start_col))
            i = j
            continue

        # --- annotation enum value: #MAIN, or the #( ... ) collection -----
        if ch == "#":
            start_col = col(i)
            j = i + 1
            while j < n and source[j] in _IDENT_CONT:
                j += 1
            if j == i + 1:
                # Not a named enum. CDS also has an enum-collection form —
                # `personalData.blocking: #('TRANSACTIONAL_DATA')` — which SAP
                # uses in delivered content. Emit the '#' on its own and let
                # the following '(' be an ordinary bracket, so every
                # depth-tracking scan elsewhere keeps working unchanged.
                if j < n and source[j] == "(":
                    tokens.append(Token(TokenKind.PUNCT, "#", line, start_col))
                    i = j
                    continue
                raise LexError("'#' not followed by an enum name", line, start_col)
            tokens.append(Token(TokenKind.ENUM, source[i + 1 : j], line, start_col))
            i = j
            continue

        # --- annotation marker -------------------------------------------
        if ch == "@":
            tokens.append(Token(TokenKind.AT, "@", line, col(i)))
            i += 1
            continue

        # --- namespaced identifier ---------------------------------------
        if ch == "/":
            m = _NAMESPACE_RE.match(source, i)
            if m:
                tokens.append(Token(TokenKind.IDENT, m.group(0), line, col(i)))
                i = m.end()
                continue

        # --- number --------------------------------------------------------
        if ch.isdigit():
            start_col = col(i)
            j = i
            while j < n and source[j].isdigit():
                j += 1
            # A decimal point, but not the '..' of a cardinality [0..1].
            if j < n and source[j] == "." and not source.startswith("..", j):
                j += 1
                while j < n and source[j].isdigit():
                    j += 1
            tokens.append(Token(TokenKind.NUMBER, source[i:j], line, start_col))
            i = j
            continue

        # --- identifier ------------------------------------------------------
        if ch in _IDENT_START:
            start_col = col(i)
            j = i + 1
            while j < n and source[j] in _IDENT_CONT:
                j += 1
            tokens.append(Token(TokenKind.IDENT, source[i:j], line, start_col))
            i = j
            continue

        # --- punctuation -----------------------------------------------------
        two = source[i : i + 2]
        if two in _PUNCT_MULTI:
            tokens.append(Token(TokenKind.PUNCT, two, line, col(i)))
            i += 2
            continue
        if ch in _PUNCT_SINGLE:
            tokens.append(Token(TokenKind.PUNCT, ch, line, col(i)))
            i += 1
            continue

        raise LexError(f"unexpected character {ch!r}", line, col(i))

    end_line = line
    tokens.append(Token(TokenKind.EOF, "", end_line, col(i)))
    return tokens


class Cursor:
    """A positional reader over the token stream.

    Deliberately small. The parser is hand-written recursive descent and this is
    all the machinery it needs.
    """

    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.pos = 0

    # -- inspection -------------------------------------------------------
    def peek(self, offset: int = 0) -> Token:
        idx = min(self.pos + offset, len(self.tokens) - 1)
        return self.tokens[idx]

    @property
    def current(self) -> Token:
        return self.peek()

    @property
    def at_end(self) -> bool:
        return self.current.kind is TokenKind.EOF

    def at_word(self, *words: str) -> bool:
        return self.current.is_word(*words)

    def at_punct(self, *puncts: str) -> bool:
        return self.current.is_punct(*puncts)

    def at_sequence(self, *words: str) -> bool:
        """True when the next tokens are exactly these keywords, in order."""
        return all(self.peek(k).is_word(w) for k, w in enumerate(words))

    # -- consumption ------------------------------------------------------
    def advance(self) -> Token:
        token = self.current
        if token.kind is not TokenKind.EOF:
            self.pos += 1
        return token

    def accept_word(self, *words: str) -> Token | None:
        if self.at_word(*words):
            return self.advance()
        return None

    def accept_punct(self, *puncts: str) -> Token | None:
        if self.at_punct(*puncts):
            return self.advance()
        return None

    def accept_sequence(self, *words: str) -> bool:
        if self.at_sequence(*words):
            for _ in words:
                self.advance()
            return True
        return False
