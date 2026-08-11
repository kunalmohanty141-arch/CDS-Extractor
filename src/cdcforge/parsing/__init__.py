"""F-11 — DDL tokenizer and AST.

Zero SAP dependency. Text in, AST out.
"""

from cdcforge.parsing.ddl import ParseError, parse_ddl
from cdcforge.parsing.lexer import Token, TokenKind, tokenize
from cdcforge.parsing.nodes import (
    Association,
    Cardinality,
    DataSource,
    EntityKind,
    FieldRef,
    JoinType,
    Parameter,
    ParsedView,
    SelectItem,
)

__all__ = [
    "Association",
    "Cardinality",
    "DataSource",
    "EntityKind",
    "FieldRef",
    "JoinType",
    "Parameter",
    "ParseError",
    "ParsedView",
    "SelectItem",
    "Token",
    "TokenKind",
    "parse_ddl",
    "tokenize",
]
