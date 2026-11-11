"""Acrel DTSD1352-C (``acrel_dtsd1352_c``) protocol map checks.

Everything is read with function 03.  Voltage, current, power and energy are raw integers that the
manual scales by the PT and CT ratio registers (0x008D, 0x008E), so those rows use ``Context``
adjustments driven by the ratio rows and the protocol's ``context_variables``.  4-byte values are
read high word first (the app default); the manual does not state the order, so it is taken from
AggSoft's DTSD1352 map (big-endian dwords) and should be confirmed on a meter.
"""

from __future__ import annotations

import logging
import math

import pytest

from classes.protocol_settings import (
    Registry_Type,
    protocol_settings,
    registry_map_entry,
)

PROTOCOL: str = "acrel_dtsd1352_c"


def _load() -> protocol_settings:
    logging.disable(logging.CRITICAL)
    try:
        return protocol_settings(PROTOCOL)
    finally:
        logging.disable(logging.NOTSET)


def _entries(protocol: protocol_settings) -> dict[str, registry_map_entry]:
    return {entry.variable_name: entry for entry in protocol.registry_map[Registry_Type.HOLDING]}


def _decode(raw: dict[str, bytes]) -> dict[str, float | int | str]:
    """Decode ``raw`` (variable name -> the register bytes at its start address) in one polling pass."""
    protocol: protocol_settings = _load()
    entries: dict[str, registry_map_entry] = _entries(protocol)
    return protocol.process_registery({entries[n].register: v for n, v in raw.items()}, [entries[n] for n in raw])


def _u16(value: int) -> bytes:
    return value.to_bytes(2, "big")


def _u32(value: int, signed: bool = False) -> bytes:
    return value.to_bytes(4, "big", signed=signed)


def _num(value: float | int | str) -> float:
    assert isinstance(value, (int, float)), value
    return float(value)


def test_json_defaults_and_shared_scale() -> None:
    protocol: protocol_settings = _load()
    assert protocol.settings.get("baud") == "9600"
    assert protocol.settings.get("byteorder", "big") == "big"
    assert protocol.settings.get("send_holding_register") == "true"
    assert protocol.settings.get("send_input_register") == "false"
    assert protocol.codes["context_variables"] == {"power_scale": "{pt_ratio}*{ct_ratio}"}


def test_no_endian_overrides_and_unique_names() -> None:
    protocol: protocol_settings = _load()
    rows: list[registry_map_entry] = protocol.registry_map[Registry_Type.HOLDING]
    assert not [r.variable_name for r in rows if "Register_Endian" in r.adjustments]
    names: list[str] = [r.variable_name for r in rows]
    assert len(names) == len(set(names))


@pytest.mark.parametrize(
    ("variable", "address"),
    [
        ("total_active_energy", 0x0000), ("forward_active_energy", 0x000A), ("reverse_active_energy", 0x0014),
        ("total_reactive_energy", 0x001E), ("reverse_reactive_energy_valley", 0x003A),
        ("voltage_l1", 0x0061), ("current_l3", 0x0066), ("frequency", 0x0077),
        ("voltage_l1_l2", 0x0078), ("pt_ratio", 0x008D), ("ct_ratio", 0x008E),
        ("forward_active_energy_l1", 0x0087), ("forward_active_energy_l3", 0x008B),
        ("power_l1", 0x0164), ("power_total", 0x016A), ("reactive_power_total", 0x0172),
        ("apparent_power_total", 0x017A), ("power_factor_l1", 0x017C), ("power_factor_total", 0x017F),
        ("max_voltage_l1", 0x0200), ("max_active_power_total", 0x021E), ("min_voltage_l1", 0x0242),
        # The manual prints 027EH for the Min apparent power of phase C; 027CH continues the pitch (total at 0280H).
        ("min_apparent_power_l3", 0x027C), ("min_apparent_power_total", 0x0280),
        ("temperature_t3", 0x2002),
        ("harmonic_u_l1_2", 0x05E3), ("harmonic_u_l2_2", 0x0601), ("harmonic_i_l1_2", 0x063D), ("harmonic_i_l3_31", 0x0696),
        ("fundamental_voltage_l1", 0x0697), ("harmonic_reactive_power_total", 0x06B2),
    ],
)
def test_register_addresses(variable: str, address: int) -> None:
    assert _entries(_load())[variable].register == address


def test_energy_is_high_word_first_and_scaled_by_pt_and_ct() -> None:
    """E = data*PT*CT*0.01 kWh, 32-bit unsigned, high word first."""
    direct: dict[str, float | int | str] = _decode({"pt_ratio": _u16(1), "ct_ratio": _u16(1), "total_active_energy": _u32(123456)})
    assert math.isclose(_num(direct["total_active_energy"]), 1234.56)
    ct_meter: dict[str, float | int | str] = _decode({"pt_ratio": _u16(1), "ct_ratio": _u16(40), "total_active_energy": _u32(123456)})
    assert math.isclose(_num(ct_meter["total_active_energy"]), 1234.56 * 40)
    word_order: dict[str, float | int | str] = _decode({"pt_ratio": _u16(1), "ct_ratio": _u16(1), "total_active_energy": bytes.fromhex("0001E240")})
    assert math.isclose(_num(word_order["total_active_energy"]), 1234.56), "registers 0x0000 = high word 0x0001, 0x0001 = low word 0xE240"


def test_voltage_and_current_use_their_own_ratio() -> None:
    out: dict[str, float | int | str] = _decode({"pt_ratio": _u16(10), "ct_ratio": _u16(40), "voltage_l1": _u16(2301), "current_l1": _u16(199)})
    assert math.isclose(_num(out["voltage_l1"]), 2301 * 0.1 * 10)            # U = data*PT*0.1
    assert math.isclose(_num(out["current_l1"]), 199 * 0.01 * 40)            # I = data*CT*0.01


def test_power_is_signed_and_scaled_by_pt_times_ct() -> None:
    export: dict[str, float | int | str] = _decode({"pt_ratio": _u16(1), "ct_ratio": _u16(40), "power_total": _u32(-2500, signed=True)})
    assert math.isclose(_num(export["power_total"]), -2.5 * 40)              # PQS = data*PT*CT*0.001, negative = export
    reactive: dict[str, float | int | str] = _decode({"pt_ratio": _u16(1), "ct_ratio": _u16(1), "reactive_power_l2": _u32(-1000, signed=True)})
    assert math.isclose(_num(reactive["reactive_power_l2"]), -1.0)


def test_power_factor_frequency_and_unbalance() -> None:
    out: dict[str, float | int | str] = _decode({"power_factor_total": _u16(0xFC4A), "frequency": _u16(4998), "voltage_unbalance": _u16(15)})
    assert math.isclose(_num(out["power_factor_total"]), -0.950)            # signed: 0xFC4A = -950
    assert math.isclose(_num(out["frequency"]), 49.98)
    assert math.isclose(_num(out["voltage_unbalance"]), 1.5)


def test_values_stay_raw_when_the_ratio_rows_were_not_read() -> None:
    out: dict[str, float | int | str] = _decode({"voltage_l1": _u16(2301)})
    assert math.isclose(_num(out["voltage_l1"]), 230.1)


def test_byte_packed_settings() -> None:
    """Register 0x003F: high byte = address, low byte = baud code (1 = 9600 ... 4 = 1200)."""
    out: dict[str, float | int | str] = _decode({"communication_address_1": bytes.fromhex("0102")})
    assert out["communication_address_1"] == 1
    out = _decode({"baud_rate_1": bytes.fromhex("0F02")})
    assert out["baud_rate_1"] == 2
    parity: dict[str, float | int | str] = _decode({"parity_1": bytes.fromhex("0200")})
    assert parity["parity_1"] == 2


def test_ratio_rows_are_read_every_cycle() -> None:
    entries: dict[str, registry_map_entry] = _entries(_load())
    for name in ("pt_ratio", "ct_ratio"):
        assert entries[name].read_interval == entries["frequency"].read_interval, name
        assert entries[name].write_mode.name == "WRITE", name


def test_optional_and_unverified_rows_are_read_disabled() -> None:
    """Harmonics (CH option), Max/Min, demand, tariff energies, temperature and the clock are not polled by default."""
    entries: dict[str, registry_map_entry] = _entries(_load())
    optional: list[str] = [
        name for name in entries
        if not name.endswith("_desc") and (
            "harmonic" in name or name.startswith(("max_", "min_", "thd_", "temperature_", "clock_", "fundamental_"))
            or name.endswith(("_sharp", "_peak", "_flat", "_valley")) or "demand" in name
        )
    ]
    assert optional
    for name in optional:
        assert entries[name].write_mode.name == "READDISABLED", name


def test_core_measurements_are_polled() -> None:
    entries: dict[str, registry_map_entry] = _entries(_load())
    for name in ("voltage_l1", "current_l1", "power_total", "reactive_power_total", "apparent_power_total", "power_factor_total",
                 "frequency", "total_active_energy", "forward_active_energy", "reverse_active_energy", "forward_active_energy_l1"):
        assert entries[name].write_mode.name == "READ", name
