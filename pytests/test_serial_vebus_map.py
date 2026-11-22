# Description: Tests that the victron_mk3usb_vebus protocol map decodes what serial_vebus delivers, using the vendor documents' worked examples.
# File: test_serial_vebus_map.py
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

"""The victron_mk3usb_vebus map against the transport's payloads, through the real decoder.

Expected values come from Victron's MK2 protocol document, version 3.14: the DC, AC L1 and MasterMultiLED frames
and the scale answers are the Appendix 2 trace, whose printed results (26.4 V, 0.0 A, 50.0 Hz; 213.0 V, 0.7 A,
213.0 V, 0.4 A, 50.1 Hz; limit 50.0 A of 6.2 A to 100.0 A) are the vendor's own analysis. Values with no example in
the document (state, LED, RAM variables, settings, flags) are built by hand from the layouts the document states.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

from test_serial_vebus import (
    VENDOR_MASTER_MULTI_LED_FRAME,
    FakeDevice,
    FakeInterface,
    isolated_registries,
    log_of,
    make_transport,
    ve_bus,
)

from classes.transports.serial_base import serial_base
from classes.transports.serial_vebus import serial_vebus
from pytests.conftest import approx

_ISOLATION: object = isolated_registries
''' Referenced on purpose: the autouse fixture only applies in a module whose namespace has it, and an import nothing
uses gets removed by linters. Without it the tests share one cached serial client and see each other's simulated device. '''


def test_the_serial_client_registry_is_isolated_for_this_module() -> None:
    """Guard: fails loudly if the autouse isolation fixture stops applying here (see _ISOLATION above)."""
    assert serial_base.clients == {}, "a cached serial client from another test is visible: the isolated_registries fixture is not active in this module"

PROTOCOL_DIR: Path = Path(__file__).resolve().parent.parent / "protocols" / "victron"
MAP_FILE: Path = PROTOCOL_DIR / "victron_mk3usb_vebus.holding_registry_map.csv"
JSON_FILE: Path = PROTOCOL_DIR / "victron_mk3usb_vebus.json"


def read_cycle(device: FakeDevice | None = None, **interface_settings: Any) -> tuple[dict[str, Any], serial_vebus, FakeInterface]:
    interface = FakeInterface({0: device if device is not None else FakeDevice()})
    for key, value in interface_settings.items():
        setattr(interface, key, value)
    transport = make_transport(interface, protocol_version="victron_mk3usb_vebus")
    return transport.read_data(), transport, interface


def full_device() -> FakeDevice:
    return FakeDevice(
        ram={0: 21303, 1: 73, 2: 21303, 3: 39, 4: 2641, 5: 0x10000 - 1234, 6: 12, 7: 0x88, 8: 0xC3, 9: 5, 10: 0b1000, 11: 0, 12: 0b100000, 13: 20000},
        setting_info={0: (1, 0), 1: (1, 0), 2: (0xFF9C, 0), 7: (1, 0), 14: (1, 0), 60: (1, 0), 64: (1, 0)},
        settings={0: 0b100100, 1: 0x0801, 2: 5640, 7: 7, 14: 1, 60: 5, 64: 200},
        state=(9, 2),
    )


# --------------------------------------------------------------------------- #
# the map file                                                                  #
# --------------------------------------------------------------------------- #

def rows() -> list[dict[str, str]]:
    with MAP_FILE.open(newline="") as handle:
        return list(csv.DictReader(handle))


def test_the_map_is_a_serial_vebus_protocol() -> None:
    assert json.loads(JSON_FILE.read_text())["transport"] == "serial_vebus"
    assert json.loads(JSON_FILE.read_text())["baud"] == 2400  # 3.1: MK2.2, MK2 USB and MK3 USB
    assert {row["writable"] for row in rows()} <= {"R", "W"}


def test_only_the_intended_rows_are_writable() -> None:
    """The map is the allow list of what the transport may change: the panel rows and five settings, nothing else."""
    assert {row["variable_name"] for row in rows() if row["writable"] == "W"} == {
        "switch_state", "input_current_limit_control",
        "setting_absorption_voltage", "setting_float_voltage", "setting_bulk_current",
        "setting_input_current_limit_ac1", "setting_input_current_limit_ac2",
    }


def test_no_flag_word_and_no_ram_variable_is_writable() -> None:
    for row in rows():
        if row["writable"] == "W":
            assert not row["register"].startswith("x30"), "RAM variables are not writable"
            assert row["data_type"] not in ("8BIT_FLAGS", "16BIT_FLAGS")


def test_variable_names_are_valid_and_unique() -> None:
    names = [row["variable_name"] for row in rows()]
    assert all(re.fullmatch(r"[a-z0-9_]+", name) for name in names)
    assert len(names) == len(set(names))


def test_no_invented_names_remain() -> None:
    """The placeholder's F_AC_IN_V_L1 style names are not in the vendor document."""
    text = MAP_FILE.read_text()
    assert "F_AC_" not in text and "F_DC_" not in text


def test_every_register_is_a_command_the_transport_knows() -> None:
    """A full cycle must not log a single 'no command' warning."""
    _, transport, _ = read_cycle(full_device())
    assert not [call for call in log_of(transport).warning.call_args_list if "has no command" in str(call)]


def test_every_enum_and_flag_table_the_rows_need_is_in_the_json() -> None:
    tables = json.loads(JSON_FILE.read_text())
    needed = {row["documented_name"].strip().lower().replace(" ", "_") + "_codes" for row in rows() if row["data_type"] == "8BIT_FLAGS" or row["data_type"] == "16BIT_FLAGS"}
    needed |= {"interface_mode_codes", "device_state_codes", "charge_sub_state_codes", "phase_info_codes", "special_three_phase_setting_codes"}
    assert needed <= set(tables), needed - set(tables)


# --------------------------------------------------------------------------- #
# the vendor's Appendix 2 trace through the real map                           #
# --------------------------------------------------------------------------- #

def test_the_vendor_trace_decodes_to_the_vendors_printed_values() -> None:
    interface_frames = {5: [VENDOR_MASTER_MULTI_LED_FRAME]}
    data, _, _ = read_cycle(full_device(), system_frames={0: FakeInterface().system_frames[0], 1: FakeInterface().system_frames[1], **interface_frames})

    assert data["dc_voltage"] == approx(26.4, abs=0.05)
    assert data["dc_current"] == approx(0.0, abs=0.05)
    assert data["ac_out_frequency"] == approx(50.0, abs=0.05)
    assert data["ac_in_voltage_l1"] == approx(213.0, abs=0.05)
    assert data["ac_in_current_l1"] == approx(0.7, abs=0.05)
    assert data["ac_out_voltage_l1"] == approx(213.0, abs=0.05)
    assert data["ac_out_current_l1"] == approx(0.4, abs=0.05)
    assert data["ac_in_frequency_l1"] == approx(50.1, abs=0.05)

    assert data["last_active_ac_input"] == 0
    assert data["input_current_limit"] == approx(50.0)
    assert data["input_current_limit_minimum"] == approx(6.2)
    assert data["input_current_limit_maximum"] == approx(100.0)


def test_the_interface_version_of_the_vendor_trace() -> None:
    data, _, _ = read_cycle(full_device())
    assert data["mk2_interface_version"] == 1130131


def test_a_one_phase_system_does_not_publish_l2_and_l3() -> None:
    data, _, interface = read_cycle(full_device())
    assert not [name for name in data if name.endswith(("_l2", "_l3"))]
    assert "F 02" not in interface.commands()


# --------------------------------------------------------------------------- #
# hand-built values                                                             #
# --------------------------------------------------------------------------- #

def test_ram_variables_come_out_in_their_units() -> None:
    data, _, _ = read_cycle(full_device())
    assert data["device_battery_voltage"] == approx(26.41)
    assert data["device_battery_current"] == approx(-12.34)
    assert data["device_mains_voltage"] == approx(213.03)
    assert data["device_mains_frequency"] == approx(50.1, abs=0.05)
    assert data["device_inverter_frequency"] == approx(50.0, abs=0.05)
    assert data["device_battery_ripple_voltage"] == approx(0.12)
    assert data["device_ac_load_current"] == approx(0.05)
    assert data["state_of_charge"] == approx(100.0)  # raw 20000 x 0.005 (unit inferred, see the map note)


def test_bit_variables_are_bits() -> None:
    data, _, _ = read_cycle(full_device())
    assert (data["virtual_switch_position"], data["ignore_ac_input_state"], data["multi_functional_relay_state"]) == (1, 0, 1)


def test_device_state_and_charge_sub_state_have_descriptions() -> None:
    data, _, _ = read_cycle(full_device())
    assert (data["device_state"], data["device_state_desc"]) == (9, "Charge")
    assert (data["charge_sub_state"], data["charge_sub_state_desc"]) == (2, "Charge absorption")


def test_the_sub_state_says_not_charging_outside_the_charge_state() -> None:
    device = full_device()
    device.state = (4, 0)
    data, _, _ = read_cycle(device)
    assert data["device_state_desc"] == "Invert full"
    assert data["charge_sub_state_desc"] == "Not charging"


def test_led_status_names_the_lit_and_blinking_leds() -> None:
    data, _, _ = read_cycle(full_device(), led=(0b00010001, 0b00000001))
    assert data["led_on"] == "Mains,Inverter"
    assert data["led_blink"] == "Mains"


def test_the_software_version_is_the_decimal_number() -> None:
    assert read_cycle(full_device())[0]["software_version"] == 2612123


def test_settings_are_scaled_and_flags_are_named() -> None:
    data, _, _ = read_cycle(full_device())
    assert data["setting_absorption_voltage"] == approx(56.4)
    assert data["setting_repeated_absorption_time"] == approx(7)
    assert data["setting_special_three_phase_desc"] == "Split phase 180"
    assert data["setting_flags0"] == "60 Hz,Assist enabled"
    assert data["setting_flags1"] == "VS on bulk protection,Accept wide input frequency"
    assert data["setting_flags2"] == "VS2 off when AC1 available,VS set inverter period time"


def test_unsupported_values_are_simply_absent() -> None:
    device = full_device()
    del device.ram_info[13]
    data, transport, _ = read_cycle(device)
    assert "state_of_charge" not in data and "device_battery_voltage" in data
    assert transport.get_cycle_result().is_complete


def test_power_variables_14_to_19_are_published_when_the_device_has_them() -> None:
    device = full_device()
    device.ram_info[16] = (0x8064, 0)  # signed, 0.01
    device.ram[16] = 0x10000 - 250
    data, _, _ = read_cycle(device)
    assert data["device_output_power"] == approx(-2.5)
    assert "device_inverter_power_14" not in data


def test_the_ac_state_byte_and_the_phase_info_are_described() -> None:
    interface_frames: dict[int, list[bytes]] = {}
    # 3.14 layout: data byte 3 is <State> (3 = slave); phase info 0x0A = L1 of a 3 phase system
    interface_frames[1] = [ve_bus("20" "0101" "8103" "0A" "3753" "4900" "3753" "2700" "C3")]
    interface_frames[2] = [ve_bus("20" "0101" "8103" "07" "3853" "4A00" "3853" "2800" "C3")]
    interface_frames[3] = [ve_bus("20" "0101" "8103" "06" "3953" "4B00" "3953" "2900" "C3")]
    data, _, _ = read_cycle(full_device(), system_frames={0: FakeInterface().system_frames[0], **interface_frames})
    assert data["ac_l1_phase_info_desc"] == "L1, 3 phases in the system"
    assert data["ac_l2_phase_info_desc"] == "L2" and data["ac_l3_phase_info_desc"] == "L3"
    assert data["ac_l1_state"] == 3 and data["ac_l1_state_desc"] == "Device in slave mode"
    assert data["ac_in_voltage_l3"] == approx(213.05, abs=0.01)


def test_the_switch_register_added_in_3_14_is_published_when_sent() -> None:
    frame = ve_bus("41" "10090000" "00" "3E00" "E803" "F401" "14")  # 12th data byte: bits 2 and 4
    data, _, _ = read_cycle(full_device(), system_frames={**FakeInterface().system_frames, 5: [frame]})
    assert data["switch_register"] == "Front switch up,Switch charge (current state)"


def test_the_ac_input_count_and_remote_panel_bits_decode() -> None:
    frame = ve_bus("41" "10090000" "A4" "3E00" "E803" "F401" "00")  # config 0b10100100: overridden, 2 inputs, remote panel
    data, _, _ = read_cycle(full_device(), system_frames={**FakeInterface().system_frames, 5: [frame]})
    assert data["input_current_overridden_by_panel"] == 1
    assert data["ac_input_count"] == 2
    assert data["remote_panel_detected"] == 1
    assert data["last_active_ac_input"] == 0
