"""Unit tests for the timescaledb transport's connection_timeout setting (no database needed).

connection_timeout bounds how long a connection attempt may take, so an unreachable database host fails after that many
seconds instead of the operating system's TCP timeout (about two minutes). The setting is documented in the transport's
Configuration options and read with the other connection options; libpq itself calls the option "connect_timeout", so that
name still appears where it is handed to the driver.
"""

# These tests deliberately exercise the manager's private helpers.
# pyright: strict, reportPrivateUsage=false

from __future__ import annotations

from typing import Any

import classes.transports.timescaledb as tsdb


class _Settings:
    """The slice of a configparser section the connection manager reads."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values: dict[str, str] = values

    def get(self, key: str, fallback: str = "") -> str:
        return self._values.get(key, fallback)

    def getint(self, key: str, fallback: int = 0) -> int:
        return int(self._values[key]) if key in self._values else fallback


def test_the_connection_manager_reads_connection_timeout_from_the_settings() -> None:
    settings: Any = _Settings({"connection_timeout": "4"})
    assert tsdb.TimescaleDBConnectionManager.from_settings(settings).connection_timeout == 4


def test_connection_timeout_defaults_to_ten_seconds() -> None:
    settings: Any = _Settings({})
    assert tsdb.TimescaleDBConnectionManager.from_settings(settings).connection_timeout == 10


def test_the_old_name_is_not_read_any_more() -> None:
    settings: Any = _Settings({"connect_timeout": "99"})
    assert tsdb.TimescaleDBConnectionManager.from_settings(settings).connection_timeout == 10


def test_the_configured_value_reaches_the_driver_under_libpqs_own_option_name() -> None:
    settings: Any = _Settings({"connection_timeout": "7"})
    connect_args: dict[str, int] = tsdb.TimescaleDBConnectionManager.from_settings(settings)._connect_args()

    assert connect_args["connect_timeout"] == 7
    assert connect_args["keepalives"] == 1  # a dead link is noticed, not waited on


def test_the_transport_documents_and_defaults_the_setting_under_its_new_name() -> None:
    assert tsdb.timescaledb.connection_timeout == 10
    assert not hasattr(tsdb.timescaledb, "connect_timeout")
    assert "connection_timeout (int)" in (tsdb.timescaledb.__init__.__doc__ or "")
