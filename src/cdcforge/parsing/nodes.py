"""AST node types produced by the DDL parser.

The AST records what the rule engine needs and nothing more: source objects and
their aliases, the join list with type *and cardinality*, ON-condition field
references, the projection field list, aggregate usage, set operations,
parameters, and the annotation tree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from cdcforge.model import ParseIssue, SourceRef

from cdcforge.parsing.lexer import TokenKind

if TYPE_CHECKING:  # pragma: no cover
    from cdcforge.parsing.annotations import AnnotationTree
    from cdcforge.parsing.lexer import Token


class EntityKind(str, Enum):
    """What kind of DDL object this source defines."""

    VIEW_ENTITY = "VIEW_ENTITY"
    """``DEFINE VIEW ENTITY`` — current syntax, ABAP 7.55 / S/4HANA 2020+."""

    CLASSIC_VIEW = "CLASSIC_VIEW"
    """``DEFINE VIEW`` + ``@AbapCatalog.sqlViewName`` — deprecated since 7.57."""

    PROJECTION_VIEW = "PROJECTION_VIEW"
    """``AS PROJECTION ON`` — the shape a generated Z-wrapper takes."""

    TABLE_FUNCTION = "TABLE_FUNCTION"
    """``DEFINE TABLE FUNCTION`` — AMDP-implemented. Fatal for CDC (R-09)."""

    CUSTOM_ENTITY = "CUSTOM_ENTITY"
    ABSTRACT_ENTITY = "ABSTRACT_ENTITY"
    HIERARCHY = "HIERARCHY"
    EXTEND_VIEW = "EXTEND_VIEW"
    UNKNOWN = "UNKNOWN"

    @property
    def is_selecting_view(self) -> bool:
        """True for kinds that actually read data through a SELECT/PROJECTION."""
        return self in (
            EntityKind.VIEW_ENTITY,
            EntityKind.CLASSIC_VIEW,
            EntityKind.PROJECTION_VIEW,
        )


class JoinType(str, Enum):
    INNER = "INNER"
    LEFT_OUTER = "LEFT_OUTER"
    RIGHT_OUTER = "RIGHT_OUTER"
    FULL_OUTER = "FULL_OUTER"
    CROSS = "CROSS"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title()


class JoinCardinality(str, Enum):
    """Cardinality *declared on the join clause itself*.

    ``LEFT OUTER TO ONE JOIN`` → TO_ONE, ``LEFT OUTER TO MANY JOIN`` → TO_MANY,
    a bare ``LEFT OUTER JOIN`` → UNSPECIFIED.

    UNSPECIFIED matters. The CDC framework requires a to-one shape, and a plain
    LEFT OUTER JOIN neither declares nor denies it. The tool cannot prove the
    shape from the DDL, so R-10 returns MANUAL_REVIEW rather than guessing in
    either direction.
    """

    TO_ONE = "TO_ONE"
    TO_MANY = "TO_MANY"
    UNSPECIFIED = "UNSPECIFIED"


@dataclass(frozen=True)
class Cardinality:
    """An association cardinality such as ``[0..1]``, ``[1]``, ``[0..*]``."""

    min: int | None = None
    max: int | None = None
    """``None`` means ``*`` — unbounded."""

    raw: str = ""

    @property
    def is_specified(self) -> bool:
        """False when the DDL declared no cardinality the parser could read.

        Checked before :attr:`is_to_many`, which would otherwise read an
        unparsed cardinality as unbounded and hard-fail a view on the strength
        of a parser gap.
        """
        return bool(self.raw)

    @property
    def is_to_one(self) -> bool:
        return self.max == 1

    @property
    def is_to_many(self) -> bool:
        return self.is_specified and (self.max is None or self.max > 1)

    def __str__(self) -> str:  # pragma: no cover - display helper
        return self.raw or "[?]"


@dataclass(frozen=True)
class FieldRef:
    """A dotted reference: ``Item.Currency``, ``_Product.Name``, ``MATNR``."""

    path: tuple[str, ...]
    ref: SourceRef = field(default_factory=SourceRef)

    @property
    def root(self) -> str:
        return self.path[0] if self.path else ""

    @property
    def leaf(self) -> str:
        return self.path[-1] if self.path else ""

    @property
    def is_qualified(self) -> bool:
        return len(self.path) > 1

    @property
    def is_pseudo(self) -> bool:
        """``$projection.X``, ``$session.client``, ``$parameters.P``."""
        return self.root.startswith("$")

    def __str__(self) -> str:  # pragma: no cover - display helper
        return ".".join(self.path)


class SourceKind(str, Enum):
    FROM = "FROM"
    JOIN = "JOIN"
    PROJECTION = "PROJECTION"


@dataclass
class DataSource:
    """A table or view read by this view: the FROM entry, or one join."""

    name: str
    alias: str | None = None
    kind: SourceKind = SourceKind.FROM
    join_type: JoinType | None = None
    join_cardinality: JoinCardinality = JoinCardinality.UNSPECIFIED
    on_refs: list[FieldRef] = field(default_factory=list)
    on_tokens: list = field(default_factory=list)
    """The raw ON-condition tokens, kept so equalities can be paired.

    A flat list of references loses which field is equated with which, and that
    is the whole difference between "both sides must be exposed" and "either
    side will do" — see :attr:`on_equalities`.
    """

    ref: SourceRef = field(default_factory=SourceRef)

    @property
    def local_name(self) -> str:
        """How the projection refers to this source."""
        return self.alias or self.name

    @property
    def on_equalities(self) -> list[tuple[list[FieldRef], list[FieldRef]]]:
        """The ON condition split into ``(left refs, right refs)`` per equality.

        Which field is equated with which is the whole question for R-15. After
        the join the two sides hold the same value in every output row, so
        exposing either one gives CDC the key it needs — and a flat list of
        references cannot express that.

        Conditions are split on AND/OR at bracket depth zero, then on ``=``.
        Anything that is not a simple equality is skipped rather than guessed
        at: a comparison against a literal, or an inequality, carries no field
        pair to check.
        """
        # Imported here rather than at module scope: ddl imports nodes, so the
        # dependency only resolves once both modules are loaded.
        from cdcforge.parsing.ddl import extract_field_refs

        equalities: list[tuple[list[FieldRef], list[FieldRef]]] = []
        depth = 0
        clause: list = []

        def flush(tokens: list) -> None:
            split = -1
            inner = 0
            for index, token in enumerate(tokens):
                if token.kind is TokenKind.PUNCT:
                    if token.text in "([":
                        inner += 1
                    elif token.text in ")]":
                        inner -= 1
                    elif token.text == "=" and inner == 0:
                        split = index
                        break
            if split < 0:
                return
            left = extract_field_refs(tokens[:split])
            right = extract_field_refs(tokens[split + 1 :])
            if left and right:
                equalities.append((left, right))

        for token in self.on_tokens:
            if token.kind is TokenKind.PUNCT and token.text in "([":
                depth += 1
            elif token.kind is TokenKind.PUNCT and token.text in ")]":
                depth -= 1
            if depth == 0 and token.is_word("AND", "OR"):
                flush(clause)
                clause = []
                continue
            clause.append(token)
        flush(clause)
        return equalities

    def describe_join(self) -> str:
        if self.join_type is None:
            return "FROM"
        card = {
            JoinCardinality.TO_ONE: " TO ONE",
            JoinCardinality.TO_MANY: " TO MANY",
            JoinCardinality.UNSPECIFIED: "",
        }[self.join_cardinality]
        return f"{self.join_type.label}{card} JOIN"


@dataclass
class Association:
    name: str
    """Including the leading underscore, as written."""

    target: str = ""
    cardinality: Cardinality = field(default_factory=Cardinality)
    on_refs: list[FieldRef] = field(default_factory=list)
    on_tokens: list = field(default_factory=list)
    ref: SourceRef = field(default_factory=SourceRef)

    @property
    def is_filtered(self) -> bool:
        """Does the ON condition restrict the target beyond matching keys?

        A ``[1..*]`` association whose ON condition also pins a status, a type
        and a validity window may well yield one row — CDS has no way to
        declare "to-many, but this filter makes it to-one", so SAP writes the
        pessimistic cardinality and ships the view with CDC delta anyway.
        I_DFS_EquipmentBasicDEX does exactly that:

            association [1..*] to … on  …Equipment = …
              and …DfsAssgmtStatusCode = 'IDFS4'
              and …DfsAssgmtType       = 'TOB'
              and validity covers $session.user_date

        Detected by the presence of a literal or a session variable, which is
        what a filter looks like and a plain key equality never does. It does
        not prove the association is to-one — only that the declaration is not
        the last word, which is the difference between a hard failure and a
        review.
        """
        from cdcforge.parsing.lexer import TokenKind

        for token in self.on_tokens:
            if token.kind in (TokenKind.STRING, TokenKind.NUMBER):
                return True
            if token.kind is TokenKind.IDENT and token.text.startswith("$"):
                return True
        return False


@dataclass
class Parameter:
    name: str
    type_text: str = ""
    ref: SourceRef = field(default_factory=SourceRef)


@dataclass
class SelectItem:
    """One element of the projection list."""

    alias: str | None = None
    is_key: bool = False
    is_virtual: bool = False
    tokens: list["Token"] = field(default_factory=list)
    aggregates: list[tuple[str, SourceRef]] = field(default_factory=list)
    field_refs: list[FieldRef] = field(default_factory=list)
    exposed_association: str | None = None
    """Set when the element is a bare association exposure, e.g. ``_Product``."""

    annotations: "AnnotationTree | None" = None
    ref: SourceRef = field(default_factory=SourceRef)

    @property
    def name(self) -> str:
        """The element name as it appears in the view."""
        if self.alias:
            return self.alias
        if self.exposed_association:
            return self.exposed_association
        # An unaliased element takes the name of the field it exposes.
        if len(self.field_refs) == 1 and not self.field_refs[0].is_pseudo:
            return self.field_refs[0].leaf
        return ""

    @property
    def is_simple_field(self) -> bool:
        """True when the element is exactly one field reference, nothing else.

        Only simple elements can be traced back to a base-table column, which is
        what the key-exposure rules need. Anything computed is reported as
        unresolvable rather than guessed at.
        """
        if self.exposed_association or self.aggregates:
            return False
        if len(self.field_refs) != 1:
            return False
        significant = [
            t for t in self.tokens if t.text not in {".", "as", "AS", "key", "KEY"}
        ]
        return len(significant) == len(self.field_refs[0].path)

    # ``source_field`` used to live here, returning field_refs[0] only for a
    # bare field. Nothing calls it any more and nothing should: it silently
    # returned None for a cast, so every SAP view whose key is written
    # ``cast(mara.matnr as productnumber preserving type)`` looked as though it
    # exposed no key at all. That produced false findings on R-13, R-15 and
    # R-23 and made F-09 reject I_Product as a base for MARA. Use
    # :attr:`lineage_ref`, which sees through the cast.

    @property
    def cast_operand(self) -> FieldRef | None:
        """The field inside ``CAST(field AS type)``, if that is what this is.

        SAP's VDM is full of these, usually
        ``cast(PurchasingDocument as vdm_purchaseorder preserving type)``,
        which exists only to give the element a domain-specific data element.
        The value is unchanged.

        The type operand has to be excluded explicitly: ``vdm_purchaseorder``
        and ``abap.char`` both parse as field references, so "the cast contains
        exactly one reference" is never true. Only the tokens before the
        cast's own ``AS`` describe the value.

        Deliberately narrow — it must *start* with CAST. ``a + 1`` also reads a
        single field and does not preserve the value, and treating that as
        lineage would put arithmetic into a CDC key mapping.
        """
        if self.aggregates or self.exposed_association or not self.tokens:
            return None
        if not self.tokens[0].is_word("CAST"):
            return None
        if len(self.tokens) < 4 or not self.tokens[1].is_punct("("):
            return None

        # Walk to the CAST's own AS, at bracket depth 1.
        depth = 0
        operand: list = []
        for token in self.tokens[1:]:
            if token.kind is TokenKind.PUNCT:
                if token.text == "(":
                    depth += 1
                    if depth == 1:
                        continue
                elif token.text == ")":
                    depth -= 1
                    if depth == 0:
                        break
            if depth == 1 and token.is_word("AS"):
                break
            operand.append(token)

        from cdcforge.parsing.ddl import extract_field_refs

        refs = extract_field_refs(operand)
        return refs[0] if len(refs) == 1 else None

    @property
    def lineage_ref(self) -> FieldRef | None:
        """The field this element's value comes from, seeing through a cast.

        For column lineage and key mapping, what matters is which column
        supplies the value — not whether the DDL wrapped it in a cast.
        """
        if self.is_simple_field:
            return self.field_refs[0]
        return self.cast_operand

    def text(self) -> str:
        return " ".join(t.text for t in self.tokens)


@dataclass
class ParsedView:
    """The AST for one DDL source."""

    name: str = ""
    entity_kind: EntityKind = EntityKind.UNKNOWN
    sql_view_name: str | None = None
    annotations: "AnnotationTree | None" = None
    parameters: list[Parameter] = field(default_factory=list)
    from_source: DataSource | None = None
    joins: list[DataSource] = field(default_factory=list)
    associations: list[Association] = field(default_factory=list)
    select_items: list[SelectItem] = field(default_factory=list)
    has_distinct: bool = False
    distinct_ref: SourceRef = field(default_factory=SourceRef)
    group_by_ref: SourceRef | None = None
    having_ref: SourceRef | None = None
    where_ref: SourceRef | None = None
    set_operations: list[tuple[str, SourceRef]] = field(default_factory=list)
    union_sources: list[DataSource] = field(default_factory=list)
    """Sources read by the branches of a UNION / INTERSECT / EXCEPT.

    Kept apart from :attr:`joins` on purpose. A set operation is already a hard
    CDC failure (R-06), so re-reporting its joins under R-10/R-11 would only
    bury the real reason. The dependency resolver still walks them, so the
    graph stays complete.
    """

    used_association_names: set[str] = field(default_factory=set)
    """Associations *followed* in a path expression — these pull data through."""

    exposed_association_names: set[str] = field(default_factory=set)
    """Associations merely *exposed* as elements. These publish a navigation,
    read no data, and are ignored by ODP extraction."""

    issues: list[ParseIssue] = field(default_factory=list)
    source_text: str = ""
    is_root: bool = False

    # -- derived views over the AST ---------------------------------------
    @property
    def sources(self) -> list[DataSource]:
        """FROM entry plus every join, in declaration order."""
        return ([self.from_source] if self.from_source else []) + list(self.joins)

    @staticmethod
    def _unique(names: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for name in names:
            if name and name.upper() not in seen:
                seen.add(name.upper())
                ordered.append(name)
        return ordered

    @property
    def data_path_objects(self) -> list[str]:
        """Objects whose contents actually reach this view's result set.

        FROM and every join, always. Association targets **only when the
        association is followed** in a path expression — a declared-but-
        unfollowed association contributes no columns, so what is inside it
        cannot affect this view.

        This is the list the rule engine walks, and the distinction is not
        academic. Walking every declared association pulled SAP's entire
        business-partner and address graph into the stack of a sales-pricing
        extraction view, and the forbidden-construct rules then hard-failed it
        on an aggregation four views away that it never reads. SAP ships that
        view as delta-capable; the tool was wrong, not SAP.

        It is also most of the cost: unfollowed associations are what made a
        single view fan out into thousands of round-trips.
        """
        followed = self.used_association_names
        names = [s.name for s in self.sources + self.union_sources]
        names += [
            a.target
            for a in self.associations
            if a.target and a.name.upper() in followed
        ]
        return self._unique(names)

    @property
    def all_referenced_objects(self) -> list[str]:
        """Every object this view mentions, followed or not.

        For the where-used graph and the dependency viewer, where a declared
        association is still a relationship worth showing. Not for the rules —
        see :attr:`data_path_objects`.
        """
        names = [s.name for s in self.sources + self.union_sources]
        names += [a.target for a in self.associations if a.target]
        return self._unique(names)

    @property
    def alias_map(self) -> dict[str, DataSource]:
        """Local name (alias, else object name) → source, upper-cased keys."""
        return {s.local_name.upper(): s for s in self.sources}

    @property
    def key_items(self) -> list[SelectItem]:
        return [i for i in self.select_items if i.is_key]

    @property
    def element_names(self) -> list[str]:
        return [i.name for i in self.select_items if i.name]

    @property
    def aggregates(self) -> list[tuple[str, SourceRef]]:
        out: list[tuple[str, SourceRef]] = []
        for item in self.select_items:
            out.extend(item.aggregates)
        return out

    @property
    def association_map(self) -> dict[str, Association]:
        return {a.name.upper(): a for a in self.associations}

    @property
    def has_fatal_issue(self) -> bool:
        return any(i.fatal for i in self.issues)

    def find_element(self, name: str) -> SelectItem | None:
        target = name.upper()
        for item in self.select_items:
            if item.name.upper() == target:
                return item
        return None

    def find_source(self, local_name: str) -> DataSource | None:
        return self.alias_map.get(local_name.upper())
