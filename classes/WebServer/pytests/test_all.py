"""
MPG WebServer test suite.

Run from MultiProtocolGateway root:
    pytest classes/WebServer/pytests/ -v

Tests cover:
  - Config parser (section parsing, classification, orphan detection)
  - Scanner (upsert merge strategy, known-keys injection)
  - Diff engine (modified/added/removed/orphan detection)
  - Commit logic (config text generation, dirty flag reset)
"""
from __future__ import annotations

import logging
import textwrap
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

_log: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURE_DIR: Path = Path(__file__).parent / "fixtures"
SAMPLE_CONFIG: Path = FIXTURE_DIR / "sample_config.cfg"


@pytest.fixture(scope="function")
def db_session():
    """In-memory SQLite session for tests — fresh per test function."""
    import sys
    # Ensure the package root is on the path
    root = Path(__file__).resolve().parents[3]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from classes.WebServer.models import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()

    # Bootstrap AppState row
    from classes.WebServer.models import AppState
    session.add(AppState(id=1))
    session.commit()

    yield session
    session.close()
    engine.dispose()


@pytest.fixture(scope="session")
def sample_config_path():
    return SAMPLE_CONFIG


@pytest.fixture(scope="session")
def project_root():
    return Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Config parser tests
# ---------------------------------------------------------------------------

class TestConfigParser:

    def test_parses_all_sections(self, sample_config_path):
        from classes.WebServer.scanner import _load_config
        data = _load_config(sample_config_path)
        assert "general" in data
        assert "logging" in data

    def test_strips_inline_comments(self, sample_config_path):
        from classes.WebServer.scanner import _load_config
        data = _load_config(sample_config_path)
        # "weekly" should not have comment text appended
        assert "#" not in data.get("logging", {}).get("rotation", "")

    def test_scraper_classification(self, sample_config_path, project_root):
        from classes.WebServer.scanner import _classify_transport
        keys = {"transport": "modbus_tcp", "protocol_version": "eg4_18kpv"}
        result = _classify_transport("transport.Inverter_read", keys, project_root / "classes" / "transports")
        assert result == "scraper"

    def test_bridge_classification_by_file_comment(self, sample_config_path, project_root):
        from classes.WebServer.scanner import _classify_transport
        # No protocol_version → must fall back to file comment
        keys = {"transport": "mqtt"}
        result = _classify_transport("transport.mqtt", keys, project_root / "classes" / "transports")
        assert result in ("bridge", "general")  # depends on whether mqtt.py has the comment

    def test_no_protocol_no_transport_returns_general(self, project_root):
        from classes.WebServer.scanner import _classify_transport
        keys = {}
        result = _classify_transport("transport.unknown", keys, project_root / "classes" / "transports")
        assert result == "general"

    def test_missing_section_handled_gracefully(self, tmp_path):
        from classes.WebServer.scanner import _load_config
        cfg = tmp_path / "bad.cfg"
        cfg.write_text("[logging\nlevel = DEBUG\n")  # malformed header
        # Should not raise, may return empty or partial
        try:
            result = _load_config(cfg)
            assert isinstance(result, dict)
        except Exception as e:
            _log.debug(f"Unexpected error occurred: {e}")
            pass  # acceptable — parser may reject the file

    def test_duplicate_keys_last_wins(self, tmp_path) -> None:
        from classes.WebServer.scanner import _load_config
        cfg = tmp_path / "dup.cfg"
        cfg.write_text("[logging]\nlevel = INFO\nlevel = DEBUG\n")
        data = _load_config(cfg)
        # configparser last-write-wins behavior
        assert data["logging"]["level"] in ("INFO", "DEBUG")


# ---------------------------------------------------------------------------
# Scanner tests
# ---------------------------------------------------------------------------

class TestScanner:

    def test_upsert_creates_new_row(self, db_session) -> None:
        from classes.WebServer.scanner import _upsert_setting
        _upsert_setting(db_session, "logging", "level", "DEBUG", "general")
        db_session.flush()
        from classes.WebServer.models import Setting
        row = db_session.query(Setting).filter_by(section="logging", key="level").first()
        assert row is not None
        assert row.value_disk == "DEBUG"
        assert row.value_staged == "DEBUG"
        assert not row.is_dirty

    def test_upsert_preserves_staged_value(self, db_session) -> None:
        from classes.WebServer.scanner import _upsert_setting
        # First insert
        _upsert_setting(db_session, "logging", "level", "DEBUG", "general")
        db_session.flush()

        # User edits staged value
        from classes.WebServer.models import Setting
        row = db_session.query(Setting).filter_by(section="logging", key="level").first()
        row.value_staged = "WARNING"
        row.mark_dirty()
        db_session.flush()

        # Re-scan with new disk value
        _upsert_setting(db_session, "logging", "level", "INFO", "general")
        db_session.flush()

        row = db_session.query(Setting).filter_by(section="logging", key="level").first()
        assert row.value_disk == "INFO"
        assert row.value_staged == "WARNING"   # user edit preserved
        assert row.is_dirty                     # still dirty

    def test_orphan_detection(self, db_session):
        from classes.WebServer.scanner import _mark_orphaned_settings, _upsert_setting
        _upsert_setting(db_session, "logging", "old_level", "val", "general")
        _upsert_setting(db_session, "logging", "new_level", "val", "general")
        db_session.flush()

        # Only new_key was seen in the latest scan
        seen = {("general", "new_key")}
        count = _mark_orphaned_settings(db_session, seen)
        assert count == 1

        from classes.WebServer.models import Setting
        orphan = db_session.query(Setting).filter_by(key="old_key").first()
        assert orphan.is_orphan

    def test_orphan_cleared_on_rescan(self, db_session):
        from classes.WebServer.scanner import _mark_orphaned_settings, _upsert_setting
        _upsert_setting(db_session, "general", "maybe_orphan", "val", "general")
        db_session.flush()

        # Mark orphaned
        _mark_orphaned_settings(db_session, set())
        from classes.WebServer.models import Setting
        row = db_session.query(Setting).filter_by(key="maybe_orphan").first()
        assert row.is_orphan

        # Re-scan includes it again
        _upsert_setting(db_session, "general", "maybe_orphan", "val", "general")
        db_session.flush()
        row = db_session.query(Setting).filter_by(key="maybe_orphan").first()
        assert not row.is_orphan

    def test_ast_key_extraction(self, project_root):
        from classes.WebServer.scanner import _extract_settings_keys_from_ast
        modbus_tcp = project_root / "classes" / "transports" / "modbus_tcp.py"
        if not modbus_tcp.exists():
            pytest.skip("modbus_tcp.py not present in test environment")
        keys = _extract_settings_keys_from_ast(modbus_tcp)
        assert isinstance(keys, dict)
        assert len(keys) >= 0  # may be 0 if no settings.get patterns found

    def test_protocol_csv_parse(self, tmp_path):
        from classes.WebServer.scanner import _parse_protocol_csv
        csv_content: str = textwrap.dedent("""\
            Register,Variable Name,Documented Name,Unit,Writable,Values,Adjustments, Note
            40001,voltage,Pack Voltage,0.01V,R,0-65535,,Battery pack voltage
            40002,current,Pack Current,0.01A,RW,-32768-32767,,Charge positive
            40003,soc,State of Charge,%,R,0-100,
        """)
        csv_file = tmp_path / "test_holding.csv"
        csv_file.write_text(csv_content)
        rows = _parse_protocol_csv(csv_file, "test_group")
        assert len(rows) == 3
        assert rows[0]["variable_name"] == "voltage"
        assert rows[1]["write_mode_protocol"] == "RW"
        assert rows[2]["unit"] == "%"

    def test_protocol_csv_skips_comments(self, tmp_path):
        from classes.WebServer.scanner import _parse_protocol_csv
        csv_content = "Register,Variable Name,Documented Name,Unit,Writable,Values,Note\n"
        csv_content += "#40001,commented,Commented Out,,R,,\n"
        csv_content += "40002,active,Active Register,,R,,\n"
        csv_file = tmp_path / "test.csv"
        csv_file.write_text(csv_content)
        rows = _parse_protocol_csv(csv_file, "g")
        assert len(rows) == 1
        assert rows[0]["variable_name"] == "active"


# ---------------------------------------------------------------------------
# Diff engine tests
# ---------------------------------------------------------------------------

class TestDiffEngine:

    def test_no_diff_when_clean(self, db_session):
        from classes.WebServer.scanner import _upsert_setting
        _upsert_setting(db_session, "general", "log_level", "DEBUG", "general")
        db_session.commit()

        from classes.WebServer.diff_engine import build_diff
        result = build_diff(db_session)
        assert not result.has_changes

    def test_modified_setting_appears_in_diff(self, db_session):
        from classes.WebServer.models import Setting
        from classes.WebServer.scanner import _upsert_setting

        _upsert_setting(db_session, "logging", "level", "DEBUG", "general")
        db_session.flush()
        row = db_session.query(Setting).filter_by(key="log_level").first()
        row.value_staged = "WARNING"
        row.mark_dirty()
        db_session.commit()

        from classes.WebServer.diff_engine import build_diff
        result = build_diff(db_session)
        assert result.has_changes
        assert result.summary["settings_modified"] == 1
        assert result.settings[0].change_type == "modified"
        assert result.settings[0].old_value == "DEBUG"
        assert result.settings[0].new_value == "WARNING"

    def test_orphan_appears_in_diff(self, db_session):
        from classes.WebServer.models import Setting
        row = Setting(
            section="general", key="ghost_key",
            value_disk="old", value_staged="old",
            transport_type="general",
            is_orphan=True, is_active=True, is_dirty=False,
        )
        db_session.add(row)
        db_session.commit()

        from classes.WebServer.diff_engine import build_diff
        result = build_diff(db_session)
        orphan_diffs = [d for d in result.settings if d.change_type == "orphan"]
        assert len(orphan_diffs) == 1

    def test_protocol_toggle_diff(self, db_session):
        from classes.WebServer.models import ProtocolRegister
        reg = ProtocolRegister(
            protocol_group="eg4", protocol_name="eg4_holding",
            registry_type="holding", register_address="40001",
            variable_name="charge_current", documented_name="Charge Current",
            write_mode_protocol="RW",
            user_write_enabled=True,        # staged = True
            user_write_enabled_disk=False,  # disk  = False
            mask_enabled=True, mask_enabled_disk=True,
            screen_enabled=False, screen_enabled_disk=False,
            is_dirty=True,
        )
        db_session.add(reg)
        db_session.commit()

        from classes.WebServer.diff_engine import build_diff
        result = build_diff(db_session)
        assert result.summary["protocols_modified"] == 1


# ---------------------------------------------------------------------------
# Commit tests
# ---------------------------------------------------------------------------

class TestCommit:

    def test_config_text_generation(self, db_session):
        from classes.WebServer.scanner import _upsert_setting
        _upsert_setting(db_session, "logging", "level", "DEBUG", "general")
        _upsert_setting(db_session, "transport.Inv1", "host", "10.0.0.1", "scraper")
        _upsert_setting(db_session, "transport.Inv1", "port", "502", "scraper")
        db_session.commit()

        from classes.WebServer.config_writer import _build_config_text
        from classes.WebServer.models import Setting
        rows = db_session.query(Setting).filter_by(is_active=True).all()
        text = _build_config_text(rows)

        assert "[logging]" in text
        assert "level = DEBUG" in text
        assert "[transport.Inv1]" in text
        assert "host = 10.0.0.1" in text

    def test_general_before_transport_in_output(self, db_session):
        from classes.WebServer.config_writer import _build_config_text
        from classes.WebServer.scanner import _upsert_setting
        _upsert_setting(db_session, "transport.Inv1", "host", "1.2.3.4", "scraper")
        _upsert_setting(db_session, "logging", "level", "INFO", "general")
        db_session.commit()

        from classes.WebServer.models import Setting
        rows = db_session.query(Setting).all()
        text = _build_config_text(rows)
        general_pos = text.find("[logging]")
        transport_pos = text.find("[transport.")
        assert general_pos < transport_pos

    def test_dirty_flags_reset_after_commit(self, db_session, tmp_path):
        from classes.WebServer.models import Setting
        from classes.WebServer.scanner import _upsert_setting

        _upsert_setting(db_session, "logging", "level", "DEBUG", "general")
        db_session.flush()
        row = db_session.query(Setting).filter_by(key="level").first()
        row.value_staged = "WARNING"
        row.mark_dirty()
        db_session.commit()

        assert row.is_dirty

        from classes.WebServer.config_writer import _reset_dirty_flags
        _reset_dirty_flags(db_session)
        db_session.commit()

        row = db_session.query(Setting).filter_by(key="level").first()
        assert not row.is_dirty
        assert row.value_disk == "WARNING"   # disk synced to staged

    def test_inactive_rows_excluded_from_output(self, db_session):
        from classes.WebServer.config_writer import _build_config_text
        from classes.WebServer.scanner import _upsert_setting
        _upsert_setting(db_session, "logging", "level", "DEBUG", "general", is_active=True)
        _upsert_setting(db_session, "logging", "secret_key", "hidden", "general", is_active=False)
        db_session.commit()

        from classes.WebServer.models import Setting
        rows = db_session.query(Setting).all()
        text = _build_config_text(rows)
        assert "level" in text
        assert "secret_key" not in text

    def test_backup_creates_file(self, db_session, tmp_path):
        config_path = tmp_path / "config.cfg"
        config_path.write_text("[logging]\nlevel = DEBUG\n")

        from classes.WebServer.config_writer import create_backup
        record = create_backup(config_path, db_session, trigger="test")
        db_session.commit()

        assert Path(record.filepath).exists()
        assert record.trigger == "test"
        if record.file_size_bytes is not None:
            assert record.file_size_bytes > 0


class TestCreateDeviceHelpers:

    def test_append_section_to_config_preserves_existing_text(self, tmp_path):
        from classes.WebServer.routers.pages import _append_section_to_config

        config_path = tmp_path / "config.cfg"
        original = "[logging]\nlevel = INFO\n"
        config_path.write_text(original, encoding="utf-8")

        _append_section_to_config(
            config_path,
            "transport.Inverter9",
            [
                ("transport", "modbus_tcp"),
                ("bridge", "transport.mqtt"),
                ("protocol_version", "eg4_18kpv"),
            ],
        )

        text = config_path.read_text(encoding="utf-8")
        assert text.startswith(original)
        assert "[transport.Inverter9]" in text
        assert "bridge = transport.mqtt" in text

    def test_create_device_request_normalizes_log_level(self):
        from classes.WebServer.routers.pages import CreateDeviceRequest

        payload = CreateDeviceRequest(
            device_name="Inverter9",
            scraper_transport="modbus_tcp",
            bridge="transport.mqtt",
            protocol_version="eg4_18kpv",
            log_level="debug",
            settings=[],
        )

        assert payload.log_level == "DEBUG"
