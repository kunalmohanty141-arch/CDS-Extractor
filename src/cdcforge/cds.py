"""The CDC annotation vocabulary, in one place.

Both the rule engine and the generator need to agree on exactly what a CDC
mapping *is*. Putting the vocabulary here means a change to the annotation
contract is a single edit, and means the generator cannot emit a shape the
validator would reject.

Annotation syntax below is taken from the verified working example in SAP's own
TechEd DA281 repository (Appendix A.2 of the specification).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cdcforge.model import SourceRef
from cdcforge.parsing.annotations import AnnotationObject, AnnotationTree, EnumValue

# ---------------------------------------------------------------------------
# Annotation paths (lower-cased — AnnotationTree normalises names)
# ---------------------------------------------------------------------------

ANN_EXTRACTION_ENABLED = "analytics.dataextraction.enabled"
ANN_CDC_ROOT = "analytics.dataextraction.delta.changedatacapture"
ANN_CDC_AUTOMATIC = f"{ANN_CDC_ROOT}.automatic"
ANN_CDC_MAPPING = f"{ANN_CDC_ROOT}.mapping"
ANN_DELTA_BY_ELEMENT = "analytics.dataextraction.delta.byelement"
ANN_DATA_CATEGORY = "analytics.datacategory"
ANN_VDM_VIEW_TYPE = "vdm.viewtype"

#: SAP's Virtual Data Model layers, best-first for extraction purposes.
#:
#: The strongest quality signal SAP gives about a view's intended use, and it
#: costs nothing to read. BASIC views are the reuse layer and are what
#: extraction should sit on. CONSUMPTION views are built for Fiori — they
#: routinely carry parameters, aggregation and @ObjectModel behaviour, all of
#: which are fatal for CDC.
VDM_LAYER_RANK = {
    "BASIC": 0,
    "COMPOSITE": 1,
    "CONSUMPTION": 2,
    "": 3,
}

#: Name prefixes, used when @VDM.viewType is absent — which it often is.
VDM_PREFIX_RANK = {
    "I_": 0,   # interface / basic — the reuse layer
    "A_": 1,
    "E_": 1,   # extension
    "C_": 2,   # consumption — for Fiori
    "X_": 2,
    "R_": 2,
    "P_": 9,   # private — never meant for reuse
}


def vdm_layer(annotations, name: str) -> str:
    """The VDM layer, from the annotation if present, else the name prefix."""
    declared = annotations.get(ANN_VDM_VIEW_TYPE) if annotations else None
    if declared is not None:
        return getattr(declared, "name", str(declared)).upper()
    prefix = name.upper()[:2]
    return {
        "I_": "BASIC", "A_": "BASIC", "E_": "COMPOSITE",
        "C_": "CONSUMPTION", "X_": "CONSUMPTION", "R_": "CONSUMPTION",
        "P_": "PRIVATE",
    }.get(prefix, "")


def is_private_layer(annotations, name: str) -> bool:
    """``P_*`` views are SAP-internal building blocks.

    Not meant for reuse, not stable, and SAP changes them without notice — so
    they must never be suggested as the base for a customer wrapper.
    """
    return vdm_layer(annotations, name) == "PRIVATE" or name.upper().startswith("P_")
ANN_SQL_VIEW_NAME = "abapcatalog.sqlviewname"
ANN_VIEW_ENHANCEMENT_CATEGORY = "abapcatalog.viewenhancementcategory"
ANN_LABEL = "endusertext.label"

#: DDIC-only annotations that must be removed from a view *entity*, or
#: extraction validation fails (Appendix A.8).
DDIC_ONLY_ANNOTATIONS = (
    ANN_SQL_VIEW_NAME,
    ANN_VIEW_ENHANCEMENT_CATEGORY,
    "abapcatalog.compiler.comparefilter",
    "abapcatalog.preservekey",
)

# ---------------------------------------------------------------------------
# Mapping roles
# ---------------------------------------------------------------------------

ROLE_MAIN = "MAIN"
"""The root table. Its key must equal the view key, and only its deletions
propagate (Appendix A.6)."""

ROLE_LEFT_OUTER_TO_ONE_JOIN = "LEFT_OUTER_TO_ONE_JOIN"
"""Every joined table. Changes generate updates, never deletions."""

VALID_ROLES = (ROLE_MAIN, ROLE_LEFT_OUTER_TO_ONE_JOIN)

# ---------------------------------------------------------------------------
# Table classification
# ---------------------------------------------------------------------------

CLIENT_FIELD_NAMES = frozenset({"MANDT", "MANDANT", "CLIENT"})

#: Known high-write transactional tables. Enabling CDC adds INSERT/UPDATE/DELETE
#: triggers, so these carry the most operational risk (F-17).
#:
#: This list is a heuristic aid, not an SAP-published set. SAP publishes no
#: number capping how many tables can safely be CDC-enabled, and the UI says so
#: rather than inventing a threshold.
HOT_TABLES = frozenset(
    {
        "ACDOCA",
        "ACDOCP",
        "BKPF",
        "BSEG",
        "BSIS",
        "COEP",
        "COSP",
        "COSS",
        "EKBE",
        "EKKO",
        "EKPO",
        "FAGLFLEXA",
        "LIPS",
        "LIKP",
        "MARC",
        "MARD",
        "MATDOC",
        "MBEW",
        "MSEG",
        "PRCD_ELEMENTS",
        "VBAK",
        "VBAP",
        "VBEP",
        "VBFA",
        "VBRK",
        "VBRP",
        "KONV",
    }
)

#: Configuration / customising tables. Mapping these into CDC means a single
#: config change regenerates deltas for every dependent record — which is why
#: SAP omits T001/TVKO from C_SalesDocumentItemDEX_1's mapping (Note 3070845).
CONFIG_TABLES = frozenset(
    {
        "T000",
        "T001",
        "T001K",
        "T001L",
        "T001W",
        "T005",
        "T006",
        "T023",
        "T134",
        "T156",
        "TCURC",
        "TCURR",
        "TCURT",
        "TVKO",
        "TVTW",
        "TSPA",
        "TVAK",
        "TVAP",
    }
)

#: DDIC delivery classes that mean "customising / system content", i.e. not
#: transactional data worth triggering a delta on.
CONFIG_DELIVERY_CLASSES = frozenset({"C", "G", "E", "S", "W"})


def is_client_field(name: str) -> bool:
    return name.upper() in CLIENT_FIELD_NAMES


# ---------------------------------------------------------------------------
# CDC mapping
# ---------------------------------------------------------------------------


@dataclass
class CdcMappingEntry:
    """One element of ``changeDataCapture.mapping``."""

    table: str = ""
    role: str = ""
    """Upper-cased role name without the ``#``. Empty when absent or malformed."""

    view_elements: list[str] = field(default_factory=list)
    table_elements: list[str] = field(default_factory=list)
    filter: object | None = None
    ref: SourceRef = field(default_factory=SourceRef)
    problems: list[str] = field(default_factory=list)
    """Structural problems found while reading the entry — a missing ``table``,
    a role given as a string instead of an enum, mismatched element lists."""

    @property
    def is_main(self) -> bool:
        return self.role == ROLE_MAIN

    @property
    def pairs(self) -> list[tuple[str, str]]:
        """viewElement ↔ tableElement pairs, positionally matched.

        Returns nothing when the two lists differ in length, rather than
        pairing what it can. A bare ``zip`` silently drops the tail, so a
        mapping that has lost a key field would come back looking complete —
        which is precisely the failure this tool exists to catch, arriving
        through its own back door. The length mismatch is already reported as a
        problem by :func:`read_cdc_mapping`; this refuses to let any caller act
        on half a mapping.
        """
        if len(self.view_elements) != len(self.table_elements):
            return []
        return list(zip(self.view_elements, self.table_elements, strict=True))


def _as_string_list(value: object) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        if all(isinstance(v, str) for v in value):
            return list(value)
        return None
    return None


def read_cdc_mapping(annotations: AnnotationTree | None) -> list[CdcMappingEntry] | None:
    """Read the CDC mapping array, or ``None`` when there isn't one.

    Malformed entries are returned with their problems attached rather than
    dropped. A mapping the tool cannot read is not the same as no mapping, and
    the difference decides between FAIL_FIXABLE and MANUAL_REVIEW.
    """
    if annotations is None:
        return None
    raw = annotations.get(ANN_CDC_MAPPING)
    if raw is None:
        return None

    base_ref = annotations.ref(ANN_CDC_MAPPING)
    if not isinstance(raw, list):
        entry = CdcMappingEntry(ref=base_ref)
        entry.problems.append(
            "changeDataCapture.mapping must be an array of objects"
        )
        return [entry]

    entries: list[CdcMappingEntry] = []
    for index, raw_entry in enumerate(raw):
        entry = CdcMappingEntry(ref=base_ref)
        if not isinstance(raw_entry, dict):
            entry.problems.append(f"mapping entry {index + 1} is not an object")
            entries.append(entry)
            continue
        if isinstance(raw_entry, AnnotationObject):
            entry.ref = raw_entry.ref

        table = raw_entry.get("table")
        if isinstance(table, str) and table:
            entry.table = table.upper()
        else:
            entry.problems.append(f"mapping entry {index + 1} has no 'table'")

        role = raw_entry.get("role")
        if isinstance(role, EnumValue):
            entry.role = role.name.upper()
            if entry.role not in VALID_ROLES:
                entry.problems.append(
                    f"role #{role.name} is not one of #{ROLE_MAIN} / "
                    f"#{ROLE_LEFT_OUTER_TO_ONE_JOIN}"
                )
        elif isinstance(role, str):
            entry.problems.append(
                f"role must be an enum (#{ROLE_MAIN}), not the string {role!r}"
            )
        else:
            entry.problems.append(f"mapping entry {index + 1} has no 'role'")

        view_elements = _as_string_list(raw_entry.get("viewelement"))
        table_elements = _as_string_list(raw_entry.get("tableelement"))
        if view_elements is None and raw_entry.get("viewelement") is not None:
            entry.problems.append("viewElement must be a string or a list of strings")
        if table_elements is None and raw_entry.get("tableelement") is not None:
            entry.problems.append("tableElement must be a string or a list of strings")
        entry.view_elements = view_elements or []
        entry.table_elements = table_elements or []
        entry.filter = raw_entry.get("filter")

        if entry.view_elements and entry.table_elements:
            if len(entry.view_elements) != len(entry.table_elements):
                entry.problems.append(
                    f"viewElement has {len(entry.view_elements)} entries but "
                    f"tableElement has {len(entry.table_elements)} — they are "
                    f"matched positionally and must be the same length"
                )
        elif entry.filter is None:
            # A filter can stand in for a missing element mapping when it is a
            # single value (Appendix A.2), so only complain when there is none.
            if not entry.view_elements:
                entry.problems.append("no viewElement and no filter")
            if not entry.table_elements:
                entry.problems.append("no tableElement and no filter")

        entries.append(entry)
    return entries


def extraction_enabled(annotations: AnnotationTree | None) -> bool:
    return annotations is not None and annotations.is_true(ANN_EXTRACTION_ENABLED)
