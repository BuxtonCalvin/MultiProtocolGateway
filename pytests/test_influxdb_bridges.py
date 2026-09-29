# Description: Unit tests for the InfluxDB v1/v3 bridge backlog, stale-data and shutdown behavior
# File: test_influxdb_bridges.py
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

"""Tests for influxdb_out / influxdb3_out: single-copy backlog, stale-data throttling, clean shutdown."""

from __future__ import annotations

import pickle
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from conftest import DummySettings

from classes.transports.influxdb3_out import influxdb3_out
from classes.transports.influxdb_out import influxdb_out
from classes.transports.transport_base import transport_base

Bridge = influxdb_out | influxdb3_out


def _make_bridge(
    bridge_cls: type[Bridge],
    settings_cls: type[DummySettings],
    backlog_file: Path,
    **overrides: Any,
) -> Bridge:
    """Build a bridge with persistent storage pointed at a temp file and no real server."""
    values: dict[str, Any] = {
        "enable_persistent_storage": "false",  # avoid touching the real backlogs folder
        "batch_size": 100,
        "batch_timeout": 1_000_000.0,
    }
    values.update(overrides)
    bridge: Bridge = bridge_cls(settings_cls(**values))
    bridge.enable_persistent_storage = True
    bridge.backlog_file = backlog_file
    bridge.last_batch_time = time.time()  # so the batch timeout does not trigger a flush
    return bridge


def _set_online(bridge: Bridge, monkeypatch: pytest.MonkeyPatch, online: bool) -> None:
    monkeypatch.setattr(bridge, "_check_connection", lambda: online)


def _source(settings_cls: type[DummySettings]) -> transport_base:
    return transport_base(settings_cls(name="transport.src", device_serial_number="SN1"))


def _saved_backlog(path: Path) -> list[dict[str, object]]:
    loaded: object = pickle.loads(path.read_bytes())  # noqa: S301 - test reads its own tmp file
    assert isinstance(loaded, list)
    return [p for p in loaded if isinstance(p, dict)] # type: ignore


def _field_values(points: list[dict[str, object]], name: str) -> list[object]:
    values: list[object] = []
    for point in points:
        fields: object = point["fields"]
        assert isinstance(fields, dict)
        values.append(fields[name]) # type: ignore
    return values


@pytest.mark.parametrize("bridge_cls", [influxdb_out, influxdb3_out])
def test_offline_points_are_stored_once_and_in_order(
    bridge_cls: type[Bridge],
    dummy_settings: type[DummySettings],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Points captured offline live only in the backlog (no batch copy), older batch points first."""
    bridge: Bridge = _make_bridge(bridge_cls, dummy_settings, tmp_path / "b.pkl")
    source: transport_base = _source(dummy_settings)

    # One point captured while online, still waiting in the in-memory batch.
    _set_online(bridge, monkeypatch, True)  # noqa: FBT003
    bridge.write_data({"a": 0}, source)
    assert len(bridge.batch_points) == 1

    # Connection drops: two more points arrive.
    _set_online(bridge, monkeypatch, False)  # noqa: FBT003
    bridge.write_data({"a": 1}, source)
    bridge.write_data({"a": 2}, source)

    assert bridge.batch_points == []
    assert _field_values(bridge.backlog_points, "a") == [0.0, 1.0, 2.0]
    assert _field_values(_saved_backlog(tmp_path / "b.pkl"), "a") == [0.0, 1.0, 2.0]


@pytest.mark.parametrize("bridge_cls", [influxdb_out, influxdb3_out])
def test_failed_batch_flush_backlogs_points_with_single_save(
    bridge_cls: type[Bridge],
    dummy_settings: type[DummySettings],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch that cannot be delivered moves to the backlog once, with one file write."""
    bridge: Bridge = _make_bridge(bridge_cls, dummy_settings, tmp_path / "b.pkl")
    source: transport_base = _source(dummy_settings)

    _set_online(bridge, monkeypatch, True)  # noqa: FBT003
    for i in range(3):
        bridge.write_data({"a": i}, source)
    assert len(bridge.batch_points) == 3

    _set_online(bridge, monkeypatch, False)  # noqa: FBT003
    save_spy: MagicMock = MagicMock(wraps=bridge._save_backlog)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(bridge, "_save_backlog", save_spy)
    bridge._flush_batch()  # pyright: ignore[reportPrivateUsage]

    assert bridge.batch_points == []
    assert _field_values(bridge.backlog_points, "a") == [0.0, 1.0, 2.0]
    assert save_spy.call_count == 1


@pytest.mark.parametrize("bridge_cls", [influxdb_out, influxdb3_out])
def test_backlog_trims_oldest_points_when_full(
    bridge_cls: type[Bridge],
    dummy_settings: type[DummySettings],
    tmp_path: Path,
) -> None:
    """max_backlog_size keeps the newest points."""
    bridge: Bridge = _make_bridge(bridge_cls, dummy_settings, tmp_path / "b.pkl", max_backlog_size=3)
    points: list[dict[str, object]] = [{"measurement": "m", "tags": {}, "fields": {"a": i}} for i in range(5)]

    bridge._add_points_to_backlog(points)  # pyright: ignore[reportPrivateUsage]

    assert _field_values(bridge.backlog_points, "a") == [2, 3, 4]


@pytest.mark.parametrize("bridge_cls", [influxdb_out, influxdb3_out])
def test_disabled_persistence_discards_points_without_error(
    bridge_cls: type[Bridge],
    dummy_settings: type[DummySettings],
    tmp_path: Path,
) -> None:
    """With persistent storage off, undeliverable points are dropped (and logged), never stored."""
    bridge: Bridge = _make_bridge(bridge_cls, dummy_settings, tmp_path / "b.pkl")
    bridge.enable_persistent_storage = False

    bridge._add_points_to_backlog([{"measurement": "m", "tags": {}, "fields": {"a": 1}}])  # pyright: ignore[reportPrivateUsage]

    assert bridge.backlog_points == []
    assert not (tmp_path / "b.pkl").exists()


@pytest.mark.parametrize("bridge_cls", [influxdb_out, influxdb3_out])
def test_stale_reconnect_is_requested_capped_and_throttled(
    bridge_cls: type[Bridge],
    dummy_settings: type[DummySettings],
    tmp_path: Path,
) -> None:
    """Reconnect requests start at the first stale detection, are spaced by retry_delay_mins,
    stop at max_stale_attempts, and reset once the data changes."""
    bridge: Bridge = _make_bridge(
        bridge_cls,
        dummy_settings,
        tmp_path / "b.pkl",
        stale_data_timeout=300,
        max_stale_attempts=2,
        retry_delay_mins=5,
    )
    reconnect: MagicMock = MagicMock()
    bridge.request_upstream_reconnect = reconnect
    bridge.send_message = MagicMock()

    t0: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)
    frozen: dict[str, int | float | str] = {"soc": 50.0}

    def step(seconds: int, row: dict[str, int | float | str]) -> None:
        ts: datetime = t0 + timedelta(seconds=seconds)
        stale: bool = bridge._check_is_stale("src", row, ts)  # pyright: ignore[reportPrivateUsage]
        bridge._commit_transport_state("src", row, ts, stale)  # pyright: ignore[reportPrivateUsage]

    step(0, frozen)          # first sighting, starts the unchanged timer
    step(200, frozen)        # unchanged, not yet stale
    assert reconnect.call_count == 0

    step(400, frozen)        # stale -> attempt 1
    assert reconnect.call_count == 1
    step(410, frozen)        # throttled (< 5 min since attempt 1)
    assert reconnect.call_count == 1

    step(710, frozen)        # 5+ min after attempt 1 -> attempt 2
    assert reconnect.call_count == 2
    step(1200, frozen)       # cap reached
    assert reconnect.call_count == 2

    step(1300, {"soc": 51.0})  # data changed -> reset
    state = bridge._stale_registry["src"]  # pyright: ignore[reportPrivateUsage]
    assert state["is_stale"] is False
    assert state["stale_event_count"] == 0

    step(1300 + 400, {"soc": 51.0})  # stale again -> a fresh set of attempts
    assert reconnect.call_count == 3


@pytest.mark.parametrize("bridge_cls", [influxdb_out, influxdb3_out])
def test_cleanup_saves_pending_batch_to_backlog_when_offline(
    bridge_cls: type[Bridge],
    dummy_settings: type[DummySettings],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gateway stop/reload (cleanup) must not lose pending batch points when the server is down."""
    bridge: Bridge = _make_bridge(bridge_cls, dummy_settings, tmp_path / "b.pkl")
    source: transport_base = _source(dummy_settings)
    client: MagicMock = MagicMock()
    bridge.client = client

    _set_online(bridge, monkeypatch, True)  # noqa: FBT003
    bridge.write_data({"a": 1}, source)
    bridge.write_data({"a": 2}, source)
    assert len(bridge.batch_points) == 2

    bridge.connected = False
    bridge.cleanup()

    assert _field_values(_saved_backlog(tmp_path / "b.pkl"), "a") == [1.0, 2.0]
    assert bridge.batch_points == []
    client.close.assert_called_once()
    assert bridge.client is None


@pytest.mark.parametrize("bridge_cls", [influxdb_out, influxdb3_out])
def test_cleanup_writes_pending_batch_when_connected_and_is_idempotent(
    bridge_cls: type[Bridge],
    dummy_settings: type[DummySettings],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When connected, cleanup writes the pending batch directly; repeat calls do nothing."""
    bridge: Bridge = _make_bridge(bridge_cls, dummy_settings, tmp_path / "b.pkl")
    source: transport_base = _source(dummy_settings)
    client: MagicMock = MagicMock()
    bridge.client = client

    _set_online(bridge, monkeypatch, True)  # noqa: FBT003
    bridge.write_data({"a": 1}, source)
    bridge.write_data({"a": 2}, source)
    bridge.connected = True

    write_call: MagicMock = client.write_points if isinstance(bridge, influxdb_out) else client.write

    bridge.cleanup()
    bridge.cleanup()  # second call (and a later __del__) must be a no-op

    assert write_call.call_count == 1
    assert bridge.backlog_points == []
    assert client.close.call_count == 1
