"""Tokenizer tests.

The spec calls for a real tokenizer rather than regex, and these are the cases
that justify that decision: comments containing `join`, literals containing SQL
keywords, nested CASE, and path expressions.
"""

from __future__ import annotations

import pytest

from cdcforge.parsing.lexer import LexError, TokenKind, tokenize


def texts(source: str) -> list[str]:
    return [t.text for t in tokenize(source) if t.kind is not TokenKind.EOF]


def test_line_comment_is_not_code():
    assert texts("a // group by b\nc") == ["a", "c"]


def test_a_non_breaking_space_is_whitespace():
    """SAP's delivered DDL is full of U+00A0.

    28 of the 34 views that failed to parse across the 887 that S/4HANA ships
    with CDC delta declared died on a single one — an UNPARSEABLE verdict on a
    perfectly good view, over a character nobody can see.
    """
    # Written as escapes on purpose: an invisible character in a test is a
    # test nobody can maintain.
    assert texts("cast(a\xa0as\xa0b)") == ["cast", "(", "a", "as", "b", ")"]


def test_other_unicode_spaces_are_whitespace_too():
    # U+2007 figure space, U+202F narrow no-break space, U+3000 ideographic.
    assert texts("a\u2007b\u202fc\u3000d") == ["a", "b", "c", "d"]


def test_a_genuinely_unexpected_character_is_still_an_error():
    # Widening whitespace must not widen into accepting junk.
    with pytest.raises(LexError):
        tokenize("a \x00 b")


def test_dash_dash_is_a_line_comment():
    # ABAP CDS accepts '--' as well as '//'. SAP's own delivered DDL uses it
    # heavily; missing it made I_MaterialDocumentRecord UNPARSEABLE.
    assert texts("a -- group by b\nc") == ["a", "c"]


def test_apostrophe_inside_a_dash_comment_does_not_open_a_literal():
    # This is the exact shape that broke on real SAP content: prose containing
    # "don't" inside a '--' comment swallowed the rest of the file as a string.
    assert texts("a -- we don't use any key fields\nkey b") == ["a", "key", "b"]


def test_block_comment_is_not_code():
    assert texts("a /* inner join\n sum(x) */ b") == ["a", "b"]


def test_block_comment_keeps_line_numbers_honest():
    tokens = tokenize("a\n/* one\n two\n three */\nb")
    b = tokens[1]
    assert b.text == "b"
    assert b.line == 5


def test_string_literal_containing_keywords_is_one_token():
    tokens = tokenize("x = 'group by having union distinct'")
    literal = [t for t in tokens if t.kind is TokenKind.STRING]
    assert len(literal) == 1
    assert literal[0].text == "group by having union distinct"


def test_escaped_quote_inside_literal():
    tokens = tokenize("'it''s here'")
    assert tokens[0].text == "it's here"


def test_unterminated_literal_is_an_error_not_a_silent_swallow():
    # Silently consuming the rest of the file would hide a GROUP BY from the
    # rule engine and turn a hard failure into a PASS.
    with pytest.raises(LexError):
        tokenize("x = 'never closed\nkey y")


def test_unterminated_block_comment_is_an_error():
    with pytest.raises(LexError):
        tokenize("a /* opened and never closed")


def test_enum_value_is_distinct_from_a_string():
    tokens = tokenize("role: #MAIN")
    enum = [t for t in tokens if t.kind is TokenKind.ENUM]
    assert len(enum) == 1 and enum[0].text == "MAIN"


def test_namespaced_identifier_is_one_token():
    assert "/1DH/OBSERVE_LOGTAB" in texts("from /1DH/OBSERVE_LOGTAB")


def test_double_dot_is_not_two_dots():
    assert ".." in texts("[0..1]")


def test_decimal_number_survives_but_cardinality_does_not_become_a_float():
    assert texts("15.2") == ["15.2"]
    assert texts("[0..1]") == ["[", "0", "..", "1", "]"]


def test_byte_order_mark_is_tolerated():
    # DDL exported from Eclipse or written on Windows often carries a BOM.
    assert texts("﻿define view entity X") == ["define", "view", "entity", "X"]


def test_dollar_identifiers():
    assert texts("$projection.Field") == ["$projection", ".", "Field"]
    assert texts("$session.client") == ["$session", ".", "client"]


def test_positions_are_reported_for_every_token():
    tokens = tokenize("define\n  view")
    assert (tokens[0].line, tokens[0].column) == (1, 1)
    assert (tokens[1].line, tokens[1].column) == (2, 3)
