"""Annotation parsing.

CDS lets the same annotation be written several ways::

    @Analytics.dataExtraction.enabled: true
    @Analytics: { dataExtraction: { enabled: true } }
    @Analytics.dataExtraction: { enabled: true }

All three mean the same thing, and a real view often mixes them. The parser
normalises every form into one nested dictionary with lower-cased keys, so the
rule engine asks one question — ``tree.get("analytics.dataextraction.enabled")``
— regardless of how the developer chose to write it.

Annotation names are case-insensitive in ABAP CDS; annotation *values* are not,
so string and enum values keep their original case.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

from cdcforge.model import SourceRef
from cdcforge.parsing.lexer import Cursor, TokenKind


class AnnotationError(Exception):
    """Malformed annotation syntax."""

    def __init__(self, message: str, ref: SourceRef) -> None:
        super().__init__(f"{message} (line {ref.line})")
        self.message = message
        self.ref = ref


@dataclass(frozen=True)
class EnumValue:
    """An annotation enum such as ``#MAIN``.

    Kept distinct from the string ``'MAIN'`` on purpose: ``role: #MAIN`` and
    ``role: 'MAIN'`` are not interchangeable, and a validator that conflated
    them would pass DDL the framework rejects.
    """

    name: str

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"#{self.name}"

    def matches(self, name: str) -> bool:
        return self.name.upper() == name.upper()


class AnnotationObject(dict):
    """A ``{ ... }`` annotation value.

    A dict (lower-cased keys) that also remembers where it was written, so a
    rule can point at the exact offending entry of a CDC mapping array rather
    than at the whole annotation block.
    """

    def __init__(self, *args: Any, ref: SourceRef | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.ref = ref or SourceRef()
        self.key_refs: dict[str, SourceRef] = {}


def _set_path(target: dict, path: list[str], value: Any) -> None:
    """Assign ``value`` at a dotted path, merging objects rather than replacing."""
    node = target
    for part in path[:-1]:
        existing = node.get(part)
        if not isinstance(existing, dict):
            existing = AnnotationObject()
            node[part] = existing
        node = existing
    leaf = path[-1]
    existing = node.get(leaf)
    if isinstance(existing, dict) and isinstance(value, dict):
        _merge(existing, value)
    else:
        node[leaf] = value


def _merge(base: dict, incoming: dict) -> None:
    for key, value in incoming.items():
        existing = base.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            _merge(existing, value)
        else:
            base[key] = value


class AnnotationTree:
    """A normalised annotation block."""

    def __init__(self) -> None:
        self.values: AnnotationObject = AnnotationObject()
        self.locations: dict[str, SourceRef] = {}

    # -- construction -----------------------------------------------------
    def assign(self, path: list[str], value: Any, ref: SourceRef) -> None:
        lowered = [p.lower() for p in path]
        _set_path(self.values, lowered, value)
        # Record a location for the full path and for each prefix, so a rule can
        # cite the nearest known position even when it asks about a parent node.
        for i in range(1, len(lowered) + 1):
            self.locations.setdefault(".".join(lowered[:i]), ref)
        self.locations[".".join(lowered)] = ref

    # -- lookup -----------------------------------------------------------
    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.values
        for part in path.lower().split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def has(self, path: str) -> bool:
        sentinel = object()
        return self.get(path, sentinel) is not sentinel

    def is_true(self, path: str) -> bool:
        return self.get(path) is True

    def ref(self, path: str) -> SourceRef:
        """Position of an annotation, falling back to the nearest ancestor."""
        parts = path.lower().split(".")
        for i in range(len(parts), 0, -1):
            found = self.locations.get(".".join(parts[:i]))
            if found is not None:
                return found
        return SourceRef()

    def paths(self) -> Iterator[str]:
        """Every leaf path in the tree, dotted and lower-cased."""

        def walk(node: Any, prefix: list[str]) -> Iterator[str]:
            if isinstance(node, dict):
                for key, value in node.items():
                    yield from walk(value, [*prefix, key])
            else:
                yield ".".join(prefix)

        yield from walk(self.values, [])

    def __bool__(self) -> bool:
        return bool(self.values)

    def __repr__(self) -> str:  # pragma: no cover - display helper
        return f"AnnotationTree({self.values!r})"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_annotation(cursor: Cursor, tree: AnnotationTree) -> None:
    """Parse one ``@...`` annotation from the cursor into ``tree``.

    The cursor must be positioned on the ``@``.
    """
    at = cursor.advance()
    if at.kind is not TokenKind.AT:  # pragma: no cover - guarded by callers
        raise AnnotationError("expected '@'", at.ref())

    # '@<' ... '>' is the pre-annotation form used before an element; the name
    # follows the same grammar, so just skip the bracket if present.
    cursor.accept_punct("<")

    path = _parse_path(cursor)
    ref = at.ref(".".join(path))

    if cursor.accept_punct(":"):
        value = _parse_value(cursor)
    else:
        # A bare annotation means true: '@Semantics.calculated' == ': true'.
        value = True

    tree.assign(path, value, ref)


def parse_annotation_block(cursor: Cursor) -> AnnotationTree:
    """Consume every consecutive annotation at the cursor."""
    tree = AnnotationTree()
    while cursor.current.kind is TokenKind.AT:
        parse_annotation(cursor, tree)
    return tree


def _parse_path(cursor: Cursor) -> list[str]:
    token = cursor.current
    if token.kind is not TokenKind.IDENT:
        raise AnnotationError("expected an annotation name", token.ref())
    path = [cursor.advance().text]
    while cursor.at_punct("."):
        cursor.advance()
        nxt = cursor.current
        if nxt.kind is not TokenKind.IDENT:
            raise AnnotationError("expected a name after '.'", nxt.ref())
        path.append(cursor.advance().text)
    return path


def _parse_value(cursor: Cursor) -> Any:
    token = cursor.current

    if token.kind is TokenKind.STRING:
        cursor.advance()
        return token.text

    if token.kind is TokenKind.ENUM:
        cursor.advance()
        return EnumValue(token.text)

    if token.kind is TokenKind.NUMBER:
        cursor.advance()
        return float(token.text) if "." in token.text else int(token.text)

    if token.is_punct("-") and cursor.peek(1).kind is TokenKind.NUMBER:
        cursor.advance()
        number = cursor.advance()
        return -(float(number.text) if "." in number.text else int(number.text))

    if token.kind is TokenKind.IDENT:
        lowered = token.upper
        if lowered in ("TRUE", "FALSE", "NULL"):
            cursor.advance()
            return {"TRUE": True, "FALSE": False, "NULL": None}[lowered]
        # An unquoted word here is not legal CDS annotation syntax. Rather than
        # silently accepting it, surface it — an unparseable annotation must not
        # become an assumed-absent annotation.
        raise AnnotationError(
            f"unquoted annotation value {token.text!r}; expected a literal, "
            f"#enum, [array] or {{object}}",
            token.ref(),
        )

    if token.is_punct("#") and cursor.peek(1).is_punct("("):
        return _parse_enum_collection(cursor)

    if token.is_punct("["):
        return _parse_array(cursor)

    if token.is_punct("{"):
        return _parse_object(cursor)

    raise AnnotationError(f"unexpected annotation value {token.text!r}", token.ref())


def _parse_enum_collection(cursor: Cursor) -> list:
    """``#('A', 'B')`` — the enum-collection annotation value.

    Found in SAP's delivered content on
    ``@AccessControl.personalData.blocking``. No CDC annotation uses this form,
    so the members are kept as-is; the point is that an unfamiliar-but-valid
    construct must not make a whole view UNPARSEABLE.
    """
    cursor.advance()  # '#'
    cursor.advance()  # '('
    items: list = []
    if cursor.accept_punct(")"):
        return items
    while True:
        items.append(_parse_value(cursor))
        if cursor.accept_punct(","):
            if cursor.accept_punct(")"):
                return items
            continue
        closing = cursor.current
        if not cursor.accept_punct(")"):
            raise AnnotationError(
                "expected ',' or ')' in an enum collection", closing.ref()
            )
        return items


def _parse_array(cursor: Cursor) -> list:
    cursor.advance()  # '['
    items: list = []
    if cursor.accept_punct("]"):
        return items
    while True:
        items.append(_parse_value(cursor))
        if cursor.accept_punct(","):
            # Tolerate a trailing comma before the closing bracket.
            if cursor.accept_punct("]"):
                return items
            continue
        closing = cursor.current
        if not cursor.accept_punct("]"):
            raise AnnotationError("expected ',' or ']' in annotation array", closing.ref())
        return items


def _parse_object(cursor: Cursor) -> AnnotationObject:
    open_brace = cursor.advance()  # '{'
    obj = AnnotationObject(ref=open_brace.ref())
    if cursor.accept_punct("}"):
        return obj
    while True:
        key_token = cursor.current
        path = _parse_path(cursor)
        colon = cursor.current
        if not cursor.accept_punct(":"):
            # A bare member inside an object also means true.
            value: Any = True
            if not (colon.is_punct(",") or colon.is_punct("}")):
                raise AnnotationError(
                    "expected ':' after annotation key", colon.ref()
                )
        else:
            value = _parse_value(cursor)

        lowered = [p.lower() for p in path]
        _set_path(obj, lowered, value)
        obj.key_refs[lowered[0]] = key_token.ref(".".join(path))

        if cursor.accept_punct(","):
            if cursor.accept_punct("}"):
                return obj
            continue
        closing = cursor.current
        if not cursor.accept_punct("}"):
            raise AnnotationError("expected ',' or '}' in annotation object", closing.ref())
        return obj
