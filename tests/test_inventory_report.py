"""Inventory sweep (F-05/F-06) and assessment report (F-34)."""

from __future__ import annotations

import json

import pytest

from cdcforge.inventory import InventoryScanner, ScanProgress, describe_extraction
from cdcforge.metadata.base import MetadataSource
from cdcforge.metadata.types import (
    ApiState,
    FieldMeta,
    ObjectMeta,
    Owner,
    TableClass,
    TableMeta,
)
from cdcforge.parsing.ddl import parse_ddl
from cdcforge.report import EffortModel, ReportData, write_all, write_excel, write_html, write_json
from cdcforge.store import Store
from cdcforge.triage import Bucket


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "scan.sqlite", profile_id="TEST")


# ---------------------------------------------------------------------------
# A metadata source whose contents can change between scans
# ---------------------------------------------------------------------------


class MutableMetadata(MetadataSource):
    name = "mutable"

    def __init__(self, sources: dict[str, str]):
        self.sources = dict(sources)
        self.reads: list[str] = []
        self.table = TableMeta(
            name="ZCUSTORDER",
            table_class=TableClass.TRANSPARENT,
            fields=[
                FieldMeta("MANDT", 1, is_key=True),
                FieldMeta("ORDERID", 2, is_key=True),
                FieldMeta("CUSTOMER", 3),
            ],
        )

    def get_view_source(self, name):
        self.reads.append(name.upper())
        return self.sources.get(name.upper())

    @property
    def view_reads(self) -> list[str]:
        """Reads that actually fetched a view.

        The stack resolver asks whether every source name is a view before
        falling back to a table, so the raw read log contains table names too.
        Those probes are answered from metadata the scanner already holds; only
        the view fetches are the expensive ones the cache exists to avoid.
        """
        return [n for n in self.reads if n in self.sources]

    def get_table(self, name):
        return self.table if name.upper() == "ZCUSTORDER" else None

    def get_object(self, name):
        return ObjectMeta(
            name=name.upper(), owner=Owner.CUSTOMER, api_state=ApiState.NOT_RELEASED
        )

    def list_views(self):
        return sorted(self.sources)

    def list_tables(self):
        return ["ZCUSTORDER"]


GOOD = (
    "@Analytics: { dataExtraction: { enabled: true,"
    " delta.changeDataCapture.automatic: true } }\n"
    "define view entity ZI_ORD as select from zcustorder"
    " { key orderid as OrderId, customer as Customer }"
)
NO_EXTRACTION = (
    "define view entity ZI_ORD as select from zcustorder"
    " { key orderid as OrderId, customer as Customer }"
)


# ---------------------------------------------------------------------------
# Annotation state extraction
# ---------------------------------------------------------------------------


def test_describe_extraction_reads_the_three_columns():
    enabled, delta, cdc = describe_extraction(parse_ddl(GOOD))
    assert (enabled, delta, cdc) == (True, "CDC", "automatic")

    enabled, delta, cdc = describe_extraction(parse_ddl(NO_EXTRACTION))
    assert (enabled, delta, cdc) == (False, "none", "none")


def test_describe_extraction_distinguishes_mapping_from_automatic(metadata):
    view = parse_ddl(metadata.get_view_source("ZI_SALESORDER_CDC"))
    assert describe_extraction(view)[2] == "mapping"


def test_describe_extraction_spots_timestamp_delta(metadata):
    view = parse_ddl(metadata.get_view_source("ZI_BYELEMENT_DELTA"))
    enabled, delta, cdc = describe_extraction(view)
    assert (enabled, delta, cdc) == (True, "byElement", "none")


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def test_scan_populates_the_store(metadata, store):
    result = InventoryScanner(metadata, store).scan()
    assert result.total == len(metadata.list_views())
    assert store.view_count() == result.total
    assert store.stats()["verdicts"] > 0
    assert result.summary.count(Bucket.READY) > 0


def test_second_scan_comes_from_the_cache(store):
    source = MutableMetadata({"ZI_ORD": GOOD})
    scanner = InventoryScanner(source, store)

    first = scanner.scan()
    assert first.fetched == 1 and first.cached == 0

    second = scanner.scan()
    assert second.fetched == 0 and second.cached == 1
    assert source.view_reads == ["ZI_ORD"]  # the source was not fetched twice


def test_refresh_re_reads_the_source(store):
    source = MutableMetadata({"ZI_ORD": GOOD})
    scanner = InventoryScanner(source, store)
    scanner.scan()
    scanner.scan(refresh=True)
    assert source.view_reads == ["ZI_ORD", "ZI_ORD"]


def test_changed_source_produces_a_fresh_verdict(store):
    source = MutableMetadata({"ZI_ORD": GOOD})
    scanner = InventoryScanner(source, store)
    first = scanner.scan()
    assert first.summary.count(Bucket.READY) == 1

    source.sources["ZI_ORD"] = NO_EXTRACTION
    second = scanner.scan(refresh=True)
    assert second.summary.count(Bucket.READY) == 0
    assert second.summary.count(Bucket.FIXABLE) == 1


def test_unreadable_objects_are_listed_separately_from_verdicts(store):
    class Missing(MutableMetadata):
        def get_view_source(self, name):
            return None

    result = InventoryScanner(Missing({"ZI_GONE": ""}), store).scan()
    assert result.unreadable == [("ZI_GONE", "no DDL source returned")]
    assert result.assessments == []


def test_limit_stops_early(metadata, store):
    result = InventoryScanner(metadata, store).scan(limit=3)
    assert result.total == 3


def test_named_scan_only_touches_those_objects(metadata, store):
    result = InventoryScanner(metadata, store).scan(["ZI_BUSINESSAREA"])
    assert [a.object_name for a in result.assessments] == ["ZI_BUSINESSAREA"]


def test_dependencies_and_base_tables_are_recorded(metadata, store):
    InventoryScanner(metadata, store).scan(["ZI_SALESORDER_CDC"])
    row = store.views()[0]
    assert "SNWD_SO" in row["base_tables"]
    assert "ZI_SALESORDER_CDC" in store.dependents_of("SNWD_SO")


def test_table_inventory_marks_which_tables_have_a_view(metadata, store):
    InventoryScanner(metadata, store).scan()
    bare = {t["table_name"] for t in store.tables(bare_only=True)}
    assert "ZLEGACY_POOL" in bare      # no view reads it
    assert "ZCUSTORDER" not in bare     # ZI_CUSTORDER does


# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------


def test_progress_is_reported_for_every_object(metadata, store):
    seen: list[ScanProgress] = []
    InventoryScanner(metadata, store, progress=seen.append).scan(limit=5)
    assert len(seen) == 6  # one per object, plus the final 100%
    assert seen[-1].done == seen[-1].total
    assert seen[-1].percent == 100.0


def test_eta_is_withheld_until_it_would_be_honest(metadata, store):
    seen: list[ScanProgress] = []
    InventoryScanner(metadata, store, progress=seen.append).scan(limit=8)
    # An ETA from one or two samples swings wildly and teaches the user to
    # distrust the number.
    assert seen[0].eta_s is None
    assert seen[1].eta_s is None
    assert seen[-2].eta_s is not None


# ---------------------------------------------------------------------------
# Report (F-34)
# ---------------------------------------------------------------------------


@pytest.fixture
def scanned(metadata, store) -> Store:
    InventoryScanner(metadata, store).scan()
    return store


def test_report_counts_match_the_store(scanned):
    data = ReportData.from_store(scanned, scanned.all_assessments())
    assert data.counts["TOTAL_VIEWS"] == scanned.view_count()
    assert data.counts[Bucket.READY.value] > 0
    assert data.counts["BARE_TABLES"] == len(scanned.tables(bare_only=True))


def test_a_report_with_no_inventory_says_why_its_counts_are_zero(store, metadata):
    """Counts come from the inventory's view records; findings come from the
    assessments handed in. They are different tables and can disagree.

    A report whose headline says nothing was scanned while carrying findings
    for dozens of objects is contradicting itself, and reading the zero as
    "nothing to do" is the obvious mistake.
    """
    from cdcforge.rules import validate_object
    from cdcforge.store import source_hash

    for name in metadata.list_views()[:5]:
        body = metadata.get_view_source(name)
        store.put_verdicts(validate_object(name, metadata), source_hash(body))

    data = ReportData.from_store(store, store.all_assessments())
    assert data.counts["TOTAL_VIEWS"] == 0
    assert data.findings, "findings are present even though counts are not"
    assert any("no inventory has been run" in w for w in data.warnings), (
        f"the contradiction must be stated, got {data.warnings}"
    )


def test_a_scanned_report_carries_no_such_warning(scanned):
    data = ReportData.from_store(scanned, scanned.all_assessments())
    assert not any("no inventory has been run" in w for w in data.warnings)


def test_report_carries_findings_with_their_sap_source(scanned):
    data = ReportData.from_store(scanned, scanned.all_assessments())
    assert data.findings
    finding = next(f for f in data.findings if f["rule"] == "R-03")
    assert finding["sap_source"]
    assert finding["message"]


def test_effort_estimate_shows_its_working(scanned):
    data = ReportData.from_store(scanned, scanned.all_assessments())
    assert data.total_effort_days > 0
    assert sum(data.effort_days.values()) == pytest.approx(data.total_effort_days, abs=0.05)
    # The numbers are planning defaults, and the report has to say so.
    assumptions = " ".join(data.effort.assumptions()).lower()
    assert "not measurements" in assumptions


def test_effort_model_is_configurable(scanned):
    doubled = EffortModel(fixable_annotate_days=1.0, wrapper_days=2.0,
                          review_days=2.0, bare_table_days=1.0, redesign_days=6.0)
    base = ReportData.from_store(scanned, scanned.all_assessments())
    tuned = ReportData.from_store(scanned, scanned.all_assessments(), effort=doubled)
    assert tuned.total_effort_days == pytest.approx(base.total_effort_days * 2, abs=0.1)


def test_recommended_actions_are_specific(scanned):
    actions = ReportData.from_store(scanned, scanned.all_assessments()).recommended_actions()
    text = " ".join(actions)
    assert "CDC-ready" in text
    assert "checkruns" in text


def test_report_notes_when_findings_were_not_supplied(scanned):
    data = ReportData.from_store(scanned)  # no assessments passed
    assert data.warnings
    assert "counts and per-object verdicts only" in data.warnings[0]


def test_json_export_round_trips(scanned, tmp_path):
    data = ReportData.from_store(scanned, scanned.all_assessments())
    path = write_json(data, tmp_path / "r.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["counts"]["TOTAL_VIEWS"] == data.counts["TOTAL_VIEWS"]
    assert payload["effort_assumptions"]
    assert payload["recommended_actions"]


def test_html_export_is_self_contained_and_complete(scanned, tmp_path):
    data = ReportData.from_store(scanned, scanned.all_assessments())
    html = write_html(data, tmp_path / "r.html").read_text(encoding="utf-8")
    assert "<style>" in html and "http://" not in html.split("<style>")[1][:2000]
    for heading in ("Ready", "Fixable", "What to do", "Effort estimate"):
        assert heading in html
    assert "No business data was read" in html


def test_html_escapes_content(tmp_path, store):
    data = ReportData(profile_id="P", system_id="<script>alert(1)</script>")
    html = write_html(data, tmp_path / "r.html").read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_excel_export_has_the_three_lists(scanned, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    data = ReportData.from_store(scanned, scanned.all_assessments())
    path = write_excel(data, tmp_path / "r.xlsx")
    workbook = openpyxl.load_workbook(path)
    assert {"Summary", "Ready", "Fixable", "Bare tables"} <= set(workbook.sheetnames)
    assert workbook["Ready"].max_row >= 2


def test_report_says_so_when_excel_is_unavailable(scanned, tmp_path, monkeypatch):
    """A report that quietly omits a format is a silent gap.

    The check has to happen before anything is written: catching the failure
    afterwards meant the JSON and HTML had already gone out with no mention
    that a format was missing.
    """
    import cdcforge.report as report_module

    monkeypatch.setattr(report_module, "excel_available", lambda: False)
    data = report_module.ReportData.from_store(scanned, scanned.all_assessments())
    written = report_module.write_all(data, tmp_path)

    assert not any(p.suffix == ".xlsx" for p in written)
    assert any("openpyxl" in w for w in data.warnings)
    html = next(p for p in written if p.suffix == ".html").read_text(encoding="utf-8")
    assert "openpyxl" in html  # the warning reaches the reader, not just a log


def test_write_all_produces_every_format(scanned, tmp_path):
    written = write_all(ReportData.from_store(scanned, scanned.all_assessments()), tmp_path)
    suffixes = {p.suffix for p in written}
    assert {".json", ".html"} <= suffixes
