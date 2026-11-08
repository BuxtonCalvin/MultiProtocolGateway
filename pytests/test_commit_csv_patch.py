"""Committing a protocol edit patches the CSV in place and keeps its comments.

The commit used to regenerate each edited protocol's CSV from the database rows, which dropped every ``#`` comment
row (the notes that document a map), blank lines, the file's own column order and the formatting of every row nobody
touched.  These tests drive the real commit helpers against a SQLite schema built from the models.

The patching itself is covered without a database in ``test_protocol_csv_patch``.
"""

from __future__ import annotations

# These tests exercise the commit's private helpers.
# pyright: reportPrivateUsage=false
import csv
import io
from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from classes.WebServer.config_writer import (
    _collect_pending_protocol_deletions,
    _delete_pending_protocol_registers,
    _write_protocol_csvs,
)
from classes.WebServer.models import Base, ProtocolRegister
from classes.WebServer.scanner import _upsert_protocol_register, scan_protocol_files

PROTOCOL: str = "vendor_map.holding_registry_map"
HEADER: str = "register,variable_name,documented_name,unit,data_type,values,read_interval,writable,adjustments,note\n"
DOCUMENTED: str = (
    HEADER
    + '"# DYNAMIC MAP: template rows use [anchor+offset]; see the docs, section 4.",,,,,,,,,\n'
    + "\n"
    + "# --- Inverter ---\n"
    + "[inverter_1_base+14],ac_power,AC Power,W,int16,,,R,,Total power\n"
    + "[inverter_1_base+15],ac_power_sf,AC Power SF,,int16,-10-10,,R,,Scale factor\n"
    + "# --- Common ---\n"
    + "40004,manufacturer,Manufacturer,,string32,,,R,,Mn\n"
)


@pytest.fixture
def db(tmp_path: Path) -> Generator[Session, None, None]:
    """A session on a fresh SQLite database with the schema the models define, configured like the app's own."""
    engine = create_engine(f"sqlite:///{tmp_path / 'staging.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session: Session = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


def load(db: Session, protocols_dir: Path, content: bytes | str = DOCUMENTED) -> Path:
    """Write ``protocols/vendor/<PROTOCOL>.csv`` and scan it into the database like the scanner does."""
    path: Path = protocols_dir / "vendor" / f"{PROTOCOL}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    for protocol_file in scan_protocol_files(protocols_dir):
        for reg in protocol_file.registers:
            _upsert_protocol_register(db, reg)
    db.commit()
    return path


def row(db: Session, address: str) -> ProtocolRegister:
    """The stored register at ``address``."""
    return db.query(ProtocolRegister).filter(ProtocolRegister.protocol_name == PROTOCOL, ProtocolRegister.register_address == address).one()


def test_committing_an_edit_keeps_the_comment_rows_and_every_untouched_line(db: Session, tmp_path: Path) -> None:
    path: Path = load(db, tmp_path)
    edited: ProtocolRegister = row(db, "40004")
    edited.documented_name = "Maker"
    edited.is_dirty = True
    db.commit()

    written: int = _write_protocol_csvs(db, tmp_path)

    before, after = DOCUMENTED.split("\n"), path.read_text(encoding="utf-8").split("\n")
    assert written == 1 and len(before) == len(after)
    assert [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b] == [7]
    assert after[7].startswith("40004,manufacturer,Maker,")


def test_the_files_header_and_column_order_are_not_rewritten(db: Session, tmp_path: Path) -> None:
    reordered: str = "register,variable_name,note,documented_name,unit,data_type,values,writable\n40000,a,Old note,Alpha,V,int16,,R\n# kept\n"
    path: Path = load(db, tmp_path, reordered)
    edited: ProtocolRegister = row(db, "40000")
    edited.note = "New note"
    edited.is_dirty = True
    db.commit()

    _write_protocol_csvs(db, tmp_path)

    assert path.read_text(encoding="utf-8") == "register,variable_name,note,documented_name,unit,data_type,values,writable\n40000,a,New note,Alpha,V,int16,,R\n# kept\n"


def test_a_protocol_nobody_edited_is_not_touched(db: Session, tmp_path: Path) -> None:
    path: Path = load(db, tmp_path)
    before: bytes = path.read_bytes()
    assert _write_protocol_csvs(db, tmp_path) == 0
    assert path.read_bytes() == before


def test_a_deletion_only_commit_removes_the_row_and_keeps_the_comments(db: Session, tmp_path: Path) -> None:
    path: Path = load(db, tmp_path)
    doomed: ProtocolRegister = row(db, "[inverter_1_base+15]")
    doomed.pending_delete = True
    db.commit()

    pending = _collect_pending_protocol_deletions(db)  # must be collected before the rows are deleted
    _, touched = _delete_pending_protocol_registers(db)
    written: int = _write_protocol_csvs(db, tmp_path, force_protocols=touched, deletions=pending)

    text: str = path.read_text(encoding="utf-8")
    names: list[str] = [r[1] for r in list(csv.reader(io.StringIO(text)))[1:] if len(r) > 1 and r[0] and not r[0].startswith("#")]
    assert written == 1 and names == ["ac_power", "manufacturer"]
    assert "# --- Inverter ---" in text and "# --- Common ---" in text and text.startswith(HEADER)


def test_a_virtual_row_that_is_dirty_never_reaches_the_file(db: Session, tmp_path: Path) -> None:
    path: Path = load(db, tmp_path)
    before: bytes = path.read_bytes()
    virtual = _upsert_protocol_register(
        db,
        {"protocol_group": "vendor", "protocol_name": PROTOCOL, "registry_type": "holding", "register_address": "~metric_1",
         "variable_name": "metric_1", "documented_name": "Metric", "write_mode_protocol": "R"},
    )  # fmt: skip
    assert virtual is not None
    virtual.is_synthetic = True
    virtual.is_dirty = True
    db.commit()

    _write_protocol_csvs(db, tmp_path)

    assert path.read_bytes() == before


def test_non_ascii_bytes_in_untouched_rows_and_in_the_edited_row_survive_unchanged(db: Session, tmp_path: Path) -> None:
    """The scanner reads these files as latin-1; the patch writes them back as latin-1, so the bytes are exactly what they were."""
    original: bytes = (HEADER + "40000,temp,Temperature,\u00b0C,int16,,,R,,\n40002,hum,Humidity,%,int16,,,R,,Rel \u2013 humidity\n").encode("utf-8")
    path: Path = load(db, tmp_path, original)
    edited: ProtocolRegister = row(db, "40000")
    edited.note = "checked"
    edited.is_dirty = True
    db.commit()

    _write_protocol_csvs(db, tmp_path)

    expected: bytes = original.replace(b"40000,temp,Temperature,\xc2\xb0C,int16,,,R,,\n", b"40000,temp,Temperature,\xc2\xb0C,int16,,,R,,checked\n")
    assert path.read_bytes() == expected  # the UTF-8 degree sign and en dash are the same bytes as before


def test_a_column_the_file_lacks_is_added_only_when_an_edit_needs_it(db: Session, tmp_path: Path) -> None:
    path: Path = load(db, tmp_path)
    edited: ProtocolRegister = row(db, "40004")
    edited.ha_device_class = "energy"
    edited.is_dirty = True
    db.commit()

    _write_protocol_csvs(db, tmp_path)

    lines: list[str] = path.read_text(encoding="utf-8").splitlines()
    assert lines[0].endswith(",note,ha_device_class") and lines[-1].endswith(",Mn,energy")
    assert "# --- Common ---" in lines


def test_a_file_that_cannot_be_patched_falls_back_to_regenerating_it(db: Session, tmp_path: Path) -> None:
    """With no usable header there is nothing to patch, so the previous behavior (rebuild from the database) applies."""
    path: Path = load(db, tmp_path)
    edited: ProtocolRegister = row(db, "40004")
    edited.documented_name = "Maker"
    edited.is_dirty = True
    db.commit()
    path.write_text("foo,bar\n1,2\n", encoding="utf-8")  # the file changed under us

    written: int = _write_protocol_csvs(db, tmp_path)

    text: str = path.read_text(encoding="utf-8")
    assert written == 1 and text.startswith("register,variable_name,documented_name,unit,data_type,values,adjustments,note,writable,read_interval")
    assert "Maker" in text
