"""UI input parsing.

The forgiving behaviour here is deliberate: a consultant pastes a column out of
a spreadsheet the customer sent, and it arrives with quotes, blank rows, a
header and trailing commas. Refusing that makes the tool feel broken when the
user did nothing wrong.
"""

from __future__ import annotations

import io

import pytest

from cdcforge.ui.inputs import (
    looks_like_header,
    read_csv,
    read_upload,
    read_workbook,
    sort_by_kind,
    split_names,
)


# ---------------------------------------------------------------------------
# Which sheet is which — workbook_bytes is defined further down
# ---------------------------------------------------------------------------


def test_sheet_names_win_over_sheet_order():
    """A tab called "Tables" means tables wherever it sits.

    Reading it positionally would tag every object in the workbook wrongly, and
    the tool would then send tables to the view validator.
    """
    data = workbook_bytes(
        [("Tables", ["ZCUSTORDER", "ZORDERITEM"]), ("Views", ["ZI_CUSTORDER"])]
    )
    views, tables = read_workbook(data)
    assert split_names(views) == ["ZI_CUSTORDER"]
    assert split_names(tables) == ["ZCUSTORDER", "ZORDERITEM"]


def test_sheet_order_still_decides_when_the_names_say_nothing():
    data = workbook_bytes([("Sheet1", ["ZI_CUSTORDER"]), ("Sheet2", ["ZCUSTORDER"])])
    views, tables = read_workbook(data)
    assert split_names(views) == ["ZI_CUSTORDER"]
    assert split_names(tables) == ["ZCUSTORDER"]


# ---------------------------------------------------------------------------
# Re-sorting against the system
# ---------------------------------------------------------------------------


def test_a_table_listed_as_a_view_is_moved(metadata):
    """The sheet is the customer's opinion; DDIC is the fact.

    Left where it was, ZCUSTORDER would go to the view validator and come back
    UNPARSEABLE — an answer to a question the user never asked.
    """
    views, tables, moved, unknown = sort_by_kind(
        metadata, ["ZI_CUSTORDER", "ZCUSTORDER"], []
    )
    assert views == ["ZI_CUSTORDER"]
    assert tables == ["ZCUSTORDER"]
    assert moved == [("ZCUSTORDER", "tables")]
    assert unknown == []


def test_a_view_listed_as_a_table_is_moved(metadata):
    views, tables, moved, _unknown = sort_by_kind(metadata, [], ["ZI_CUSTORDER"])
    assert views == ["ZI_CUSTORDER"]
    assert tables == []
    assert moved == [("ZI_CUSTORDER", "views")]


def test_an_unknown_name_is_reported_not_guessed(metadata):
    """Silently dropping it would hide a typo the user needs to see."""
    views, tables, _moved, unknown = sort_by_kind(metadata, ["ZI_NOPE_AT_ALL"], [])
    assert views == []
    assert tables == []
    assert unknown == ["ZI_NOPE_AT_ALL"]


def test_correctly_sorted_names_are_left_alone(metadata):
    views, tables, moved, unknown = sort_by_kind(
        metadata, ["ZI_CUSTORDER"], ["ZCUSTORDER"]
    )
    assert views == ["ZI_CUSTORDER"]
    assert tables == ["ZCUSTORDER"]
    assert moved == []
    assert unknown == []


# ---------------------------------------------------------------------------
# Pasted text
# ---------------------------------------------------------------------------


def test_names_are_upper_cased_and_deduplicated():
    assert split_names("zi_a\nZI_A\n zi_b ") == ["ZI_A", "ZI_B"]


def test_order_is_preserved():
    assert split_names("ZI_C\nZI_A\nZI_B") == ["ZI_C", "ZI_A", "ZI_B"]


@pytest.mark.parametrize(
    "text",
    [
        "ZI_A,ZI_B",
        "ZI_A; ZI_B",
        "ZI_A\n\n\nZI_B\n",
        '"ZI_A"\n\'ZI_B\'',
        "  ZI_A  \n\tZI_B\t",
    ],
)
def test_common_paste_shapes_all_parse(text):
    assert split_names(text) == ["ZI_A", "ZI_B"]


def test_empty_input_is_not_an_error():
    assert split_names("") == []
    assert split_names(None) == []


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["View name", "CDS View", "Object", "TABLE NAME"])
def test_obvious_headings_are_recognised(value):
    assert looks_like_header(value)


@pytest.mark.parametrize(
    "value", ["ZI_SALESORDER_CDC", "I_GoodsMovementDocumentDEX", "ZCUSTORDER"]
)
def test_real_object_names_are_not_mistaken_for_headings(value):
    # Losing a row the user asked about is worse than carrying one junk row.
    assert not looks_like_header(value)


def test_csv_drops_a_header_but_keeps_the_rest():
    csv = "View Name\nZI_A\nZI_B\n"
    assert split_names(read_csv(csv.encode())) == ["ZI_A", "ZI_B"]


def test_csv_without_a_header_keeps_every_row():
    assert split_names(read_csv(b"ZI_A\nZI_B\n")) == ["ZI_A", "ZI_B"]


def test_csv_takes_only_the_first_column():
    assert split_names(read_csv(b"ZI_A,ignored,junk\nZI_B,more")) == ["ZI_A", "ZI_B"]


def test_csv_tolerates_a_byte_order_mark():
    assert split_names(read_csv("﻿ZI_A\nZI_B".encode())) == ["ZI_A", "ZI_B"]


# ---------------------------------------------------------------------------
# Workbooks
# ---------------------------------------------------------------------------


def workbook_bytes(sheets: list[tuple[str, list[str]]]) -> bytes:
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    book.remove(book.active)
    for title, rows in sheets:
        sheet = book.create_sheet(title=title)
        for value in rows:
            sheet.append([value])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_first_sheet_is_views_and_second_is_tables():
    """Sheet *order* carries the meaning — the customer's tabs will be called
    whatever they were called."""
    data = workbook_bytes([
        ("Anything", ["View Name", "ZI_A", "ZI_B"]),
        ("Whatever", ["Table", "ZCUSTORDER"]),
    ])
    views, tables = read_workbook(data)
    assert split_names(views) == ["ZI_A", "ZI_B"]
    assert split_names(tables) == ["ZCUSTORDER"]


def test_a_single_sheet_yields_no_tables():
    views, tables = read_workbook(workbook_bytes([("S", ["ZI_A"])]))
    assert split_names(views) == ["ZI_A"]
    assert tables == ""


def test_blank_rows_in_a_sheet_are_skipped():
    data = workbook_bytes([("S", ["ZI_A", "", "   ", "ZI_B"])])
    assert split_names(read_workbook(data)[0]) == ["ZI_A", "ZI_B"]


def test_read_upload_dispatches_on_the_extension():
    views, tables = read_upload("list.csv", b"ZI_A\nZI_B")
    assert split_names(views) == ["ZI_A", "ZI_B"]
    assert tables == ""

    data = workbook_bytes([("a", ["ZI_A"]), ("b", ["ZTAB"])])
    views, tables = read_upload("list.xlsx", data)
    assert split_names(views) == ["ZI_A"]
    assert split_names(tables) == ["ZTAB"]
