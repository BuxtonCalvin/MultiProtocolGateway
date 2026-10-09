# Description: Adds the optional Home Assistant columns to every protocol registry-map CSV and fills them with best-guess values inferred from each row's existing data.
# File: add_ha_columns.py
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

"""Populate the ``ha device class`` / ``ha state class`` / ``ha entity category`` columns.

For every registry-map CSV under the protocols directory this tool

1. adds the three optional columns if they are missing, and
2. fills each BLANK cell with a best-guess value inferred from what the row
   already says (unit, data type, enum mapping, writable flag, variable name).

It is a dry run unless ``--apply`` is given. Existing non-blank cells are never
changed unless ``--overwrite`` is given, so hand-edited values survive re-runs
and running the tool twice changes nothing.

The inferred values are suggestions: review the diff (``git diff protocols/``)
before committing. A blank cell is always safe; the MQTT bridge then falls back
to its own conservative inference at runtime.

Usage::

    python tools/add_ha_columns.py                    # dry run, summary only
    python tools/add_ha_columns.py --verbose          # dry run, one line per file
    python tools/add_ha_columns.py --apply            # write the files
    python tools/add_ha_columns.py --apply --config-writable
    python tools/add_ha_columns.py --apply --overwrite protocols/eg4

Files are read and written as latin-1 with their original delimiter and line
endings, exactly as the gateway's own loader reads them, and every file is
re-parsed after rewriting to prove no pre-existing cell changed.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from classes.ha_metadata import DISABLED_ALIASES, infer_ha_attributes  # noqa: E402

HA_COLUMNS: tuple[str, str, str] = ("ha device class", "ha state class", "ha entity category")


@dataclass
class FileResult:
    """Outcome of processing one CSV."""

    path: Path
    skipped_reason: str = ""
    rows: int = 0
    added_columns: list[str] = field(default_factory=lambda: list[str]())
    filled: Counter[str] = field(default_factory=lambda: Counter[str]())
    new_text: str | None = None

    @property
    def changed(self) -> bool:
        return self.new_text is not None


def _norm(cell: str) -> str:
    """Normalize a header cell exactly as the gateway's loader does."""
    return re.sub(r"\s+", " ", cell.strip().lower().replace("_", " "))


def _codes_for(csv_path: Path) -> set[str]:
    """Names of the ``<name>_codes`` tables in the protocol's JSON descriptor."""
    descriptor: Path = csv_path.with_name(csv_path.name.split(".")[0] + ".json")
    if not descriptor.exists():
        return set()
    try:
        data: Any = json.loads(descriptor.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {k for k, v in data.items() if k.endswith("_codes") and isinstance(v, dict)}


def _slug(name: str) -> str:
    return name.strip().lower().replace(" ", "_").replace("__", "_")


def process_file(path: Path, *, overwrite: bool = False, config_writable: bool = False) -> FileResult:
    """Compute the updated text for one CSV (without writing it)."""
    result = FileResult(path)
    raw: str = path.read_bytes().decode("latin-1")
    if not raw.strip():
        result.skipped_reason = "empty"
        return result

    first_line: str = raw.splitlines()[0]
    delimiter = ";" if first_line.count(";") >= first_line.count(",") else ","
    eol = "\r\n" if "\r\n" in raw else "\n"

    table: list[list[str]] = list(csv.reader(io.StringIO(raw, newline=""), delimiter=delimiter))
    header: list[str] = table[0]
    columns: dict[str, int] = {_norm(cell): i for i, cell in enumerate(header)}
    if "register" not in columns or "documented name" not in columns:
        result.skipped_reason = "not a registry map"
        return result

    underscore_style: bool = any("_" in cell for cell in header)
    original_width: int = len(header)
    new_header: list[str] = list(header)
    for column in HA_COLUMNS:
        if column not in columns:
            new_header.append(column.replace(" ", "_") if underscore_style else column)
            columns[column] = len(new_header) - 1
            result.added_columns.append(column)
    ha_index: dict[str, int] = {column: columns[column] for column in HA_COLUMNS}
    width: int = len(new_header)

    def cell(row: list[str], column: str) -> str:
        i: int | None = columns.get(column)
        return row[i].strip() if i is not None and i < len(row) else ""

    codes: set[str] = _codes_for(path)
    writable_column: Literal['writable'] | Literal['write'] = "writable" if "writable" in columns else "write"
    out_table: list[list[str]] = [new_header]

    for row in table[1:]:
        if not row:  # blank line, keep as is
            out_table.append(row)
            continue

        # Keep the row rectangular. A row longer than the header keeps its extra
        # cells AFTER the new ones, so they stay "extra" for the loader.
        extras: list[str] = row[original_width:]
        body: list[str] = list(row[:original_width]) + [""] * (original_width - len(row))
        body += [""] * (width - original_width)
        out_row: list[str] = body + extras

        register: str = cell(row, "register")
        documented: str = cell(row, "documented name")
        variable: str = cell(row, "variable name")
        is_comment: bool = register.startswith("#") or variable.startswith("#")
        if is_comment or not (register or variable or documented):
            out_table.append(out_row)
            continue

        result.rows += 1
        write_flag: str = cell(row, writable_column).upper()
        if write_flag in DISABLED_ALIASES:  # never published, nothing to describe
            out_table.append(out_row)
            continue

        name: str = _slug(variable or documented)
        is_enum: bool = (
            _slug(documented) + "_codes" in codes
            or name + "_codes" in codes
        )
        inferred: tuple[str, str, str] = infer_ha_attributes(
            variable_name=name,
            unit=cell(row, "unit"),
            data_type=cell(row, "data type"),
            values=cell(row, "values"),
            writable=write_flag,
            is_enum=is_enum,
            config_writable=config_writable,
        )
        for column, value in zip(HA_COLUMNS, inferred, strict=True):
            index: int = ha_index[column]
            current: str = out_row[index].strip() if index < len(out_row) else ""
            if value and (overwrite or not current):
                if index >= len(out_row):
                    out_row.extend([""] * (index + 1 - len(out_row)))
                if out_row[index].strip() != value:
                    out_row[index] = value
                    result.filled[column] += 1
        out_table.append(out_row)

    if not result.added_columns and not result.filled:
        return result  # nothing to do

    buffer = io.StringIO(newline="")
    csv.writer(buffer, delimiter=delimiter, lineterminator=eol).writerows(out_table)
    new_text: str = buffer.getvalue()

    # Prove no pre-existing cell changed (quoting may differ, content may not).
    reparsed: list[list[str]] = list(csv.reader(io.StringIO(new_text, newline=""), delimiter=delimiter))
    if len(reparsed) != len(table):
        result.skipped_reason = "verification failed (row count changed); file left untouched"
        return result
    for old_row, new_row in zip(table, reparsed, strict=True):
        if not old_row:
            continue
        for i, old_cell in enumerate(old_row[:original_width]):
            if i in ha_index.values() and overwrite:
                continue
            if new_row[i] != old_cell and not (i in ha_index.values() and not old_cell.strip()):
                result.skipped_reason = "verification failed (a cell changed); file left untouched"
                return result

    result.new_text = new_text
    return result


def find_csvs(targets: list[Path]) -> list[Path]:
    """Every ``*.csv`` under the given files/directories, sorted."""
    found: set[Path] = set()
    for target in targets:
        if target.is_file():
            found.add(target)
        else:
            found.update(target.rglob("*.csv"))
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Add and populate the Home Assistant columns in protocol CSVs (dry run unless --apply).",
    )
    parser.add_argument("paths", nargs="*", type=Path, help="files or folders (default: ./protocols)")
    parser.add_argument("--apply", action="store_true", help="write the changes (default is a dry run)")
    parser.add_argument("--overwrite", action="store_true", help="replace non-blank cells too")
    parser.add_argument(
        "--config-writable",
        action="store_true",
        help="mark writable registers with entity category 'config' (hides them from default HA dashboards)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="one line per file")
    args: argparse.Namespace = parser.parse_args(argv)

    targets: Any | list[Path] = args.paths or [Path(__file__).resolve().parent.parent / "protocols"]
    files: list[Path] = find_csvs(targets)

    totals: Counter[str] = Counter()
    problems: list[FileResult] = []
    for path in files:
        result: FileResult = process_file(path, overwrite=args.overwrite, config_writable=args.config_writable)
        if result.skipped_reason == "not a registry map":
            totals["not a registry map"] += 1
            continue
        if result.skipped_reason:
            problems.append(result)
            continue
        totals["registry maps"] += 1
        totals["rows"] += result.rows
        for column, n in result.filled.items():
            totals[column] += n
        if result.changed:
            totals["files changed"] += 1
            if args.apply and result.new_text is not None:
                path.write_bytes(result.new_text.encode("latin-1"))
        if args.verbose and result.changed:
            filled: str = ", ".join(f"{c.removeprefix('ha ')}={n}" for c, n in sorted(result.filled.items())) or "columns only"
            print(f"{path}: {result.rows} rows ({filled})")

    mode: Literal['APPLIED'] | Literal['DRY RUN (nothing written; use --apply)'] = "APPLIED" if args.apply else "DRY RUN (nothing written; use --apply)"
    print(f"\n{mode}")
    for key in ("registry maps", "files changed", "rows", *HA_COLUMNS, "not a registry map"):
        print(f"  {key:<22} {totals[key]}")
    for result in problems:
        print(f"  ! {result.path}: {result.skipped_reason}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
