"""The scanner keeps the staging database's register rows in step with the protocol CSVs.

The scenario behind these tests is a ``docker pull``: the database lives in a persistent volume, the
``protocols/`` directory is replaced by the new image, and the first scan finds CSV files whose addresses no
longer match the rows already stored.  Each test drives the same sequence the scanner runs (parse every file,
upsert the rows, reconcile each file) against a real SQLite schema built from the models.

The decision logic itself is covered without a database in ``test_protocol_reconcile``.
"""

from __future__ import annotations

# These tests exercise the scanner's private reconcile and upsert helpers.
# pyright: reportPrivateUsage=false
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from classes.WebServer.models import (
    Base,
    DeviceProtocolSelection,
    ProtocolFileState,
    ProtocolRegister,
)
from classes.WebServer.protocol_reconcile import ReconcileOutcome
from classes.WebServer.scanner import (
    ProtocolFileScan,
    _reconcile_protocol_file,
    _reconcile_protocol_files,
    _upsert_protocol_register,
    scan_protocol_files,
)

HEADER: str = "register,variable_name,documented_name,unit,data_type,values,read_interval,writable,adjustments,note\n"
PROTOCOL: str = "vendor_map.holding_registry_map"
REGISTRY: str = "holding"


@pytest.fixture
def db(tmp_path: Path) -> Generator[Session, None, None]:
    """A session on a fresh SQLite database with the schema the models define, configured like the app's own."""
    engine = create_engine(f"sqlite:///{tmp_path / 'staging.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session: Session = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


def write_csv(protocols_dir: Path, rows: list[tuple[str, str]], text: str | None = None) -> Path:
    """Write ``protocols/vendor/<PROTOCOL>.csv`` with ``(register, variable_name)`` rows, or raw ``text``."""
    path: Path = protocols_dir / "vendor" / f"{PROTOCOL}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    body: str = "".join(f'"{register}",{name},{name.replace("_", " ").title()},,uint16,,,R,,\n' for register, name in rows)
    path.write_text(text if text is not None else HEADER + body, encoding="utf-8")
    return path


def scan(db: Session, protocols_dir: Path) -> list[ReconcileOutcome]:
    """Run the scanner's sequence for the protocol files: upsert every row, then reconcile every file."""
    files: list[ProtocolFileScan] = scan_protocol_files(protocols_dir)
    for protocol_file in files:
        for reg in protocol_file.registers:
            _upsert_protocol_register(db, reg)
    outcomes: list[ReconcileOutcome] = [_reconcile_protocol_file(db, protocol_file) for protocol_file in files]
    db.commit()
    return outcomes


def stored(db: Session) -> dict[str, str]:
    """``register_address -> variable_name`` for the protocol's stored rows."""
    rows = db.query(ProtocolRegister).filter(ProtocolRegister.protocol_name == PROTOCOL).all()
    return {row.register_address: row.variable_name for row in rows}


def remembered_hash(db: Session) -> str | None:
    """The hash remembered for the protocol, if any."""
    state = db.query(ProtocolFileState).filter(ProtocolFileState.protocol_name == PROTOCOL).first()
    return state.file_sha256 if state is not None else None


V1: list[tuple[str, str]] = [("40001", "sunspec_id"), ("40005", "manufacturer"), ("40021", "model")]
V2: list[tuple[str, str]] = [("40000", "sunspec_id"), ("40004", "manufacturer"), ("40020", "model")]


def test_a_first_scan_stores_the_rows_and_remembers_the_files_hash(db: Session, tmp_path: Path) -> None:
    write_csv(tmp_path, V1)
    scan(db, tmp_path)
    assert stored(db) == {"40001": "sunspec_id", "40005": "manufacturer", "40021": "model"}
    assert remembered_hash(db) is not None


def test_a_docker_pull_that_moves_addresses_removes_the_old_rows(db: Session, tmp_path: Path) -> None:
    write_csv(tmp_path, V1)
    scan(db, tmp_path)

    write_csv(tmp_path, V2)  # the new image's copy of the file
    outcomes: list[ReconcileOutcome] = scan(db, tmp_path)

    assert stored(db) == {"40000": "sunspec_id", "40004": "manufacturer", "40020": "model"}
    assert sum(o.pruned_registers for o in outcomes) == 3


def test_converted_template_rows_replace_the_numeric_rows_they_came_from(db: Session, tmp_path: Path) -> None:
    write_csv(tmp_path, [("40083", "ac_power"), ("40002", "sunspec_did")])
    scan(db, tmp_path)
    write_csv(tmp_path, [("[inverter_1_base+14]", "ac_power"), ("40002", "sunspec_did")])
    scan(db, tmp_path)
    assert stored(db) == {"[inverter_1_base+14]": "ac_power", "40002": "sunspec_did"}


def test_the_first_scan_after_the_feature_arrives_heals_rows_that_were_already_stale(db: Session, tmp_path: Path) -> None:
    """No hash is remembered yet, so the protocol is reconciled even though the file did not just change."""
    write_csv(tmp_path, V1)
    for reg in scan_protocol_files(tmp_path)[0].registers:
        _upsert_protocol_register(db, reg)
    db.commit()
    assert remembered_hash(db) is None

    write_csv(tmp_path, V2)
    scan(db, tmp_path)
    assert set(stored(db)) == {"40000", "40004", "40020"}


def test_an_unchanged_file_is_not_reconciled_again(db: Session, tmp_path: Path) -> None:
    write_csv(tmp_path, V2)
    scan(db, tmp_path)
    _upsert_protocol_register(
        db,
        {"protocol_group": "vendor", "protocol_name": PROTOCOL, "registry_type": REGISTRY, "register_address": "49999",
         "variable_name": "leftover", "documented_name": "Leftover", "write_mode_protocol": "R"},
    )  # fmt: skip
    db.commit()

    outcomes: list[ReconcileOutcome] = scan(db, tmp_path)

    assert [o.skipped_reason for o in outcomes] == ["unchanged"]
    assert "49999" in stored(db)


def test_a_csv_that_cannot_be_read_prunes_nothing_and_keeps_the_old_hash(db: Session, tmp_path: Path) -> None:
    write_csv(tmp_path, V1)
    scan(db, tmp_path)
    before: str | None = remembered_hash(db)

    write_csv(tmp_path, [], text="")  # an empty file: no header row
    outcomes: list[ReconcileOutcome] = scan(db, tmp_path)

    assert [o.skipped_reason for o in outcomes] == ["unparsed"]
    assert set(stored(db)) == {"40001", "40005", "40021"}
    assert remembered_hash(db) == before


def test_rows_that_are_not_csv_rows_are_never_removed(db: Session, tmp_path: Path) -> None:
    write_csv(tmp_path, V1)
    scan(db, tmp_path)
    for address, flag in (("~metric_1", "is_synthetic"), ("~desc_1", "is_json_desc")):
        row = _upsert_protocol_register(
            db,
            {"protocol_group": "vendor", "protocol_name": PROTOCOL, "registry_type": REGISTRY, "register_address": address,
             "variable_name": address.lstrip("~"), "documented_name": "", "write_mode_protocol": "R"},
        )  # fmt: skip
        assert row is not None
        setattr(row, flag, True)
    db.commit()

    write_csv(tmp_path, V2)
    scan(db, tmp_path)

    assert {"~metric_1", "~desc_1"} <= set(stored(db))


def test_a_pending_edit_follows_its_variable_to_the_new_address(db: Session, tmp_path: Path) -> None:
    write_csv(tmp_path, [("40083", "ac_power")])
    scan(db, tmp_path)
    row = db.query(ProtocolRegister).filter(ProtocolRegister.register_address == "40083").one()
    row.documented_name = "Grid power (mine)"
    row.is_dirty = True
    db.commit()

    write_csv(tmp_path, [("[inverter_1_base+14]", "ac_power")])
    outcomes: list[ReconcileOutcome] = scan(db, tmp_path)

    kept = db.query(ProtocolRegister).filter(ProtocolRegister.protocol_name == PROTOCOL).all()
    assert [(r.register_address, r.documented_name, r.is_dirty) for r in kept] == [("[inverter_1_base+14]", "Grid power (mine)", True)]
    assert sum(o.carried_edits for o in outcomes) == 1


def test_a_pending_edit_with_no_matching_variable_is_dropped(db: Session, tmp_path: Path) -> None:
    write_csv(tmp_path, [("40083", "ac_power"), ("40002", "sunspec_did")])
    scan(db, tmp_path)
    row = db.query(ProtocolRegister).filter(ProtocolRegister.register_address == "40083").one()
    row.is_dirty = True
    db.commit()

    write_csv(tmp_path, [("40002", "sunspec_did")])
    outcomes: list[ReconcileOutcome] = scan(db, tmp_path)

    assert stored(db) == {"40002": "sunspec_did"}
    assert sum(o.dropped_edits for o in outcomes) == 1


def test_a_staged_device_selection_follows_the_variable_to_its_new_address(db: Session, tmp_path: Path) -> None:
    write_csv(tmp_path, [("40083", "ac_power")])
    scan(db, tmp_path)
    selection = DeviceProtocolSelection(
        device_name="inverter_a", protocol_name=PROTOCOL, registry_type=REGISTRY, register_address="40083",
        mask_enabled=True, mask_enabled_disk=False, is_dirty=True,
    )  # fmt: skip
    db.add(selection)
    db.commit()

    write_csv(tmp_path, [("[inverter_1_base+14]", "ac_power")])
    scan(db, tmp_path)

    moved = db.query(DeviceProtocolSelection).filter(DeviceProtocolSelection.device_name == "inverter_a").one()
    assert (moved.register_address, moved.mask_enabled, moved.is_dirty) == ("[inverter_1_base+14]", True, True)


def test_a_selection_pointing_at_a_vanished_address_is_removed(db: Session, tmp_path: Path) -> None:
    write_csv(tmp_path, [("40083", "ac_power"), ("40002", "sunspec_did")])
    scan(db, tmp_path)
    db.add(DeviceProtocolSelection(device_name="inverter_a", protocol_name=PROTOCOL, registry_type=REGISTRY, register_address="40083"))
    db.commit()

    write_csv(tmp_path, [("40002", "sunspec_did")])
    scan(db, tmp_path)

    assert db.query(DeviceProtocolSelection).filter(DeviceProtocolSelection.device_name == "inverter_a").count() == 0


def test_a_protocol_name_claimed_by_two_groups_is_never_reconciled(db: Session, tmp_path: Path) -> None:
    """Their rows share one key space, so each file would look like it lacked the other's addresses."""
    for group, rows in (("group_a", V1), ("group_b", V2)):
        path: Path = tmp_path / group / f"{PROTOCOL}.csv"
        path.parent.mkdir(parents=True)
        path.write_text(HEADER + "".join(f'"{register}",{name},{name},,uint16,,,R,,\n' for register, name in rows), encoding="utf-8")

    files: list[ProtocolFileScan] = scan_protocol_files(tmp_path)
    for protocol_file in files:
        for reg in protocol_file.registers:
            _upsert_protocol_register(db, reg)
    pruned: int = _reconcile_protocol_files(db, files)
    db.commit()

    assert pruned == 0
    assert set(stored(db)) == {"40001", "40005", "40021", "40000", "40004", "40020"}
    assert remembered_hash(db) is None


def test_a_failure_while_reconciling_leaves_the_rows_and_the_remembered_hash_alone(db: Session, tmp_path: Path) -> None:
    """The savepoint is rolled back and nothing is remembered, so the next scan tries again."""
    write_csv(tmp_path, V1)
    scan(db, tmp_path)
    before: str | None = remembered_hash(db)

    write_csv(tmp_path, V2)
    files: list[ProtocolFileScan] = scan_protocol_files(tmp_path)
    for protocol_file in files:
        for reg in protocol_file.registers:
            _upsert_protocol_register(db, reg)
    with patch.object(db, "delete", side_effect=RuntimeError("disk full")):
        outcome: ReconcileOutcome = _reconcile_protocol_file(db, files[0])

    assert outcome.skipped_reason == "error"
    assert remembered_hash(db) == before

    retry: ReconcileOutcome = _reconcile_protocol_file(db, files[0])  # the next scan, without the fault
    assert retry.pruned_registers == 3 and set(stored(db)) == {"40000", "40004", "40020"}
