"""EG4 device identification is read from the hardware once and cached; scrape cycles must not re-read it.

The tests drive the REAL identification code (identify_eg4_device, read_eg4_serial_number, the metadata
readers and compute_eg4_post_process_fields) against a fake device that answers Modbus reads from a register
table and counts every request, so "no extra reads per cycle" is measured, not assumed.
"""

from __future__ import annotations

# These tests deliberately exercise private helpers and use a duck-typed fake transport.
# pyright: reportPrivateUsage=false, reportArgumentType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false
from types import SimpleNamespace
from typing import Any, LiteralString

import pytest

from classes import eg4_metadata as eg4
from classes.protocol_settings import Registry_Type


def _text_regs(first_register: int, text: str) -> dict[int, int]:
    """Pack text two characters per register the way the EG4 does (first character in the LOW byte)."""
    padded: str = text if len(text) % 2 == 0 else text + "\0"
    return {first_register + i // 2: ord(padded[i]) | (ord(padded[i + 1]) << 8) for i in range(0, len(padded), 2)}


SERIAL = "AB12345678"
BATTERY_SERIAL_1: LiteralString = "Battery_ID_01xxxx"[:16]
BATTERY_SERIAL_2: LiteralString = "Battery_ID_02yyyy"[:16]


def _inverter_registers() -> dict[Registry_Type, dict[int, int]]:
    holding: dict[int, int] = {0: 0x0000, 1: 0x0000, 19: eg4.EG4_DEVICE_TYPE_CODE_HYBRID}
    holding.update(_text_regs(2, SERIAL))
    holding.update(_text_regs(7, "FAAB"))
    holding[9] = 0x2700
    holding[10] = 0x0027
    inputs: dict[int, int] = {}
    inputs.update(_text_regs(100, BATTERY_SERIAL_1))  # 8 registers
    inputs.update(_text_regs(110, BATTERY_SERIAL_2))
    return {Registry_Type.HOLDING: holding, Registry_Type.INPUT: inputs}


class FakeDevice:
    """Just enough of modbus_base for eg4_metadata, with every request counted."""

    def __init__(self, protocol: str = "eg4_18kpv", battery_entries: tuple[tuple[str, int], ...] = (("batteryserialnumber_1", 100), ("batteryserialnumber_2", 110))) -> None:
        self.transport_name = "transport.fake"
        self.modbus_delay = 0.0
        self.send_holding_register = True
        self.send_input_register = True
        self.device_serial_number = ""
        self.eg4_hardware_kind_cache: str | None = None
        self.device_metadata: Any = None
        self.eg4_cache = eg4.EG4IdentificationCache()
        self.regs: dict[Registry_Type, dict[int, int]] = _inverter_registers()
        self.variables: dict[str, float] = {}
        self.reads: list[tuple[str, int, int]] = []
        self.variable_reads: list[str] = []
        entries: list[SimpleNamespace] = [SimpleNamespace(variable_name=name, register=reg) for name, reg in battery_entries]
        self._proto = SimpleNamespace(protocol=protocol, get_registry_map=lambda _t: entries)

    @property
    def proto(self) -> Any:
        return self._proto

    def read_variable(self, variable_name: str, registry_type: Registry_Type) -> float | None:  # noqa: ARG002
        self.variable_reads.append(variable_name)
        return self.variables.get(variable_name)

    def read_modbus_registers(self, ranges: Any = None, start: int = 0, end: int | None = None, batch_size: Any = None,  # noqa: ARG002
                              registry_type: Registry_Type = Registry_Type.INPUT, entries: Any = None) -> dict[int, int]:  # noqa: ARG002
        last: int = start if end is None else end
        self.reads.append((registry_type.name, start, last))
        table: dict[int, int] = self.regs[registry_type]
        return {r: table[r] for r in range(start, last + 1) if r in table}

    @property
    def request_count(self) -> int:
        return len(self.reads) + len(self.variable_reads)

    def connect_time_identification(self) -> None:
        """What modbus_base.init_after_connect() does on the first connect."""
        self.device_serial_number, self.device_metadata = eg4.identify_eg4_device(self)


class Clock:
    def __init__(self, start: float = 10_000.0) -> None:
        self.now: float = start

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(eg4.time, "monotonic", c)
    return c


# --------------------------------------------------------------------------- steady state: zero reads
def test_cycles_after_connect_make_no_modbus_requests_beyond_the_first_battery_serial_pass(clock: Clock) -> None:  # noqa: ARG001
    dev = FakeDevice()
    dev.connect_time_identification()
    assert dev.device_serial_number == SERIAL and dev.request_count > 0  # connect really did read the hardware

    first: dict[str, int | float | str] = eg4.compute_eg4_post_process_fields(dev, {})
    after_first: int = dev.request_count
    for _ in range(60):
        assert eg4.compute_eg4_post_process_fields(dev, {}) == first
    assert dev.request_count == after_first, "scrape cycles re-read the hardware"

    assert first["serial_number"] == SERIAL
    assert first["model"] == "18kPV" and first["hardware_kind"] == "inverter" and first["is_gridboss"] == 0
    assert first["firmware_version"] == "FAAB-2727"
    assert first["batteryserialnumber_1"] == BATTERY_SERIAL_1 and first["batteryserialnumber_2"] == BATTERY_SERIAL_2


def test_battery_serials_are_read_exactly_once_each(clock: Clock) -> None:  # noqa: ARG001
    dev = FakeDevice()
    dev.connect_time_identification()
    before: int = len(dev.reads)
    for _ in range(10):
        eg4.compute_eg4_post_process_fields(dev, {})
    assert list(dev.reads[before:]) == [("INPUT", 100, 107), ("INPUT", 110, 117)]


def test_battery_hardware_is_identified_once_and_never_re_read(clock: Clock) -> None:  # noqa: ARG001
    dev = FakeDevice(battery_entries=())
    dev.variables["cell_01_voltage"] = 3.3
    dev.connect_time_identification()
    assert dev.eg4_hardware_kind_cache == "battery" and isinstance(dev.device_metadata, eg4.EG4BatteryMetadata)
    after_connect: int = dev.request_count
    for _ in range(20):
        assert eg4.compute_eg4_post_process_fields(dev, {}) == {"hardware_kind": "battery"}
    assert dev.request_count == after_connect


def test_non_eg4_protocol_is_a_no_op() -> None:
    dev = FakeDevice(protocol="growatt_v6")
    assert eg4.compute_eg4_post_process_fields(dev, {}) == {}
    assert dev.request_count == 0


def test_transport_without_a_cache_object_gets_one() -> None:
    dev = FakeDevice()
    del dev.eg4_cache
    dev.connect_time_identification()
    eg4.compute_eg4_post_process_fields(dev, {})
    assert isinstance(dev.eg4_cache, eg4.EG4IdentificationCache)


# --------------------------------------------------------------------------- incomplete identification is retried, with back-off
def _break_serial(dev: FakeDevice) -> dict[int, int]:
    saved: dict[int, int] = {r: dev.regs[Registry_Type.HOLDING].pop(r) for r in (2, 3, 4, 5, 6)}
    dev.regs[Registry_Type.INPUT].pop(115, None)
    return saved


def test_unreadable_serial_is_retried_with_exponential_backoff_then_stops_once_found(clock: Clock) -> None:
    dev = FakeDevice(battery_entries=())
    saved: dict[int, int] = _break_serial(dev)
    dev.connect_time_identification()
    assert dev.device_serial_number == "" and not eg4._identification_complete(dev.device_metadata)

    def requests_for_one_cycle() -> int:
        before: int = dev.request_count
        eg4.compute_eg4_post_process_fields(dev, {})
        return dev.request_count - before

    assert requests_for_one_cycle() > 0  # first retry is immediate: the connect-time read may have been a one-off
    clock.now += 30
    assert requests_for_one_cycle() == 0  # inside the 60 s back-off
    clock.now += 31
    assert requests_for_one_cycle() > 0  # second retry after 60 s
    clock.now += 119
    assert requests_for_one_cycle() == 0  # now waiting 120 s
    clock.now += 2
    assert requests_for_one_cycle() > 0  # third retry
    assert eg4._retry_delay(1) == 60 and eg4._retry_delay(2) == 120 and eg4._retry_delay(3) == 240
    assert eg4._retry_delay(30) == 3600  # capped at an hour

    dev.regs[Registry_Type.HOLDING].update(saved)  # the device starts answering
    clock.now += 3600
    fields: dict[str, int | float | str] = eg4.compute_eg4_post_process_fields(dev, {})
    assert fields["serial_number"] == SERIAL
    settled: int = dev.request_count
    for _ in range(30):
        clock.now += 15
        eg4.compute_eg4_post_process_fields(dev, {})
    assert dev.request_count == settled, "kept reading after identification completed"


# --------------------------------------------------------------------------- reconnect refresh
def test_reconnect_refresh_picks_up_a_firmware_change(clock: Clock) -> None:
    dev = FakeDevice()
    dev.connect_time_identification()
    assert eg4.compute_eg4_post_process_fields(dev, {})["firmware_version"] == "FAAB-2727"

    dev.regs[Registry_Type.HOLDING][9] = 0x2800  # firmware updated while the device was away
    dev.regs[Registry_Type.HOLDING][10] = 0x0028
    assert eg4.compute_eg4_post_process_fields(dev, {})["firmware_version"] == "FAAB-2727"  # not re-read mid-session

    clock.now += 600
    eg4.refresh_eg4_identification(dev)
    fields: dict[str, int | float | str] = eg4.compute_eg4_post_process_fields(dev, {})
    assert fields["firmware_version"] == "FAAB-2828"
    assert dev.eg4_cache.battery_serials_loaded  # battery serials were re-read by that cycle


def test_reconnect_refresh_is_rate_limited_for_a_flapping_link(clock: Clock) -> None:
    dev = FakeDevice()
    dev.connect_time_identification()
    clock.now += 600
    eg4.refresh_eg4_identification(dev)
    used: int = dev.request_count
    for _ in range(5):
        clock.now += 20
        eg4.refresh_eg4_identification(dev)
    assert dev.request_count == used
    clock.now += 300
    eg4.refresh_eg4_identification(dev)
    assert dev.request_count > used


def test_reconnect_refresh_never_replaces_good_metadata_with_a_failed_read(clock: Clock) -> None:
    dev = FakeDevice()
    dev.connect_time_identification()
    good = dev.device_metadata
    _break_serial(dev)
    clock.now += 600
    eg4.refresh_eg4_identification(dev)
    assert dev.device_metadata is good and good.serial == SERIAL  # type: ignore[union-attr]


def test_reconnect_refresh_skips_batteries(clock: Clock) -> None:
    dev = FakeDevice(battery_entries=())
    dev.variables["cell_01_voltage"] = 3.3
    dev.connect_time_identification()
    used: int = dev.request_count
    clock.now += 600
    eg4.refresh_eg4_identification(dev)
    assert dev.request_count == used


# --------------------------------------------------------------------------- battery serial cache: partial failure
def test_battery_serial_pass_with_a_failed_read_keeps_what_decoded_and_retries_the_rest(clock: Clock) -> None:
    dev = FakeDevice()
    dev.connect_time_identification()
    second_block: dict[int, int] = {r: dev.regs[Registry_Type.INPUT].pop(r) for r in range(110, 118)}  # second module not answering
    first: dict[str, int | float | str] = eg4.compute_eg4_post_process_fields(dev, {})
    assert first["batteryserialnumber_1"] == BATTERY_SERIAL_1 and "batteryserialnumber_2" not in first
    assert dev.eg4_cache.battery_serials_loaded is False

    clock.now += 30
    before: int = dev.request_count
    eg4.compute_eg4_post_process_fields(dev, {})
    assert dev.request_count == before  # backing off

    dev.regs[Registry_Type.INPUT].update(second_block)
    clock.now += 61
    healed: dict[str, int | float | str] = eg4.compute_eg4_post_process_fields(dev, {})
    assert healed["batteryserialnumber_1"] == BATTERY_SERIAL_1 and healed["batteryserialnumber_2"] == BATTERY_SERIAL_2
    settled: int = dev.request_count
    for _ in range(10):
        clock.now += 15
        eg4.compute_eg4_post_process_fields(dev, {})
    assert dev.request_count == settled


def test_protocol_without_battery_serial_fields_costs_nothing(clock: Clock) -> None:  # noqa: ARG001
    dev = FakeDevice(battery_entries=())
    dev.connect_time_identification()
    after_connect: int = dev.request_count
    for _ in range(10):
        eg4.compute_eg4_post_process_fields(dev, {})
    assert dev.request_count == after_connect and dev.eg4_cache.battery_serials_loaded
