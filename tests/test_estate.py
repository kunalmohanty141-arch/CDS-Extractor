"""What has already been built here.

The gap this closes was found by running the tool twice. Planning BKPF
suggested a wrapper over I_AccountingDocument called ZW_ACCOUNTINGDOCUMENT,
while ZW_ACCTGDOC already existed, was already delta-supported, and was already
a wrapper over exactly that view. Nothing said so, and a near-duplicate got
built.
"""

from __future__ import annotations

import pytest

from cdcforge.estate import Estate, ExistingObject, survey
from cdcforge.metadata.base import MetadataSource

_WRAPPER = """
@Analytics.dataExtraction: {{ enabled: true,
  delta.changeDataCapture.automatic: true }}
@EndUserText.label: '{label}'
define view entity {name} as select from {base} {{ key Foo as Foo }}
"""

_PLAIN = """
@EndUserText.label: 'not an extraction view'
define view entity {name} as select from {base} {{ key Foo as Foo }}
"""


class FakeSource(MetadataSource):
    """Views by name, tables by name, and nothing else."""

    def __init__(self, views: dict[str, str], tables: tuple[str, ...] = ()):
        self.views = {k.upper(): v for k, v in views.items()}
        self.tables = {t.upper() for t in tables}
        self.reads: list[str] = []
        self.nameset_reads = 0
        self.forgotten = 0

    def describe(self) -> str:
        return "fake"

    def get_view_source(self, name: str):
        self.reads.append(name.upper())
        return self.views.get(name.upper())

    def get_table(self, name: str):
        if name.upper() not in self.tables:
            return None
        from cdcforge.metadata.types import TableMeta

        return TableMeta(name=name.upper())

    def get_object(self, name: str):
        return getattr(self, "objects", {}).get(name.upper())

    def extraction_enabled_views(self):
        self.nameset_reads += 1
        return set(self.views)

    def forget_extraction_enabled(self) -> None:
        self.forgotten += 1


def _system() -> FakeSource:
    return FakeSource(
        views={
            # ours, a wrapper over a view that is rooted on BKPF
            "ZW_ACCTGDOC": _WRAPPER.format(
                name="ZW_ACCTGDOC", base="I_ACCOUNTINGDOCUMENT",
                label="Extraction wrapper for I_AccountingDocument",
            ),
            # ours, straight over a table
            "ZI_KNA1": _WRAPPER.format(
                name="ZI_KNA1", base="KNA1", label="Extraction view for KNA1",
            ),
            # SAP's — a candidate, not something "we" built
            "I_ACCOUNTINGDOCUMENT": _PLAIN.format(
                name="I_ACCOUNTINGDOCUMENT", base="BKPF",
            ),
            "C_SOMETHINGDEX": _WRAPPER.format(
                name="C_SOMETHINGDEX", base="BKPF", label="SAP's own",
            ),
        },
        tables=("BKPF", "KNA1"),
    )


def test_only_customer_objects_count_as_ours():
    """SAP's own extraction views are candidates to build on, not things we
    built. Counting them would suppress every suggestion."""
    estate = survey(_system())
    assert sorted(o.name for o in estate.objects) == ["ZI_KNA1", "ZW_ACCTGDOC"]


def test_a_wrapper_is_attributed_to_the_table_it_is_rooted_on():
    estate = survey(_system())
    wrapper = next(o for o in estate.objects if o.name == "ZW_ACCTGDOC")
    assert wrapper.base == "I_ACCOUNTINGDOCUMENT"
    assert wrapper.root_table == "BKPF"
    assert wrapper.declares_cdc


def test_covering_finds_objects_by_root_table():
    estate = survey(_system())
    assert [o.name for o in estate.covering("BKPF")] == ["ZW_ACCTGDOC"]
    assert [o.name for o in estate.covering("KNA1")] == ["ZI_KNA1"]
    assert estate.covering("VBAP") == []


def test_over_base_is_a_different_question_from_covering():
    """Two wrappers over different views of one table are not duplicates.
    Two over the same view almost certainly are."""
    estate = survey(_system())
    assert [o.name for o in estate.over_base("I_ACCOUNTINGDOCUMENT")] == [
        "ZW_ACCTGDOC"
    ]
    assert estate.over_base("BKPF") == [o for o in estate.objects if o.name == "ZI_KNA1"][:0]


def test_the_note_names_the_exact_duplicate_first():
    estate = survey(_system())
    note = estate.note_for("BKPF", "I_ACCOUNTINGDOCUMENT")
    assert note.startswith("ALREADY BUILT:")
    assert "ZW_ACCTGDOC" in note


def test_a_different_base_over_the_same_table_is_flagged_more_softly():
    """It may be a genuinely different feed, so the note says so rather than
    asserting a duplicate."""
    estate = survey(_system())
    note = estate.note_for("BKPF", "I_SOMETHINGELSE")
    assert note.startswith("ALREADY BUILT over BKPF")
    assert "possibly not a duplicate" in note


def test_nothing_existing_yields_no_note():
    estate = survey(_system())
    assert estate.note_for("VBAP", "I_SALESDOCUMENTITEM") == ""


def test_a_view_that_cannot_be_read_is_counted_not_silently_dropped():
    source = FakeSource(views={"ZBROKEN": "this is not DDL at all"})
    estate = survey(source)
    assert estate.unreadable == 1
    assert estate.objects[0].unreadable
    assert not estate.objects[0].covers_a_table


def test_an_unresolvable_root_is_empty_not_guessed():
    """The FROM chain runs into a view nobody can read. Attributing it to the
    wrong table would suppress a suggestion that should have been made."""
    source = FakeSource(
        views={
            "ZW_MYSTERY": _WRAPPER.format(
                name="ZW_MYSTERY", base="I_UNKNOWN", label="x"
            )
        }
    )
    estate = survey(source)
    assert estate.objects[0].root_table == ""
    assert estate.covering("BKPF") == []


def test_a_system_that_cannot_answer_yields_an_empty_survey():
    """`None` means the system could not say, and an empty estate must never
    read as "nothing has been built" — `surveyed` is how a caller tells them
    apart."""

    class Silent(FakeSource):
        def extraction_enabled_views(self):
            return None

    estate = survey(Silent(views={}))
    assert estate.objects == []
    assert estate.surveyed == 0


def test_a_cycle_in_the_from_chain_terminates():
    source = FakeSource(
        views={
            "ZW_A": _WRAPPER.format(name="ZW_A", base="ZW_B", label="a"),
            "ZW_B": _WRAPPER.format(name="ZW_B", base="ZW_A", label="b"),
        }
    )
    estate = survey(source)  # must not hang
    assert all(o.root_table == "" for o in estate.objects)


def test_the_name_list_is_never_served_from_cache():
    """The one list this tool invalidates itself.

    Measured on the reference system: a cached run found 25 custom extraction
    views where there were 42, missing every object created that day — so the
    survey reported that nothing fed BKPF while ZW_ACCTGDOC had fed it for
    hours. That is the precise wrong answer the survey exists to prevent, and
    caching reintroduced it.
    """
    source = _system()
    survey(source)
    assert source.forgotten == 1, "the cached name list must be dropped first"


def test_one_object_that_explodes_does_not_lose_the_survey():
    class _Exploding(FakeSource):
        def get_view_source(self, name):
            if name.upper() == "ZW_BOOM":
                raise RuntimeError("the connection went away")
            return super().get_view_source(name)

    views = dict(_system().views)
    views["ZW_BOOM"] = _WRAPPER.format(name="ZW_BOOM", base="BKPF", label="x")
    estate = survey(_Exploding(views=views, tables=("BKPF", "KNA1")))

    assert {o.name for o in estate.objects} == {"ZI_KNA1", "ZW_ACCTGDOC", "ZW_BOOM"}
    assert next(o for o in estate.objects if o.name == "ZW_BOOM").unreadable
    assert [o.name for o in estate.covering("BKPF")] == ["ZW_ACCTGDOC"]


def test_render_groups_by_table():
    estate = survey(_system())
    text = estate.render()
    assert "BKPF" in text and "ZW_ACCTGDOC" in text
    assert "KNA1" in text and "ZI_KNA1" in text


def test_a_bare_estate_answers_nothing_rather_than_erroring():
    estate = Estate()
    assert estate.covering("BKPF") == []
    assert estate.note_for("BKPF", "I_X") == ""


@pytest.mark.parametrize(
    ("obj", "expected"),
    [
        (ExistingObject("ZW_X", root_table="BKPF", declares_cdc=True),
         "ZW_X over BKPF (delta)"),
        (ExistingObject("ZW_Y", base="I_X"), "ZW_Y over I_X (no delta)"),
        (ExistingObject("ZW_Z", unreadable=True), "ZW_Z (could not be read)"),
    ],
)
def test_rendering_one_object(obj, expected):
    assert obj.render() == expected
