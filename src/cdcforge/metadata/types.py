"""Metadata value objects.

These mirror the DDIC objects listed in Appendix D — DD02L for table class,
DD03L for field-level metadata, TADIR for package and ownership, APIS for the
release contract — but deliberately as plain dataclasses with no SAP
dependency, so the rule engine can be driven from fixtures.

Appendix D.6 carries a warning worth repeating here: SAP describes several of
those tables as internal and subject to change between NetWeaver versions. Read
defensively; never assume a column exists. Every field below therefore has a
default, and "unknown" is a representable state rather than a crash.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from cdcforge.cds import CONFIG_DELIVERY_CLASSES, CONFIG_TABLES, HOT_TABLES, is_client_field


class TableClass(str, Enum):
    """DD02L-TABCLASS."""

    TRANSPARENT = "TRANSP"
    CLUSTER = "CLUSTER"
    POOL = "POOL"
    VIEW = "VIEW"
    STRUCTURE = "INTTAB"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def parse(cls, value: str | None) -> "TableClass":
        if not value:
            return cls.UNKNOWN
        normalised = value.strip().upper()
        aliases = {
            "TRANSP": cls.TRANSPARENT,
            "TRANSPARENT": cls.TRANSPARENT,
            "CLUSTER": cls.CLUSTER,
            "CLUSTERTAB": cls.CLUSTER,
            "POOL": cls.POOL,
            "POOLTAB": cls.POOL,
            "VIEW": cls.VIEW,
            "INTTAB": cls.STRUCTURE,
            "STRUCTURE": cls.STRUCTURE,
        }
        return aliases.get(normalised, cls.UNKNOWN)


class Owner(str, Enum):
    SAP = "SAP"
    CUSTOMER = "CUSTOMER"
    UNKNOWN = "UNKNOWN"


class ApiState(str, Enum):
    """Release contract, stored as transport object ``APIS`` (Appendix D.5)."""

    C0 = "C0"
    """Extend."""

    C1 = "C1"
    """System-internal use — must not be modified."""

    C2 = "C2"
    """Remote API — must not be modified."""

    C3 = "C3"
    """Configuration."""

    RELEASED = "RELEASED"
    """Released, but the contract level could not be determined.

    An APIS transport object exists for the object, which establishes that SAP
    has released it — the level (C0…C3) is held inside that object and is not
    reachable from the metadata this tool reads. The *action* is the same
    either way: do not modify it, build a wrapper.
    """

    NOT_RELEASED = "NOT_RELEASED"
    UNKNOWN = "UNKNOWN"
    """State could not be determined. Treated as unmodifiable — fail safe."""

    @property
    def forbids_modification(self) -> bool:
        return self in (ApiState.C1, ApiState.C2, ApiState.RELEASED)

    @classmethod
    def parse(cls, value: str | None) -> "ApiState":
        if value is None:
            return cls.UNKNOWN
        normalised = str(value).strip().upper().replace("-", "_")
        try:
            return cls(normalised)
        except ValueError:
            return cls.UNKNOWN


def derive_owner(name: str) -> Owner:
    """Customer namespace by name. TADIR/package is authoritative when known."""
    upper = name.upper().lstrip("/")
    if upper[:1] in ("Z", "Y"):
        return Owner.CUSTOMER
    if name.startswith("/") and not upper.startswith(("SAP", "1")):
        # A registered customer namespace such as /ACME/TAB.
        return Owner.CUSTOMER
    return Owner.SAP


@dataclass
class FieldMeta:
    """One column — DD03L."""

    name: str
    position: int = 0
    is_key: bool = False
    data_element: str = ""
    data_type: str = ""
    length: int = 0
    decimals: int = 0
    label: str = ""
    not_null: bool = False

    ref_table: str = ""
    """DD03L REFTABLE — where the currency or unit for this column lives."""

    ref_field: str = ""
    """DD03L REFFIELD — the currency or unit column itself."""

    @property
    def is_amount_or_quantity(self) -> bool:
        """A CURR or QUAN column, which CDS refuses without its reference.

        SAP resolves the reference by itself when it points at a column of the
        same table that the view also exposes. When it points somewhere else —
        EKPO.NETPR is priced in EKKO.WAERS — a single-table view has nothing to
        point at, and activation fails with "reference information missing".
        """
        return self.data_type.upper() in ("CURR", "QUAN")

    def reference_is_local(self, table_name: str) -> bool:
        return bool(self.ref_table) and self.ref_table.upper() == table_name.upper()

    @property
    def is_client(self) -> bool:
        """Is this the client column?

        The DDIC data type is the answer; the name is only a hint. ``CLNT`` is
        what makes a column the client, and SAP does not insist it be called
        MANDT — ACDOCA calls it **RCLNT**, and a name-only check let it through
        as an ordinary key field. SAP then refused the generated view outright:

            E  Client field is not allowed in the entity view

        The name check stays as a fallback for metadata sources that do not
        carry a data type, such as the fixtures.
        """
        return self.data_type.upper() == "CLNT" or is_client_field(self.name)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "position": self.position,
            "is_key": self.is_key,
            "data_element": self.data_element,
            "data_type": self.data_type,
            "length": self.length,
            "decimals": self.decimals,
            "label": self.label,
            "not_null": self.not_null,
            # Without these the cache silently drops the currency and unit
            # references, and a cached table generates DDL that a freshly-read
            # one would not.
            "ref_table": self.ref_table,
            "ref_field": self.ref_field,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "FieldMeta":
        return cls(**raw)


@dataclass
class TableMeta:
    """A DDIC table — DD02L header plus DD03L fields."""

    name: str
    table_class: TableClass = TableClass.UNKNOWN
    delivery_class: str = ""
    package: str = ""
    owner: Owner = Owner.UNKNOWN
    description: str = ""
    fields: list[FieldMeta] = field(default_factory=list)
    estimated_rows: int | None = None

    # -- keys -------------------------------------------------------------
    @property
    def key_fields(self) -> list[FieldMeta]:
        return [f for f in self.fields if f.is_key]

    @property
    def business_key_fields(self) -> list[FieldMeta]:
        """Key fields excluding the client column.

        The client is handled by the framework, not exposed as an ordinary key
        (R-23 / Note 2890171), so key-exposure checks compare against this list.
        """
        return [f for f in self.key_fields if not f.is_client]

    @property
    def has_client_field(self) -> bool:
        return any(f.is_client for f in self.fields)

    @property
    def client_field(self) -> FieldMeta | None:
        return next((f for f in self.fields if f.is_client), None)

    @property
    def has_primary_key(self) -> bool:
        """A genuine primary key — a Datasphere Replication Flow prerequisite."""
        return bool(self.business_key_fields)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "table_class": self.table_class.value,
            "delivery_class": self.delivery_class,
            "package": self.package,
            "owner": self.owner.value,
            "description": self.description,
            "estimated_rows": self.estimated_rows,
            "fields": [f.to_dict() for f in self.fields],
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "TableMeta":
        return cls(
            name=raw["name"],
            table_class=TableClass.parse(raw.get("table_class")),
            delivery_class=raw.get("delivery_class", ""),
            package=raw.get("package", ""),
            owner=Owner(raw.get("owner", Owner.UNKNOWN.value)),
            description=raw.get("description", ""),
            estimated_rows=raw.get("estimated_rows"),
            fields=[FieldMeta.from_dict(f) for f in raw.get("fields", [])],
        )

    def field_by_name(self, name: str) -> FieldMeta | None:
        target = name.upper()
        return next((f for f in self.fields if f.name.upper() == target), None)

    # -- risk classification (F-17, F-21) ---------------------------------
    @property
    def is_hot(self) -> bool:
        """High-write transactional table — highest trigger-load risk."""
        return self.name.upper() in HOT_TABLES

    @property
    def is_configuration(self) -> bool:
        """Customising/config content, by delivery class then by known name.

        Drives the smart default in the CDC mapping builder: map transactional
        tables, omit config tables. Delivery class is checked first because it
        is authoritative; the name list only catches tables whose class was not
        read.
        """
        if self.delivery_class:
            return self.delivery_class.strip().upper() in CONFIG_DELIVERY_CLASSES
        return self.name.upper() in CONFIG_TABLES


@dataclass
class ObjectMeta:
    """Repository object header — TADIR plus the APIS release contract."""

    name: str
    kind: str = "DDLS"
    package: str = ""
    software_component: str = ""
    owner: Owner = Owner.UNKNOWN
    api_state: ApiState = ApiState.UNKNOWN
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "package": self.package,
            "software_component": self.software_component,
            "owner": self.owner.value,
            "api_state": self.api_state.value,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "ObjectMeta":
        return cls(
            name=raw["name"],
            kind=raw.get("kind", "DDLS"),
            package=raw.get("package", ""),
            software_component=raw.get("software_component", ""),
            owner=Owner(raw.get("owner", Owner.UNKNOWN.value)),
            api_state=ApiState.parse(raw.get("api_state")),
            description=raw.get("description", ""),
        )

    @property
    def is_modifiable(self) -> bool:
        """Only customer objects. SAP objects are never modified by this tool.

        This is stricter than Appendix D.5, which allows that an unreleased SAP
        view is "technically modifiable" and asks for a hard warning. It is a
        deliberate policy decision: the tool never edits, and never offers to
        edit, a standard SAP object — released or not. The route for an SAP
        view is always a Z-wrapper.

        The reasoning is that "technically modifiable" is a trap. The
        modification survives until the next upgrade or support package
        overwrites it, at which point the extraction silently stops working and
        nobody connects the two events. Offering it as an option means somebody
        eventually takes it.

        Unknown ownership is also unmodifiable — fail safe.
        """
        return self.owner is Owner.CUSTOMER

    @property
    def modifiability_reason(self) -> str:
        if self.owner is Owner.CUSTOMER:
            return "customer object — annotate in place"
        if self.owner is Owner.UNKNOWN:
            return "ownership unknown — assumed unmodifiable (fail safe)"
        if self.api_state.forbids_modification:
            return (
                f"SAP object, released {self.api_state.value} — wrapper only, "
                f"never modify"
            )
        if self.api_state is ApiState.UNKNOWN:
            return "SAP object, release state unknown — wrapper only"
        return (
            "SAP object, not released — wrapper only. It is technically "
            "modifiable, but the modification is silently lost at the next "
            "upgrade and the extraction stops working with nothing to connect "
            "it to."
        )
