# Description: Edits a protocol CSV row by row for the web UI's commit, keeping comments, blank lines, column order and every untouched row exactly as they were.
# File: protocol_csv_patch.py
#
# Copyright 2026 Kevin Burke
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://apache.org
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Patch a protocol CSV instead of regenerating it.

Committing a protocol edit used to rebuild the whole file from the database rows: a fixed header,
every register sorted by address.  That silently discarded everything in the file that is not a
register: the ``#`` comment rows (the notes that document a map, section headings, sources,
"not verified" warnings), blank lines, the file's own column order and delimiter, and the exact
formatting of rows nobody touched.

This module edits the file in place instead.  ``patch_protocol_csv`` takes the file's text and a
list of row updates and deletions, and returns the new text:

* every record that is not changed is copied through **verbatim** (same quoting, same line ending);
* a changed row is rewritten in the file's own delimiter and line ending, through the file's own
  header (so a differently ordered or aliased header still gets the right cell).  Only a cell whose
  value differs from what the scanner reads from the file today is replaced, so a row the user edited
  in one field changes in that field and nowhere else (the scanner lower-cases variable names, upper-cases
  the write mode and so on, and a cell that merely differs in those ways is left as written);
* a deleted row is dropped; rows with no counterpart in the file are appended at the end;
* a column the file does not have is added (at the end) only when a row being written has a value
  for it, so a file without the Home Assistant columns does not gain three empty ones;
* an ``_l`` / ``_h`` register pair, which the scanner presents as one row with the address
  ``low_high`` (for example ``40_41``), is written back as the single ``low_high`` row the runtime
  loader understands, replacing the two original rows, exactly as the previous writer did.

The function works on text and does no file I/O.  The commit code (``config_writer``) reads and
writes the file as latin-1, which is how the scanner and the runtime loader read it, so the bytes of
untouched rows round-trip exactly.

``patch_protocol_csv`` raises ``PatchError`` rather than guess when the file cannot be patched
safely, and checks its own result before returning it.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

HEADER_ALIASES: dict[str, str] = {
    "address": "register",
    "adjustments": "adjustments",
    "data_type": "data_type",
    "datatype": "data_type",
    "desc": "note",
    "description": "note",
    "documented": "documented_name",
    "documented_name": "documented_name",
    "interval": "read_interval",
    "name": "documented_name",
    "note": "note",
    "notes": "note",
    "r_w": "write_mode",
    "range": "values",
    "read_interval": "read_interval",
    "read_interval_ms": "read_interval",
    "readinterval": "read_interval",
    "reg": "register",
    "register": "register",
    "register_address": "register",
    "type": "data_type",
    "unit": "unit",
    "units": "unit",
    "value": "values",
    "values": "values",
    "values_range": "values",
    "var_name": "variable_name",
    "varname": "variable_name",
    "variable": "variable_name",
    "variable_name": "variable_name",
    "doc_name": "documented_name",
    "writable": "write_mode",
    "write": "write_mode",
    "write_mode": "write_mode",
}
"""Header spellings the project's CSV readers accept, mapped to one canonical column name."""

CANONICAL_COLUMNS: tuple[str, ...] = (
    "register",
    "variable_name",
    "documented_name",
    "unit",
    "data_type",
    "values",
    "adjustments",
    "note",
    "write_mode",
    "read_interval",
    "ha_device_class",
    "ha_state_class",
    "ha_entity_category",
)
"""The columns a row update carries, in the order a new row's cells are filled."""

NEW_COLUMN_NAMES: dict[str, str] = {
    "write_mode": "writable",
}
"""Header text for a column that has to be added; canonical names are used as they are, except these."""

_PAIR_ADDRESS: re.Pattern[str] = re.compile(r"^(\d+)_(\d+)$")
_LINE_ENDING: re.Pattern[str] = re.compile(r"(\r\n|\n|\r)$")

RecordKind = Literal["header", "comment", "blank", "data"]


class PatchError(Exception):
    """The file cannot be patched safely (empty, not parseable, or the result failed its own check)."""


@dataclass(frozen=True)
class RowValues:
    """One register row's cells, by canonical column name.  Every value is text; empty means a blank cell."""

    register: str
    variable_name: str
    documented_name: str = ""
    unit: str = ""
    data_type: str = ""
    values: str = ""
    adjustments: str = ""
    note: str = ""
    write_mode: str = ""
    read_interval: str = ""
    ha_device_class: str = ""
    ha_state_class: str = ""
    ha_entity_category: str = ""


@dataclass(frozen=True)
class RowUpdate:
    """Write ``values`` to the row at ``address``, or append it when the file has no such row.

    ``address`` is the register address as the database holds it: the cell in the ``register`` column,
    or ``low_high`` for an ``_l`` / ``_h`` pair that the scanner merged.
    """

    address: str
    values: RowValues


@dataclass(frozen=True)
class RowDeletion:
    """Remove the row at ``address`` (or both rows of a ``low_high`` pair)."""

    address: str


@dataclass
class PatchResult:
    """The patched text and what was done to it."""

    text: str
    changed: bool = False
    updated: int = 0
    added: int = 0
    deleted: int = 0
    columns_added: tuple[str, ...] = ()
    notes: list[str] = field(default_factory=lambda: [])
    """Rows that were asked for but not found (a deletion of a row the file does not have, for example)."""


@dataclass
class _Record:
    """One CSV record (which may span several physical lines) with its exact original text."""

    raw: str
    cells: list[str]
    kind: RecordKind
    rewrite: bool = False
    """True when ``raw`` must be regenerated from ``cells``."""
    deleted: bool = False
    pad: int = 0
    """Empty cells to append to ``raw`` (when columns were added and this row itself is untouched)."""


def normalize_header(value: str) -> str:
    """Normalize a header cell exactly as the scanner does: strip, lower-case, spaces and slashes to underscores."""
    return (value or "").strip().lower().replace(" ", "_").replace("/", "_")


def detect_delimiter(text: str) -> str:
    """Return ``;`` when the first 4 KB has more semicolons than commas, else ``,`` (the project's own rule)."""
    sample: str = text[:4096]
    return ";" if sample.count(";") > sample.count(",") else ","


def encode_for_file(text: str) -> bytes:
    """Encode patched CSV text for writing, as latin-1 (how the scanner and the runtime loader read the file).

    The text comes from a latin-1 decode of the original file plus values from the database, so it round-trips
    byte for byte.  A character latin-1 cannot hold (typed in the editor, for example an emoji) is written as its
    UTF-8 bytes instead of failing, which matches how every other non-ASCII character in these files is stored.
    """
    try:
        return text.encode("latin-1")
    except UnicodeEncodeError:
        return b"".join(ch.encode("latin-1") if ord(ch) < 256 else ch.encode("utf-8") for ch in text)


def _split_records(text: str, delimiter: str) -> list[_Record]:
    """Split ``text`` into records, each remembering exactly the text it came from."""
    lines: list[str] = io.StringIO(text, newline="").readlines()
    reader = csv.reader(iter(lines), delimiter=delimiter)
    records: list[_Record] = []
    consumed: int = 0
    try:
        for cells in reader:
            raw: str = "".join(lines[consumed : reader.line_num])
            consumed = reader.line_num
            kind: RecordKind
            if not records:
                kind = "header"
            elif not any(cell.strip() for cell in cells):
                kind = "blank"
            elif cells[0].lstrip().startswith("#"):
                kind = "comment"
            else:
                kind = "data"
            records.append(_Record(raw=raw, cells=list(cells), kind=kind))
    except csv.Error as exc:
        msg: str = f"the CSV cannot be parsed: {exc}"
        raise PatchError(msg) from exc
    return records


def _header_map(header: list[str]) -> dict[str, int]:
    """Canonical column name -> index of its first occurrence in ``header``."""
    mapping: dict[str, int] = {}
    for index, cell in enumerate(header):
        normalized: str = normalize_header(cell)
        mapping.setdefault(HEADER_ALIASES.get(normalized, normalized), index)
    return mapping


def _line_ending_of(raw: str) -> str:
    """The line ending ``raw`` ends with, or empty when it has none (the last line of a file)."""
    match: re.Match[str] | None = _LINE_ENDING.search(raw)
    return match.group(1) if match else ""


def _format_row(cells: Sequence[str], delimiter: str, eol: str) -> str:
    """Serialize ``cells`` the way the csv module would for a file with this delimiter and line ending."""
    buffer = io.StringIO()
    csv.writer(buffer, delimiter=delimiter, lineterminator=eol, quoting=csv.QUOTE_MINIMAL).writerow(cells)
    return buffer.getvalue()


def _pad_raw(raw: str, delimiter: str, count: int) -> str:
    """Append ``count`` empty cells to a record's original text, before its line ending."""
    eol: str = _line_ending_of(raw)
    body: str = raw[: len(raw) - len(eol)] if eol else raw
    return body + delimiter * count + eol


def _cell(values: RowValues, column: str) -> str:
    """The value ``values`` holds for canonical ``column``."""
    return getattr(values, column)


def _text(record: _Record, mapping: dict[str, int], column: str) -> str:
    """The stripped text of ``column`` in ``record``; empty if the file has no such column or the row is short."""
    index: int | None = mapping.get(column)
    if index is None or index >= len(record.cells):
        return ""
    return record.cells[index].strip()


def _is_register_row(record: _Record, mapping: dict[str, int]) -> bool:
    """True when the scanner would store ``record`` as a register.

    The scanner's rule: a register cell, a variable or documented name, neither name starting with ``#``, and a
    register that is not a repeated header word.  (A comment row whose text was split by an unquoted comma can
    satisfy it, which is how such a row shows up in the editor; keeping the same rule lets it be deleted there.)
    """
    if record.kind in ("header", "blank"):
        return False
    register: str = _text(record, mapping, "register")
    variable: str = _text(record, mapping, "variable_name")
    documented: str = _text(record, mapping, "documented_name")
    if not register or not (variable or documented):
        return False
    if variable.startswith("#") or documented.startswith("#"):
        return False
    return register.lower() not in ("register", "reg", "address")


def _register_index(records: list[_Record], mapping: dict[str, int]) -> dict[str, int]:
    """Register cell -> index of the record defining it.  Like the scanner, the last definition of an address wins."""
    index: dict[str, int] = {}
    for position, record in enumerate(records):
        if not record.deleted and _is_register_row(record, mapping):
            index[_text(record, mapping, "register")] = position
    return index


def _scanner_view(record: _Record, mapping: dict[str, int], column: str) -> str:
    """What the scanner stores for ``column`` of ``record``, so an unchanged cell can be left exactly as written."""
    cell: str = _text(record, mapping, column)
    if column == "variable_name":
        return (cell or _text(record, mapping, "documented_name")).strip().lower().replace(" ", "_")
    if column == "documented_name":
        return cell or _text(record, mapping, "variable_name")
    if column == "write_mode":
        return cell.upper() or "R"
    if column in ("ha_device_class", "ha_state_class", "ha_entity_category"):
        return cell.lower()
    return cell


def scan_rows(text: str) -> list[RowValues]:
    """The register rows of ``text`` as the scanner would store them (the last definition of each address).

    Writing exactly these values back with ``patch_protocol_csv`` changes nothing, which is how the patcher's
    cell-level rule is tested against every real protocol file.  Raises ``PatchError`` for a file that cannot
    be read the way ``patch_protocol_csv`` reads it.
    """
    if not text.strip():
        msg: str = "the CSV is empty"
        raise PatchError(msg)
    records: list[_Record] = _split_records(text, detect_delimiter(text))
    mapping: dict[str, int] = _header_map(records[0].cells)
    if "register" not in mapping or "variable_name" not in mapping:
        msg = "the CSV header has no register / variable_name columns"
        raise PatchError(msg)
    return [
        RowValues(**{column: _scanner_view(records[position], mapping, column) for column in CANONICAL_COLUMNS})
        for position in _register_index(records, mapping).values()
    ]


def _locate(by_register: dict[str, int], address: str) -> list[int]:
    """Record positions for ``address``: the row itself, or the two rows of a ``low_high`` pair; empty if absent."""
    if address in by_register:
        return [by_register[address]]
    pair: re.Match[str] | None = _PAIR_ADDRESS.match(address)
    if pair and pair.group(1) in by_register and pair.group(2) in by_register:
        low, high = by_register[pair.group(1)], by_register[pair.group(2)]
        if low != high:
            return [low, high]
    return []


def patch_protocol_csv(text: str, updates: Sequence[RowUpdate], deletions: Sequence[RowDeletion] = ()) -> PatchResult:
    """Apply ``updates`` and ``deletions`` to the CSV in ``text`` and return the patched text.

    Raises ``PatchError`` if the file is empty, cannot be parsed, or the patched text fails its own
    check (the header intact and the number of register rows as expected).  Nothing is written anywhere.
    """
    if not text.strip():
        msg: str = "the CSV is empty"
        raise PatchError(msg)

    delimiter: str = detect_delimiter(text)
    records: list[_Record] = _split_records(text, delimiter)
    header: _Record = records[0]
    mapping: dict[str, int] = _header_map(header.cells)
    if "register" not in mapping or "variable_name" not in mapping:
        msg = "the CSV header has no register / variable_name columns"
        raise PatchError(msg)

    result = PatchResult(text=text)
    original_rows: int = sum(1 for r in records if _is_register_row(r, mapping))

    # Columns the file lacks but a row being written has something to say in.  A missing write-mode column reads as
    # "R", so only another value there needs the column.
    absent_default: _Record = _Record(raw="", cells=[], kind="data")
    added_columns: list[str] = []
    for column in CANONICAL_COLUMNS:
        if column in mapping:
            continue
        missing_view: str = _scanner_view(absent_default, mapping, column)
        if any(_cell(update.values, column).strip() not in ("", missing_view) for update in updates):
            added_columns.append(column)
            mapping[column] = len(header.cells) + len(added_columns) - 1
    if added_columns:
        header.cells = header.cells + [NEW_COLUMN_NAMES.get(column, column) for column in added_columns]
        header.rewrite = True
        for record in records[1:]:
            if record.kind == "data":
                record.pad = len(added_columns)

    by_register: dict[str, int] = _register_index(records, mapping)
    width: int = len(header.cells)

    for deletion in deletions:
        positions: list[int] = _locate(by_register, deletion.address)
        if not positions:
            result.notes.append(f"no row for {deletion.address} to delete")
            continue
        for position in positions:
            records[position].deleted = True
        result.deleted += len(positions)
        by_register = _register_index(records, mapping)

    appended: list[_Record] = []
    for update in updates:
        positions = _locate(by_register, update.address)
        if positions:
            target: _Record = records[positions[0]]
            cells: list[str] = target.cells + [""] * (width - len(target.cells))
            changed_cells: int = 0
            for column in CANONICAL_COLUMNS:
                if column in mapping and column != "register":
                    wanted: str = _cell(update.values, column)
                    if wanted != _scanner_view(target, mapping, column):
                        cells[mapping[column]] = wanted
                        changed_cells += 1
            if len(positions) == 2:
                cells[mapping["register"]] = update.address
                records[positions[1]].deleted = True
                result.deleted += 1
                changed_cells += 1
            if changed_cells:
                target.cells, target.rewrite, target.pad = cells, True, 0
                result.updated += 1
                by_register = _register_index(records, mapping)
        else:
            cells = [""] * width
            for column in CANONICAL_COLUMNS:
                if column in mapping:
                    cells[mapping[column]] = _cell(update.values, column)
            appended.append(_Record(raw="", cells=cells, kind="data", rewrite=True))
            result.added += 1

    default_eol: str = _line_ending_of(header.raw) or "\n"
    pieces: list[str] = []
    for record in records:
        if record.deleted:
            continue
        if record.rewrite:
            eol: str = _line_ending_of(record.raw) if record.raw else default_eol
            pieces.append(_format_row(record.cells, delimiter, eol))
        elif record.pad:
            pieces.append(_pad_raw(record.raw, delimiter, record.pad))
        else:
            pieces.append(record.raw)
    if appended:
        if pieces and not _line_ending_of(pieces[-1]):
            pieces[-1] += default_eol
        pieces.extend(_format_row(record.cells, delimiter, default_eol) for record in appended)

    patched: str = "".join(pieces)
    result.text = patched
    result.changed = patched != text
    result.columns_added = tuple(added_columns)
    _verify(patched, delimiter, header.cells, original_rows - result.deleted + result.added)
    return result


def _verify(patched: str, delimiter: str, expected_header: list[str], expected_rows: int) -> None:
    """Re-read ``patched`` and raise ``PatchError`` unless its header and register-row count are as expected."""
    records: list[_Record] = _split_records(patched, delimiter)
    if not records or records[0].cells != expected_header:
        msg: str = "the patched CSV does not have the expected header"
        raise PatchError(msg)
    mapping: dict[str, int] = _header_map(records[0].cells)
    rows: int = sum(1 for record in records if _is_register_row(record, mapping))
    if rows != expected_rows:
        msg = f"the patched CSV has {rows} register rows, expected {expected_rows}"
        raise PatchError(msg)
