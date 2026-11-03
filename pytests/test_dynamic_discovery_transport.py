"""The Modbus transport's discovery hook: when it runs, what it rebuilds, and what it must never do.

The tests call the REAL ``discover_register_structure``, ``read_discovery_registers`` and ``init_after_connect``
on a duck-typed fake transport that answers Modbus reads from a register table, so no client or serial port is
needed.
"""

from __future__ import annotations

# These tests deliberately exercise private helpers and use a duck-typed fake transport.
# pyright: reportPrivateUsage=false, reportArgumentType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false
import logging
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from classes.protocol_settings import (
    Registry_Type,
    protocol_settings,
    registry_map_entry,
)
from classes.transports.modbus_base import modbus_base

SUNS: tuple[int, int] = (0x5375, 0x6E53)


def chain_image(models: list[tuple[int, int]], base: int = 40000) -> dict[int, int]:
    """Build the register image of a SunSpec chain: marker, one header per model, then the end header."""
    words: dict[int, int] = {base: SUNS[0], base + 1: SUNS[1]}
    address: int = base + 2
    for model_id, length in models:
        words[address], words[address + 1] = model_id, length
        address += 2 + length
    words[address], words[address + 1] = 0xFFFF, 0
    return words


def load(name: str) -> protocol_settings:
    """Load a protocol with logging silenced."""
    logging.disable(logging.CRITICAL)
    try:
        return protocol_settings(name)
    finally:
        logging.disable(logging.NOTSET)


class FakeModbus:
    """Just enough of ``modbus_base`` for the discovery hook, borrowing the real methods under test."""

    def __init__(self, protocol: protocol_settings, words: dict[int, int] | None = None) -> None:
        """Wrap ``protocol`` and answer reads from ``words`` (address -> value); unknown addresses fail."""
        self._protocol: protocol_settings = protocol
        self._log: logging.Logger = logging.getLogger("fake_modbus")
        self.transport_name: str = "transport.fake"
        self.words: dict[int, int] = dict(words or {})
        self.reads: int = 0
        self.failure_resets: int = 0
        self.events: list[str] = []
        self._transport_lock: threading.Lock = threading.Lock()
        self.write_enabled: bool = False
        self.identification_attempted: bool = False
        self.device_serial_number: str = ""
        self.port: str = "fake"

    def read_registers(self, start: int, count: int = 1, registry_type: Registry_Type = Registry_Type.INPUT, device_id: int | None = None) -> Any:
        """Answer like pymodbus: a response with ``registers`` or an error response."""
        self.reads += 1
        if any(start + offset not in self.words for offset in range(count)):
            return SimpleNamespace(isError=lambda: True)
        return SimpleNamespace(isError=lambda: False, registers=[self.words[start + offset] for offset in range(count)])

    def _extract_response_values(self, response: Any, registry_type: Registry_Type, register_range: tuple[int, int]) -> dict[int, int] | None:
        """Use the real extraction."""
        return modbus_base._extract_response_values(self, response, registry_type, register_range)

    def read_discovery_registers(self, start: int, count: int, registry_type: Registry_Type) -> list[int] | None:
        """Use the real adapter."""
        return modbus_base.read_discovery_registers(self, start, count, registry_type)

    def reset_register_failure_tracking(self) -> None:
        """Record that the trackers were cleared."""
        self.failure_resets += 1

    def discover_register_structure(self) -> bool:
        """Use the real hook, noting when it ran."""
        self.events.append("discover")
        return modbus_base.discover_register_structure(self)

    def read_serial_number(self) -> str:
        """Stand in for identification, noting when it ran."""
        self.events.append("identify")
        return "SN"

    def update_identifier(self) -> None:
        """Nothing to refresh on the fake."""


DEFAULT_CHAIN: list[tuple[int, int]] = [(1, 65), (103, 50), (120, 26), (121, 30)]


def holding(proto: protocol_settings) -> dict[str, registry_map_entry]:
    """Return the HOLDING map keyed by variable name."""
    return {entry.variable_name: entry for entry in proto.registry_map[Registry_Type.HOLDING]}


def test_a_static_protocol_never_touches_the_device() -> None:
    fake: FakeModbus = FakeModbus(load("eastron_eastron_sdm120m"))
    assert fake.discover_register_structure() is False
    assert fake.reads == 0 and fake.failure_resets == 0


def test_a_dynamic_protocol_is_rebuilt_from_the_device_and_failure_tracking_is_cleared() -> None:
    fake: FakeModbus = FakeModbus(load("solaredge_sunspec"), chain_image([*DEFAULT_CHAIN, (203, 105)]))
    assert fake.discover_register_structure() is True
    assert "meter_1_ac_power" in holding(fake._protocol)
    assert fake.failure_resets == 1


def test_a_reconnect_that_finds_the_same_layout_changes_nothing() -> None:
    fake: FakeModbus = FakeModbus(load("solaredge_sunspec"), chain_image(DEFAULT_CHAIN))
    fake.discover_register_structure()
    resets_after_first: int = fake.failure_resets
    assert fake.discover_register_structure() is False
    assert fake.failure_resets == resets_after_first


def test_a_reconnect_notices_equipment_that_appeared_or_vanished() -> None:
    fake: FakeModbus = FakeModbus(load("solaredge_sunspec"), chain_image([*DEFAULT_CHAIN, (203, 105)]))
    fake.discover_register_structure()
    fake.words = chain_image(DEFAULT_CHAIN)
    assert fake.discover_register_structure() is True
    assert "meter_1_ac_power" not in holding(fake._protocol)
    assert fake.failure_resets == 2


def test_a_silent_device_leaves_the_default_layout_in_place() -> None:
    proto: protocol_settings = load("solaredge_sunspec")
    fake: FakeModbus = FakeModbus(proto, {})
    assert fake.discover_register_structure() is False
    assert holding(proto)["ac_power"].register == 40083
    assert fake.failure_resets == 0


def test_a_provider_that_raises_cannot_break_the_connection() -> None:
    fake: FakeModbus = FakeModbus(load("solaredge_sunspec"), chain_image(DEFAULT_CHAIN))
    with patch("classes.transports.modbus_base.run_discovery", side_effect=RuntimeError("boom")):
        assert fake.discover_register_structure() is False
    assert holding(fake._protocol)["ac_power"].register == 40083


def test_the_reader_adapter_returns_exactly_the_requested_words_or_none() -> None:
    fake: FakeModbus = FakeModbus(load("solaredge_sunspec"), {100: 1, 101: 2, 102: 3})
    assert fake.read_discovery_registers(100, 3, Registry_Type.HOLDING) == [1, 2, 3]
    assert fake.read_discovery_registers(100, 4, Registry_Type.HOLDING) is None
    assert fake.read_discovery_registers(500, 1, Registry_Type.HOLDING) is None


def test_init_after_connect_discovers_before_it_identifies_the_device() -> None:
    fake: FakeModbus = FakeModbus(load("solaredge_sunspec"), chain_image(DEFAULT_CHAIN))
    modbus_base.init_after_connect(fake)
    assert fake.events == ["discover", "identify"]
    assert fake.device_serial_number == "SN"
