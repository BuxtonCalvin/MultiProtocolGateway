# Description: Unit tests for the serial_vebus scraper transport (MK2 framing, checksums, addressing, scaling, scheduling), against a simulated MK2 interface.
# File: test_serial_vebus.py
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
# These tests deliberately exercise protected members (_read_command, _log, _ram_info, ...).

"""Unit tests for classes.transports.serial_vebus (no serial hardware required).

Test data comes from Victron's "Interfacing with VE.Bus products - MK2 Protocol" (version 3.14):

* the request frames are the document's Appendix 1 examples, byte for byte, checksums included;
* the scale answers and the DC, AC and MasterMultiLED frames are the document's Appendix 2 trace. The
  trace is printed without checksums ("checksums are not shown in the dump"), so the checksum bytes
  here are computed, not copied;
* values the document gives no example for (device state, RAM variable and setting values, the
  software version) are built by hand from the layouts the document states.

No file from documentation/3rdparty is read: every byte is embedded.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
import serial
from conftest import DummySettings

from classes.transports.serial_base import serial_base
from classes.transports.serial_vebus import (
    RamScale,
    decode_ram_scale,
    decode_setting_scale,
    display_value,
    extract_frame,
    frame_checksum,
    frequency_from_period,
    serial_mk2_client,
    serial_vebus,
)
from pytests.conftest import approx

BASE_MODULE = "classes.transports.serial_base"


@pytest.fixture(autouse=True)
def isolated_registries(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test gets empty shared-client/lock registries and ports that resolve to themselves."""
    def _passthrough_port(port: str) -> str:
        return port

    def _mock_port_info(_port: str) -> str:
        return "mock"

    monkeypatch.setattr(serial_base, "clients", {})
    monkeypatch.setattr(serial_base, "_port_locks", {})
    monkeypatch.setattr(f"{BASE_MODULE}.find_usb_serial_port", _passthrough_port)
    monkeypatch.setattr(f"{BASE_MODULE}.get_usb_serial_port_info", _mock_port_info)
    yield



# --------------------------------------------------------------------------- #
# frame helpers written independently of the transport                         #
# --------------------------------------------------------------------------- #

def mk2(body: bytes) -> bytes:
    """<Length> body <Checksum>; body starts with 0xFF for a frame from the MK2 itself."""
    frame: bytes = bytes([len(body)]) + body
    return frame + bytes([(-sum(frame)) & 0xFF])


def mk2_reply(command: str, data: bytes) -> bytes:
    return mk2(b"\xff" + command.encode("ascii") + data)


def w_reply(*codes: tuple[int, int]) -> bytes:
    """One MK2 'W' response frame holding up to three <W frame>s of <code> <lo> <hi>."""
    assert 1 <= len(codes) <= 3
    return mk2_reply("W", b"".join(bytes([code, value & 0xFF, value >> 8]) for code, value in codes))


def ve_bus(data: str) -> bytes:
    """A VE.Bus frame (no 0xFF) from hex, with the checksum computed."""
    return mk2(bytes.fromhex(data))


# Appendix 2: the scale answers of a Multi (RAM variable ID -> (Sc, Offset)).
VENDOR_RAM_INFO: dict[int, tuple[int, int]] = {
    0: (0x7F9C, 0x0000), 1: (0x7F9C, 0x0000), 2: (0x7F9C, 0x0000), 3: (0x7F9C, 0x0000), 4: (0x7F9C, 0x0000),
    5: (0x8064, 0x0000), 6: (0x7F9C, 0x0000), 7: (0x7857, 0x0100), 8: (0x7C2F, 0x0000), 9: (0x8064, 0x0000),
    10: (0x0004, 0x8000), 11: (0x0001, 0x8000), 12: (0x0006, 0x8000), 13: (0x7F38, 0x0000),
}

# Appendix 2 info frames (checksum computed).
VENDOR_DC_FRAME: bytes = ve_bus("20" "7F9A8179" "0C" "510A" "000000" "000000" "88")
VENDOR_AC_L1_FRAME: bytes = ve_bus("20" "0101" "8179" "08" "3753" "4900" "3753" "2700" "C3")
VENDOR_MASTER_MULTI_LED_FRAME: bytes = ve_bus("41" "10090000" "00" "3E00" "E803" "F401")
VENDOR_VERSION_FRAME: bytes = mk2_reply("V", bytes.fromhex("933E110000"))  # 07 FF 'V' 93 3E 11 00 00 (Appendix 2)


@dataclass
class FakeDevice:
    """What one VE.Bus device knows. Raw values are what the wire carries."""

    ram_info: dict[int, tuple[int, int]] = field(default_factory=lambda: dict(VENDOR_RAM_INFO))
    ram: dict[int, int] = field(default_factory=dict[int, int])
    setting_info: dict[int, tuple[int, int]] = field(default_factory=dict[int, tuple[int, int]])
    settings: dict[int, int] = field(default_factory=dict[int, int])
    state: tuple[int, int] = (4, 0)
    software_version: int = 2612123
    unknown_commands: set[int] = field(default_factory=set[int])
    ''' 'W' commands answered with 0x80 "unknown command" '''
    setting_limits: dict[int, tuple[int, int]] = field(default_factory=dict[int, tuple[int, int]])
    ''' setting ID -> (minimum, maximum) raw, as reported with the setting info; default is the whole 16-bit range '''
    ignore_writes: bool = False
    ''' answers a setting write with 0x88 but does not store it '''
    silent_writes: bool = False
    ''' does not answer the setting write data frame at all '''



@dataclass
class PanelModel:
    """The panel side of a simulated Multi: what the MasterMultiLED frame reports and what the 'S' command changes."""

    state: int = 3
    limit: int = 500
    minimum: int = 62
    maximum: int = 1000
    overridable: bool = True
    dmc_dedicated: bool = False
    switch_register: bool = True
    accept: bool = True
    ''' False: the device ignores 'S' (another panel or a GX device overrules it) '''

    def frame(self) -> bytes:
        config: int = (0x04 if self.overridable else 0) | (0x08 if self.dmc_dedicated else 0)
        charge, invert = {1: (1, 0), 2: (0, 1), 3: (1, 1), 4: (0, 0)}[self.state]
        body: bytes = bytes([0x41, 0x10, 0x09, 0x00, 0x00, config]) + self.minimum.to_bytes(2, "little") + self.maximum.to_bytes(2, "little") + self.limit.to_bytes(2, "little")
        if self.switch_register:
            body += bytes([(charge << 4) | (invert << 5)])
        return mk2(body)

    def apply(self, state: int, limit: int) -> None:
        self.state = state
        if limit < 0x8000 and self.overridable:
            self.limit = max(self.minimum, min(self.maximum, limit))  # the device clamps


class FakeInterface(serial_mk2_client):
    """
    A simulated MK2 interface with VE.Bus devices behind it. It answers the way the vendor document says,
    keeps the selected address like the real one, and remembers every frame written to it.
    """

    def __init__(self, devices: dict[int, FakeDevice] | None = None) -> None:  # pyright: ignore[reportMissingSuperCall]
        self.timeout = 1.0
        self.current_address = None
        self.devices: dict[int, FakeDevice] = devices if devices is not None else {0: FakeDevice()}
        self.address: int = 0xFF
        self.written: list[bytes] = []
        self.replies: deque[bytes | None] = deque()
        self.system_frames: dict[int, list[bytes]] = {0: [VENDOR_DC_FRAME], 1: [VENDOR_AC_L1_FRAME], 5: [VENDOR_MASTER_MULTI_LED_FRAME]}
        self.noise: list[bytes] = []
        ''' frames delivered ahead of the next answer, as unsolicited traffic would be '''
        self.silent_requests: int = 0
        ''' the next this many requests get no answer '''
        self.raise_on_write: Exception | None = None
        self.closed: bool = False
        self.led: tuple[int, int] = (0x00, 0x00)
        self.in_request: bool = False
        self.pending_setting: int | None = None
        self.panel: PanelModel | None = None
        ''' when set, the MasterMultiLED frame is built from it and 'S' commands change it '''

    # -- the SerialClient interface --
    def connect(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True

    def flush_input(self) -> None:
        self.replies.clear()

    def write(self, data: bytes) -> None:
        if self.raise_on_write is not None:
            raise self.raise_on_write
        self.written.append(bytes(data))
        self.replies.extend(self.noise)
        self.noise = []
        if self.silent_requests > 0:
            self.silent_requests -= 1
            return
        self.replies.extend(self.respond(bytes(data)))

    def read_frame(self, timeout: float) -> bytes | None:
        return self.replies.popleft() if self.replies else None

    def read(self, reset_buffer: bool = True, frames: int = 1) -> bytes | None:
        return self.read_frame(self.timeout)

    # -- the simulated interface --
    def respond(self, frame: bytes) -> list[bytes]:
        assert sum(frame) & 0xFF == 0, f"the transport sent a frame with a bad checksum: {frame.hex()}"
        assert len(frame) == (frame[0] & 0x7F) + 2 and frame[1] == 0xFF
        command: str = chr(frame[2])
        data: bytes = frame[3:-1]

        if command == "V":
            return [mk2_reply("V", bytes.fromhex("933E1100") + (b"B" if self.address == 0xFF else bytes([self.address])))]
        if command == "L":
            return [mk2_reply("L", bytes(self.led) + b"\x00")]
        if command == "A":
            if data[0] & 1:
                self.address = data[1] if data[1] <= 0x1F else 0xFF
            return [mk2_reply("A", bytes([data[0], self.address]))]
        if command == "F":
            if data[0] == 5 and self.panel is not None:
                return [self.panel.frame()]
            return list(self.system_frames.get(data[0], []))
        if command == "S":
            assert data[3] == 0x01 and data[4] == 0x80, f"only variant 2 with no other flags is allowed: {data.hex()}"
            if self.panel is not None and self.panel.accept:
                self.panel.apply(data[0], data[1] | (data[2] << 8))
            return [mk2_reply("S", b"")]
        if command == "W":
            device: FakeDevice | None = self.devices.get(self.address)
            return self.respond_w(device, data[0], data[1] | (data[2] << 8)) if device is not None else []
        msg: str = f"the transport sent an unexpected MK2 command {command!r}"
        raise AssertionError(msg)

    def respond_w(self, device: FakeDevice, w_command: int, argument: int) -> list[bytes]:
        if w_command in device.unknown_commands:
            return [w_reply((0x80, 0))]
        if w_command == 0x05:
            return [w_reply((0x82, device.software_version & 0xFFFF))]
        if w_command == 0x06:
            return [w_reply((0x83, device.software_version >> 16))]
        if w_command == 0x0E:
            return [w_reply((0x94, device.state[0] | (device.state[1] << 8)))]
        if w_command == 0x36:
            if argument not in device.ram_info:
                return [w_reply((0x8E, 0))]
            sc, offset = device.ram_info[argument]
            return [w_reply((0x8E, sc), (0x8F, offset))]
        if w_command == 0x30:
            if argument not in device.ram:
                return [w_reply((0x90, 0))]
            return [w_reply((0x85, device.ram[argument]))]
        if w_command == 0x35:
            if argument not in device.setting_info:
                return [w_reply((0x89, 0))]
            sc, offset = device.setting_info[argument]
            low, high = device.setting_limits.get(argument, (0, 0xFFFF))
            return [w_reply((0x89, sc), (0x8A, offset), (0x8B, 0)), w_reply((0x8C, low), (0x8D, high))]
        if w_command == 0x31:
            if argument not in device.settings:
                return [w_reply((0x91, 0))]
            return [w_reply((0x86, device.settings[argument]))]
        if w_command == 0x33:
            self.pending_setting = argument
            return []
        if w_command == 0x34:
            pending, self.pending_setting = self.pending_setting, None
            if device.silent_writes:
                return []
            if pending is None:
                return [w_reply((0x80, 0))]
            if not device.ignore_writes:
                device.settings[pending] = argument
            return [w_reply((0x88, 0))]
        msg: str = f"the transport sent an unexpected W command 0x{w_command:02X}"
        raise AssertionError(msg)

    def commands(self) -> list[str]:
        """What was sent, as 'W 30 0d 00' / 'A 01 00' / 'F 00' strings, to assert on."""
        return [f"{chr(frame[2])} {frame[3:-1].hex(' ')}".strip() for frame in self.written]


def make_transport(interface: FakeInterface, **values: Any) -> serial_vebus:
    """A serial_vebus whose port is the given simulated interface."""
    merged: dict[str, Any] = {"port": "/dev/ttyUSB0", "address": 0, **values}
    transport: serial_vebus = serial_vebus(DummySettings(**merged))  # type: ignore[arg-type]
    transport._create_client = lambda: interface  # type: ignore[method-assign]
    transport._log = MagicMock()
    return transport


def log_of(transport: serial_vebus) -> MagicMock:
    return cast(MagicMock, transport._log)


# --------------------------------------------------------------------------- #
# request frames: the vendor's Appendix 1 examples, byte for byte               #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("command", "info", "vendor_frame"),
    [
        ("A", "01 00", "04 FF 41 01 00 BB"),                 # Instruct the MK2 to communicate with the device at address 0
        ("W", "05 00 00", "05 FF 57 05 00 00 A0"),           # Read the software version, part 0
        ("W", "06 00 00", "05 FF 57 06 00 00 9F"),           # Read the software version, part 1
        ("W", "36 04 00", "05 FF 57 36 04 00 6B"),           # Request the scale and offset of the DC voltage
        ("W", "30 0D 00", "05 FF 57 30 0D 00 68"),           # Request the State of Charge value
        ("F", "00", "03 FF 46 00 B8"),                       # Request the DC info frame
        ("L", "", "02 FF 4C B3"),                            # Request the LED status
        ("S", "03 C0 10 01 01", "07 FF 53 03 C0 10 01 01 D2"),   # remote panel: on, 12 A of 16 A (variant 1)
        ("S", "03 3B 01 01 80", "07 FF 53 03 3B 01 01 80 E7"),   # remote panel: on, 31.5 A (variant 2)
    ],
)
def test_requests_match_the_vendor_examples_byte_for_byte(command: str, info: str, vendor_frame: str) -> None:
    """The S rows only prove the frame builder; the transport never sends 'S' (see the read-only test)."""
    transport: serial_vebus = make_transport(FakeInterface())
    assert transport.build_frame(ord(command), bytes.fromhex(info)) == bytes.fromhex(vendor_frame)


def test_request_frames_for_commands_without_a_printed_checksum_follow_the_same_rule() -> None:
    """3.4 and 3.6 give 'V' and the reset command without a checksum: the rule is the sum being zero."""
    transport: serial_vebus = make_transport(FakeInterface())
    for command in ("V", "R"):
        frame: bytes = transport.build_frame(ord(command))
        assert frame[:3] == bytes([2, 0xFF, ord(command)])
        assert sum(frame) & 0xFF == 0


def test_a_last_data_byte_of_ff_is_padded_with_zero() -> None:
    """3.2: old interface firmware needs 0x00 after a final 0xFF (and Length counts it); newer firmware accepts it."""
    transport: serial_vebus = make_transport(FakeInterface())
    frame: bytes = transport.build_frame(ord("W"), bytes([0x05, 0x00, 0xFF]))
    assert frame[:-1] == bytes([6, 0xFF, ord("W"), 0x05, 0x00, 0xFF, 0x00])
    assert sum(frame) & 0xFF == 0
    assert transport.build_frame(ord("W"), bytes([0x05, 0x00, 0x00]))[0] == 5, "no padding when the last byte is not 0xFF"


@pytest.mark.parametrize("body", [b"", b"\x00", b"\xff\x57\x30\x0d\x00", bytes(range(1, 40))])
def test_the_checksum_makes_the_frame_sum_to_zero(body: bytes) -> None:
    assert (sum(body) + frame_checksum(body)) & 0xFF == 0


# --------------------------------------------------------------------------- #
# frame extraction from the byte stream                                         #
# --------------------------------------------------------------------------- #

def test_extract_frame_takes_one_frame_and_leaves_the_rest() -> None:
    buffer = bytearray(VENDOR_VERSION_FRAME + VENDOR_DC_FRAME)
    assert extract_frame(buffer) == VENDOR_VERSION_FRAME
    assert extract_frame(buffer) == VENDOR_DC_FRAME
    assert extract_frame(buffer) is None
    assert buffer == bytearray()


def test_extract_frame_waits_for_the_rest_of_a_frame() -> None:
    buffer = bytearray(VENDOR_DC_FRAME[:7])
    assert extract_frame(buffer) is None
    assert bytes(buffer) == VENDOR_DC_FRAME[:7], "a partial frame is kept, not dropped"
    buffer += VENDOR_DC_FRAME[7:]
    assert extract_frame(buffer) == VENDOR_DC_FRAME


def test_extract_frame_resynchronizes_after_garbage_and_a_corrupt_frame() -> None:
    corrupt: bytes = VENDOR_AC_L1_FRAME[:-1] + bytes([VENDOR_AC_L1_FRAME[-1] ^ 0x01])
    buffer = bytearray(b"\x00\xff\x01" + corrupt + VENDOR_VERSION_FRAME)
    assert extract_frame(buffer) == VENDOR_VERSION_FRAME, "no waiting on a bogus Length: the second byte of a frame must be a known marker"


def test_a_lone_byte_that_looks_like_a_length_is_dropped_without_waiting() -> None:
    buffer = bytearray(bytes([0x3F, 0x12]) + VENDOR_VERSION_FRAME)  # 0x3F would ask for 65 bytes
    assert extract_frame(buffer) == VENDOR_VERSION_FRAME


# --------------------------------------------------------------------------- #
# decode_frame                                                                  #
# --------------------------------------------------------------------------- #

def test_decode_frame_splits_an_mk2_frame() -> None:
    decoded: SimpleNamespace | None = make_transport(FakeInterface()).decode_frame(VENDOR_VERSION_FRAME)
    assert decoded is not None
    assert (decoded.marker, decoded.command, decoded.data, decoded.led) == (0xFF, ord("V"), bytes.fromhex("933E110000"), None)


def test_decode_frame_splits_a_ve_bus_frame() -> None:
    decoded: SimpleNamespace | None = make_transport(FakeInterface()).decode_frame(VENDOR_DC_FRAME)
    assert decoded is not None
    assert decoded.marker == 0x20 and decoded.command == 0x20 and len(decoded.data) == 14


def test_decode_frame_removes_appended_led_status() -> None:
    frame: bytes = bytes([0x84, 0xFF, ord("S"), 0x12, 0x34])
    frame += bytes([frame_checksum(frame)])
    decoded: SimpleNamespace | None = make_transport(FakeInterface()).decode_frame(frame)
    assert decoded is not None
    assert decoded.led == (0x12, 0x34) and decoded.data == b""


@pytest.mark.parametrize("data", [b"", b"\x01\xff\x00", VENDOR_DC_FRAME[:-1], VENDOR_DC_FRAME + b"\x00"])
def test_decode_frame_rejects_short_and_inconsistent_frames(data: bytes) -> None:
    assert make_transport(FakeInterface()).decode_frame(data) is None


def test_decode_frame_rejects_a_bad_checksum_and_says_so() -> None:
    transport: serial_vebus = make_transport(FakeInterface())
    corrupt: bytes = VENDOR_VERSION_FRAME[:-1] + bytes([VENDOR_VERSION_FRAME[-1] ^ 0xFF])
    assert transport.decode_frame(corrupt) is None
    assert "checksum" in str(log_of(transport).warning.call_args.args[0])


def test_the_old_firmware_zero_pad_after_a_reply_is_harmless() -> None:
    """Appendix 1: older interfaces answer 'A' with a trailing 00 (05 FF 'A' 01 00 00 BA)."""
    reply: bytes = bytes.fromhex("05FF41010000BA")
    assert sum(reply) & 0xFF == 0, "the vendor frame is a valid frame"
    decoded: SimpleNamespace | None = make_transport(FakeInterface()).decode_frame(reply)
    assert decoded is not None and decoded.command == ord("A") and decoded.data[:2] == b"\x01\x00"


# --------------------------------------------------------------------------- #
# scale and offset: Appendix 2 and the formulas of 7.3.8 / 7.3.9                #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("variable", "scale", "signed"),
    [(0, 0.01, False), (1, 0.01, False), (4, 0.01, False), (5, 0.01, True), (7, 0.000510, False), (8, 0.001024, False),
     (9, 0.01, True), (13, 0.005, False)],
)
def test_ram_scales_match_the_scales_in_the_vendor_trace(variable: int, scale: float, signed: bool) -> None:
    """The trace prints 'scale for N = 0.010000' etc; those figures are the expected values."""
    info: RamScale | None = decode_ram_scale(*VENDOR_RAM_INFO[variable])
    assert info is not None
    assert info.scale == approx(scale, abs=5e-7)
    assert info.signed is signed and info.bit is None


def test_the_period_variable_has_the_vendor_offset() -> None:
    info: RamScale | None = decode_ram_scale(*VENDOR_RAM_INFO[7])
    assert info is not None and info.offset == 256


@pytest.mark.parametrize(("variable", "bit"), [(10, 3), (11, 0), (12, 5)])
def test_bit_variables_are_found_from_the_scale_answer(variable: int, bit: int) -> None:
    """The trace: '10 is a bit field, bit 3', '11 ... bit 0', '12 ... bit 5' (Sc is the bit number + 1, Offset 0x8000)."""
    info: RamScale | None = decode_ram_scale(*VENDOR_RAM_INFO[variable])
    assert info is not None and info.bit == bit


def test_a_ram_variable_with_sc_zero_is_not_supported() -> None:
    assert decode_ram_scale(0, 0) is None


def test_an_unusable_ram_scale_is_refused_not_divided_by_zero() -> None:
    assert decode_ram_scale(0x8000, 0) is None  # |Sc| = 0x8000 would divide by zero


def test_settings_use_the_other_reading_of_sc() -> None:
    """7.3.8: Sc > 0 is the scale, Sc < 0 means 1 / -Sc, Sc = 0 is unsupported; settings are never signed."""
    assert decode_setting_scale(1, 0) == RamScale(1.0, 0, signed=False)
    assert decode_setting_scale(0xFF9C, 0) == RamScale(1 / 100, 0, signed=False)  # -100
    assert decode_setting_scale(0, 0) is None
    assert decode_setting_scale(1, 0xFFFF) == RamScale(1.0, -1, signed=False), "the offset is a signed 16-bit value"


def test_display_value_applies_scale_and_offset_and_signedness() -> None:
    assert display_value(0x0A51, RamScale(0.01, 0, signed=False)) == approx(26.41)
    assert display_value(0xFF9C, RamScale(0.01, 0, signed=True)) == approx(-1.0)   # signed: 0xFF9C = -100
    assert display_value(0xFF9C, RamScale(0.01, 0, signed=False)) == approx(654.36)  # the same bits, unsigned


def test_the_frequencies_of_the_vendor_trace() -> None:
    """Appendix 2 prints 50.0 Hz for the inverter period byte 0x88 and 50.1 Hz for the mains period byte 0xC3."""
    inverter: RamScale | None = decode_ram_scale(*VENDOR_RAM_INFO[7])
    mains: RamScale | None = decode_ram_scale(*VENDOR_RAM_INFO[8])
    assert inverter is not None and mains is not None
    assert frequency_from_period(display_value(0x88, inverter)) == approx(50.0, abs=0.05)
    assert frequency_from_period(display_value(0xC3, mains)) == approx(50.1, abs=0.05)
    assert frequency_from_period(0) == 0.0


# --------------------------------------------------------------------------- #
# polling a device                                                              #
# --------------------------------------------------------------------------- #

def test_the_address_is_selected_once_and_before_the_first_w_command() -> None:
    interface = FakeInterface({3: FakeDevice(ram={4: 2641})})
    transport: serial_vebus = make_transport(interface, address=3)

    assert transport._read_command(0x3004) == (26410).to_bytes(4, "big", signed=True)
    assert transport._read_command(0x3004) == (26410).to_bytes(4, "big", signed=True)

    assert interface.commands()[0] == "A 01 03"
    assert interface.commands().count("A 01 03") == 1
    assert interface.address == 3


def test_ram_variables_are_scaled_with_the_scale_the_device_reports() -> None:
    """A device with a 0.1 V resolution (Sc 0x7FF6: 1 / (0x8000 - 0x7FF6) = 0.1): the same raw value means ten times as much."""
    device: FakeDevice = FakeDevice(ram={4: 2641})
    device.ram_info[4] = (0x7FF6, 0)
    transport: serial_vebus = make_transport(FakeInterface({0: device}))
    assert transport._read_command(0x3004) == (264100).to_bytes(4, "big", signed=True)


def test_a_signed_ram_variable_may_be_negative() -> None:
    interface = FakeInterface({0: FakeDevice(ram={5: 0x10000 - 1234})})  # IBat, signed, 0.01 A
    assert make_transport(interface)._read_command(0x3005) == (-12340).to_bytes(4, "big", signed=True)


def test_bit_variables_are_published_as_zero_or_one() -> None:
    interface = FakeInterface({0: FakeDevice(ram={10: 0b1000, 11: 0b0001, 12: 0b100000})})  # bits 3, 0 and 5 set
    transport: serial_vebus = make_transport(interface)
    assert [transport._read_command(0x3000 + i) for i in (10, 11, 12)] == [(1).to_bytes(4, "big", signed=True)] * 3

    interface.devices[0].ram = {10: 0b0111, 11: 0b1110, 12: 0b011111}  # the other bits set, not those
    assert [transport._read_command(0x3000 + i) for i in (10, 11, 12)] == [(0).to_bytes(4, "big", signed=True)] * 3


def test_the_period_variables_are_published_as_frequency_in_millihertz() -> None:
    interface = FakeInterface({0: FakeDevice(ram={7: 0x88, 8: 0xC3})})
    transport: serial_vebus = make_transport(interface)
    assert int.from_bytes(transport._read_command(0x3007) or b"", "big", signed=True) == approx(50000, abs=60)
    assert int.from_bytes(transport._read_command(0x3008) or b"", "big", signed=True) == approx(50100, abs=60)


def test_an_unsupported_ram_variable_is_skipped_and_is_not_a_failure() -> None:
    interface = FakeInterface({0: FakeDevice(ram={})})  # answers 0x90, "variable not supported"
    transport: serial_vebus = make_transport(interface)

    assert transport._read_command(0x3004) is None
    assert transport._last_outcome == "unsupported"


def test_a_ram_variable_with_scale_zero_is_never_read_and_not_asked_for_again() -> None:
    device: FakeDevice = FakeDevice(ram={13: 100})
    del device.ram_info[13]  # the device answers Sc = 0 for it: the battery monitor function is off
    interface = FakeInterface({0: device})
    transport: serial_vebus = make_transport(interface)

    assert transport._read_command(0x300D) is None and transport._last_outcome == "unsupported"
    sent: int = len(interface.written)
    assert transport._read_command(0x300D) is None and transport._last_outcome == "unsupported"
    assert len(interface.written) == sent, "the answer is remembered for the connection"
    assert "W 30 0d 00" not in interface.commands(), "a variable the scale says is missing is not even read"


def test_device_state_inquires_only_and_hides_the_sub_state_outside_charge() -> None:
    interface = FakeInterface({0: FakeDevice(state=(4, 3))})  # Invert full; the sub-state byte is noise
    transport: serial_vebus = make_transport(interface)
    assert transport._read_command(0x0E00) == bytes([4, 0xFF])
    assert "W 0e 00 00" in interface.commands(), "State 0 only inquires (7.3.2)"

    interface.devices[0].state = (9, 2)  # Charge, absorption
    assert transport._read_command(0x0E00) == bytes([9, 2])


def test_an_unknown_command_answer_is_unsupported_not_silent() -> None:
    """7.3.2: firmware before xxyy122 has no device state command and answers 0x80."""
    interface = FakeInterface({0: FakeDevice(unknown_commands={0x0E})})
    transport: serial_vebus = make_transport(interface)
    assert transport._read_command(0x0E00) is None
    assert transport._last_outcome == "unsupported"


def test_the_software_version_is_assembled_from_both_parts() -> None:
    """7.3.1 and the Appendix 1 example of 3.14: part 0 (low) is answered 0x82, part 1 (high) 0x83."""
    interface = FakeInterface({0: FakeDevice(software_version=2612123)})
    assert make_transport(interface)._read_command(0x0500) == (2612123).to_bytes(4, "big")


def test_settings_return_the_raw_word_and_the_scaled_value() -> None:
    device: FakeDevice = FakeDevice(setting_info={2: (0x7F9C, 0)}, settings={2: 5640})
    # setting scale: Sc is read directly (7.3.8), so use Sc = -100 (0xFF9C) for 0.01
    device.setting_info[2] = (0xFF9C, 0)
    transport: serial_vebus = make_transport(FakeInterface({0: device}))
    payload: bytes | None = transport._read_command(0x3102)
    assert payload == (5640).to_bytes(2, "big") + (56400).to_bytes(4, "big", signed=True)


def test_a_setting_with_an_offset_applies_it() -> None:
    device: FakeDevice = FakeDevice(setting_info={63: (1, 0x8000)}, settings={63: 0x8000 + 5})
    payload: bytes | None = make_transport(FakeInterface({0: device}))._read_command(0x313F)
    assert payload is not None and int.from_bytes(payload[2:], "big", signed=True) == 5 * 1000 - 0  # (x + offset) with offset -0x8000


def test_a_setting_the_device_does_not_have_is_unsupported() -> None:
    transport: serial_vebus = make_transport(FakeInterface({0: FakeDevice()}))
    assert transport._read_command(0x3102) is None and transport._last_outcome == "unsupported"


# --------------------------------------------------------------------------- #
# info frames: the vendor trace                                                 #
# --------------------------------------------------------------------------- #

def ints(payload: bytes | None, start: int, count: int = 1) -> list[int]:
    assert payload is not None
    return [int.from_bytes(payload[start + 4 * i:start + 4 * i + 4], "big", signed=True) for i in range(count)]


def test_the_dc_info_frame_of_the_vendor_trace() -> None:
    """Appendix 2 prints: dc V 26.4 V, dc I 0.0 A, output frequency 50.0 Hz."""
    transport: serial_vebus = make_transport(FakeInterface())
    payload: bytes | None = transport._read_command(0x4600)
    assert payload is not None and len(payload) == 21 and payload[0] == 0x0C
    voltage, inverting, charging, net, frequency = ints(payload, 1, 5)
    assert voltage == 26410
    assert (inverting, charging, net) == (0, 0, 0)
    assert frequency == approx(50000, abs=60)


def test_the_dc_currents_are_unsigned_24_bit_and_the_net_current_is_charging_minus_inverting() -> None:
    interface = FakeInterface()
    # inverting 0x000064 = 100 -> 1.00 A, charging 0x0001F4 = 500 -> 5.00 A
    interface.system_frames[0] = [ve_bus("20" "7F9A8179" "0C" "510A" "640000" "F40100" "88")]
    payload: bytes | None = make_transport(interface)._read_command(0x4600)
    _, inverting, charging, net, _ = ints(payload, 1, 5)
    assert (inverting, charging, net) == (1000, 5000, 4000)


def test_the_ac_l1_info_frame_of_the_vendor_trace() -> None:
    """Appendix 2 prints: input V 213.0, input I 0.7, inverter voltage 213.0, inverter current 0.4, input period 50.1 Hz."""
    transport: serial_vebus = make_transport(FakeInterface())
    payload: bytes | None = transport._read_command(0x4601)
    assert payload is not None and len(payload) == 24
    assert payload[23] == 0x79, "byte 3 is <State> in 3.14; the Appendix 2 trace (older firmware) has 0x79 there, passed through as received"
    assert payload[:3] == bytes([0x08, 1, 1]), "phase info, BF factor, inverter factor"
    mains_v, mains_i, inverter_v, inverter_i, frequency = ints(payload, 3, 5)
    assert (mains_v, mains_i, inverter_v, inverter_i) == (213030, 730, 213030, 390)
    assert frequency == approx(50100, abs=60)


def test_the_ac_current_is_multiplied_by_the_frames_factors() -> None:
    interface = FakeInterface()
    interface.system_frames[1] = [ve_bus("20" "0302" "8179" "08" "3753" "4900" "3753" "2700" "C3")]  # BF factor 3, inverter factor 2
    payload: bytes | None = make_transport(interface)._read_command(0x4601)
    _, mains_i, _, inverter_i, _ = ints(payload, 3, 5)
    assert (mains_i, inverter_i) == (730 * 3, 390 * 2)


def test_an_inverter_factor_of_zero_counts_as_one() -> None:
    """Footnote c: inverters on firmware xxyy120 to 125 report 0. MPG counts 1 rather than zeroing the current."""
    interface = FakeInterface()
    interface.system_frames[1] = [ve_bus("20" "0100" "8179" "08" "3753" "4900" "3753" "2700" "C3")]
    payload: bytes | None = make_transport(interface)._read_command(0x4601)
    assert ints(payload, 3, 5)[3] == 390


def test_the_scales_for_info_frames_come_from_the_device() -> None:
    """6.1: info frames use the scales of the equivalent RAM variables, so a different device scale changes the result."""
    device: FakeDevice = FakeDevice()
    device.ram_info[4] = (0x7FF6, 0)  # UBat in 0.1 V
    interface = FakeInterface({0: device})
    assert ints(make_transport(interface)._read_command(0x4600), 1)[0] == 264100


def test_the_master_multi_led_frame_is_passed_through_as_sent() -> None:
    """Appendix 2 prints 'raw current limit 500 [62 - 1000]', i.e. 50.0 A of 6.2 A to 100.0 A, last active input 0."""
    payload: bytes | None = make_transport(FakeInterface())._read_command(0x4605)
    assert payload is not None
    assert payload == bytes.fromhex("1009000000" "3E00" "E803" "F401")
    assert int.from_bytes(payload[5:7], "little") == 62 and int.from_bytes(payload[7:9], "little") == 1000 and int.from_bytes(payload[9:11], "little") == 500


def test_only_the_asked_for_phase_is_accepted_as_the_answer() -> None:
    interface = FakeInterface()
    l3_frame: bytes = ve_bus("20" "0101" "8179" "06" "3753" "4900" "3753" "2700" "C3")  # phase info 0x06 = L3
    interface.noise = [l3_frame]  # arrives ahead of the L1 frame that was asked for
    payload: bytes | None = make_transport(interface)._read_command(0x4601)
    assert payload is not None and payload[0] == 0x08


def test_the_phase_count_of_the_l1_frame_decides_whether_l2_and_l3_are_asked_for() -> None:
    interface = FakeInterface()  # a one phase system: phase info 0x08 on L1, and nothing answers 'F' 2
    transport: serial_vebus = make_transport(interface)
    assert transport._read_command(0x4601) is not None
    assert transport._phases == 1

    assert transport._read_command(0x4602) is None and transport._last_outcome == "unsupported"
    assert "F 02" not in interface.commands(), "no time is spent on a phase the system does not have"


def test_l2_and_l3_are_read_on_a_three_phase_system() -> None:
    interface = FakeInterface()
    interface.system_frames[1] = [ve_bus("20" "0101" "8179" "0A" "3753" "4900" "3753" "2700" "C3")]  # L1 of 3 phases
    interface.system_frames[2] = [ve_bus("20" "0101" "8179" "07" "3853" "4A00" "3853" "2800" "C3")]
    interface.system_frames[3] = [ve_bus("20" "0101" "8179" "06" "3953" "4B00" "3953" "2900" "C3")]
    transport: serial_vebus = make_transport(interface)

    for command, volts in ((0x4601, 213.03), (0x4602, 213.04), (0x4603, 213.05)):
        payload: bytes | None = transport._read_command(command)
        assert ints(payload, 3)[0] == approx(volts * 1000, abs=1)
    assert transport._phases == 3


# --------------------------------------------------------------------------- #
# the interface's own commands                                                  #
# --------------------------------------------------------------------------- #

def test_the_interface_version_frame_of_the_vendor_trace() -> None:
    """Appendix 2: 07 FF 'V' 93 3E 11 00 00 is version 0x00113E93 = 1130131, mode 0x00 (the address set)."""
    payload: bytes | None = make_transport(FakeInterface())._read_command(0x5600)
    assert payload is not None
    assert int.from_bytes(payload[:4], "little") == 1130131


def test_the_led_status_is_returned_as_two_bytes() -> None:
    interface = FakeInterface()
    interface.led = (0b00010001, 0b00000001)
    assert make_transport(interface)._read_command(0x4C00) == bytes([0b00010001, 0b00000001])


# --------------------------------------------------------------------------- #
# noise, garbage, silence                                                       #
# --------------------------------------------------------------------------- #

def test_the_interfaces_unsolicited_version_frames_are_skipped() -> None:
    """3.4: the MK2 sends a version frame about once a second on its own; it may arrive ahead of the answer."""
    interface = FakeInterface({0: FakeDevice(ram={4: 2641})})
    transport: serial_vebus = make_transport(interface)
    transport._read_command(0x3004)  # learns the scale, selects the address
    interface.noise = [VENDOR_VERSION_FRAME, VENDOR_VERSION_FRAME]
    assert transport._read_command(0x3004) == (26410).to_bytes(4, "big", signed=True)


class FakeSerialPort:
    """Just enough of serial.Serial for serial_mk2_client: hands out queued bytes a few at a time."""

    def __init__(self, incoming: bytes = b"") -> None:
        self.incoming = bytearray(incoming)
        self.written: list[bytes] = []
        self.is_open = True
        self.input_resets = 0

    @property
    def in_waiting(self) -> int:
        return len(self.incoming)

    def read(self, count: int) -> bytes:
        taken, self.incoming = bytes(self.incoming[:count]), self.incoming[count:]
        return taken

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def reset_input_buffer(self) -> None:
        self.incoming.clear()
        self.input_resets += 1

    def close(self) -> None:
        self.is_open = False


def wire_client(incoming: bytes = b"") -> tuple[serial_mk2_client, FakeSerialPort]:
    client: serial_mk2_client = serial_mk2_client.__new__(serial_mk2_client)
    client._buffer = bytearray()
    port = FakeSerialPort(incoming)
    client.client = port  # type: ignore[assignment]
    client.timeout = 0.2
    return client, port


def test_the_wire_layer_returns_a_good_frame_behind_a_corrupt_one() -> None:
    good: bytes = w_reply((0x85, 2641))
    client, _ = wire_client(good[:-1] + bytes([good[-1] ^ 0x55]) + good + VENDOR_VERSION_FRAME)  # one bit-flipped frame, length intact
    assert client.read_frame(0.2) == good


def test_the_wire_layer_assembles_a_frame_that_arrives_in_pieces() -> None:
    client, port = wire_client(VENDOR_DC_FRAME[:5])
    assert client.read_frame(0.05) is None, "incomplete: nothing returned, nothing lost"
    port.incoming += VENDOR_DC_FRAME[5:]
    assert client.read_frame(0.2) == VENDOR_DC_FRAME


def test_the_wire_layer_times_out_on_silence() -> None:
    client, _ = wire_client()
    assert client.read_frame(0.05) is None


def test_flushing_discards_what_the_port_and_the_buffer_hold() -> None:
    client, port = wire_client(VENDOR_VERSION_FRAME[:4])
    client.read_frame(0.01)  # moves the bytes into the frame buffer
    client.flush_input()
    assert port.input_resets == 1
    port.incoming += VENDOR_DC_FRAME
    assert client.read_frame(0.2) == VENDOR_DC_FRAME, "the discarded half frame does not corrupt the next one"


def test_the_wire_layer_writes_frames_untouched_and_closes_idempotently() -> None:
    client, port = wire_client()
    client.write(b"\x02\xffV\xa9")
    assert port.written == [b"\x02\xffV\xa9"]
    client.close()
    client.close()
    assert port.is_open is False


def test_no_answer_is_a_silent_outcome_and_never_raises() -> None:
    interface = FakeInterface({0: FakeDevice(ram={4: 2641})})
    transport: serial_vebus = make_transport(interface)
    transport._read_command(0x3004)

    interface.silent_requests = 5
    assert transport._read_command(0x3004) is None
    assert transport._last_outcome == "silent"


def test_extra_bytes_after_a_w_answer_are_ignored() -> None:
    """3.2 and Appendix 1 footnote g: newer products add [DD DD] after the value, and old interfaces pad with 00."""
    interface = FakeInterface({0: FakeDevice(ram={4: 2641})})
    transport: serial_vebus = make_transport(interface)
    transport._read_command(0x3004)

    original = interface.respond_w
    interface.respond_w = lambda device, w_command, argument: (  # type: ignore[method-assign]
        [mk2_reply("W", bytes([0x85]) + (2641).to_bytes(2, "little") + b"\xdd\xdd")] if w_command == 0x30 else original(device, w_command, argument))
    assert transport._read_command(0x3004) == (26410).to_bytes(4, "big", signed=True)


def test_a_serial_error_marks_the_transport_disconnected_and_releases_the_port() -> None:
    interface = FakeInterface()
    transport: serial_vebus = make_transport(interface)
    assert transport.connect() is True

    interface.raise_on_write = serial.SerialException("device disconnected")
    assert transport._read_command(0x3004) is None
    assert transport.connected is False
    assert interface.closed is True
    assert serial_base.clients == {}, "the next connect opens the port afresh"


def test_an_unexpected_error_is_logged_and_the_row_skipped() -> None:
    interface = FakeInterface()
    transport: serial_vebus = make_transport(interface)
    transport._dispatch = MagicMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    assert transport._read_command(0x3004) is None
    log_of(transport).exception.assert_called()


def test_a_register_the_transport_does_not_know_is_skipped_with_a_warning() -> None:
    transport: serial_vebus = make_transport(FakeInterface())
    assert transport._read_command(0x9999) is None
    assert transport._last_outcome == "unsupported"
    assert "0x9999" in str(log_of(transport).warning.call_args.args[0])


# --------------------------------------------------------------------------- #
# connect, handshake, cycles                                                    #
# --------------------------------------------------------------------------- #

def test_connect_selects_the_address_and_reads_the_software_version() -> None:
    interface = FakeInterface()
    transport: serial_vebus = make_transport(interface)
    assert transport.connect() is True
    assert interface.commands() == ["A 01 00", "W 05 00 00", "W 06 00 00"]
    assert any("2612123" in str(call.args[0]) for call in log_of(transport).info.call_args_list)


def test_connect_fails_when_no_device_answers_at_the_address() -> None:
    interface = FakeInterface({1: FakeDevice()})  # nothing at address 0
    transport: serial_vebus = make_transport(interface)
    assert transport.connect() is False
    assert transport.connected is False
    assert "address 0" in str(log_of(transport).warning.call_args.args[0])


def test_an_address_beyond_0x1f_is_refused() -> None:
    with pytest.raises(ValueError, match="address"):
        make_transport(FakeInterface(), address=32)


def test_the_defaults_are_the_documented_serial_parameters() -> None:
    """3.1: 2400 baud, 8 data bits, no parity, 1 stop bit."""
    transport: serial_vebus = make_transport(FakeInterface())
    assert transport.baudrate == 2400
    parameters: dict[str, Any] = transport._serial_parameters()
    assert (parameters["bytesize"], parameters["parity"], parameters["stopbits"]) == (8, serial.PARITY_NONE, 1)


def test_the_timeout_defaults_above_the_slowest_documented_answer_and_can_be_set() -> None:
    """3.3: 500 ms for most commands, more than 750 ms for the MasterMultiLED frame."""
    assert make_transport(FakeInterface())._timeout == 1.0
    assert make_transport(FakeInterface(), timeout="2.5")._timeout == 2.5
    assert make_transport(FakeInterface())._client_timeout() > 0.75


def test_a_cycle_is_abandoned_after_three_silent_commands_and_reconnects_next_time() -> None:
    interface = FakeInterface({0: FakeDevice(ram=dict.fromkeys(range(14), 100))})
    transport: serial_vebus = make_transport(interface, protocol_version="victron_mk3usb_vebus")
    assert transport.connect() is True
    interface.silent_requests = 1000  # the device goes away
    sent_before: int = len(interface.written)

    transport.read_data()
    asked: int = len(interface.written) - sent_before
    assert asked <= 3 * 3, f"a few timeouts, not one per row; {asked} requests were sent"
    assert transport.connected is False
    assert not transport.get_cycle_result().is_complete

    interface.silent_requests = 0  # the device is back
    data: dict[str, int | float | str] = transport.read_data()
    assert transport.connected is True and data, "the next cycle reconnects and reads"


def test_a_missing_device_costs_one_handshake_per_cycle() -> None:
    interface = FakeInterface({})  # no devices
    transport: serial_vebus = make_transport(interface, protocol_version="victron_mk3usb_vebus")
    assert transport.read_data() == {}
    assert interface.commands() == ["A 01 00", "W 05 00 00"], "only the handshake was tried"
    assert not transport.get_cycle_result().is_complete


def test_unsupported_commands_do_not_make_a_cycle_incomplete() -> None:
    """A Multi without the battery monitor function answers 'not supported' every cycle; that must not suppress bridge writes."""
    device: FakeDevice = FakeDevice(ram=dict.fromkeys(range(10), 100), state=(4, 0))
    del device.ram_info[13]
    interface = FakeInterface({0: device})
    transport: serial_vebus = make_transport(interface, protocol_version="victron_mk3usb_vebus")
    data: dict[str, int | float | str] = transport.read_data()
    assert data, "the supported rows are published"
    assert "state_of_charge" not in data
    assert transport.get_cycle_result().is_complete


def test_startup_rows_are_read_once_per_connection() -> None:
    interface = FakeInterface({0: FakeDevice(ram=dict.fromkeys(range(10), 100))})
    transport: serial_vebus = make_transport(interface, protocol_version="victron_mk3usb_vebus")
    first: dict[str, int | float | str] = transport.read_data()
    asked_first: int = len(interface.written)
    assert first["software_version"] == 2612123

    second: dict[str, int | float | str] = transport.read_data()
    assert second["software_version"] == 2612123, "cached values are still published every cycle"
    assert interface.commands()[asked_first:].count("W 05 00 00") == 0
    assert interface.commands()[asked_first:].count("V") == 0


def test_the_transport_is_read_only() -> None:
    """Nothing but reads and inquiries is ever sent: no 'S', no 'R', and none of the W write commands 0x32-0x34."""
    interface = FakeInterface({0: FakeDevice(ram=dict.fromkeys(range(14), 100), setting_info={2: (0xFF9C, 0)}, settings={2: 5640})})
    transport: serial_vebus = make_transport(interface, protocol_version="victron_mk3usb_vebus")
    transport.read_data()
    transport.read_data()

    assert interface.written
    for frame in interface.written:
        command: str = chr(frame[2])
        assert command in "AVLFW", f"unexpected command {command}"
        if command == "W":
            assert frame[3] in (0x05, 0x06, 0x0E, 0x30, 0x31, 0x35, 0x36), f"W 0x{frame[3]:02X} is not a read"
        if command == "A":
            assert frame[3] == 0x01 and frame[4] <= 0x1F
        if command == "F":
            assert frame[3] in (0, 1, 2, 3, 4, 5)
    assert not [c for c in interface.commands() if c.startswith("W 0e") and c != "W 0e 00 00"], "the state command only ever inquires"


# --------------------------------------------------------------------------- #
# several devices on one interface                                              #
# --------------------------------------------------------------------------- #

def three_phase_interface() -> FakeInterface:
    return FakeInterface({n: FakeDevice(ram={4: 2600 + n}) for n in range(3)})


def test_each_device_is_addressed_before_it_is_read() -> None:
    interface: FakeInterface = three_phase_interface()
    transports: list[serial_vebus] = [make_transport(interface, address=n) for n in range(3)]

    volts: list[int] = [int.from_bytes(t._read_command(0x3004) or b"", "big", signed=True) for t in transports]
    assert volts == [26000, 26010, 26020]

    # reading them again in another order re-selects the address only when it changed
    interface.written.clear()
    transports[2]._read_command(0x3004)
    transports[2]._read_command(0x3004)
    transports[0]._read_command(0x3004)
    assert interface.commands() == ["W 30 04 00", "W 30 04 00", "A 01 00", "W 30 04 00"]


def test_devices_sharing_the_interface_share_one_port_client() -> None:
    interface: FakeInterface = three_phase_interface()
    first, second = make_transport(interface, address=0), make_transport(interface, address=1)
    first.connect()
    second.connect()
    assert first.client is second.client
    assert len(serial_base.clients) == 1


def test_reads_from_several_threads_never_mix_up_the_devices() -> None:
    """The address selection and the command after it are one locked sequence, so devices read in parallel get their own values."""
    interface: FakeInterface = three_phase_interface()
    transports: list[serial_vebus] = [make_transport(interface, address=n) for n in range(3)]
    for transport in transports:
        transport.connect()
    results: dict[int, list[int]] = {n: [] for n in range(3)}

    def work(n: int) -> None:
        for _ in range(40):
            results[n].append(int.from_bytes(transports[n]._read_command(0x3004) or b"", "big", signed=True))

    threads: list[threading.Thread] = [threading.Thread(target=work, args=(n,)) for n in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert {n: set(values) for n, values in results.items()} == {0: {26000}, 1: {26010}, 2: {26020}}


def test_interleaved_reads_hold_the_bus_lock_without_deadlocking() -> None:
    """The gateway's bus_lock is not reentrant: a read that needs a scale first must not take it twice."""
    interface = FakeInterface({0: FakeDevice(ram=dict.fromkeys(range(14), 100))})
    transport: serial_vebus = make_transport(interface, protocol_version="victron_mk3usb_vebus")
    transport.bus_lock = threading.Lock()

    outcome: dict[str, Any] = {}

    def run() -> None:
        outcome["steps"] = sum(1 for _ in transport.read_data_iter())
        outcome["data"] = transport.get_partial_data()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=20)
    assert not worker.is_alive(), "deadlocked on the bus lock"
    assert outcome["steps"] > 5 and outcome["data"]
    assert transport.bus_lock.locked() is False


# --------------------------------------------------------------------------- #
# scheduling through the gateway                                                #
# --------------------------------------------------------------------------- #

def test_the_scrape_target_names_the_port_and_the_device_address() -> None:
    interface = FakeInterface()
    target: str = make_transport(interface, address=2, protocol_version="victron_mk3usb_vebus").scrape_target
    assert target == "/dev/ttyUSB0:victron_mk3usb_vebus:2"


def test_the_gateway_schedules_vebus_sections_one_group_per_device() -> None:
    """Through the gateway's own grouping: a VE.Bus section is a scraper (non-empty scrape_target), a bridge is not.

    Two sections for the same device are read once; devices on one port stay separate groups.
    """
    from protocol_gateway import Protocol_Gateway, ScrapeGroup

    interface: FakeInterface = three_phase_interface()
    values: dict[str, Any] = {"protocol_version": "victron_mk3usb_vebus", "bridge": "x"}
    device0, device1, device1_again = (make_transport(interface, address=n, **values) for n in (0, 1, 1))
    bridge: Any = SimpleNamespaceLike(scrape_target="")  # what a bridge (MQTT, Home Assistant) reports

    gateway: Protocol_Gateway = Protocol_Gateway.__new__(Protocol_Gateway)
    gateway._Protocol_Gateway__transports = [device0, device1, device1_again, bridge]  # type: ignore[attr-defined]
    groups: list[ScrapeGroup] = gateway._build_scrape_groups()

    assert len(groups) == 2  # device 0, and device 1 read once for both of its sections
    assert sorted(len(group.members) for group in groups) == [1, 2]


class SimpleNamespaceLike:
    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)


def test_the_interleaved_cycle_timeout_scales_with_the_commands() -> None:
    transport: serial_vebus = make_transport(FakeInterface(), protocol_version="victron_mk3usb_vebus")
    commands: int = len(transport._poll_commands())
    assert commands > 40
    assert transport.interleaved_cycle_timeout() >= 2.0 * commands * transport._client_timeout()
