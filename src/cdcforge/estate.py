"""What has already been built here.

The tool was good at answering "what should we build for VBAP" and had no
memory at all. Run it twice over the same list and it proposes the same objects
the second time, because nothing ever looked at what was already in the system.

That is not a theoretical gap. On the reference system, planning ``BKPF``
suggested a wrapper over ``I_AccountingDocument`` named ``ZW_ACCOUNTINGDOCUMENT``
— while ``ZW_ACCTGDOC`` already existed, was already delta-supported, and was
already a wrapper over exactly that view. The sheet said nothing, and a
near-duplicate got built.

So before suggesting anything, the tool now looks. The question it answers is
narrow and worth stating precisely: *which custom extraction-enabled views are
rooted on this table, or built over this view?* Rooted, not merely mentioning —
following the FROM chain rather than any join, because what a row **is** comes
from the root, and a view that joins VBAP is not a feed for VBAP.

This reads only. It is deliberately cheap enough to run before every plan: the
name list is one query the connector already caches, and the sources it then
reads are the customer's own objects, of which there are tens, not thousands.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cdcforge.cds import ANN_CDC_AUTOMATIC, ANN_CDC_MAPPING, extraction_enabled
from cdcforge.lineage import root_table_name
from cdcforge.metadata.base import MetadataSource
from cdcforge.parsing.ddl import parse_ddl

#: Customer namespaces. Everything else is SAP's, and SAP's own extraction
#: views are candidates rather than things "we" built.
CUSTOMER_PREFIXES = ("Z", "Y")

_MAX_DEPTH = 8


@dataclass
class ExistingObject:
    """One custom extraction view already in the system."""

    name: str
    base: str = ""
    """What its FROM names directly — a view or a table."""

    root_table: str = ""
    """The table its FROM chain terminates on. Empty when it could not be
    followed, which is reported rather than guessed at."""

    declares_cdc: bool = False
    extraction_enabled: bool = False
    label: str = ""
    unreadable: bool = False

    @property
    def covers_a_table(self) -> bool:
        return bool(self.root_table)

    def render(self) -> str:
        if self.unreadable:
            return f"{self.name} (could not be read)"
        over = self.root_table or self.base or "?"
        delta = "delta" if self.declares_cdc else "no delta"
        return f"{self.name} over {over} ({delta})"


@dataclass
class Estate:
    """Every custom extraction view, indexed by what it feeds."""

    objects: list[ExistingObject] = field(default_factory=list)
    surveyed: int = 0
    unreadable: int = 0

    def covering(self, table: str) -> list[ExistingObject]:
        """Objects rooted on ``table`` — the ones that already feed it."""
        upper = (table or "").upper()
        return [o for o in self.objects if o.root_table == upper]

    def over_base(self, view: str) -> list[ExistingObject]:
        """Objects built directly over ``view``.

        Kept apart from :meth:`covering` because they answer different
        questions. Two wrappers over different views of the same table are not
        duplicates; two over the same view almost certainly are.
        """
        upper = (view or "").upper()
        return [o for o in self.objects if o.base == upper]

    def note_for(self, table: str, base: str = "") -> str:
        """One line for the decision sheet, or empty when nothing exists.

        Says *what* exists rather than what to do about it. Whether an existing
        object makes the new one unnecessary depends on which columns it
        exposes and who is already consuming it — facts the tool does not have
        and the reader does.
        """
        exact = self.over_base(base) if base else []
        if exact:
            names = ", ".join(o.name for o in exact)
            return (
                f"ALREADY BUILT: {names} "
                f"{'is' if len(exact) == 1 else 'are'} already built over "
                f"{base}. Check it before building another."
            )
        covering = self.covering(table)
        if covering:
            names = ", ".join(o.render() for o in covering[:4])
            more = f" (+{len(covering) - 4} more)" if len(covering) > 4 else ""
            return (
                f"ALREADY BUILT over {table}: {names}{more}. A different base, "
                f"so possibly not a duplicate — check what they expose."
            )
        return ""

    def render(self) -> str:
        lines = [
            f"{len(self.objects)} custom extraction view(s) "
            f"from {self.surveyed} name(s)"
        ]
        if self.unreadable:
            lines.append(f"  {self.unreadable} could not be read")
        by_table: dict[str, list[ExistingObject]] = {}
        for obj in self.objects:
            by_table.setdefault(obj.root_table or "(unresolved)", []).append(obj)
        for table in sorted(by_table):
            names = ", ".join(o.name for o in sorted(by_table[table], key=lambda o: o.name))
            lines.append(f"  {table:<18} {names}")
        return "\n".join(lines)


def survey(
    metadata: MetadataSource,
    *,
    prefixes: tuple[str, ...] = CUSTOMER_PREFIXES,
    limit: int = 500,
) -> Estate:
    """Read every custom extraction-enabled view and work out what it feeds.

    ``None`` from :meth:`extraction_enabled_views` means the system could not
    say, which yields an empty estate — and an empty estate must never be read
    as "nothing has been built". :attr:`Estate.surveyed` is how a caller tells
    the two apart.
    """
    estate = Estate()
    # Always from the system. This is the one list the tool invalidates itself
    # — every view it creates or drops changes it — and a stale copy produces
    # the precise wrong answer this module exists to prevent: "nothing feeds
    # BKPF" when ZW_ACCTGDOC has fed it since Tuesday. Measured: a cached run
    # found 25 where the system had 42, missing every object created that day.
    metadata.forget_extraction_enabled()
    names = metadata.extraction_enabled_views()
    if not names:
        return estate

    custom = sorted(
        n for n in names if n.upper().startswith(tuple(p.upper() for p in prefixes))
    )[:limit]
    estate.surveyed = len(custom)

    for name in custom:
        try:
            estate.objects.append(_examine(metadata, name))
        except Exception:
            # One unreadable object must not cost the survey. "Could not read
            # this one" is a result the estate already has a shape for.
            estate.objects.append(ExistingObject(name=name.upper(), unreadable=True))
    estate.unreadable = sum(1 for o in estate.objects if o.unreadable)
    return estate


def _examine(metadata: MetadataSource, name: str) -> ExistingObject:
    obj = ExistingObject(name=name.upper())
    source = metadata.get_view_source(name)
    if source is None:
        obj.unreadable = True
        return obj

    view = parse_ddl(source, name_hint=name)
    if view.has_fatal_issue or view.from_source is None:
        obj.unreadable = True
        return obj

    label = (
        view.annotations.get("endusertext.label")
        if view.annotations is not None
        else None
    )
    obj.label = str(label) if isinstance(label, str) else ""
    obj.base = view.from_source.name.upper()
    obj.extraction_enabled = extraction_enabled(view.annotations)
    obj.declares_cdc = bool(
        view.annotations is not None
        and (
            view.annotations.is_true(ANN_CDC_AUTOMATIC)
            or view.annotations.get(ANN_CDC_MAPPING) is not None
        )
    )
    obj.root_table = root_table_name(metadata, obj.base)
    return obj


