"""Unit tests for WebServer service and diff helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from classes.WebServer.diff_engine import (
    DiffResult,
    ProtocolDiff,
    SettingDiff,
    build_diff,
)
from classes.WebServer.models import ProtocolRegister, Setting  # noqa: F401
from classes.WebServer.services import analysis_service, backup_service, device_service


class QueryStub:
    """Tiny SQLAlchemy query test double for all/filter/order_by/limit chains."""

    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self


def test_diff_result_summary_counts_changes() -> None:
    """Happy path: DiffResult.summary aggregates setting and protocol change counts."""
    result = DiffResult()
    result.settings.append(SettingDiff("transport.inv", "host", "old", "new", "modified"))
    result.settings.append(SettingDiff("transport.inv", "unused", "x", "x", "orphan", is_orphan=True))
    result.protocols.append(
        ProtocolDiff("eg4", "input", "1", "soc", "mask_enabled", False, True, "modified")  # noqa: FBT003
    )
    assert result.has_changes is True
    assert result.summary["total_changes"] == 3
    assert result.summary["settings_orphaned"] == 1


def test_build_diff_uses_mocked_database_rows() -> None:
    """Mocks database session: build_diff returns staged setting and protocol changes."""
    dirty_setting = SimpleNamespace(
        section="transport.inv", key="host", value_disk="old", value_staged="new",
        is_dirty=True, is_active=True, is_orphan=False,
    )
    orphan = SimpleNamespace(
        section="transport.inv", key="unused", value_disk="x", value_staged="x",
        is_dirty=False, is_active=True, is_orphan=True,
    )
    dirty_protocol = SimpleNamespace(
        protocol_name="eg4", registry_type="input", register_address="1", variable_name="soc",
        user_write_enabled_disk=False, user_write_enabled=True,
        mask_enabled_disk=False, mask_enabled=False,
        screen_enabled_disk=True, screen_enabled=True,
    )
    db = MagicMock()
    db.query.side_effect = [QueryStub([dirty_setting, orphan]), QueryStub([dirty_protocol])]

    result = build_diff(db)

    assert [d.change_type for d in result.settings] == ["modified", "orphan"]
    assert result.protocols[0].field == "user_write_enabled"


def test_analysis_service_handles_missing_gateway_and_live_transports() -> None:
    """Happy path and null handling: analysis helpers tolerate None and summarize live transports."""
    assert analysis_service.get_scraper_transports(None) == []
    assert analysis_service.get_transport_connection_status(None) == {}

    protocol = SimpleNamespace(protocol="eg4")
    scraper = SimpleNamespace(transport_name="inv", connected=True, protocolSettings=protocol)
    bridge = SimpleNamespace(transport_name="mqtt", connected=False, protocolSettings=None)
    gateway = SimpleNamespace(_Protocol_Gateway__transports=[scraper, bridge])
    assert analysis_service.get_scraper_transports(gateway) == [
        {"transport_name": "inv", "transport_type": "SimpleNamespace", "connected": True, "protocol": "eg4"}
    ]
    assert analysis_service.get_transport_connection_status(gateway) == {"inv": True, "mqtt": False}


def test_device_service_delete_orphan_commits_only_for_orphans() -> None:
    """Mocks database session: deleting an orphan commits, while normal rows are ignored."""
    db = MagicMock()
    db.get.return_value = SimpleNamespace(is_orphan=True)
    assert device_service.delete_orphan(db, 1) is True
    db.delete.assert_called_once()
    db.commit.assert_called_once()

    db.reset_mock()
    db.get.return_value = SimpleNamespace(is_orphan=False)
    assert device_service.delete_orphan(db, 2) is False
    db.delete.assert_not_called()


def test_backup_service_rollback_restores_file_and_records_backup(tmp_path: Path) -> None:
    """Mocks config backup storage: rollback copies a backup over config and records a pre-rollback backup."""
    config = tmp_path / "config.cfg"
    backup = tmp_path / "backup.cfg"
    config.write_text("current", encoding="utf-8")
    backup.write_text("previous", encoding="utf-8")
    db = MagicMock()
    db.get.return_value = SimpleNamespace(filepath=str(backup))

    assert backup_service.rollback_to(db, 7, config) is True

    assert config.read_text(encoding="utf-8") == "previous"
    db.add.assert_called_once()
    db.commit.assert_called_once()


def test_backup_service_rollback_returns_false_for_missing_records(tmp_path: Path) -> None:
    """Error handling: rollback returns False when the DB record or backup file is missing."""
    db = MagicMock()
    db.get.return_value = None
    assert backup_service.rollback_to(db, 1, tmp_path / "config.cfg") is False

    db.get.return_value = SimpleNamespace(filepath=str(tmp_path / "missing.cfg"))
    assert backup_service.rollback_to(db, 1, tmp_path / "config.cfg") is False


@patch("classes.WebServer.services.device_service.scan_transport_library")
def test_get_transport_library_shapes_scanner_output(mock_scan: MagicMock, tmp_path: Path) -> None:
    """Mocks scanner dependency: transport library output is sorted and includes key summaries."""
    mock_scan.return_value = {
        "mqtt": {"classification": "bridge", "keys": {"host": "", "port": ""}},
        "modbus_tcp": {"classification": "scraper", "keys": {"host": "", "protocol_version": ""}},
    }
    result = device_service.get_transport_library(tmp_path)
    assert [row["name"] for row in result] == ["modbus_tcp", "mqtt"]
    assert result[0]["key_count"] == 2
