"""Committing a protocol edit patches the CSV row by row instead of regenerating the file.

The point of these tests is what the patcher must *not* change: comment rows, blank lines, column order,
the delimiter, line endings and the exact text of every row that was not edited.  They need no database.
"""

from __future__ import annotations

import csv
import io
from dataclasses import replace
from pathlib import Path

import pytest

from classes.WebServer.protocol_csv_patch import (
    PatchError,
    PatchResult,
    RowDeletion,
    RowUpdate,
    RowValues,
    patch_protocol_csv,
    scan_rows,
)

HEADER: str = "register,variable_name,documented_name,unit,data_type,values,read_interval,writable,adjustments,note\n"

DOCUMENTED: str = (
    HEADER
    + '"# DYNAMIC MAP: rows whose register column is [anchor+offset] are template rows, see the docs.",,,,,,,,,\n'
    + "\n"
    + "# --- Inverter ---\n"
    + "[inverter_1_base+14],ac_power,AC Power,W,int16,,,R,\"{\"\"Context\"\":{\"\"key\"\":\"\"ac_power_sf\"\",\"\"range\"\":[-10,10]}}\",Total power\n"
    + "[inverter_1_base+15],ac_power_sf,AC Power SF,,int16,-10-10,,R,,Scale factor\n"
    + "# --- Common ---\n"
    + '40004,manufacturer,Manufacturer,,string32,,,R,,"Mn, 16 registers"\n'
)


def values(register: str, name: str, **fields: str) -> RowValues:
    """A full row of cells for ``register`` with the given overrides."""
    base = {"documented_name": name.title(), "unit": "", "data_type": "int16", "values": "", "adjustments": "", "note": "",
            "write_mode": "R", "read_interval": ""}  # fmt: skip
    base.update(fields)
    return RowValues(register=register, variable_name=name, **base)


def update(text: str, *updates: RowUpdate, deletions: tuple[RowDeletion, ...] = ()) -> PatchResult:
    """Patch ``text`` and return the whole result."""
    return patch_protocol_csv(text, list(updates), list(deletions))


def rows(text: str) -> list[list[str]]:
    """The CSV's records as cell lists (comments and blanks included)."""
    return list(csv.reader(io.StringIO(text, newline="")))


# --------------------------------------------------------------------------------------------------
# What must survive
# --------------------------------------------------------------------------------------------------


def test_a_patch_with_nothing_to_do_returns_the_text_unchanged() -> None:
    result: PatchResult = update(DOCUMENTED)
    assert result.text == DOCUMENTED and result.changed is False


def test_editing_one_row_leaves_every_other_line_exactly_as_it_was() -> None:
    result: PatchResult = update(DOCUMENTED, RowUpdate("40004", values("40004", "manufacturer", documented_name="Maker", data_type="string32")))
    before, after = DOCUMENTED.split("\n"), result.text.split("\n")
    changed_lines: list[int] = [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]
    assert changed_lines == [7] and "Maker" in after[7]
    assert result.updated == 1 and result.added == 0 and result.deleted == 0


def test_comment_rows_blank_lines_and_the_template_rows_survive_an_edit() -> None:
    result: PatchResult = update(DOCUMENTED, RowUpdate("40004", values("40004", "manufacturer", unit="x")))
    for line in DOCUMENTED.split("\n")[1:7]:
        assert line in result.text
    assert result.text.startswith(HEADER)


def test_the_files_own_column_order_is_kept() -> None:
    text: str = "register;variable_name;note;unit;documented_name;data_type;values;writable\n40000;a;n;V;A;int16;;R\n"
    result: PatchResult = update(text, RowUpdate("40000", values("40000", "a", documented_name="Alpha", unit="kV", note="changed")))
    assert result.text.splitlines()[0] == "register;variable_name;note;unit;documented_name;data_type;values;writable"
    assert result.text.splitlines()[1] == "40000;a;changed;kV;Alpha;int16;;R"


def test_a_semicolon_file_stays_semicolon_separated() -> None:
    text: str = "register;variable_name;documented_name\n40000;a;Old\n"
    result: PatchResult = update(text, RowUpdate("40000", RowValues(register="40000", variable_name="a", documented_name="New")))
    assert result.text.splitlines()[1] == "40000;a;New"


def test_crlf_line_endings_are_kept() -> None:
    text: str = HEADER.replace("\n", "\r\n") + "40000,a,Old,,int16,,,R,,\r\n# note\r\n40002,b,B,,int16,,,R,,\r\n"
    result: PatchResult = update(text, RowUpdate("40000", values("40000", "a", documented_name="New")))
    assert "\n" not in result.text.replace("\r\n", "")
    assert result.text.endswith("# note\r\n40002,b,B,,int16,,,R,,\r\n")


def test_untouched_rows_keep_their_original_quoting() -> None:
    odd: str = '"40002","b","B",,"int16",,,R,,"a ""quoted"" note"\n'
    text: str = HEADER + "40000,a,Old,,int16,,,R,,\n" + odd
    assert odd in update(text, RowUpdate("40000", values("40000", "a", documented_name="New"))).text


def test_a_json_adjustment_with_quotes_and_commas_round_trips_through_an_edit() -> None:
    adjustment: str = '{"Context":{"key":"ac_power_sf","range":[-10,10]}}'
    result: PatchResult = update(DOCUMENTED, RowUpdate("[inverter_1_base+14]", values("[inverter_1_base+14]", "ac_power", adjustments=adjustment, note="n")))
    edited: list[str] = next(r for r in rows(result.text) if r and r[0] == "[inverter_1_base+14]")
    assert edited[8] == adjustment


def test_bytes_that_are_not_ascii_round_trip_as_latin_1() -> None:
    """The scanner reads these files as latin-1, so a UTF-8 degree sign appears as two characters and is written back as the same two."""
    original: bytes = "register,variable_name,unit\n40000,t,\u00b0C\n40002,u,\u00b0C\n".encode("utf-8")
    text: str = original.decode("latin-1")
    result: PatchResult = update(text, RowUpdate("40000", RowValues(register="40000", variable_name="t", unit=text.splitlines()[1].split(",")[2])))
    assert result.text.encode("latin-1") == original


# --------------------------------------------------------------------------------------------------
# Edits
# --------------------------------------------------------------------------------------------------


def test_an_edit_replaces_only_the_cells_the_database_holds_and_keeps_the_register_cell() -> None:
    result: PatchResult = update(DOCUMENTED, RowUpdate("[inverter_1_base+15]", values("[inverter_1_base+15]", "ac_power_sf", unit="", note="New note")))
    edited: list[str] = next(r for r in rows(result.text) if r and r[0] == "[inverter_1_base+15]")
    assert edited[0] == "[inverter_1_base+15]" and edited[-1] == "New note"


def test_a_column_the_file_lacks_is_added_when_a_row_has_a_value_for_it() -> None:
    result: PatchResult = update(DOCUMENTED, RowUpdate("40004", values("40004", "manufacturer", ha_device_class="energy")))
    parsed = rows(result.text)
    assert parsed[0][-1] == "ha_device_class" and result.columns_added == ("ha_device_class",)
    assert next(r for r in parsed if r and r[0] == "40004")[-1] == "energy"
    other = next(r for r in parsed if r and r[0] == "[inverter_1_base+15]")
    assert len(other) == len(parsed[0]) and other[-1] == ""  # an untouched data row is padded to the new width


def test_a_column_the_file_lacks_is_not_added_when_every_value_is_blank() -> None:
    result: PatchResult = update(DOCUMENTED, RowUpdate("40004", values("40004", "manufacturer", documented_name="Maker")))
    assert result.columns_added == () and rows(result.text)[0] == rows(DOCUMENTED)[0]


def test_aliased_headers_are_understood() -> None:
    text: str = "Register,Variable Name,Documented Name,R/W\n40000,a,Old,R\n"
    result: PatchResult = update(text, RowUpdate("40000", RowValues(register="40000", variable_name="a", documented_name="New", write_mode="RW")))
    assert result.text.splitlines()[1] == "40000,a,New,RW"


# --------------------------------------------------------------------------------------------------
# Deletions, additions, pairs
# --------------------------------------------------------------------------------------------------


def test_a_deleted_row_is_removed_and_its_neighbors_and_comments_stay() -> None:
    result: PatchResult = update(DOCUMENTED, deletions=(RowDeletion("[inverter_1_base+15]"),))
    names: list[str] = [r[1] for r in rows(result.text)[1:] if len(r) > 1 and r[0] and not r[0].startswith("#")]
    assert names == ["ac_power", "manufacturer"] and "# --- Common ---" in result.text and "# --- Inverter ---" in result.text
    assert result.deleted == 1


def test_deleting_a_row_the_file_does_not_have_is_reported_not_fatal() -> None:
    result: PatchResult = update(DOCUMENTED, deletions=(RowDeletion("49999"),))
    assert result.text == DOCUMENTED and result.notes == ["no row for 49999 to delete"]


def test_a_row_with_no_counterpart_is_appended_after_the_existing_content() -> None:
    result: PatchResult = update(DOCUMENTED, RowUpdate("40100", values("40100", "extra")))
    assert result.text.startswith(DOCUMENTED) and result.text.splitlines()[-1].startswith("40100,extra") and result.added == 1


def test_appending_to_a_file_with_no_final_newline_does_not_glue_the_rows_together() -> None:
    text: str = HEADER + "40000,a,A,,int16,,,R,,"
    result: PatchResult = update(text, RowUpdate("40002", values("40002", "b")))
    assert [r[0] for r in rows(result.text)[1:]] == ["40000", "40002"]


def test_a_low_high_pair_is_written_back_as_the_single_row_the_loader_understands() -> None:
    text: str = HEADER + "# energy\n40,energy_l,Energy Low,,uint16,,,R,,\n41,energy_h,Energy High,,uint16,,,R,,\n50,other,Other,,int16,,,R,,\n"
    result: PatchResult = update(text, RowUpdate("40_41", values("40_41", "energy", documented_name="Energy", data_type="uint32")))
    registers: list[str] = [r[0] for r in rows(result.text)[1:] if r and not r[0].startswith("#")]
    assert registers == ["40_41", "50"] and "# energy" in result.text
    assert result.updated == 1 and result.deleted == 1


def test_a_row_already_in_low_high_form_is_edited_in_place() -> None:
    text: str = HEADER + "40_41,energy,Energy,,uint32,,,R,,\n"
    result: PatchResult = update(text, RowUpdate("40_41", values("40_41", "energy", documented_name="Total energy")))
    assert result.text.splitlines()[1].startswith("40_41,energy,Total energy")


def test_deleting_a_pair_removes_both_rows() -> None:
    text: str = HEADER + "40,energy_l,L,,uint16,,,R,,\n41,energy_h,H,,uint16,,,R,,\n50,other,O,,int16,,,R,,\n"
    result: PatchResult = update(text, deletions=(RowDeletion("40_41"),))
    assert [r[0] for r in rows(result.text)[1:]] == ["50"] and result.deleted == 2


def test_the_last_definition_of_a_duplicated_address_is_the_one_edited() -> None:
    text: str = HEADER + "40000,first,A,,int16,,,R,,\n40000,second,B,,int16,,,R,,\n"
    result: PatchResult = update(text, RowUpdate("40000", values("40000", "second", documented_name="Edited")))
    lines: list[str] = result.text.splitlines()
    assert lines[1] == "40000,first,A,,int16,,,R,," and "Edited" in lines[2]


# --------------------------------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   \n\n"])
def test_an_empty_file_cannot_be_patched(text: str) -> None:
    with pytest.raises(PatchError):
        update(text, RowUpdate("40000", values("40000", "a")))


def test_a_file_without_register_and_variable_columns_cannot_be_patched() -> None:
    with pytest.raises(PatchError):
        update("foo,bar\n1,2\n", RowUpdate("40000", values("40000", "a")))


# --------------------------------------------------------------------------------------------------
# Only what really changed is rewritten
# --------------------------------------------------------------------------------------------------

PROTOCOL_CSVS: list[Path] = sorted((Path(__file__).resolve().parent.parent / "protocols").rglob("*.csv"))


@pytest.mark.parametrize("csv_path", PROTOCOL_CSVS, ids=[p.name for p in PROTOCOL_CSVS])
def test_committing_what_the_scanner_already_read_changes_no_protocol_file(csv_path: Path) -> None:
    """Every real protocol CSV, comments and all, comes back byte for byte when each row is 'edited' to its own values."""
    text: str = csv_path.read_bytes().decode("latin-1")
    try:
        current: list[RowValues] = scan_rows(text)
    except PatchError:
        pytest.skip("no register/variable_name header")
    result: PatchResult = update(text, *(RowUpdate(row.register, row) for row in current))
    assert result.text == text and result.updated == 0 and result.added == 0 and result.columns_added == ()


def test_a_cell_that_only_differs_by_the_scanners_normalization_is_left_as_written() -> None:
    text: str = HEADER + "40000,AC Power,,W,INT16,,,r,,\n"
    current: RowValues = scan_rows(text)[0]
    assert (current.variable_name, current.documented_name, current.write_mode) == ("ac_power", "AC Power", "R")
    assert update(text, RowUpdate("40000", current)).text == text


def test_only_the_cell_that_changed_is_rewritten_in_an_edited_row() -> None:
    text: str = HEADER + "40000,AC Power,,W,INT16,,,r,,Keep me\n"
    current: RowValues = scan_rows(text)[0]
    edited: RowValues = replace(current, unit="kW")
    assert update(text, RowUpdate("40000", edited)).text == HEADER + "40000,AC Power,,kW,INT16,,,r,,Keep me\n"


def test_a_comment_row_split_by_an_unquoted_comma_can_be_deleted_like_the_editor_lists_it() -> None:
    text: str = HEADER + "# IMPORTANT: one master at a time - either RTU or TCP, not both,,,,,,,\n40000,a,A,,int16,,,R,,\n"
    stray: RowValues = scan_rows(text)[0]
    assert stray.register.startswith("# IMPORTANT")
    result: PatchResult = update(text, deletions=(RowDeletion(stray.register),))
    assert "IMPORTANT" not in result.text and "40000,a,A" in result.text
