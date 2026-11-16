# Description: Pylon RS485 v3.3 protocol map decoded end to end through the serial_pylon transport.
# File: test_serial_pylon_map.py
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

# pyright: reportPrivateUsage=false
"""
The frames are taken from the Pylontech "RS485 protocol, low voltage, V3.3" document:

* the request for battery 2's analog values is the document's example, byte for byte;
* the 74 Ah analog response is the document's example INFO (its own checksum field does not match its data, so
  the checksum is recomputed here);
* the charge/discharge management response is a capture from a US3000 posted to a public forum.
"""

from __future__ import annotations

from typing import Any

from test_serial_pylon import make_transport

from classes.transports.serial_pylon import serial_pylon
from pytests.conftest import approx

# INFO of the vendor's 74 Ah example: flag, command value 2, 15 cells, 5 temperatures, 0 A, 50981 mV,
# remain 1 = FFFF, user defined = 4, total 1 = FFFF, 2 cycles, remain 2 = 51.800 Ah, total 2 = 74.000 Ah
VENDOR_ANALOG_INFO: str = (
    "11020F0D450D440D450D440D450D440D3E0D450D4A0D4A0D4B0D4A0D4A0D4A0D4A"
    "050BC30BC30BC30BCD0BCD0000C725FFFF04FFFF000200CA58012110"
)


def _info(*groups: str) -> bytes:
    return bytes.fromhex("".join(groups))


def _analog(cells: list[int], temperatures: list[int], current: int, voltage: int, remaining: int, total: int,
            cycles: int, *, large: bool) -> bytes:
    """An analog response INFO: a battery up to 65 Ah (user defined = 2) or over it (user defined = 4)."""
    out = bytearray([0x11, 0x02, len(cells)])
    for cell in cells:
        out += cell.to_bytes(2, "big")
    out.append(len(temperatures))
    for temperature in temperatures:
        out += temperature.to_bytes(2, "big")
    out += current.to_bytes(2, "big", signed=True) + voltage.to_bytes(2, "big")
    if large:
        out += b"\xff\xff" + bytes([4]) + b"\xff\xff" + cycles.to_bytes(2, "big") + remaining.to_bytes(3, "big") + total.to_bytes(3, "big")
    else:
        out += remaining.to_bytes(2, "big") + bytes([2]) + total.to_bytes(2, "big") + cycles.to_bytes(2, "big")
    return bytes(out)


def _transport(responses: dict[int, bytes], **settings: Any) -> tuple[serial_pylon, list[tuple[int, bytes]]]:
    """A serial_pylon on the real v3.3 map whose battery answers each command with the given INFO."""
    transport: serial_pylon = make_transport(protocol_version="pylon_rs485_v3.3", **settings)
    sent: list[tuple[int, bytes]] = []

    def exchange(command: int, info: bytes = b"") -> bytes | None:
        sent.append((command, info))
        if command not in responses:
            return None
        return transport.build_frame(command, responses[command])

    transport._exchange = exchange  # type: ignore[method-assign]
    return transport, sent


# --- requests --------------------------------------------------------------------------------------------------


def test_the_analog_request_is_the_vendor_example_byte_for_byte() -> None:
    """7E 32 30 30 32 34 36 34 32 45 30 30 32 30 32 46 44 33 33 0D from the document, without SOI / EOI.

    LENGTH is E002 because LENID counts the ASCII characters of INFO (two for one byte).
    """
    transport = make_transport(address=2)

    assert transport.build_frame(0x4642, b"\x02") == b"20024642E00202FD33"


def test_commands_that_take_a_battery_number_send_it_as_info() -> None:
    transport, sent = _transport({}, address=5)
    transport.protocolSettings = None  # only the request side is being checked

    assert [transport._request_info(command) for command in (0x4642, 0x4644, 0x4692, 0x4693, 0x4696)] == [b"\x05"] * 5
    assert [transport._request_info(command) for command in (0x464F, 0x4651, 0x4647)] == [b""] * 3
    assert sent == []


def test_command_info_can_ask_for_the_system_totals() -> None:
    """INFO = 0xFF requests the whole stack (spec 2.5.3): analog and alarm only."""
    assert make_transport(address=2, command_info="ff").command_info == 0xFF
    assert make_transport(address=2, command_info="0x22").command_info == 0x22
    assert make_transport(address=7).command_info == 7


# --- the analog response ---------------------------------------------------------------------------------------


def _decoded(responses: dict[int, bytes], **settings: Any) -> dict[str, Any]:
    transport, _ = _transport(responses, **settings)
    return transport.read_data()


def test_the_vendor_74ah_example_decodes_to_the_documents_stated_values() -> None:
    info: dict[str, Any] = _decoded({0x4642: bytes.fromhex(VENDOR_ANALOG_INFO)})

    assert info["number_of_cells"] == 15
    assert info["number_of_temperatures"] == 5
    expected_voltages: dict[int, float] = {1: 3.397, 2: 3.396, 7: 3.39, 15: 3.402}
    for cell, expected in expected_voltages.items():
        assert info[f"cell_{cell}_voltage"] == approx(expected)
    assert info["bms_temperature"] == approx(28.0)  # 3011 * 0.1 K - 273.1
    assert info["cell_temperature_13_15"] == approx(29.0)
    assert info["current"] == 0
    assert info["module_voltage"] == approx(50.981)
    assert info["remaining_capacity"] == approx(51.8)
    assert info["total_capacity"] == approx(74.0)
    assert info["cycle_count"] == 2
    assert info["state_of_charge"] == approx(70.0)
    assert "cell_16_voltage" not in info  # this battery has 15 cells
    assert "mosfet_temperature" not in info  # and 5 temperature sensors


def test_a_16_cell_battery_does_not_shift_the_other_values() -> None:
    cells: list[int] = [3300 + i for i in range(16)]
    info: dict[str, Any] = _decoded({0x4642: _analog(cells, [2981, 2991, 3001, 3011, 3021, 3031], -400, 52400, 20000, 50000, 11, large=True)})

    assert info["number_of_cells"] == 16
    assert info["cell_16_voltage"] == approx(3.315)
    assert info["mosfet_temperature"] == approx(30.0)
    assert info["current"] == approx(-40.0)  # -400 * 0.1 A; negative is discharge
    assert info["module_voltage"] == approx(52.4)
    assert info["remaining_capacity"] == approx(20.0)
    assert info["state_of_charge"] == approx(40.0)
    assert info["cycle_count"] == 11


def test_a_battery_up_to_65ah_uses_the_two_byte_capacities() -> None:
    info: dict[str, Any] = _decoded({0x4642: _analog([3300] * 15, [2981] * 5, 250, 51000, 30000, 50000, 5, large=False)})

    assert info["remaining_capacity"] == approx(30.0)
    assert info["total_capacity"] == approx(50.0)
    assert info["state_of_charge"] == approx(60.0)
    assert info["current"] == approx(25.0)  # positive is charge


def test_the_system_request_reports_the_battery_count_as_the_command_value() -> None:
    payload = bytearray(_analog([3300] * 15, [2981] * 5, 0, 51000, 1000, 2000, 1, large=False))
    payload[1] = 4  # command 0xFF: "the amount of Pack" (V2.8 3.5 note)

    assert _decoded({0x4642: bytes(payload)})["battery_number"] == 4


def test_a_malformed_analog_response_is_discarded_with_a_warning() -> None:
    transport, _ = _transport({0x4642: bytes.fromhex("11020F0D45")})  # claims 15 cells, carries one

    info: dict[str, int | float | str] = transport.read_data()

    assert "number_of_cells" not in info
    assert "malformed" in str(transport._log.warning.call_args)  # type: ignore[attr-defined]


# --- other commands --------------------------------------------------------------------------------------------


def test_charge_discharge_management_from_a_real_us3000_capture() -> None:
    """INFO 03 D002 B798 0025 FE8E C0: 53.250 V, 47.000 V, 3.7 A, -37.0 A, charge and discharge enabled."""
    info: dict[str, Any] = _decoded({0x4692: _info("03", "D002", "B798", "0025", "FE8E", "C0")})

    assert info["charge_voltage_limit"] == approx(53.25)
    assert info["discharge_voltage_limit"] == approx(47.0)
    assert info["charge_current_limit"] == approx(3.7)
    assert info["discharge_current_limit"] == approx(-37.0)
    assert info["charge_discharge_status"] == "Discharge enable,Charge enable"


def test_the_full_charge_request_bit() -> None:
    info: dict[str, Any] = _decoded({0x4692: _info("02", "D002", "B798", "0025", "FE8E", "88")})  # charge enable + full charge request

    assert info["charge_discharge_status"] == "Full charge request,Charge enable"


def test_system_parameters_skip_the_info_flag() -> None:
    words: list[int] = [3650, 3500, 2900, 3131, 2731, 370, 54750, 47000, 43500, 3231, 2531, -370 & 0xFFFF]
    info: dict[str, Any] = _decoded({0x4647: bytes([0x11]) + b"".join(word.to_bytes(2, "big") for word in words)})

    assert info["cell_high_voltage_limit"] == approx(3.65)
    assert info["charge_high_temperature_limit"] == approx(40.0)  # 3131 -> 313.1 K - 273.1
    assert info["charge_low_temperature_limit"] == approx(0.0)
    assert info["system_charge_current_limit"] == approx(37.0)
    assert info["module_high_voltage_limit"] == approx(54.75)
    assert info["discharge_low_temperature_limit"] == approx(-20.0)
    assert info["system_discharge_current_limit"] == approx(-37.0)


def test_alarm_response_states_and_status_bits() -> None:
    cells = bytes([0] * 14 + [2])  # cell 15 above the higher limit
    temps = bytes([0, 0, 1, 0, 0])
    status = bytes([0b00010001, 0b00000110, 0b11001000, 0b00000010, 0b00000000])
    payload: bytes = bytes([0x11, 0x02, 15]) + cells + bytes([5]) + temps + bytes([0, 2, 0]) + status

    info: dict[str, Any] = _decoded({0x4644: payload})

    assert info["cell_15_alarm_state"] == 2
    assert info["cell_15_alarm_state_desc"] == "above higher limit"
    assert info["cell_temperature_5_8_alarm_state_desc"] == "below lower limit"
    assert info["module_voltage_alarm_desc"] == "above higher limit"
    assert info["alarm_status_1"] == "Module over voltage,Discharge over current"
    assert info["alarm_status_2"] == "Charge MOSFET on,Discharge MOSFET on"
    assert info["alarm_status_3"] == "Fully charged,Effective discharge current,Effective charge current"
    assert info["alarm_status_4"] == "Cell 2 voltage error"


def test_manufacturer_serial_number_and_software_version() -> None:
    manufacturer: bytes = b"US3000C   " + bytes([0x12, 0x34]) + b"PYLONTECH           "
    info: dict[str, Any] = _decoded({
        0x4651: manufacturer,
        0x4693: bytes([2]) + b"PPTBH02345678901",
        0x4696: bytes([2]) + bytes.fromhex("1234") + bytes.fromhex("0A0B0C"),
    })

    assert info["battery_name"].strip() == "US3000C"
    assert info["manufacturer_name"].strip() == "PYLONTECH"
    assert info["serial_number"] == "PPTBH02345678901"


def test_a_battery_that_does_not_answer_one_command_still_reports_the_rest() -> None:
    info: dict[str, Any] = _decoded({0x4692: _info("03", "D002", "B798", "0025", "FE8E", "C0")})

    assert "charge_voltage_limit" in info
    assert "number_of_cells" not in info


def test_each_command_is_sent_once_per_poll() -> None:
    """Analog and alarm responses feed several blocks; the battery is not asked again for each of them."""
    transport, sent = _transport({0x4642: bytes.fromhex(VENDOR_ANALOG_INFO)})

    transport.read_data()

    assert [command for command, _ in sent].count(0x4642) == 1
    assert 0x46F1 not in [command for command, _ in sent]  # pseudo commands never go on the wire
    assert (0x4642, b"\x02") in sent  # with the battery number as INFO
