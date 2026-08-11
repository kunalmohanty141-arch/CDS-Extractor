"""F-11 — the DDL parser.

Hand-written recursive descent over the token stream. It is deliberately
*tolerant about what it does not need* and *intolerant about what it does*:
an unrecognised clause in a tail position is recorded as a non-fatal issue,
but anything that would make the join list, projection list or annotation tree
untrustworthy is fatal.

Failure behaviour, straight from the specification:

    If the parser cannot produce a confident AST, the verdict is UNPARSEABLE.
    It is never PASS. This single decision is what separates a trustworthy tool
    from a dangerous one.

So this module never guesses. When it cannot tell, it says so.
"""

from __future__ import annotations

from functools import lru_cache

from cdcforge.model import ParseIssue, SourceRef
from cdcforge.parsing.annotations import (
    AnnotationError,
    parse_annotation_block,
)
from cdcforge.parsing.lexer import Cursor, LexError, Token, TokenKind, tokenize
from cdcforge.parsing.nodes import (
    Association,
    Cardinality,
    DataSource,
    EntityKind,
    FieldRef,
    JoinCardinality,
    JoinType,
    Parameter,
    ParsedView,
    SelectItem,
    SourceKind,
)

#: Functions whose presence anywhere in the stack is fatal for CDC (R-03).
AGGREGATE_FUNCTIONS = frozenset(
    {
        "SUM",
        "MIN",
        "MAX",
        "AVG",
        "COUNT",
        "STDDEV",
        "VAR",
        "MEDIAN",
        "CORR",
        "CORR_SPEARMAN",
        "GROUPING",
    }
)

#: Words that introduce a new top-level clause. Used to stop ON-condition and
#: expression scans without needing a full expression grammar.
_CLAUSE_BOUNDARIES = frozenset(
    {
        "INNER",
        "LEFT",
        "RIGHT",
        "CROSS",
        "FULL",
        "JOIN",
        "ASSOCIATION",
        "COMPOSITION",
        "WHERE",
        "GROUP",
        "HAVING",
        "UNION",
        "INTERSECT",
        "EXCEPT",
    }
)

#: Keywords that must never be mistaken for a field reference.
_RESERVED = frozenset(
    {
        "SELECT",
        "DISTINCT",
        "FROM",
        "AS",
        "ON",
        "KEY",
        "VIRTUAL",
        "CASE",
        "WHEN",
        "THEN",
        "ELSE",
        "END",
        "AND",
        "OR",
        "NOT",
        "IS",
        "NULL",
        "INITIAL",
        "BETWEEN",
        "LIKE",
        "ESCAPE",
        "IN",
        "CAST",
        "PRESERVING",
        "TYPE",
        "TRUE",
        "FALSE",
        "UNION",
        "ALL",
        "INTERSECT",
        "EXCEPT",
        "WHERE",
        "GROUP",
        "BY",
        "HAVING",
        "ORDER",
        "ASSOCIATION",
        "COMPOSITION",
        "TO",
        "OF",
        "ONE",
        "MANY",
        "EXACT",
        "REDIRECTED",
        "PARENT",
        "CHILD",
        "DEFINE",
        "VIEW",
        "ENTITY",
        "ROOT",
        "WITH",
        "PARAMETERS",
        "PROJECTION",
        "ABSTRACT",
        "CUSTOM",
        "TABLE",
        "FUNCTION",
        "IMPLEMENTED",
        "METHOD",
        "RETURNS",
        "INNER",
        "LEFT",
        "RIGHT",
        "OUTER",
        "CROSS",
        "FULL",
        "JOIN",
        "PROVIDER",
        "CONTRACT",
    }
)


class ParseError(Exception):
    """A problem that prevents a confident AST — i.e. UNPARSEABLE."""

    def __init__(self, message: str, ref: SourceRef | None = None) -> None:
        super().__init__(message if not ref or not ref.line else f"{message} (line {ref.line})")
        self.message = message
        self.ref = ref or SourceRef()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_ddl(source: str, *, name_hint: str = "") -> ParsedView:
    """Parse CDS DDL into a :class:`ParsedView`.

    Never raises. A failure is recorded as a fatal :class:`ParseIssue`, which
    the rule engine turns into an UNPARSEABLE verdict.

    Memoized, because the same DDL is parsed many times over in one run: F-09
    screens 200 views over a table, every one resolves a dependency stack, and
    those stacks overlap heavily — a handful of VDM basic views appear in
    almost all of them. Parsing a 300-element SAP view is not cheap, and doing
    it hundreds of times was the single largest cost in the search.

    The cache is safe because a :class:`ParsedView` is written only while it is
    being built: every mutation lives in this module, and nothing touches one
    after it is returned. **A caller that mutates a parsed view would corrupt
    every later reader of the same source** — copy it first if you ever need
    to.
    """
    return _parse_cached(source, name_hint)


@lru_cache(maxsize=1024)
def _parse_cached(source: str, name_hint: str) -> ParsedView:
    """The real parse. Keyed on the source text, so a changed view re-parses.

    Hashing a long string is O(n) once and free afterwards — CPython caches a
    str's hash on the object, and the metadata cache hands back the same object
    each time.
    """
    view = ParsedView(source_text=source, name=name_hint)
    try:
        tokens = tokenize(source)
    except LexError as exc:
        view.issues.append(
            ParseIssue(exc.message, SourceRef(exc.line, exc.column), fatal=True)
        )
        return view

    cursor = Cursor(tokens)
    try:
        _parse_definition(cursor, view)
    except ParseError as exc:
        view.issues.append(ParseIssue(exc.message, exc.ref, fatal=True))
    except AnnotationError as exc:
        view.issues.append(ParseIssue(exc.message, exc.ref, fatal=True))
    except RecursionError:  # pragma: no cover - defensive
        view.issues.append(
            ParseIssue("expression nesting too deep to parse", SourceRef(), fatal=True)
        )
    return view


# ---------------------------------------------------------------------------
# Definition header
# ---------------------------------------------------------------------------


def _parse_definition(cursor: Cursor, view: ParsedView) -> None:
    view.annotations = parse_annotation_block(cursor)
    sql_view = view.annotations.get("abapcatalog.sqlviewname")
    if isinstance(sql_view, str):
        view.sql_view_name = sql_view

    if cursor.at_end:
        raise ParseError("empty DDL source")

    kind, name_token = _parse_kind_and_name(cursor)
    view.entity_kind = kind
    if name_token is not None:
        view.name = name_token.text

    if kind is EntityKind.TABLE_FUNCTION:
        # Nothing further is worth parsing: a table function is implemented in
        # AMDP, so the DDL carries no join or projection information the rule
        # engine could use. R-09 fails it on the strength of the kind alone.
        return

    if kind in (EntityKind.EXTEND_VIEW, EntityKind.CUSTOM_ENTITY, EntityKind.ABSTRACT_ENTITY,
                EntityKind.HIERARCHY, EntityKind.UNKNOWN):
        view.issues.append(
            ParseIssue(
                f"{kind.value} is not a data-selecting view; no SELECT analysed",
                name_token.ref() if name_token else SourceRef(),
            )
        )
        return

    if cursor.at_word("WITH"):
        _parse_parameters(cursor, view)

    if not cursor.accept_word("AS"):
        raise ParseError("expected 'AS' after the view name", cursor.current.ref())

    if cursor.accept_word("PROJECTION"):
        view.entity_kind = EntityKind.PROJECTION_VIEW
        if not cursor.accept_word("ON"):
            raise ParseError("expected 'ON' after 'AS PROJECTION'", cursor.current.ref())
        view.from_source = _parse_source(cursor, SourceKind.PROJECTION)
    elif cursor.accept_word("SELECT"):
        # 'SELECT *' and 'SELECT single' variants do not occur in view DDL.
        if cursor.at_word("DISTINCT"):
            token = cursor.advance()
            view.has_distinct = True
            view.distinct_ref = token.ref()
        if not cursor.accept_word("FROM"):
            raise ParseError("expected 'FROM' after 'SELECT'", cursor.current.ref())
        view.from_source = _parse_source(cursor, SourceKind.FROM)
        _parse_joins(cursor, view)
    else:
        raise ParseError(
            "expected 'SELECT' or 'PROJECTION ON' after 'AS'", cursor.current.ref()
        )

    _parse_associations(cursor, view)

    if not cursor.at_punct("{"):
        raise ParseError(
            "expected the projection list '{ ... }'", cursor.current.ref()
        )
    _parse_projection(cursor, view)
    _parse_tail(cursor, view)
    _collect_used_associations(view)


def _parse_kind_and_name(cursor: Cursor) -> tuple[EntityKind, Token | None]:
    if cursor.accept_word("EXTEND"):
        cursor.accept_word("VIEW")
        cursor.accept_word("ENTITY")
        return EntityKind.EXTEND_VIEW, _accept_object_name(cursor)

    if cursor.accept_word("ANNOTATE"):
        cursor.accept_word("VIEW")
        cursor.accept_word("ENTITY")
        return EntityKind.EXTEND_VIEW, _accept_object_name(cursor)

    if not cursor.accept_word("DEFINE"):
        raise ParseError(
            f"expected 'DEFINE' at the start of the definition, found "
            f"{cursor.current.text!r}",
            cursor.current.ref(),
        )

    cursor.accept_word("ROOT")

    if cursor.accept_sequence("TABLE", "FUNCTION"):
        return EntityKind.TABLE_FUNCTION, _accept_object_name(cursor)
    if cursor.accept_sequence("CUSTOM", "ENTITY"):
        return EntityKind.CUSTOM_ENTITY, _accept_object_name(cursor)
    if cursor.accept_sequence("ABSTRACT", "ENTITY"):
        return EntityKind.ABSTRACT_ENTITY, _accept_object_name(cursor)
    if cursor.accept_word("HIERARCHY"):
        return EntityKind.HIERARCHY, _accept_object_name(cursor)
    if cursor.accept_word("VIEW"):
        if cursor.accept_word("ENTITY"):
            return EntityKind.VIEW_ENTITY, _accept_object_name(cursor)
        return EntityKind.CLASSIC_VIEW, _accept_object_name(cursor)

    raise ParseError(
        f"unrecognised definition kind {cursor.current.text!r}", cursor.current.ref()
    )


def _accept_object_name(cursor: Cursor) -> Token | None:
    token = cursor.current
    if token.kind is not TokenKind.IDENT:
        raise ParseError("expected an object name", token.ref())
    return cursor.advance()


def _parse_parameters(cursor: Cursor, view: ParsedView) -> None:
    """``WITH PARAMETERS p1 : type, p2 : type``.

    Parameters are a hard stop for CDC (R-08 / Note 2890171), but they still
    have to be parsed correctly so the projection list that follows is read
    from the right position.
    """
    cursor.advance()  # WITH
    if not cursor.accept_word("PARAMETERS"):
        raise ParseError("expected 'PARAMETERS' after 'WITH'", cursor.current.ref())
    while True:
        # Parameters can carry their own annotations.
        if cursor.current.kind is TokenKind.AT:
            parse_annotation_block(cursor)
        name_token = cursor.current
        if name_token.kind is not TokenKind.IDENT:
            raise ParseError("expected a parameter name", name_token.ref())
        cursor.advance()
        type_tokens: list[Token] = []
        if cursor.accept_punct(":"):
            depth = 0
            while not cursor.at_end:
                token = cursor.current
                if token.kind is TokenKind.PUNCT:
                    if token.text in "([":
                        depth += 1
                    elif token.text in ")]":
                        depth -= 1
                    elif token.text == "," and depth == 0:
                        break
                if depth == 0 and token.is_word("AS"):
                    break
                type_tokens.append(cursor.advance())
        view.parameters.append(
            Parameter(
                name=name_token.text,
                type_text=" ".join(t.text for t in type_tokens),
                ref=name_token.ref(),
            )
        )
        if not cursor.accept_punct(","):
            break


# ---------------------------------------------------------------------------
# FROM / JOIN
# ---------------------------------------------------------------------------


def _parse_source(cursor: Cursor, kind: SourceKind) -> DataSource:
    token = cursor.current
    if token.is_punct("("):
        raise ParseError(
            "sub-query in FROM position is not supported by the parser",
            token.ref(),
        )
    if token.kind is not TokenKind.IDENT:
        raise ParseError("expected a table or view name", token.ref())
    cursor.advance()

    source = DataSource(name=token.text, kind=kind, ref=token.ref())

    # A source can be parameterised: I_View( p_date: $session.system_date ).
    if cursor.at_punct("("):
        _skip_bracketed(cursor)

    if cursor.accept_word("AS"):
        alias_token = cursor.current
        if alias_token.kind is not TokenKind.IDENT:
            raise ParseError("expected an alias after 'AS'", alias_token.ref())
        source.alias = cursor.advance().text
    elif (
        cursor.current.kind is TokenKind.IDENT
        and cursor.current.upper not in _RESERVED
        and not cursor.at_punct("{")
    ):
        # Alias without 'AS' — legal, and common in hand-written classic views.
        source.alias = cursor.advance().text

    return source


def _parse_joins(cursor: Cursor, view: ParsedView) -> None:
    while True:
        join = _try_parse_join_clause(cursor)
        if join is None:
            return
        view.joins.append(join)


def _try_parse_join_clause(cursor: Cursor) -> DataSource | None:
    token = cursor.current
    if token.kind is not TokenKind.IDENT:
        return None

    word = token.upper
    join_type: JoinType | None = None
    cardinality = JoinCardinality.UNSPECIFIED

    if word == "INNER":
        cursor.advance()
        _expect_word(cursor, "JOIN")
        join_type = JoinType.INNER
    elif word == "CROSS":
        cursor.advance()
        _expect_word(cursor, "JOIN")
        join_type = JoinType.CROSS
    elif word in ("LEFT", "RIGHT", "FULL"):
        # Guard against the LEFT()/RIGHT() string functions.
        if cursor.peek(1).is_punct("("):
            return None
        cursor.advance()
        cursor.accept_word("OUTER")
        cardinality = _parse_join_cardinality(cursor)
        _expect_word(cursor, "JOIN")
        join_type = {
            "LEFT": JoinType.LEFT_OUTER,
            "RIGHT": JoinType.RIGHT_OUTER,
            "FULL": JoinType.FULL_OUTER,
        }[word]
    elif word == "JOIN":
        cursor.advance()
        join_type = JoinType.INNER  # bare JOIN is an inner join
    else:
        return None

    source = _parse_source(cursor, SourceKind.JOIN)
    source.join_type = join_type
    source.join_cardinality = cardinality
    source.ref = token.ref(source.name)

    if join_type is not JoinType.CROSS:
        if not cursor.accept_word("ON"):
            raise ParseError(
                f"expected 'ON' after joining {source.name}", cursor.current.ref()
            )
        on_tokens = _collect_until_clause_boundary(cursor)
        source.on_refs = extract_field_refs(on_tokens)
        source.on_tokens = on_tokens

    return source


def _parse_join_cardinality(cursor: Cursor) -> JoinCardinality:
    """``TO ONE`` / ``TO EXACT ONE`` / ``TO MANY`` between OUTER and JOIN.

    ``TO EXACT ONE`` is the same shape as ``TO ONE`` for CDC purposes — it
    cannot multiply main-table rows, which is the whole question — and it
    additionally promises a match exists. Missing it made three of SAP's own
    delta views UNPARSEABLE.
    """
    if cursor.at_sequence("TO", "EXACT", "ONE"):
        cursor.advance()
        cursor.advance()
        cursor.advance()
        return JoinCardinality.TO_ONE
    if cursor.at_sequence("TO", "ONE"):
        cursor.advance()
        cursor.advance()
        return JoinCardinality.TO_ONE
    if cursor.at_sequence("TO", "MANY"):
        cursor.advance()
        cursor.advance()
        return JoinCardinality.TO_MANY
    return JoinCardinality.UNSPECIFIED


def _expect_word(cursor: Cursor, word: str) -> Token:
    if not cursor.at_word(word):
        raise ParseError(
            f"expected {word!r}, found {cursor.current.text!r}", cursor.current.ref()
        )
    return cursor.advance()


# ---------------------------------------------------------------------------
# Associations
# ---------------------------------------------------------------------------


def _parse_associations(cursor: Cursor, view: ParsedView) -> None:
    while cursor.at_word("ASSOCIATION", "COMPOSITION"):
        view.associations.append(_parse_association(cursor))


def _parse_association(cursor: Cursor) -> Association:
    keyword = cursor.advance()  # ASSOCIATION / COMPOSITION
    assoc = Association(name="", ref=keyword.ref())

    # Cardinality: [0..1], [1], [*], [0..*] — or the textual OF EXACT ONE form.
    if cursor.at_punct("["):
        assoc.cardinality = _parse_cardinality(cursor)
    elif cursor.at_word("OF"):
        assoc.cardinality = _parse_textual_cardinality(cursor)
    elif keyword.is_word("COMPOSITION"):
        # A composition without an explicit cardinality is to-many by default.
        assoc.cardinality = Cardinality(min=0, max=None, raw="[0..*] (implied)")

    # A redirected association in a projection view names itself first:
    #   ASSOCIATION _Product TO I_Product ...
    if cursor.current.kind is TokenKind.IDENT and cursor.current.text.startswith("_"):
        assoc.name = cursor.advance().text

    if cursor.accept_word("TO") or cursor.accept_word("OF"):
        # ``association to one I_Product as _Product`` — the cardinality is
        # written as a keyword after TO rather than as a [0..1] bracket. Not
        # handling it read "one" as the target entity and then failed on the
        # real one, which cost three of SAP's delta views an UNPARSEABLE.
        #
        # The keyword is consumed whether or not a cardinality was already
        # read, because SAP writes both forms at once — ``association of one to
        # one E_GrantorAgreement``. Only the first one read wins; skipping the
        # consumption instead left "one" sitting there to be taken as the
        # target.
        exact = bool(cursor.accept_word("EXACT"))
        keyword_cardinality: Cardinality | None = None
        if cursor.accept_word("ONE"):
            keyword_cardinality = Cardinality(
                min=1 if exact else 0,
                max=1,
                raw="to exact one" if exact else "to one",
            )
        elif cursor.accept_word("MANY"):
            keyword_cardinality = Cardinality(min=0, max=None, raw="to many")
        if keyword_cardinality is not None and not assoc.cardinality.is_specified:
            assoc.cardinality = keyword_cardinality
        cursor.accept_word("PARENT")
        cursor.accept_word("CHILD")
        if cursor.current.kind is TokenKind.IDENT:
            assoc.target = cursor.advance().text
            if cursor.at_punct("("):
                _skip_bracketed(cursor)

    if cursor.accept_word("AS"):
        if cursor.current.kind is not TokenKind.IDENT:
            raise ParseError("expected an association name after 'AS'", cursor.current.ref())
        assoc.name = cursor.advance().text

    if cursor.accept_word("ON"):
        on_tokens = _collect_until_clause_boundary(cursor)
        assoc.on_refs = extract_field_refs(on_tokens)
        assoc.on_tokens = on_tokens

    if not assoc.name:
        assoc.name = assoc.target or "<anonymous>"
    return assoc


def _parse_cardinality(cursor: Cursor) -> Cardinality:
    open_bracket = cursor.advance()  # '['
    raw = ["["]
    lo: int | None = None
    hi: int | None = None
    parts: list[str] = []
    while not cursor.at_end and not cursor.at_punct("]"):
        token = cursor.advance()
        raw.append(token.text)
        parts.append(token.text)
    if not cursor.accept_punct("]"):
        raise ParseError("unterminated cardinality bracket", open_bracket.ref())
    raw.append("]")

    text = "".join(parts)
    if ".." in text:
        left, _, right = text.partition("..")
        lo = int(left) if left.isdigit() else None
        hi = None if right.strip() == "*" else (int(right) if right.isdigit() else None)
    elif text.strip() == "*":
        lo, hi = 0, None
    elif text.isdigit():
        lo = hi = int(text)
    return Cardinality(min=lo, max=hi, raw="".join(raw))


def _parse_textual_cardinality(cursor: Cursor) -> Cardinality:
    """``OF EXACT ONE`` / ``OF ONE`` / ``OF MANY``."""
    cursor.advance()  # OF
    exact = bool(cursor.accept_word("EXACT"))
    if cursor.accept_word("ONE"):
        return Cardinality(min=1 if exact else 0, max=1, raw="of exact one" if exact else "of one")
    if cursor.accept_word("MANY"):
        return Cardinality(min=0, max=None, raw="of many")
    return Cardinality(raw="of ?")


# ---------------------------------------------------------------------------
# Projection list
# ---------------------------------------------------------------------------


def _parse_projection(cursor: Cursor, view: ParsedView) -> None:
    cursor.advance()  # '{'
    while True:
        if cursor.at_end:
            raise ParseError("unterminated projection list", cursor.current.ref())
        if cursor.accept_punct("}"):
            return
        if cursor.accept_punct(","):
            continue
        view.select_items.append(_parse_select_item(cursor))


def _parse_select_item(cursor: Cursor) -> SelectItem:
    item = SelectItem(ref=cursor.current.ref())

    if cursor.current.kind is TokenKind.AT:
        item.annotations = parse_annotation_block(cursor)
        item.ref = cursor.current.ref()

    if cursor.accept_word("KEY"):
        item.is_key = True
    if cursor.accept_word("VIRTUAL"):
        item.is_virtual = True

    tokens = _collect_select_item_tokens(cursor)
    if not tokens:
        raise ParseError("empty element in the projection list", item.ref)

    expression, alias = _split_alias(tokens)
    item.tokens = expression
    item.alias = alias
    item.aggregates = extract_aggregates(expression)
    item.field_refs = extract_field_refs(expression)

    if (
        len(expression) == 1
        and expression[0].kind is TokenKind.IDENT
        and expression[0].text.startswith("_")
    ):
        item.exposed_association = expression[0].text

    return item


def _collect_select_item_tokens(cursor: Cursor) -> list[Token]:
    """Collect one element's tokens, stopping at a top-level ',' or '}'."""
    tokens: list[Token] = []
    depth = 0
    while not cursor.at_end:
        token = cursor.current
        if token.kind is TokenKind.PUNCT:
            if token.text in "([{":
                depth += 1
            elif token.text in ")]}":
                if token.text == "}" and depth == 0:
                    break
                depth -= 1
            elif token.text == "," and depth == 0:
                break
        tokens.append(cursor.advance())
    return tokens


def _split_alias(tokens: list[Token]) -> tuple[list[Token], str | None]:
    """Split ``expr AS alias`` at the last top-level ``AS``."""
    depth = 0
    alias_index: int | None = None
    for index, token in enumerate(tokens):
        if token.kind is TokenKind.PUNCT:
            if token.text in "([{":
                depth += 1
            elif token.text in ")]}":
                depth -= 1
        elif depth == 0 and token.is_word("AS"):
            alias_index = index
    if (
        alias_index is not None
        and alias_index == len(tokens) - 2
        and tokens[-1].kind is TokenKind.IDENT
    ):
        return tokens[:alias_index], tokens[-1].text
    return tokens, None


# ---------------------------------------------------------------------------
# Tail clauses
# ---------------------------------------------------------------------------


def _parse_tail(cursor: Cursor, view: ParsedView) -> None:
    """Scan everything after the projection list.

    WHERE / GROUP BY / HAVING / UNION are recorded with their positions. A set
    operation is a hard CDC failure (R-06), but its branches are still scanned
    for data sources so the dependency graph (F-08) stays complete.
    """
    depth = 0
    while not cursor.at_end:
        token = cursor.current
        if token.kind is TokenKind.PUNCT:
            if token.text in "([{":
                depth += 1
            elif token.text in ")]}":
                depth -= 1
            cursor.advance()
            continue

        if depth != 0 or token.kind is not TokenKind.IDENT:
            cursor.advance()
            continue

        word = token.upper
        if word == "WHERE" and view.where_ref is None:
            view.where_ref = token.ref()
            cursor.advance()
        elif word == "GROUP" and cursor.peek(1).is_word("BY"):
            view.group_by_ref = view.group_by_ref or token.ref("GROUP BY")
            cursor.advance()
            cursor.advance()
        elif word == "HAVING":
            view.having_ref = view.having_ref or token.ref()
            cursor.advance()
        elif word in ("UNION", "INTERSECT", "EXCEPT"):
            cursor.advance()
            label = word
            if word == "UNION" and cursor.accept_word("ALL"):
                label = "UNION ALL"
            view.set_operations.append((label, token.ref(label)))
        elif word == "FROM" or word == "JOIN":
            cursor.advance()
            if cursor.current.kind is TokenKind.IDENT and view.set_operations:
                extra = _parse_source(cursor, SourceKind.FROM)
                view.union_sources.append(extra)
        else:
            cursor.advance()


def _skip_bracketed(cursor: Cursor) -> None:
    depth = 0
    while not cursor.at_end:
        token = cursor.advance()
        if token.is_punct("(", "[", "{"):
            depth += 1
        elif token.is_punct(")", "]", "}"):
            depth -= 1
            if depth == 0:
                return


def _collect_until_clause_boundary(cursor: Cursor) -> list[Token]:
    """Collect tokens up to the next top-level clause keyword or '{'."""
    tokens: list[Token] = []
    depth = 0
    while not cursor.at_end:
        token = cursor.current
        if token.kind is TokenKind.PUNCT:
            if token.text in "([":
                depth += 1
            elif token.text in ")]":
                depth -= 1
            elif token.text == "{" and depth == 0:
                break
        elif (
            depth == 0
            and token.kind is TokenKind.IDENT
            and token.upper in _CLAUSE_BOUNDARIES
            and not cursor.peek(1).is_punct("(")
        ):
            break
        tokens.append(cursor.advance())
    return tokens


# ---------------------------------------------------------------------------
# Token-stream analysis helpers
# ---------------------------------------------------------------------------


def extract_aggregates(tokens: list[Token]) -> list[tuple[str, SourceRef]]:
    """Find aggregate calls. Only counts an identifier followed by '('.

    This is why the tokenizer matters: ``'total sum(x)'`` is a string literal
    and ``// sum(x)`` is a comment, and neither reaches this function.
    """
    found: list[tuple[str, SourceRef]] = []
    for index, token in enumerate(tokens):
        if token.kind is not TokenKind.IDENT:
            continue
        if token.upper not in AGGREGATE_FUNCTIONS:
            continue
        nxt = tokens[index + 1] if index + 1 < len(tokens) else None
        if nxt is not None and nxt.is_punct("("):
            found.append((token.upper, token.ref()))
    return found


def extract_field_refs(tokens: list[Token]) -> list[FieldRef]:
    """Pull dotted field references out of an expression token list."""
    refs: list[FieldRef] = []
    index = 0
    count = len(tokens)
    while index < count:
        token = tokens[index]
        if token.kind is not TokenKind.IDENT:
            index += 1
            continue
        if token.upper in _RESERVED and not token.text.startswith("$"):
            index += 1
            continue
        # A name immediately followed by '(' is a function call, not a field.
        if index + 1 < count and tokens[index + 1].is_punct("("):
            index += 1
            continue

        path = [token.text]
        cursor_index = index + 1
        while (
            cursor_index + 1 < count
            and tokens[cursor_index].is_punct(".")
            and tokens[cursor_index + 1].kind is TokenKind.IDENT
        ):
            path.append(tokens[cursor_index + 1].text)
            cursor_index += 2
        refs.append(FieldRef(path=tuple(path), ref=token.ref(".".join(path))))
        index = cursor_index
    return refs


def _collect_used_associations(view: ParsedView) -> None:
    """Separate associations that are *followed* from those merely *exposed*.

    The distinction decides R-12, and getting it wrong is expensive in both
    directions.

    Following an association in a path expression (``_Text.Description``) pulls
    data through it, and a to-many association there multiplies rows exactly as
    a to-many join does. That is the case R-12 exists for.

    Exposing an association as an element (``_Text`` on its own) publishes a
    navigation for consumers. It reads no data and multiplies nothing, and ODP
    extraction ignores it entirely — the extractor sees flat columns.

    Conflating the two hard-fails almost every SAP standard view, including
    I_BusinessArea, which is SAP's own documented example of a *working*
    automatic-CDC view. A validator that rejects the canonical positive case is
    not being careful; it is being wrong.
    """
    declared = {a.name.upper() for a in view.associations}
    followed: set[str] = set()
    exposed: set[str] = set()

    for item in view.select_items:
        if item.exposed_association:
            exposed.add(item.exposed_association.upper())
        for ref in item.field_refs:
            if ref.is_qualified and ref.root.upper() in declared:
                followed.add(ref.root.upper())

    view.used_association_names = followed
    view.exposed_association_names = exposed
