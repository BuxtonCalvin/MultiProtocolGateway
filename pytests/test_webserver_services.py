# Description: Unit tests for WebServer service and diff helpers.
# File: test_webserver_services.py
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

"""Unit tests for WebServer service and diff helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from classes.WebServer.diff_engine import (
    ChangeType,
    DiffResult,
    ProtocolDiff,
    SettingDiff,
    build_diff,
)
from classes.WebServer.scanner import _classify_transport, scan_transport_library
from classes.WebServer.services import analysis_service, backup_service, device_service
from classes.WebServer.services.device_service import TransportLibraryRow


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
    result.settings.append(SettingDiff("transport.inv", "host", "old", "new", ChangeType.MODIFIED))
    result.settings.append(SettingDiff("transport.inv", "unused", "x", "x", ChangeType.ORPHAN, is_orphan=True))
    result.protocols.append(
        ProtocolDiff("eg4", "input", "1", "soc", "mask_enabled", False, True, ChangeType.MODIFIED)  # noqa: FBT003
    )
    assert result.has_changes is True
    assert result.summary["total_changes"] == 3
    assert result.summary["settings_orphaned"] == 1


def test_build_diff_uses_mocked_database_rows() -> None:
    """Mocks database session: build_diff returns staged setting changes.

    build_diff() now issues two queries: Setting (as before) and
    ProtocolRegister, the latter scoped to pending_delete only — see
    diff_engine.py. Ordinary ProtocolRegister field edits (variable_name,
    documented_name, etc.) are NOT surfaced here; they already highlight
    inline in the protocol table itself (dirty-row highlight + "*"
    indicator), so duplicating each one into this panel wouldn't add
    information. A staged deletion is different: it's destructive and
    irreversible once committed, unlike everything else in that table, so
    it gets its own explicit line — see
    test_build_diff_surfaces_pending_protocol_deletions below for that case.
    This test covers the "nothing pending deletion" case, where the second
    query returns empty and result.protocols stays [].
    """
    dirty_setting = SimpleNamespace(
        section="transport.inv", key="host", value_disk="old", value_staged="new",
        is_dirty=True, is_active=True, is_orphan=False,
    )
    orphan = SimpleNamespace(
        section="transport.inv", key="unused", value_disk="x", value_staged="x",
        is_dirty=False, is_active=True, is_orphan=True,
    )
    db = MagicMock()
    db.query.side_effect = [
        QueryStub([dirty_setting, orphan]),  # Setting query
        QueryStub([]),                       # ProtocolRegister.pending_delete query
    ]

    result: DiffResult = build_diff(db)

    assert [d.change_type for d in result.settings] == ["modified", "orphan"]
    assert result.protocols == []


def test_build_diff_surfaces_pending_protocol_deletions() -> None:
    """Edge case: a ProtocolRegister staged for deletion (pending_delete)
    is surfaced as its own ProtocolDiff with field "pending_delete" and
    change_type REMOVED, distinct from — and not to be confused with — the
    old_value/new_value=False/True pair, which is just how a boolean-shaped
    dataclass represents "didn't exist as staged for deletion, now does";
    the diff panel template renders this with its own "will be deleted"
    language rather than showing that raw boolean transition to the user.
    """
    pending = SimpleNamespace(
        protocol_name="eg4_ll_s", registry_type="holding", register_address="47",
        variable_name="register_47",
    )
    db = MagicMock()
    db.query.side_effect = [
        QueryStub([]),         # Setting query — nothing staged
        QueryStub([pending]),  # ProtocolRegister.pending_delete query
    ]

    result: DiffResult = build_diff(db)

    assert result.settings == []
    assert len(result.protocols) == 1

    diff: ProtocolDiff = result.protocols[0]
    assert diff.protocol_name == "eg4_ll_s"
    assert diff.registry_type == "holding"
    assert diff.register_address == "47"
    assert diff.variable_name == "register_47"
    assert diff.field == "pending_delete"
    assert diff.old_value is False
    assert diff.new_value is True
    assert diff.change_type == ChangeType.REMOVED

    assert result.summary["protocols_removed"] == 1
    assert result.summary["total_changes"] == 1


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
    config: Path = tmp_path / "config.cfg"
    backup: Path = tmp_path / "backup.cfg"
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
    result: list[TransportLibraryRow] = device_service.get_transport_library(tmp_path)
    assert [row["name"] for row in result] == ["modbus_tcp", "mqtt"]
    assert result[0]["key_count"] == 2


def test_scanner_classifies_transport_from_transport_type_attribute(tmp_path: Path) -> None:
    """Happy path: scanner classification uses the transport_type class attribute, not comments."""
    transports_dir = tmp_path / "transports"
    transports_dir.mkdir()
    (transports_dir / "fake_bridge.py").write_text(
        "# scraper misleading old comment\n"
        "class fake_bridge:\n"
        "    transport_type = 'bridge'\n"
        "    def __init__(self, settings):\n"
        "        self.host = settings.get('host', fallback='localhost')\n",
        encoding="utf-8",
    )
    (transports_dir / "fake_scraper.py").write_text(
        "# bridge misleading old comment\n"
        "class fake_scraper:\n"
        "    transport_type = 'scraper'\n"
        "    def __init__(self, settings):\n"
        "        self.port = settings.get('port', fallback='/dev/ttyUSB0')\n",
        encoding="utf-8",
    )

    assert _classify_transport("transport.out", {"transport": "fake_bridge"}, transports_dir) == "bridge"
    assert _classify_transport("transport.in", {"transport": "fake_scraper"}, transports_dir) == "scraper"

    library = scan_transport_library(transports_dir)
    assert library["fake_bridge"]["classification"] == "bridge"
    assert library["fake_scraper"]["classification"] == "scraper"


def test_scanner_treats_missing_transport_type_as_base_class(tmp_path: Path) -> None:
    """Edge case: modules without a transport_type attribute are base classes, regardless of comments."""
    transports_dir: Path = tmp_path / "transports"
    transports_dir.mkdir()
    (transports_dir / "helper.py").write_text(
        "# scraper misleading old comment\n"
        "class helper:\n"
        "    pass\n",
        encoding="utf-8",
    )

    library = scan_transport_library(transports_dir)
    assert library["helper"]["classification"] == "base class"
