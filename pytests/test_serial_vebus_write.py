# Description: Tests for the serial_vebus write path: the safety layers, the S command and setting writes, against a simulated MK2 interface.
# File: test_serial_vebus_write.py
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

"""The write path of serial_vebus (Victron MK2 protocol 3.14).

* The 'S' frame for "on, 31.5 A" is the document's own Appendix 1 example (07 FF 'S' 03 3B 01 01 80 E7), byte for byte.
* The setting write is the two-frame sequence of 7.3.6 and 7.3.7 (0x33 without a response, then 0x34 answered 0x88).
* The device behavior (clamping, ignoring a panel, answering "unknown command") is simulated from what the document
  says; where it says nothing, for instance how long a Multi takes to act on 'S', the simulation is the assumption.

Every safety layer named at the top of serial_vebus.py has a test that shows it refusing, and that nothing was sent.
"""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import MagicMock

import pytest
import serial
from test_serial_vebus import (
    FakeDevice,
    FakeInterface,
    PanelModel,
    isolated_registries,
    log_of,
    make_transport,
)

from classes.protocol_settings import registry_map_entry
from classes.transports.serial_base import serial_base
from classes.transports.serial_vebus import serial_vebus
from pytests.conftest import approx

_ISOLATION: object = isolated_registries
''' Referenced on purpose: the autouse fixture only applies in a module whose namespace has it, and an import nothing
uses gets removed by linters. Without it the tests share one cached serial client and see each other's simulated device. '''


def test_the_serial_client_registry_is_isolated_for_this_module() -> None:
    """Guard: fails loudly if the autouse isolation fixture stops applying here (see _ISOLATION above)."""
    assert serial_base.clients == {}, "a cached serial client from another test is visible: the isolated_registries fixture is not active in this module"

SETTINGS_INFO: dict[int, tuple[int, int]] = {2: (0xFF9C, 0), 3: (0xFF9C, 0), 4: (0xFFF6, 0), 6: (0xFFF6, 0), 49: (0xFFF6, 0)}
''' Sc = -100 (scale 0.01) for the voltages, -10 (scale 0.1) for the currents (7.3.8) '''


def write_device() -> FakeDevice:
    return FakeDevice(
        ram={4: 2641},
        setting_info=dict(SETTINGS_INFO),
        settings={2: 5640, 3: 5520, 4: 500, 6: 160, 49: 300},
        setting_limits={2: (4000, 6000), 3: (4000, 6000), 4: (0, 1000), 6: (0, 1000), 49: (0, 1000)},
    )


def writer(interface: FakeInterface, *, connect: bool = True, panel: bool = True, settings: bool = True, **values: Any) -> serial_vebus:
    """A transport with writes requested; connect() runs the handshake and enable_write, as the gateway does."""
    merged: dict[str, Any] = {
        "protocol_version": "victron_mk3usb_vebus", "write_enabled": True, "allow_panel_writes": panel,
        "allow_setting_writes": settings, "write_min_interval": 0, **values,
    }
    transport: serial_vebus = make_transport(interface, **merged)
    transport._verify_delay = 0.0
    if connect:
        transport.connect()
    return transport


def logged(transport: serial_vebus, level: str) -> str:
    return "\n".join(str(call.args[0]) for call in getattr(log_of(transport), level).call_args_list)


def sent(interface: FakeInterface, command: str) -> list[str]:
    return [line for line in interface.commands() if line.startswith(command)]


def panel_interface(**panel_values: Any) -> FakeInterface:
    interface = FakeInterface({0: write_device()})
    interface.panel = PanelModel(**panel_values)
    return interface


# --------------------------------------------------------------------------- #
# layers 1-4: write_enabled, opt-ins, the allow list, validation at connect    #
# --------------------------------------------------------------------------- #

def test_nothing_is_written_unless_the_section_asks_for_writes() -> None:
    interface: FakeInterface = panel_interface(state=4)
    transport: serial_vebus = make_transport(interface, protocol_version="victron_mk3usb_vebus", allow_panel_writes=True, allow_setting_writes=True)
    transport.connect()
    transport.write_data({"switch_state": 3, "setting_absorption_voltage": 57.0}, MagicMock())
    assert transport.write_enabled is False
    assert not sent(interface, "S") and not sent(interface, "W 33") and not sent(interface, "W 34")


def test_write_enabled_alone_enables_nothing_without_an_opt_in() -> None:
    interface: FakeInterface = panel_interface()
    transport: serial_vebus = writer(interface, panel=False, settings=False)
    assert transport.write_enabled is False
    assert "allow_panel_writes" in logged(transport, "error")


def test_validation_passes_when_every_probe_passes() -> None:
    transport: serial_vebus = writer(panel_interface())
    assert transport.write_enabled is True
    assert "validation passed" in logged(transport, "info")


def test_a_failed_probe_below_ninety_percent_disables_writes() -> None:
    interface: FakeInterface = panel_interface()
    del interface.devices[0].setting_info[3]  # one of the six probes fails: 83 %
    transport: serial_vebus = writer(interface)
    assert transport.write_enabled is False
    assert "WRITE DISABLED" in logged(transport, "error")


def test_relaxed_mode_accepts_a_failed_validation_but_still_checks_every_write() -> None:
    interface: FakeInterface = panel_interface()
    del interface.devices[0].setting_info[3]
    transport: serial_vebus = writer(interface, write_type="relaxed")
    assert transport.write_enabled is True
    assert "RELAXED" in logged(transport, "warning")

    transport.write_data({"setting_absorption_voltage": 99.0}, MagicMock())  # still outside the device's limits
    assert not sent(interface, "W 33")


def test_unsafe_mode_skips_the_probes() -> None:
    interface: FakeInterface = panel_interface()
    transport: serial_vebus = writer(interface, write_type="unsafe")
    assert transport.write_enabled is True
    assert interface.commands() == ["A 01 00", "W 05 00 00", "W 06 00 00"], "nothing but the handshake went out"


def test_a_section_that_failed_validation_is_validated_again_on_the_next_connect() -> None:
    interface: FakeInterface = panel_interface()
    saved: tuple[int, int] = interface.devices[0].setting_info.pop(3)
    transport: serial_vebus = writer(interface)
    assert transport.write_enabled is False

    interface.devices[0].setting_info[3] = saved  # the device is fixed (or has finished starting)
    transport.connected = False
    transport.connect()
    assert transport.write_enabled is True


def test_panel_only_does_not_probe_the_settings() -> None:
    interface: FakeInterface = panel_interface()
    del interface.devices[0].setting_info[3]  # would fail a settings probe
    assert writer(interface, settings=False).write_enabled is True


def test_settings_only_does_not_probe_the_panel() -> None:
    assert writer(panel_interface(switch_register=False), panel=False).write_enabled is True


def test_panel_only_needs_the_switch_register_to_validate() -> None:
    assert writer(panel_interface(switch_register=False), settings=False).write_enabled is False


def test_a_map_without_writable_rows_disables_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_writable_entries(self: serial_vebus) -> list[registry_map_entry]:
        return []

    monkeypatch.setattr(serial_vebus, "_writable_entries", _no_writable_entries)
    transport: serial_vebus = writer(panel_interface())
    assert transport.write_enabled is False
    assert "no registry rows are marked W" in logged(transport, "error")


def test_rows_not_marked_w_are_never_written() -> None:
    interface: FakeInterface = panel_interface()
    transport: serial_vebus = writer(interface)
    transport.write_data({"setting_flags0": 7, "device_battery_voltage": 1.0, "setting_special_three_phase": 2, "setting_repeated_absorption_time": 5}, MagicMock())
    assert not sent(interface, "W 33") and not sent(interface, "S")


# --------------------------------------------------------------------------- #
# the panel: 'S' (5)                                                            #
# --------------------------------------------------------------------------- #

def test_the_s_frame_for_on_31_5_amps_is_the_vendor_example() -> None:
    interface: FakeInterface = panel_interface(state=4, limit=500)
    transport: serial_vebus = writer(interface, settings=False)
    transport.write_data({"switch_state": 3, "input_current_limit_control": 31.5}, MagicMock())
    assert bytes.fromhex("07FF53033B010180E7") in interface.written, "the document's own example frame"
    assert interface.panel is not None and (interface.panel.state, interface.panel.limit) == (3, 315)


def test_a_batch_with_both_rows_is_one_s_command() -> None:
    interface: FakeInterface = panel_interface(state=4)
    writer(interface, settings=False).write_data({"switch_state": "On", "input_current_limit_control": 16.0}, MagicMock())
    assert len(sent(interface, "S")) == 1


def test_a_switch_write_re_sends_the_current_limit_instead_of_changing_it() -> None:
    interface: FakeInterface = panel_interface(state=3, limit=500)
    writer(interface, settings=False).write_data({"switch_state": "Off"}, MagicMock())
    assert sent(interface, "S") == ["S 04 f4 01 01 80"], "state 4, the current 50.0 A (0x01F4) pinned"
    assert interface.panel is not None and interface.panel.limit == 500


def test_a_limit_write_keeps_the_current_switch_state() -> None:
    interface: FakeInterface = panel_interface(state=2, limit=500)
    writer(interface, settings=False).write_data({"input_current_limit_control": 16.0}, MagicMock())
    assert sent(interface, "S") == ["S 02 a0 00 01 80"], "inverter only stays inverter only; 16.0 A is 0x00A0"


@pytest.mark.parametrize(
    ("values", "panel", "expected"),
    [
        ({"input_current_limit_control": 0}, {}, "invert or bypass"),
        ({"input_current_limit_control": 5.0}, {}, "outside the 6.2 A to 100.0 A"),
        ({"input_current_limit_control": 120.0}, {}, "outside the 6.2 A to 100.0 A"),
        ({"input_current_limit_control": -3.0}, {}, "not a usable"),
        ({"input_current_limit_control": 7000.0}, {}, "beyond what the protocol"),
        ({"input_current_limit_control": "lots"}, {}, "not a current"),
        ({"switch_state": 9}, {}, "is not 1"),
        ({"switch_state": "sideways"}, {}, "not a switch state"),
        ({"input_current_limit_control": 16.0}, {"overridable": False}, "ignores a panel input current limit"),
        ({"input_current_limit_control": 16.0}, {"dmc_dedicated": True}, "DMC"),
        ({"input_current_limit_control": 16.0}, {"switch_register": False}, "cannot be written alone"),
        ({"switch_state": 4}, {"switch_register": False}, "could not be confirmed"),
    ],
)
def test_the_panel_refusals_send_nothing(values: dict[str, Any], panel: dict[str, Any], expected: str) -> None:
    interface: FakeInterface = panel_interface(**panel)
    transport: serial_vebus = writer(interface, settings=False)
    transport.write_enabled = True  # the refusals under test come from the per-write checks, not from the connect probes
    transport.write_data(values, MagicMock())
    assert not sent(interface, "S"), "nothing may be sent"
    assert expected in logged(transport, "error")


def test_without_the_switch_register_a_relaxed_section_may_change_the_switch_but_not_confirm_it() -> None:
    interface: FakeInterface = panel_interface(switch_register=False)
    transport: serial_vebus = writer(interface, settings=False, write_type="relaxed")
    transport.write_data({"switch_state": 4}, MagicMock())
    assert len(sent(interface, "S")) == 1
    assert "not confirmed" not in logged(transport, "warning"), "there is nothing to compare, so nothing is claimed either way"


def test_panel_writes_need_their_own_opt_in() -> None:
    interface: FakeInterface = panel_interface(state=4)
    transport: serial_vebus = writer(interface, panel=False)
    transport.write_enabled = True
    transport.write_data({"switch_state": 3}, MagicMock())
    assert not sent(interface, "S")
    assert "allow_panel_writes" in logged(transport, "error")


def test_writing_what_is_already_there_sends_nothing() -> None:
    interface: FakeInterface = panel_interface(state=3, limit=500)
    writer(interface, settings=False).write_data({"switch_state": 3, "input_current_limit_control": 50.0}, MagicMock())
    assert not sent(interface, "S")


def test_a_second_write_inside_the_minimum_interval_is_refused() -> None:
    interface: FakeInterface = panel_interface(state=4)
    transport: serial_vebus = writer(interface, settings=False, write_min_interval=60)
    transport.write_data({"switch_state": 3}, MagicMock())
    transport.write_data({"switch_state": 4}, MagicMock())
    assert len(sent(interface, "S")) == 1
    assert "write_min_interval" in logged(transport, "error")


def test_unsafe_mode_writes_what_the_other_modes_refuse() -> None:
    interface: FakeInterface = panel_interface(overridable=False)
    transport: serial_vebus = writer(interface, settings=False, write_type="unsafe")
    transport.write_data({"input_current_limit_control": 0}, MagicMock())
    assert len(sent(interface, "S")) == 1, "limit 0 and a Multi that ignores panels are not stopped in unsafe mode"


def test_a_confirmed_panel_write_says_so() -> None:
    transport: serial_vebus = writer(panel_interface(state=4), settings=False)
    transport.write_data({"switch_state": 3}, MagicMock())
    assert "confirmed by the MasterMultiLED" in logged(transport, "info")


def test_a_panel_write_the_device_ignores_is_reported_as_not_confirmed() -> None:
    interface: FakeInterface = panel_interface(state=4, accept=False)
    transport: serial_vebus = writer(interface, settings=False)
    transport.write_data({"switch_state": 3}, MagicMock())
    assert "not confirmed" in logged(transport, "warning")


def test_no_answer_to_s_is_an_error_not_an_exception() -> None:
    interface: FakeInterface = panel_interface(state=4)
    transport: serial_vebus = writer(interface, settings=False)
    original = interface.respond
    interface.respond = lambda frame: [] if frame[2] == ord("S") else original(frame)  # type: ignore[method-assign]
    transport.write_data({"switch_state": 3}, MagicMock())
    assert "no answer to the 'S' command" in logged(transport, "error")


def test_the_panel_rows_read_back_the_state() -> None:
    interface: FakeInterface = panel_interface(state=2, limit=160)
    transport: serial_vebus = writer(interface, settings=False)
    data: dict[str, int | float | str] = transport.read_data()
    assert (data["switch_state"], data["switch_state_desc"], data["input_current_limit_control"]) == (2, "Inverter only", 16.0)


# --------------------------------------------------------------------------- #
# settings: 0x33 then 0x34 (7.3.6, 7.3.7)                                       #
# --------------------------------------------------------------------------- #

def settings_interface() -> FakeInterface:
    interface = FakeInterface({0: write_device()})
    interface.panel = PanelModel()
    return interface


def test_a_setting_is_written_with_the_two_frame_sequence_and_read_back() -> None:
    interface: FakeInterface = settings_interface()
    transport: serial_vebus = writer(interface, panel=False)
    interface.written.clear()
    transport.write_data({"setting_absorption_voltage": 57.0}, MagicMock())

    assert interface.commands()[-4:] == ["W 31 02 00", "W 33 02 00", "W 34 44 16", "W 31 02 00"], "read, select, data (5700 = 0x1644), read back"
    assert interface.devices[0].settings[2] == 5700
    assert "confirmed by reading it back" in logged(transport, "info")


def test_the_new_value_is_published_even_though_settings_are_startup_rows() -> None:
    interface: FakeInterface = settings_interface()
    transport: serial_vebus = writer(interface, panel=False)
    assert transport.read_data()["setting_absorption_voltage"] == approx(56.4)
    transport.write_data({"setting_absorption_voltage": 57.0}, MagicMock())
    assert transport.read_data()["setting_absorption_voltage"] == approx(57.0)


def test_current_settings_scale_with_the_device() -> None:
    interface: FakeInterface = settings_interface()
    transport: serial_vebus = writer(interface, panel=False)
    transport.write_data({"setting_input_current_limit_ac1": 12.5}, MagicMock())
    assert interface.devices[0].settings[6] == 125  # scale 0.1


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({"setting_absorption_voltage": 70.0}, "outside the limits the device reports"),
        ({"setting_absorption_voltage": 30.0}, "outside the limits the device reports"),
        ({"setting_absorption_voltage": "high"}, "not a number"),
        ({"setting_absorption_voltage": float("nan")}, "not a finite number"),
        ({"setting_absorption_voltage": 9999999.0}, "does not fit a 16-bit"),
    ],
)
def test_setting_values_outside_what_the_device_allows_are_refused(values: dict[str, Any], expected: str) -> None:
    interface: FakeInterface = settings_interface()
    transport: serial_vebus = writer(interface, panel=False)
    interface.written.clear()
    transport.write_data(values, MagicMock())
    assert not sent(interface, "W 33") and not sent(interface, "W 34")
    assert expected in logged(transport, "error")


def test_a_device_holding_an_unexpected_value_is_left_alone() -> None:
    interface: FakeInterface = settings_interface()
    transport: serial_vebus = writer(interface, panel=False)
    interface.devices[0].settings[2] = 7000  # outside its own 4000-6000
    transport.write_data({"setting_absorption_voltage": 57.0}, MagicMock())
    assert not sent(interface, "W 33")
    assert "unexpected state" in logged(transport, "error")


def test_a_range_in_the_map_tightens_the_device_limits() -> None:
    interface: FakeInterface = settings_interface()
    transport: serial_vebus = writer(interface, panel=False)
    row: registry_map_entry = next(entry for entry in transport._registry_map() if entry.variable_name == "setting_absorption_voltage")
    row.value_min, row.value_max = 50, 58
    transport.write_data({"setting_absorption_voltage": 59.0}, MagicMock())  # inside the device's 40-60 V
    assert not sent(interface, "W 33")
    assert "set for 'setting_absorption_voltage' in the map" in logged(transport, "error")
    transport.write_data({"setting_absorption_voltage": 57.0}, MagicMock())
    assert interface.devices[0].settings[2] == 5700


def test_flag_settings_are_never_written() -> None:
    interface: FakeInterface = settings_interface()
    transport: serial_vebus = writer(interface, panel=False, write_type="unsafe")
    row: registry_map_entry = next(entry for entry in transport._registry_map() if entry.variable_name == "setting_flags0")
    transport._attempt(row.register, lambda: transport._write_setting(row, 5))
    assert not sent(interface, "W 33")
    assert "flag word" in logged(transport, "error")


def test_an_unsupported_setting_is_reported_and_nothing_is_sent() -> None:
    interface: FakeInterface = settings_interface()
    del interface.devices[0].setting_info[3]
    transport: serial_vebus = writer(interface, panel=False, write_type="relaxed")  # relaxed: enabled although the probe for setting 3 failed
    transport.write_data({"setting_float_voltage": 55.0}, MagicMock())
    assert not sent(interface, "W 33")
    assert "does not support" in logged(transport, "error")


def test_setting_writes_need_their_own_opt_in() -> None:
    interface: FakeInterface = settings_interface()
    transport: serial_vebus = writer(interface, settings=False)
    transport.write_enabled = True
    transport.write_data({"setting_absorption_voltage": 57.0}, MagicMock())
    assert not sent(interface, "W 33")
    assert "allow_setting_writes" in logged(transport, "error")


def test_writing_the_value_already_set_sends_nothing() -> None:
    interface: FakeInterface = settings_interface()
    transport: serial_vebus = writer(interface, panel=False)
    transport.write_data({"setting_absorption_voltage": 56.4}, MagicMock())
    assert not sent(interface, "W 33")


def test_settings_are_rate_limited_per_setting() -> None:
    interface: FakeInterface = settings_interface()
    transport: serial_vebus = writer(interface, panel=False, write_min_interval=60)
    transport.write_data({"setting_absorption_voltage": 57.0}, MagicMock())
    transport.write_data({"setting_absorption_voltage": 57.5}, MagicMock())  # same setting, too soon
    transport.write_data({"setting_float_voltage": 55.0}, MagicMock())  # another setting: allowed
    assert interface.devices[0].settings[2] == 5700 and interface.devices[0].settings[3] == 5500
    assert "write_min_interval" in logged(transport, "error")


def test_a_refused_value_does_not_stop_the_rest_of_the_batch() -> None:
    interface: FakeInterface = settings_interface()
    transport: serial_vebus = writer(interface, panel=False)
    transport.write_data({"setting_absorption_voltage": 99.0, "setting_float_voltage": 55.0}, MagicMock())
    assert interface.devices[0].settings[3] == 5500 and interface.devices[0].settings[2] == 5640


def test_a_device_that_ignores_the_write_is_reported() -> None:
    interface: FakeInterface = settings_interface()
    transport: serial_vebus = writer(interface, panel=False)
    interface.devices[0].ignore_writes = True
    transport.write_data({"setting_absorption_voltage": 57.0}, MagicMock())
    assert "NOT confirmed" in logged(transport, "warning")


def test_no_answer_to_the_data_frame_is_an_error_and_the_value_is_still_read_back() -> None:
    interface: FakeInterface = settings_interface()
    transport: serial_vebus = writer(interface, panel=False)
    interface.devices[0].silent_writes = True
    transport.write_data({"setting_absorption_voltage": 57.0}, MagicMock())
    assert "no answer to the data frame" in logged(transport, "error")
    assert interface.commands()[-1] == "W 31 02 00"


def test_an_unknown_command_answer_to_the_data_frame_is_reported() -> None:
    interface: FakeInterface = settings_interface()
    transport: serial_vebus = writer(interface, panel=False)
    interface.devices[0].unknown_commands = {0x34}
    transport.write_data({"setting_absorption_voltage": 57.0}, MagicMock())
    assert "does not support" in logged(transport, "error")


def test_unsafe_mode_writes_outside_the_device_limits() -> None:
    interface: FakeInterface = settings_interface()
    transport: serial_vebus = writer(interface, panel=False, write_type="unsafe")
    transport.write_data({"setting_absorption_voltage": 70.0}, MagicMock())
    assert interface.devices[0].settings[2] == 7000


def test_a_serial_error_while_writing_is_logged_and_the_port_is_released() -> None:
    interface: FakeInterface = settings_interface()
    transport: serial_vebus = writer(interface, panel=False)
    interface.raise_on_write = serial.SerialException("unplugged")
    transport.write_data({"setting_absorption_voltage": 57.0}, MagicMock())
    assert transport.connected is False and interface.closed is True
    assert "serial error" in logged(transport, "error")


# --------------------------------------------------------------------------- #
# concurrency                                                                   #
# --------------------------------------------------------------------------- #

def test_a_write_to_one_device_never_disturbs_reads_of_another() -> None:
    """The address selection and the write sequence are one locked sequence on the shared interface."""
    interface = FakeInterface({0: write_device(), 1: FakeDevice(ram={4: 2700})})
    writer_transport: serial_vebus = writer(interface, panel=False)
    reader: serial_vebus = make_transport(interface, address=1)
    reader.connect()
    seen: list[int] = []

    def read_loop() -> None:
        for _ in range(60):
            seen.append(int.from_bytes(reader._read_command(0x3004) or b"", "big", signed=True))

    def write_loop() -> None:
        for value in (57.0, 56.0) * 15:
            writer_transport.write_data({"setting_absorption_voltage": value}, MagicMock())

    threads: list[threading.Thread] = [threading.Thread(target=read_loop), threading.Thread(target=write_loop)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert set(seen) == {27000}, "device 1 always answered for itself"
    assert interface.devices[0].settings[2] in (5600, 5700)


# --------------------------------------------------------------------------- #
# the row's own adjustments, in both directions                                 #
# --------------------------------------------------------------------------- #

def test_a_row_offset_is_reversed_when_writing() -> None:
    """Published = payload * unit_mod + Offset, so a published 58.0 with Offset 1 means the device value 57.0."""
    interface: FakeInterface = settings_interface()
    transport: serial_vebus = writer(interface, panel=False)
    row: registry_map_entry = next(entry for entry in transport._registry_map() if entry.variable_name == "setting_absorption_voltage")
    row.adjustments = {"Offset": 1.0}
    transport.write_data({"setting_absorption_voltage": 58.0}, MagicMock())
    assert interface.devices[0].settings[2] == 5700


def test_a_row_unit_multiplier_is_reversed_when_writing() -> None:
    interface: FakeInterface = settings_interface()
    transport: serial_vebus = writer(interface, panel=False)
    row: registry_map_entry = next(entry for entry in transport._registry_map() if entry.variable_name == "setting_absorption_voltage")
    row.unit_mod = 0.01  # published = payload (thousandths of a volt) * 0.01: 570 means 57.0 V
    transport.write_data({"setting_absorption_voltage": 570.0}, MagicMock())
    assert interface.devices[0].settings[2] == 5700


@pytest.mark.parametrize("adjustments", [{"High_Low": "x?(0,65535]->(x-2731)/10"}, {"Context": {"formula": "x*2"}}])
def test_a_row_whose_adjustment_cannot_be_reversed_is_not_written(adjustments: dict[str, object]) -> None:
    interface: FakeInterface = settings_interface()
    transport: serial_vebus = writer(interface, panel=False)
    row: registry_map_entry = next(entry for entry in transport._registry_map() if entry.variable_name == "setting_absorption_voltage")
    row.adjustments = adjustments
    transport.write_data({"setting_absorption_voltage": 57.0}, MagicMock())
    assert not sent(interface, "W 33")
    assert "cannot be reversed" in logged(transport, "error")


def test_the_startup_values_are_static_values_and_a_write_updates_them() -> None:
    interface: FakeInterface = settings_interface()
    transport: serial_vebus = writer(interface, panel=False)
    transport.read_data()
    assert transport.protocolSettings is not None
    assert transport.protocolSettings.static_values["setting_absorption_voltage"] == approx(56.4)
    assert transport.protocolSettings.static_values["software_version"] == 2612123

    transport.write_data({"setting_absorption_voltage": 57.0}, MagicMock())
    assert "setting_absorption_voltage" not in transport.protocolSettings.static_values, "the stale value is dropped at once"
    transport.read_data()
    assert transport.protocolSettings.static_values["setting_absorption_voltage"] == approx(57.0)
