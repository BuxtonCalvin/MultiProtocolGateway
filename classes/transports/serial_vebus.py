# Description: scraper transport for Victron VE.Bus (Multi, Quattro, Phoenix inverter) through an MK2 / MK3-USB interface, using Victron's MK2 serial protocol. Read-only.
# File: serial_vebus.py
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

from __future__ import annotations

import contextlib
import math
import struct
import threading
import time

# Victron VE.Bus through an MK2 or MK3-USB interface.
#
# Source: Victron's "Interfacing with VE.Bus products - MK2 Protocol", version 3.14 (the section
# numbers below refer to it). Everything this module does is checked against the worked
# examples in that document's appendices; see documentation/devices/Victron.md for what is and is
# not verified. Nothing has been run against real hardware.
#
# Writes (off unless configured; see "Write safety" below). Two kinds only, both from the document:
#   panel   'S' (5): the switch position (charger only / inverter only / on / off) and the input current limit, as a
#           remote panel does. Not stored by the Multi, so it is not persistent.
#   setting 'W' 0x33 + 0x34 (7.3.6, 7.3.7): a stored setting. This writes the device's EEPROM.
# Not implemented, on purpose: RAM variable writes (the document does not say which are writable), the flag settings
# (its warnings about unsupported and "not promoted" flags), forced charge states, resets and battery operational limits.
#
# Write safety. Every layer below must pass before a byte that changes the device is sent:
#   1. write_enabled in the section (and write_type for relaxed / unsafe), as for every other transport;
#   2. a capability opt-in: allow_panel_writes and/or allow_setting_writes, both off by default, so enabling writes
#      for one kind of change never enables the other (honored in every write mode);
#   3. the row must be marked W in the map, so the map is the allow list of what can change;
#   4. at connect, enable_write() probes the device and refuses unless at least 90 % of the probes pass (relaxed skips
#      this score, unsafe skips everything in 5 to 9);
#   5. the value must be inside the device's own limits: the minimum and maximum it reports for a setting (7.3.8), or
#      the minimum and maximum input current limit of the MasterMultiLED frame (6.2). Nothing is clamped silently:
#      an out-of-range value is refused, and so is a current limit of 0 (the document: it switches the Multi to invert
#      or bypass), and a range given in the row's values column is applied too;
#   6. read before write: the current value must be readable and inside those limits, a write of the value already
#      there is skipped, and a panel write that would change something that was not asked for (the switch while the
#      limit is asked for, or the other way round) re-sends the current value of the other one instead;
#   7. a minimum interval between writes of the same target (write_min_interval), because settings are EEPROM;
#   8. read back after writing and log whether the device took it, then drop the cached startup value so the new
#      value is published;
#   9. every write and every refusal is logged.
#
# Wire format (3.2): <Length> 0xFF <Command> <Data0> ... <Datan-1> <Checksum>
#   Length    bytes between Length and Checksum; bit 7 set means LED status is appended
#   Checksum  makes the sum of every byte of the frame, Length and Checksum included, 0 modulo 256
#   Values wider than a byte are sent least significant byte first.
# The interface also forwards "VE.Bus frames", which have the same shape without the 0xFF (6.1, 6.2).
#
# Commands used (all of them read-only; this transport never sends 'S', 'R' or any write):
#   'V'  interface firmware version, 'L' LED status, 'A' select the device address,
#   'F'  info frames (DC, AC L1..L4, MasterMultiLED), 'W' the per-device commands: software
#   version, device state (inquire only), read RAM variable, read setting, and the RAM variable
#   and setting info commands that give the scale and offset of a value.
#
# What the protocol map sees. The map's register numbers are command numbers: the high byte is the
# command byte of the vendor document (the ASCII letter, or the 'W' command number) and the low
# byte is its argument (the RAM variable or setting ID, the info frame type):
#
#   0x5600 'V'  0x4C00 'L'  0x4600-0x4604 'F' DC and AC L1-L4  0x4605 'F' MasterMultiLED
#   0x0500 software version  0x0E00 device state  0x30nn RAM variable nn  0x31nn setting nn
#
# The scale and offset of a RAM variable or setting differ between devices (the document says to
# ask the device, 7.3.8 and 7.3.9), so the transport asks once per connection, applies them, and
# hands the map a payload in fixed units. Those layouts are described next to the code that builds
# them (_normalize_* and the _LAYOUT_* constants); the map reads them like any other fixed-offset frame.
from collections.abc import Callable, Generator, Iterator
from types import SimpleNamespace
from typing import Any, NamedTuple

import serial

from classes.protocol_settings import Data_Type, WriteMode, registry_map_entry
from defs.common import TransportSettings

from .serial_base import SerialClient, serial_base
from .transport_base import TransportWriteMode, transport_base

MK2_MARKER : int = 0xFF
''' second byte of every frame the MK2 itself sends and receives (3.2) '''
FRAME_MARKERS : frozenset[int] = frozenset({MK2_MARKER, 0x20, 0x40, 0x41, 0x3C})
''' what can follow Length: 0xFF for an MK2 frame, or the type of a VE.Bus frame (info 0x20, panel 0x40, MasterMultiLED 0x41, 6.4 battery limits 0x3C) '''
MAX_FRAME_LENGTH : int = 0x40
''' the largest Length value taken as the start of a frame; the longest real reply is a dozen bytes '''
INT32_MAX : int = 0x7FFFFFFF


def frame_checksum(body : bytes) -> int:
    ''' the checksum byte that makes the sum of body plus the checksum zero modulo 256 (3.2) '''
    return (-sum(body)) & 0xFF


def extract_frame(buffer : bytearray) -> bytes | None:
    '''
    Removes and returns the first valid frame in the buffer, or None if more bytes are needed.

    A frame has no start marker, so the buffer is resynchronized by checksum: the first byte is taken
    as a Length, and when the frame it describes fails its checksum that byte is dropped and the next
    one is tried. Bytes that cannot start a frame (an impossible Length, or a second byte that is not a known
    frame marker) are dropped on the way, so a corrupt frame does not make the parser wait for bytes that never come.
    '''
    while buffer:
        length: int = buffer[0] & 0x7F
        if length < 2 or length > MAX_FRAME_LENGTH:
            del buffer[0]
            continue
        if len(buffer) >= 2 and buffer[1] not in FRAME_MARKERS:
            del buffer[0]  # not the start of a frame: do not wait for the bytes a bogus Length asks for
            continue
        total: int = length + 2
        if len(buffer) < total:
            return None
        if sum(buffer[:total]) & 0xFF != 0:
            del buffer[0]
            continue
        frame: bytes = bytes(buffer[:total])
        del buffer[:total]
        return frame
    return None


class serial_mk2_client():
    '''
    The wire layer for the MK2 protocol: one instance per physical serial port, shared by every
    transport (device address) on that port. Knows frames and checksums, nothing about commands.
    '''

    timeout : float = 1.0
    ''' seconds to wait for a response '''
    READ_SLICE : float = 0.05
    ''' seconds the port waits for data on each read, so the overall timeout is honored '''

    current_address : int | None = None
    '''
    The device address the interface was last told to use, or None when unknown. It belongs to the
    interface, not to a transport, so it lives here, and is only read or written while the port lock is held.
    '''

    client : serial.Serial
    ''' the serial port '''

    def __init__(self, port : str, baud : int, **kwargs : Any) -> None:
        self.port: str = port
        self.baud: int = baud
        self._buffer: bytearray = bytearray()
        self.client = serial.Serial(port, baud, timeout=self.READ_SLICE, **kwargs)

    def connect(self) -> bool:
        ''' nothing to start: frames are read on demand '''
        return True

    def close(self) -> None:
        ''' release the serial port; safe to call more than once '''
        if self.client.is_open:
            self.client.close()

    def write(self, data : bytes) -> None:
        ''' send one complete request frame, exactly as the protocol built it '''
        self.client.write(data)

    def flush_input(self) -> None:
        '''
        Discards everything received so far. The interface sends a version frame about once a second on
        its own (3.4), and answers to a request that timed out can arrive late; neither may be taken for
        the answer to the next request.
        '''
        self._buffer.clear()
        self.client.reset_input_buffer()

    def read_frame(self, timeout : float) -> bytes | None:
        ''' waits up to timeout seconds for one valid frame and returns it whole; None when none arrived '''
        deadline: float = time.monotonic() + timeout
        while True:
            frame: bytes | None = extract_frame(self._buffer)
            if frame is not None:
                return frame
            if time.monotonic() >= deadline:
                return None
            self._buffer += self.client.read(max(1, self.client.in_waiting))

    def read(self, reset_buffer : bool = True, frames : int = 1) -> bytes | None:
        ''' the SerialClient interface: one frame within the client's timeout '''
        if reset_buffer:
            self._buffer.clear()
        return self.read_frame(self.timeout)


class RamScale(NamedTuple):
    ''' how to turn a raw 16-bit RAM variable or setting into its display value (7.3.8, 7.3.9) '''
    scale : float
    offset : int
    signed : bool
    bit : int | None = None
    ''' set when the variable is a single bit (offset 0x8000): the bit number '''


def signed16(value : int) -> int:
    return value - 0x10000 if value & 0x8000 else value


def decode_ram_scale(sc : int, offset : int) -> RamScale | None:
    '''
    Interprets the answer to CommandGetRAMVarInfo (7.3.9): Sc and Offset as unsigned 16-bit words.
    Returns None when the variable is not supported (Sc = 0) or the scale is not usable.

      Sc < 0                       the value read is signed, otherwise unsigned
      Scale = |Sc|                 and, when that is 0x4000 or more, 1 / (0x8000 - |Sc|)
      display = Scale * (x + Offset)
      Offset = 0x8000              the variable is a bit, and Sc is the bit number + 1
    '''
    sc_signed: int = signed16(sc)
    offset_signed: int = signed16(offset)
    if sc_signed == 0:
        return None
    if offset_signed == -0x8000:
        return RamScale(1.0, 0, signed=False, bit=abs(sc_signed) - 1)
    magnitude: int = abs(sc_signed)
    scale: float = float(magnitude)
    if magnitude >= 0x4000:
        divisor: int = 0x8000 - magnitude
        if divisor <= 0:
            return None
        scale = 1.0 / divisor
    return RamScale(scale, offset_signed, sc_signed < 0)


def decode_setting_scale(sc : int, offset : int) -> RamScale | None:
    '''
    Interprets the answer to CommandGetSettingInfo (7.3.8). The reading of Sc differs from the RAM variable one:
    Sc > 0 is the scale itself and Sc < 0 is 1 / -Sc; 0 means the setting is not supported. Settings are unsigned.
    '''
    sc_signed: int = signed16(sc)
    if sc_signed == 0:
        return None
    scale: float = float(sc_signed) if sc_signed > 0 else 1.0 / (-sc_signed)
    return RamScale(scale, signed16(offset), signed=False)


def _to_int32(value : float) -> int:
    ''' rounds to the nearest integer and keeps it inside a signed 32-bit word '''
    return max(-INT32_MAX - 1, min(INT32_MAX, round(value)))


def display_value(raw : int, info : RamScale) -> float:
    ''' Scale * (x + Offset) of a raw 16-bit value (x is signed when the variable is) '''
    x: int = signed16(raw) if info.signed else raw
    return info.scale * (x + info.offset)


def frequency_from_period(period : float) -> float:
    ''' a period in the document's 0.1 s time base to hertz: 10 / period, 0 when there is no period (App. 2) '''
    return 10.0 / period if period else 0.0


class _Unsupported(Exception):
    ''' the device answered that it does not have what was asked for '''


class _Silent(Exception):
    ''' no usable answer arrived in time '''


class _Refused(Exception):
    ''' a write was refused by one of the safety layers; the message says which and why '''


class PanelState(NamedTuple):
    ''' what the MasterMultiLED frame says about the panel side of the system (6.2) '''
    minimum : int
    maximum : int
    actual : int
    ''' the three input current limits, in deci-amps '''
    switch : int
    ''' 1 charger only, 2 inverter only, 3 on, 4 off, 0 when the frame has no switch register (older firmware) '''
    overridable : bool
    ''' AC input configuration bit 2: a remote panel can override the input current limit '''
    dmc_dedicated : bool
    ''' AC input configuration bit 3: the document says to ignore the limit fields '''


def switch_state_from_register(register : int) -> int:
    '''
    The switch state (the 'S' command's numbering) from the switch register of the MasterMultiLED frame: bit 4 is the
    charge switch and bit 5 the invert switch, "current state" (6.2). Charge only is 1, invert only 2, both on 3, neither 4.
    The mapping of those two bits onto the 'S' numbering is an inference: the document describes both but does not tie them together.
    '''
    charge: int = (register >> 4) & 1
    invert: int = (register >> 5) & 1
    return {(1, 0): 1, (0, 1): 2, (1, 1): 3, (0, 0): 4}[(charge, invert)]


def parse_master_multi_led(data : bytes) -> PanelState:
    ''' reads the MasterMultiLED data bytes (6.2); needs the 11 bytes every firmware sends, the 12th (switch register) is optional '''
    if len(data) < 11:
        msg: str = f"MasterMultiLED frame has {len(data)} data bytes, expected at least 11"
        raise _Refused(msg)
    return PanelState(
        minimum=data[5] | (data[6] << 8), maximum=data[7] | (data[8] << 8), actual=data[9] | (data[10] << 8),
        switch=switch_state_from_register(data[11]) if len(data) >= 12 else 0,
        overridable=bool(data[4] & 0x04), dmc_dedicated=bool(data[4] & 0x08))


class serial_vebus(serial_base):

    transport_type : str = "scraper"
    ''' declared here, not inherited: the web UI scanner reads this file with ast and does not follow inheritance '''

    MAX_ADDRESS : int = 0x1F
    ''' valid device addresses are 0x00 to 0x1F (7.1) '''
    DEFAULT_BAUDRATE : int = 2400
    ''' 3.1 gives 2400 baud 8N1 for the MK2 family. The document does not say whether the MK3-USB differs; see Victron.md '''
    OPEN_ON_INIT : bool = False
    ''' open the port on connect, so an interface that is not plugged in yet is retried every cycle '''
    EXCLUSIVE_PORT : bool = True
    PRUNE_SHORT_PAYLOADS : bool = True

    DEFAULT_TIMEOUT : float = 1.0
    ''' the document advises 500 ms, and more than 750 ms for the MasterMultiLED frame (3.3) '''
    MASTER_MULTI_LED_TIMEOUT : float = 0.8
    ''' the least time allowed for the MasterMultiLED frame, whatever the timeout setting says '''
    MAX_CONSECUTIVE_FAILURES : int = 3
    ''' commands in a row that get no answer before the cycle is abandoned: a silent device must not cost one timeout per row '''

    # --- the register numbers of the map (see the header) ---
    CMD_VERSION : int = 0x5600
    CMD_LED : int = 0x4C00
    CMD_INFO_DC : int = 0x4600
    CMD_INFO_AC_L1 : int = 0x4601
    CMD_INFO_AC_L4 : int = 0x4604
    CMD_MASTER_MULTI_LED : int = 0x4605
    CMD_SOFTWARE_VERSION : int = 0x0500
    CMD_DEVICE_STATE : int = 0x0E00
    CMD_RAM_VAR : int = 0x3000
    CMD_SETTING : int = 0x3100
    CMD_PANEL : int = 0x5300
    ''' 'S' as a pseudo command: reads back the switch state and input current limit, and is what the panel rows write '''

    # --- the vendor's own 'W' command numbers (7.3) ---
    W_SOFTWARE_VERSION_PART0 : int = 0x05
    W_SOFTWARE_VERSION_PART1 : int = 0x06
    W_DEVICE_STATE : int = 0x0E
    W_READ_RAM_VAR : int = 0x30
    W_READ_SETTING : int = 0x31
    W_GET_SETTING_INFO : int = 0x35
    W_GET_RAM_VAR_INFO : int = 0x36
    W_WRITE_SETTING : int = 0x33
    W_WRITE_DATA : int = 0x34

    # --- 'W' response codes (7.3) ---
    R_UNKNOWN_COMMAND : int = 0x80
    R_SOFTWARE_VERSION_PART0 : int = 0x82
    R_SOFTWARE_VERSION_PART1 : int = 0x83
    ''' 7.3.1 and the Appendix 1 example of version 3.14 (version 3.12's example showed 0x81 and 0x82) '''
    R_DEVICE_STATE : int = 0x94
    R_RAM_OK : int = 0x85
    R_RAM_UNSUPPORTED : int = 0x90
    R_SETTING_OK : int = 0x86
    R_SETTING_UNSUPPORTED : int = 0x91
    R_SETTING_SCALE : int = 0x89
    R_SETTING_OFFSET : int = 0x8A
    R_RAM_SCALE : int = 0x8E
    R_RAM_OFFSET : int = 0x8F
    R_SETTING_MINIMUM : int = 0x8C
    R_SETTING_MAXIMUM : int = 0x8D
    R_WRITE_SETTING_OK : int = 0x88

    PANEL_FLAGS : int = 0x80
    ''' 'S' <Flags> for variant 2, the limit sent as amps x 10 (5): only bit 7; none of the automatic-send or LED flags '''
    LIMIT_IGNORED : int = 0x8000
    ''' an input current limit of 0x8000 or more is ignored by the system (5) '''
    FLAG_SETTINGS : frozenset[int] = frozenset({0, 1, 60, 61})
    ''' settings that are flag words: never written (7.3.13.3 warns about unused, unsupported and "not promoted" flags) '''
    MASTER_MULTI_LED_MAX_AGE : float = 2.0
    ''' a MasterMultiLED frame read this recently is reused when polling, so the panel rows do not cost a second 750 ms exchange '''
    DEFAULT_WRITE_MIN_INTERVAL : float = 30.0
    WRITE_VALIDATION_SCORE : float = 90.0
    ''' percent of the enable_write probes that must pass, as for the Modbus transports '''

    STATE_CHARGE : int = 9
    ''' device state "Charge": only then does the sub-state mean anything (7.3.2) '''
    SUB_STATE_NOT_APPLICABLE : int = 0xFF

    PERIOD_RAM_VARIABLES : frozenset[int] = frozenset({7, 8})
    ''' inverter and mains period time: published as a frequency '''

    # Info frame layout on the wire (6.1), as data bytes after the 0x20 marker.
    PHASE_INFO_DC : int = 0x0C
    PHASE_INFO_L1 : frozenset[int] = frozenset({0x08, 0x09, 0x0A, 0x0B})
    PHASE_INFO_L2 : int = 0x07
    PHASE_INFO_L3 : int = 0x06
    PHASE_INFO_L4 : int = 0x05
    INFO_FRAME_MARKER : int = 0x20
    MASTER_MULTI_LED_MARKER : int = 0x41
    INFO_FRAME_DATA : int = 14
    MASTER_MULTI_LED_DATA : int = 11
    ''' data bytes a MasterMultiLED frame must have; version 3.14 adds a 12th, the switch register, which older firmware (and the Appendix 2 trace) does not send '''
    MASTER_MULTI_LED_DATA_MAX : int = 12

    _lock : threading.RLock
    ''' guards this transport's caches; always taken before the bus and port locks '''
    _ram_info : dict[int, RamScale | None]
    ''' RAM variable ID -> its scale, None when the device does not support it; cleared on every connect '''
    _setting_info : dict[int, RamScale | None]
    _phases : int | None
    ''' number of phases the system reported in its L1 info frame; None until it has '''
    _master_multi_led_cache : tuple[float, bytes] | None
    _setting_limits : dict[int, tuple[int, int] | None]
    ''' setting ID -> the (minimum, maximum) raw values the device reports (7.3.8), None when unsupported '''
    _last_write : dict[int, float]
    ''' register -> time.monotonic() of the last write attempt, for write_min_interval '''
    _last_outcome : str
    ''' "ok", "unsupported" (the device answered that it has no such thing) or "silent" (no usable answer) '''

    def __init__(self, settings : TransportSettings) -> None:
        # Set first: serial_base consults the timeout while it is being constructed.
        self._timeout: float = float(settings.get("timeout", fallback=str(self.DEFAULT_TIMEOUT)))
        self._lock = threading.RLock()
        self._ram_info = {}
        self._setting_info = {}
        self._phases = None
        self._last_outcome = "ok"
        self._master_multi_led_cache = None
        self._setting_limits = {}
        self._last_write = {}
        self._allow_panel_writes: bool = settings.getboolean("allow_panel_writes", fallback=False)
        self._allow_setting_writes: bool = settings.getboolean("allow_setting_writes", fallback=False)
        self._write_min_interval: float = settings.getfloat("write_min_interval", fallback=self.DEFAULT_WRITE_MIN_INTERVAL)
        self._verify_delay: float = 0.5
        ''' seconds between a panel write and the read that checks it; the document says a state change takes place within 1 s (7.3.2) '''

        super().__init__(settings)
        self._write_requested: bool = self.write_enabled
        ''' what the configuration asked for; enable_write() may switch write_enabled off and is tried again on every connect '''

    # ------------------------------------------------------------------ #
    # serial_base interface                                                #
    # ------------------------------------------------------------------ #

    def _create_client(self) -> serial_mk2_client:
        return serial_mk2_client(self.port, self.baudrate, **self._serial_parameters())

    def _check_shared_client(self, shared : SerialClient) -> None:
        if not isinstance(shared, serial_mk2_client):
            msg: str = f"{type(self).__name__} on {self.port} cannot share a client that is not an MK2 client"
            raise ValueError(msg)  # noqa: TRY004  (serial_base's contract: ValueError for a client this section cannot share)

    def _prepare_client(self, client : SerialClient) -> None:
        if not isinstance(client, serial_mk2_client):
            msg: str = f"{type(self).__name__} needs a serial_mk2_client, got {type(client).__name__}"
            raise TypeError(msg)
        client.timeout = self._timeout

    def _client_timeout(self) -> float:
        return self._timeout

    def _mk2_client(self) -> serial_mk2_client:
        client: SerialClient = self._acquire_client()  # opens the port if a failed exchange released it
        if not isinstance(client, serial_mk2_client):
            msg: str = f"{type(self).__name__} needs a serial_mk2_client, got {type(client).__name__}"
            raise TypeError(msg)
        return client

    # ------------------------------------------------------------------ #
    # frames                                                               #
    # ------------------------------------------------------------------ #

    def build_frame(self, command : int, info : bytes = b"") -> bytes:
        '''
        A complete MK2 request: <Length> 0xFF <command> <info> <Checksum> (3.2). `command` is the command
        byte (an ASCII letter such as ord('W')) and `info` its data.

        When the last data byte is 0xFF the frame is padded with 0x00, as 3.2 describes for old interface
        firmware; newer firmware accepts the padding on every command in the document.
        '''
        data: bytes = bytes([MK2_MARKER, command]) + info
        if info and info[-1] == 0xFF:
            data += b"\x00"
        frame: bytes = bytes([len(data)]) + data
        return frame + bytes([frame_checksum(frame)])

    def decode_frame(self, raw_frame : bytes) -> SimpleNamespace | None:
        '''
        Splits a received frame into:
          marker   0xFF for a frame from the MK2 itself, otherwise the frame type of a VE.Bus frame (0x20, 0x41, 0x40)
          command  the command byte of an MK2 frame, or the frame type of a VE.Bus frame
          data     what follows, without the checksum (and without appended LED status)
          led      the appended LED status (on, blink) when bit 7 of Length is set, else None
        Returns None for a frame that is too short, has the wrong length or fails its checksum.
        '''
        frame: bytes = bytes(raw_frame)
        if len(frame) < 4 or len(frame) != (frame[0] & 0x7F) + 2:
            self._log.debug(f"MK2 frame has an inconsistent length: {frame.hex()}")
            return None
        if sum(frame) & 0xFF != 0:
            self._log.warning(f"MK2 checksum error, frame discarded: {frame.hex()}")
            return None

        body: bytes = frame[1:-1]
        led: tuple[int, int] | None = None
        if frame[0] & 0x80 and len(body) >= 2:
            led = (body[-2], body[-1])
            body = body[:-2]

        decoded = SimpleNamespace()
        decoded.led = led
        decoded.marker = body[0]
        if body[0] == MK2_MARKER:
            if len(body) < 2:
                return None
            decoded.command = body[1]
            decoded.data = body[2:]
        else:
            decoded.command = body[0]
            decoded.data = body[1:]
        return decoded

    # ------------------------------------------------------------------ #
    # exchanges (every method here that ends in _locked needs the locks)   #
    # ------------------------------------------------------------------ #

    @contextlib.contextmanager
    def _bus(self) -> Generator[None, None, None]:
        '''
        Holds the gateway's bus lock (interleaved mode) and the per-port lock, as serial_base._exchange does,
        for a whole sequence of frames: selecting the device address and the command that follows it must not
        be split by another device's address selection. The bus lock is not reentrant, so nothing inside may
        take it again; the helpers named _locked never do.
        '''
        bus_lock: threading.Lock | None = self.bus_lock
        if bus_lock is not None:
            bus_lock.acquire()
        try:
            with self._get_port_lock():
                yield
        finally:
            if bus_lock is not None:
                bus_lock.release()

    def _await_frame_locked(self, accept : Callable[[SimpleNamespace], bool], timeout : float) -> SimpleNamespace | None:
        ''' reads frames until one satisfies accept, or the time is up. Anything else (an unsolicited version frame) is skipped. '''
        client: serial_mk2_client = self._mk2_client()
        deadline: float = time.monotonic() + timeout
        while True:
            remaining: float = deadline - time.monotonic()
            if remaining <= 0:
                return None
            raw: bytes | None = client.read_frame(remaining)
            if raw is None:
                return None
            decoded: SimpleNamespace | None = self.decode_frame(raw)
            if decoded is not None and accept(decoded):
                return decoded

    def _send_locked(self, command : int, info : bytes) -> None:
        client: serial_mk2_client = self._mk2_client()
        client.flush_input()
        self.send_command(command, info)

    def _select_address_locked(self) -> bool:
        ''' points the interface at this transport's device (7.1), unless it already is. True when it is. '''
        client: serial_mk2_client = self._mk2_client()
        if client.current_address == self.address:
            return True

        self._send_locked(ord("A"), bytes([0x01, self.address]))
        reply: SimpleNamespace | None = self._await_frame_locked(
            lambda f: f.marker == MK2_MARKER and f.command == ord("A") and len(f.data) >= 2, self._timeout)
        if reply is None or reply.data[1] != self.address:
            self._log.warning(f"Interface did not accept VE.Bus device address {self.address}")
            client.current_address = None
            return False
        client.current_address = self.address
        return True

    @staticmethod
    def _w_frames(data : bytes) -> list[tuple[int, int]]:
        '''
        The <W frame>s of a 'W' response: up to three of <command> <Info0> <Info1> packed into one MK2 frame (7.3).
        Anything that does not make a whole three bytes (the zero padding of old interfaces, the extra data newer
        products append, 3.2) is ignored. Returns (command, 16-bit value) pairs.
        '''
        return [(data[i], data[i + 1] | (data[i + 2] << 8)) for i in range(0, len(data) - 2, 3)]

    def _w_exchange_locked(self, w_command : int, argument : int, done : Callable[[dict[int, int]], bool], timeout : float | None = None) -> dict[int, int] | None:
        '''
        Sends 'W' <w_command> <argument> to the selected device and collects the response codes it answers
        with, as {code: 16-bit value}, until done() says there are enough. None when nothing usable arrives.
        Raises _Unsupported when the device answers "unknown command" (7.3).
        '''
        if not self._select_address_locked():
            return None

        self._send_locked(ord("W"), bytes([w_command, argument & 0xFF, (argument >> 8) & 0xFF]))
        collected: dict[int, int] = {}
        deadline: float = time.monotonic() + (self._timeout if timeout is None else timeout)
        while True:
            remaining: float = deadline - time.monotonic()
            if remaining <= 0:
                break
            reply: SimpleNamespace | None = self._await_frame_locked(
                lambda f: f.marker == MK2_MARKER and f.command == ord("W") and len(self._w_frames(f.data)) > 0, remaining)
            if reply is None:
                break
            for code, value in self._w_frames(reply.data):
                if code == self.R_UNKNOWN_COMMAND:
                    raise _Unsupported
                collected.setdefault(code, value)
            if done(collected):
                return collected
        self._mk2_client().current_address = None  # unclear what state the interface is in: select the address again next time
        self._log.debug(f"No answer to W 0x{w_command:02X} {argument:#06x}")
        return None

    def _w_single_locked(self, w_command : int, argument : int, ok : frozenset[int], unsupported : int | None = None) -> int | None:
        '''
        One 'W' command with one answer. Returns its 16-bit value; None when there was no usable answer.
        Raises _Unsupported when the device says it does not support it.
        '''
        wanted: frozenset[int] = ok | ({unsupported} if unsupported is not None else frozenset[int]())
        collected: dict[int, int] | None = self._w_exchange_locked(w_command, argument, lambda c: any(k in c for k in wanted))
        if collected is None:
            return None
        if unsupported is not None and unsupported in collected:
            raise _Unsupported
        for code in ok:
            if code in collected:
                return collected[code]
        return None

    # ------------------------------------------------------------------ #
    # scale and offset                                                     #
    # ------------------------------------------------------------------ #

    def _ram_scale(self, variable : int) -> RamScale:
        '''
        The scale of a RAM variable, asked from the device once and remembered (7.3.9).
        Raises _Unsupported when the device does not have it, and _Silent when it did not answer.
        '''
        if variable in self._ram_info:
            cached: RamScale | None = self._ram_info[variable]
            if cached is None:
                raise _Unsupported
            return cached

        with self._bus():
            collected: dict[int, int] | None = self._w_exchange_locked(
                self.W_GET_RAM_VAR_INFO, variable,
                lambda c: self.R_RAM_SCALE in c and (c[self.R_RAM_SCALE] == 0 or self.R_RAM_OFFSET in c))
        if collected is None:
            raise _Silent
        info: RamScale | None = decode_ram_scale(collected[self.R_RAM_SCALE], collected.get(self.R_RAM_OFFSET, 0))
        self._ram_info[variable] = info
        if info is None:
            raise _Unsupported
        return info

    def _setting_scale(self, setting : int) -> RamScale:
        ''' the scale and offset of a setting (7.3.8); remembered like _ram_scale '''
        if setting in self._setting_info:
            cached: RamScale | None = self._setting_info[setting]
            if cached is None:
                raise _Unsupported
            return cached

        with self._bus():
            collected: dict[int, int] | None = self._w_exchange_locked(
                self.W_GET_SETTING_INFO, setting,
                lambda c: self.R_SETTING_SCALE in c and (c[self.R_SETTING_SCALE] == 0 or self.R_SETTING_OFFSET in c))
        if collected is None:
            raise _Silent
        info: RamScale | None = decode_setting_scale(collected[self.R_SETTING_SCALE], collected.get(self.R_SETTING_OFFSET, 0))
        self._setting_info[setting] = info
        if info is None:
            raise _Unsupported
        return info

    # ------------------------------------------------------------------ #
    # the commands of the map                                              #
    # ------------------------------------------------------------------ #

    def _read_version(self) -> bytes:
        ''' 'V': the interface's firmware version (4 bytes, least significant first) and its mode byte (3.4) '''
        with self._bus():
            self._send_locked(ord("V"), b"")
            reply: SimpleNamespace | None = self._await_frame_locked(
                lambda f: f.marker == MK2_MARKER and f.command == ord("V") and len(f.data) >= 5, self._timeout)
        if reply is None:
            raise _Silent
        return bytes(reply.data[:5])

    def _read_led(self) -> bytes:
        ''' 'L': LED on and LED blink bit masks (section 4) '''
        with self._bus():
            self._send_locked(ord("L"), b"")
            reply: SimpleNamespace | None = self._await_frame_locked(
                lambda f: f.marker == MK2_MARKER and f.command == ord("L") and len(f.data) >= 2, self._timeout)
        if reply is None:
            raise _Silent
        return bytes(reply.data[:2])

    def _read_software_version(self) -> bytes:
        ''' 7.3.1: the device's version number as 4 bytes, most significant first (part 0 is the low half, part 1 the high half) '''
        with self._bus():
            low: int | None = self._w_single_locked(self.W_SOFTWARE_VERSION_PART0, 0, frozenset({self.R_SOFTWARE_VERSION_PART0}))
            high: int | None = self._w_single_locked(self.W_SOFTWARE_VERSION_PART1, 0, frozenset({self.R_SOFTWARE_VERSION_PART1})) if low is not None else None
        if low is None or high is None:
            raise _Silent
        return struct.pack(">I", low | (high << 16))

    def _read_device_state(self) -> bytes:
        '''
        7.3.2 with State 0, which only inquires and never changes the state of the device. Returns the state and the
        sub-state; the sub-state is 0xFF unless the state is Charge, because only then does it mean anything.
        '''
        with self._bus():
            value: int | None = self._w_single_locked(self.W_DEVICE_STATE, 0, frozenset({self.R_DEVICE_STATE}))
        if value is None:
            raise _Silent
        state: int = value & 0xFF
        sub_state: int = (value >> 8) & 0xFF
        return bytes([state, sub_state if state == self.STATE_CHARGE else self.SUB_STATE_NOT_APPLICABLE])

    def _read_ram_variable(self, variable : int) -> bytes:
        '''
        7.3.3: one RAM variable, scaled with what the device says (7.3.9) and returned as a signed 32-bit
        integer, most significant byte first:
          a bit variable           0 or 1
          inverter / mains period  the frequency in millihertz (10 / period)
          anything else            the display value in thousandths of its unit
        '''
        info: RamScale = self._ram_scale(variable)
        with self._bus():
            raw: int | None = self._w_single_locked(self.W_READ_RAM_VAR, variable, frozenset({self.R_RAM_OK}), self.R_RAM_UNSUPPORTED)
        if raw is None:
            raise _Silent

        if info.bit is not None:
            return struct.pack(">i", (raw >> info.bit) & 1)
        shown: float = display_value(raw, info)
        if variable in self.PERIOD_RAM_VARIABLES:
            shown = frequency_from_period(shown)
        return struct.pack(">i", _to_int32(shown * 1000))

    def _read_setting(self, setting : int) -> bytes:
        '''
        7.3.4: one setting. Returns the raw 16-bit value (2 bytes, for flag words) followed by the display value
        Scale * (x + Offset) in thousandths of its unit as a signed 32-bit integer; all most significant byte first.
        '''
        info: RamScale = self._setting_scale(setting)
        with self._bus():
            raw: int | None = self._w_single_locked(self.W_READ_SETTING, setting, frozenset({self.R_SETTING_OK}), self.R_SETTING_UNSUPPORTED)
        if raw is None:
            raise _Silent
        return struct.pack(">Hi", raw, _to_int32(display_value(raw, info) * 1000))

    def _read_info_frame(self, frame_type : int) -> SimpleNamespace:
        '''
        'F' (6): sends the request and returns the VE.Bus frame answering it. A frame of the wrong kind, or one
        describing another phase, is skipped, so a stray frame cannot be mistaken for the answer.
        '''
        timeout: float = max(self._timeout, self.MASTER_MULTI_LED_TIMEOUT) if frame_type == 5 else self._timeout
        marker: int = self.MASTER_MULTI_LED_MARKER if frame_type == 5 else self.INFO_FRAME_MARKER
        length: int = self.MASTER_MULTI_LED_DATA if frame_type == 5 else self.INFO_FRAME_DATA

        def is_answer(frame : SimpleNamespace) -> bool:
            if frame.marker == MK2_MARKER or frame.command != marker or len(frame.data) < length:
                return False
            return frame_type == 5 or self._phase_matches(frame_type, frame.data[4])

        with self._bus():
            self._send_locked(ord("F"), bytes([frame_type]))
            reply: SimpleNamespace | None = self._await_frame_locked(is_answer, timeout)
        if reply is None:
            raise _Silent
        return reply

    def _phase_matches(self, frame_type : int, phase_info : int) -> bool:
        ''' whether the phase info byte of an info frame (6.1) is the one asked for '''
        if frame_type == 0:
            return phase_info == self.PHASE_INFO_DC
        if frame_type == 1:
            return phase_info in self.PHASE_INFO_L1
        return phase_info == {2: self.PHASE_INFO_L2, 3: self.PHASE_INFO_L3, 4: self.PHASE_INFO_L4}.get(frame_type, -1)

    @staticmethod
    def _u24(data : bytes) -> int:
        return data[0] | (data[1] << 8) | (data[2] << 16)

    def _read_dc_info(self) -> bytes:
        '''
        'F' 0 (6.1), scaled with the scales of the equivalent RAM variables, as the document says. Layout, all
        multi-byte values signed, most significant byte first:
          0 phase info (0x0C)  1-4 battery voltage (mV)  5-8 current used by inverting devices (mA)
          9-12 current provided by charging devices (mA)  13-16 net battery current, charging minus inverting (mA)
          17-20 inverter frequency (mHz)
        The two 24-bit DC currents are unsigned on the wire (6.1).
        '''
        volts: RamScale = self._ram_scale(4)
        amps: RamScale = self._ram_scale(5)
        period: RamScale = self._ram_scale(7)
        data: bytes = bytes(self._read_info_frame(0).data)

        battery_v: float = display_value(data[5] | (data[6] << 8), volts)
        inverting: float = amps.scale * (self._u24(data[7:10]) + amps.offset)
        charging: float = amps.scale * (self._u24(data[10:13]) + amps.offset)
        frequency: float = frequency_from_period(display_value(data[13], period))
        return struct.pack(">Biiiii", data[4], _to_int32(battery_v * 1000), _to_int32(inverting * 1000), _to_int32(charging * 1000),
                           _to_int32((charging - inverting) * 1000), _to_int32(frequency * 1000))

    def _read_ac_info(self, frame_type : int) -> bytes:
        '''
        'F' 1-4 (6.1), scaled with the scales of the equivalent RAM variables and multiplied by the frame's own
        factors. Layout, multi-byte values signed, most significant byte first:
          0 phase info  1 BF factor  2 inverter factor  3-6 mains voltage (mV)  7-10 mains current (mA, total
          after the BF factor)  11-14 inverter voltage (mV)  15-18 inverter current (mA, total after the inverter
          factor)  19-22 mains frequency (mHz)  23 the Multi main state (6.1; firmware 207 and later, older firmware sends 0)
        The L1 frame also says how many phases the system has, which decides whether L2 to L4 are asked for.
        '''
        mains_v: RamScale = self._ram_scale(0)
        mains_i: RamScale = self._ram_scale(1)
        inverter_v: RamScale = self._ram_scale(2)
        inverter_i: RamScale = self._ram_scale(3)
        mains_period: RamScale = self._ram_scale(8)
        data: bytes = bytes(self._read_info_frame(frame_type).data)

        bf_factor: int = data[0]
        inverter_factor: int = data[1] or 1  # inverters on firmware xxyy120 to xxyy125 report 0 (footnote c): count it as 1
        if frame_type == 1:
            self._phases = data[4] - 0x07
        mains_voltage: float = display_value(data[5] | (data[6] << 8), mains_v)
        mains_current: float = display_value(data[7] | (data[8] << 8), mains_i) * bf_factor
        inverter_voltage: float = display_value(data[9] | (data[10] << 8), inverter_v)
        inverter_current: float = display_value(data[11] | (data[12] << 8), inverter_i) * inverter_factor
        frequency: float = frequency_from_period(display_value(data[13], mains_period))
        return struct.pack(">BBBiiiiiB", data[4], bf_factor, data[1], _to_int32(mains_voltage * 1000), _to_int32(mains_current * 1000),
                           _to_int32(inverter_voltage * 1000), _to_int32(inverter_current * 1000), _to_int32(frequency * 1000), data[3])

    def _master_multi_led(self, max_age : float) -> bytes:
        ''' the MasterMultiLED data bytes, from the exchange or, when one younger than max_age seconds exists, from memory '''
        cached: tuple[float, bytes] | None = self._master_multi_led_cache
        if cached is not None and time.monotonic() - cached[0] <= max_age:
            return cached[1]
        data: bytes = bytes(self._read_info_frame(5).data[:self.MASTER_MULTI_LED_DATA_MAX])
        self._master_multi_led_cache = (time.monotonic(), data)
        return data

    def _read_master_multi_led(self) -> bytes:
        ''' 'F' 5 (6.2): the data bytes as sent (11, or 12 with the switch register), least significant byte first (see the map for the fields) '''
        return self._master_multi_led(self.MASTER_MULTI_LED_MAX_AGE)

    def _read_panel_state(self) -> bytes:
        '''
        The panel side of the system, from the same MasterMultiLED frame: the switch state (0 when the firmware does not
        report the switch register) and the actual input current limit in deci-amps (unsigned 16 bit). Most significant byte first.
        '''
        panel: PanelState = parse_master_multi_led(self._master_multi_led(self.MASTER_MULTI_LED_MAX_AGE))
        return struct.pack(">BH", panel.switch, panel.actual)

    def _phase_absent(self, frame_type : int) -> bool:
        ''' true when the system is known to have fewer phases than the L2..L4 frame asked for needs '''
        return frame_type >= 2 and self._phases is not None and self._phases < frame_type

    # ------------------------------------------------------------------ #
    # writes (see "Write safety" at the top of the file)                   #
    # ------------------------------------------------------------------ #

    def _mode(self) -> TransportWriteMode:
        '''
        The write mode: write_type when given; otherwise standard WRITE for a section that asked for writes, because the
        base class only sets write_mode when write_type is present, and READ here would silently skip the standard checks.
        '''
        mode: TransportWriteMode = getattr(self, "write_mode", TransportWriteMode.READ)
        if mode == TransportWriteMode.READ and self._write_requested:
            return TransportWriteMode.WRITE
        return mode

    def _writable_entries(self) -> list[registry_map_entry]:
        ''' the rows marked W (read and write) or WO: the allow list of what this transport may change '''
        return [entry for entry in self._registry_map() if entry.write_mode in (WriteMode.WRITE, WriteMode.WRITEONLY)]

    def _setting_limit(self, setting : int) -> tuple[int, int]:
        '''
        The (minimum, maximum) raw values the device reports for a setting (7.3.8: 0x8C and 0x8D, in the format
        CommandReadSetting returns). Raises _Unsupported or _Silent.
        '''
        if setting in self._setting_limits:
            cached: tuple[int, int] | None = self._setting_limits[setting]
            if cached is None:
                raise _Unsupported
            return cached

        with self._bus():
            collected: dict[int, int] | None = self._w_exchange_locked(
                self.W_GET_SETTING_INFO, setting,
                lambda c: self.R_SETTING_SCALE in c and (c[self.R_SETTING_SCALE] == 0 or self.R_SETTING_MAXIMUM in c))
        if collected is None:
            raise _Silent
        if signed16(collected[self.R_SETTING_SCALE]) == 0:
            self._setting_limits[setting] = None
            raise _Unsupported
        limits: tuple[int, int] = (collected.get(self.R_SETTING_MINIMUM, 0), collected.get(self.R_SETTING_MAXIMUM, 0xFFFF))
        self._setting_limits[setting] = limits
        return limits

    def _raw_setting(self, setting : int) -> int:
        ''' the raw 16-bit value of a setting, straight from the device (never the startup cache) '''
        return struct.unpack(">H", self._read_setting(setting)[:2])[0]

    def _check_interval(self, register : int) -> None:
        ''' write_min_interval: settings are stored in EEPROM, which wears, and a runaway automation must not hammer it '''
        if self._mode() == TransportWriteMode.UNSAFE:
            return
        waited: float = time.monotonic() - self._last_write.get(register, -1e12)
        if waited < self._write_min_interval:
            msg: str = f"last written {waited:.0f} s ago, write_min_interval is {self._write_min_interval:.0f} s"
            raise _Refused(msg)

    def _within_row_range(self, entry : registry_map_entry, value : float) -> None:
        ''' a range given in the row's values column (applied to the published value) tightens the device's own limits '''
        if self._mode() == TransportWriteMode.UNSAFE or self.protocolSettings is None:
            return
        default: tuple[int, int] = Data_Type.getDefaultRange(entry.data_type)
        if (entry.value_min, entry.value_max) == default:
            return
        if not entry.value_min <= value <= entry.value_max:
            msg: str = f"{value} is outside the range {entry.value_min} to {entry.value_max} set for '{entry.variable_name}' in the map"
            raise _Refused(msg)

    def write_data(self, data : dict[str, int | float | str], from_transport : transport_base) -> None:
        '''
        Applies the values that name a row marked W; everything else is ignored. A refused or failed write is logged and
        never raises: the other values of the batch are still tried. The switch state and the input current limit of one
        batch go out as one 'S' command.
        '''
        with self._lock:
            if not self.write_enabled:
                return

            writable: dict[str, registry_map_entry] = {entry.variable_name: entry for entry in self._writable_entries()}
            panel: dict[int, tuple[registry_map_entry, int | float | str]] = {}
            settings: list[tuple[registry_map_entry, int | float | str]] = []
            for name, value in data.items():
                entry: registry_map_entry | None = writable.get(name)
                if entry is None:
                    continue
                if entry.register == self.CMD_PANEL and entry.register_byte in (0, 1):
                    panel[entry.register_byte] = (entry, value)
                elif entry.register >> 8 == self.W_READ_SETTING:
                    settings.append((entry, value))
                else:
                    self._log.error(f"WRITE_ERROR: '{name}' is not something serial_vebus can write; ignored")

            if panel:
                self._attempt(self.CMD_PANEL, lambda: self._write_panel(panel.get(0), panel.get(1)))
            for entry, value in settings:
                self._attempt(entry.register, lambda e=entry, v=value: self._write_setting(e, v))

    def _attempt(self, label : int, write : Callable[[], None]) -> None:
        ''' runs one write and turns every way it can fail into a log line '''
        try:
            write()
        except _Refused as err:
            self._log.error(f"WRITE_ERROR: command 0x{label:04X} refused: {err}")
        except _Unsupported:
            self._log.error(f"WRITE_ERROR: command 0x{label:04X}: the device does not support it")
        except _Silent:
            self._log.error(f"WRITE_ERROR: command 0x{label:04X}: the device did not answer; nothing was confirmed")
        except (serial.SerialException, OSError, ValueError) as err:
            self._log.error(f"WRITE_ERROR: command 0x{label:04X}: serial error {err}; the port will be reopened")
            self.connected = False
            self._release_client()
        except Exception:
            self._log.exception(f"WRITE_ERROR: command 0x{label:04X}: unexpected error")

    # -- panel: the 'S' command (5) --

    def _switch_state_value(self, entry : registry_map_entry, value : int | float | str) -> int:
        ''' 1 charger only, 2 inverter only, 3 on, 4 off; the code text of the map is accepted too '''
        text: str | int | float = value
        if isinstance(value, str) and self.protocolSettings is not None:
            text = self.protocolSettings.get_code_by_value(entry, value, fallback=value)
        try:
            state: int = int(float(text))
        except (TypeError, ValueError):
            msg: str = f"'{value}' is not a switch state"
            raise _Refused(msg) from None
        if state not in (1, 2, 3, 4):
            msg = f"switch state {state} is not 1 (charger only), 2 (inverter only), 3 (on) or 4 (off)"
            raise _Refused(msg)
        return state

    def _write_panel(self, state_row : tuple[registry_map_entry, int | float | str] | None, limit_row : tuple[registry_map_entry, int | float | str] | None) -> None:
        mode: TransportWriteMode = self._mode()
        unsafe: bool = mode == TransportWriteMode.UNSAFE
        if not self._allow_panel_writes:
            raise _Refused("panel writes are not allowed: set allow_panel_writes = true")

        new_state: int | None = self._switch_state_value(*state_row) if state_row is not None else None
        new_limit: int | None = None
        if limit_row is not None:
            try:
                amps: float = float(limit_row[1])
            except (TypeError, ValueError):
                msg: str = f"'{limit_row[1]}' is not a current"
                raise _Refused(msg) from None
            if not math.isfinite(amps) or amps < 0:
                msg = f"{amps} A is not a usable input current limit"
                raise _Refused(msg)
            new_limit = round(amps * 10)
            if new_limit >= self.LIMIT_IGNORED:
                msg = f"{amps} A is beyond what the protocol can send as a limit"
                raise _Refused(msg)
            self._within_row_range(limit_row[0], amps)

        panel: PanelState = parse_master_multi_led(self._master_multi_led(0.0))

        if panel.dmc_dedicated and not unsafe:
            raise _Refused("the Multi reports a dedicated DMC configuration, for which the document says to ignore the limit fields")
        if panel.switch == 0:
            if new_state is None:
                raise _Refused("the input current limit cannot be written alone: this firmware does not report the switch register, so the switch state would be changed too")
            if mode == TransportWriteMode.WRITE:
                raise _Refused("this firmware does not report the switch register, so the write could not be confirmed (write_type = relaxed accepts that)")
        if new_limit is not None and not unsafe:
            if new_limit == 0:
                raise _Refused("an input current limit of 0 switches the Multi to invert or bypass (5): not written")
            if not panel.minimum <= new_limit <= panel.maximum:
                msg = f"{new_limit / 10} A is outside the {panel.minimum / 10} A to {panel.maximum / 10} A the device reports; it would be clamped, so it is refused"
                raise _Refused(msg)
            if not panel.overridable:
                raise _Refused("the Multi ignores a panel input current limit for this input (6.2: 'Input current overridden by panel' is not set)")

        target_state: int = new_state if new_state is not None else panel.switch
        target_limit: int = new_limit if new_limit is not None else min(panel.actual, self.LIMIT_IGNORED)
        if target_state == panel.switch and target_limit == panel.actual:
            self._log.debug("WRITE VE.Bus panel: already at the requested state, nothing sent")
            return

        self._check_interval(self.CMD_PANEL)
        self._last_write[self.CMD_PANEL] = time.monotonic()
        self._master_multi_led_cache = None
        self._log.info(f"WRITE VE.Bus panel: switch {panel.switch} -> {target_state}, input current limit {panel.actual / 10} A -> {target_limit / 10} A")

        with self._bus():
            self._send_locked(ord("S"), bytes([target_state, target_limit & 0xFF, target_limit >> 8, 0x01, self.PANEL_FLAGS]))
            reply: SimpleNamespace | None = self._await_frame_locked(lambda f: f.marker == MK2_MARKER and f.command == ord("S"), self._timeout)
        if reply is None:
            self._log.error("WRITE_ERROR: no answer to the 'S' command; whether the Multi took it is not known")
            return
        self._verify_panel(target_state if panel.switch else None, target_limit if new_limit is not None else None)

    def _verify_panel(self, state : int | None, limit : int | None) -> None:
        ''' reads the MasterMultiLED frame again until it shows what was sent, or says so in the log '''
        seen: PanelState | None = None
        for _ in range(3):
            time.sleep(self._verify_delay)
            try:
                seen = parse_master_multi_led(self._master_multi_led(0.0))
            except _Silent:
                continue
            if (state is None or seen.switch == state) and (limit is None or seen.actual == limit):
                self._log.info("WRITE VE.Bus panel: confirmed by the MasterMultiLED frame")
                return
        shown: str = f"switch {seen.switch}, limit {seen.actual / 10} A" if seen is not None else "no frame"
        self._log.warning(f"WRITE VE.Bus panel: not confirmed ({shown}); the Multi may have clamped it or another panel or a GX device overrules it")

    # -- settings: 'W' 0x33 and 0x34 (7.3.6, 7.3.7) --

    def _write_setting(self, entry : registry_map_entry, value : int | float | str) -> None:
        mode: TransportWriteMode = self._mode()
        unsafe: bool = mode == TransportWriteMode.UNSAFE
        setting: int = entry.register & 0xFF
        if not self._allow_setting_writes:
            raise _Refused("setting writes are not allowed: set allow_setting_writes = true")
        if setting in self.FLAG_SETTINGS:
            msg: str = f"setting {setting} is a flag word; the document warns that changing unused, unsupported or 'not promoted' flags can damage the device, so flags are never written"
            raise _Refused(msg)

        info: RamScale = self._setting_scale(setting)
        low, high = self._setting_limit(setting)
        current: int = self._raw_setting(setting)
        if not unsafe and not low <= current <= high:
            msg: str = f"the device holds {current}, outside the {low} to {high} it reports as valid for setting {setting}; not changing something in an unexpected state"
            raise _Refused(msg)

        raw: int = self._setting_raw_value(entry, info, value)
        if not unsafe and not low <= raw <= high:
            msg = f"{value} is outside the limits the device reports for setting {setting} ({self._shown(info, low, entry)} to {self._shown(info, high, entry)})"
            raise _Refused(msg)
        if raw == current:
            self._log.debug(f"WRITE VE.Bus setting {setting}: already {value}, nothing sent")
            return

        self._check_interval(entry.register)
        self._last_write[entry.register] = time.monotonic()
        self._log.info(f"WRITE VE.Bus setting {setting} ({entry.variable_name}): {current} -> {raw} (raw), requested {value}")

        with self._bus():
            if not self._select_address_locked():
                raise _Silent
            self._send_locked(ord("W"), bytes([self.W_WRITE_SETTING, setting & 0xFF, setting >> 8]))  # no response (7.3.6)
            collected: dict[int, int] | None = self._w_exchange_locked(
                self.W_WRITE_DATA, raw, lambda c: any(code in c for code in (self.R_WRITE_SETTING_OK, 0x87)))
        if collected is None:
            self._log.error(f"WRITE_ERROR: setting {setting}: no answer to the data frame; whether it was written is not known")
        elif self.R_WRITE_SETTING_OK not in collected:
            self._log.error(f"WRITE_ERROR: setting {setting}: the device answered with a RAM write confirmation (0x87), not a setting write (0x88)")

        self._invalidate_startup(entry.register)  # the startup rows must publish the new value
        verified: int = self._raw_setting(setting)
        if verified == raw:
            self._log.info(f"WRITE VE.Bus setting {setting}: confirmed by reading it back ({verified})")
        else:
            self._log.warning(f"WRITE VE.Bus setting {setting}: NOT confirmed, the device now holds {verified} (wanted {raw}); it may need a reset to take effect or have been refused")

    def _setting_raw_value(self, entry : registry_map_entry, info : RamScale, value : int | float | str) -> int:
        '''
        The raw 16-bit value for a requested one. A row on the scaled value (byte offset 2 of the payload) takes the
        display value, Scale * (x + Offset), as published; a row on the raw word takes the raw number or a code text.
        '''
        text: str | int | float = value
        if isinstance(value, str) and self.protocolSettings is not None:
            text = self.protocolSettings.get_code_by_value(entry, value, fallback=value)
        try:
            number: float = float(text)
        except (TypeError, ValueError):
            msg: str = f"'{value}' is not a number"
            raise _Refused(msg) from None
        if not math.isfinite(number):
            msg = f"'{value}' is not a finite number"
            raise _Refused(msg)
        self._within_row_range(entry, number)

        raw: int = round(number / info.scale) - info.offset if entry.register_byte >= 2 else round(number)
        if not 0 <= raw <= 0xFFFF:
            msg = f"{value} does not fit a 16-bit setting"
            raise _Refused(msg)
        return raw

    @staticmethod
    def _shown(info : RamScale, raw : int, entry : registry_map_entry) -> str:
        ''' a raw limit in the units the row publishes, for the log '''
        return f"{display_value(raw, info):g}" if entry.register_byte >= 2 else str(raw)

    def enable_write(self) -> None:
        '''
        Called on every connect when the section asks for writes. Switches write_enabled on only if the layers 1 to 4 of
        "Write safety" pass: a writable row and an allow_* opt-in exist, and at least 90 % of the probes of the rows that
        could be written pass (the panel frame reports a switch register and limits; each setting is supported and holds a
        value inside the device's own limits). write_type = relaxed accepts a lower score; unsafe skips all of it.
        '''
        mode: TransportWriteMode = self._mode()
        if self._write_requested and mode == TransportWriteMode.UNSAFE:
            self.write_enabled = True
            self._log.warning("enable write - WARNING - UNSAFE MODE - validation SKIPPED")
            return

        self._log.info("Validating Protocol for Writing")
        self.write_enabled = False
        rows: list[registry_map_entry] = self._writable_entries()
        if not rows:
            self._log.error("enable write FAILED - no registry rows are marked W in this protocol - WRITE DISABLED")
            return
        if not (self._allow_panel_writes or self._allow_setting_writes):
            self._log.error("enable write FAILED - neither allow_panel_writes nor allow_setting_writes is set - WRITE DISABLED")
            return

        probes: list[bool] = []
        panel_probed: bool = False
        for entry in rows:
            if entry.register == self.CMD_PANEL:
                if self._allow_panel_writes and not panel_probed:
                    panel_probed = True
                    probes.append(self._probe_panel())
            elif entry.register >> 8 == self.W_READ_SETTING:
                if self._allow_setting_writes:
                    probes.append(self._probe_setting(entry.register & 0xFF))
            else:
                probes.append(False)  # a W row nothing here can write
        # (rows of a capability that is not allowed are neither probed nor counted: write_data refuses them)
        score: float = 100.0 * sum(probes) / len(probes) if probes else 0.0

        if score > self.WRITE_VALIDATION_SCORE:
            self.write_enabled = True
            self._log.info(f"enable write - validation passed ({sum(probes)} of {len(probes)} probes)")
        elif mode == TransportWriteMode.RELAXED:
            self.write_enabled = True
            self._log.warning(f"enable write - WARNING - RELAXED MODE - {sum(probes)} of {len(probes)} probes passed; every write is still checked")
        else:
            self._log.error(f"enable write FAILED - {sum(probes)} of {len(probes)} probes passed - WRITE DISABLED")

    def _probe_panel(self) -> bool:
        try:
            panel: PanelState = parse_master_multi_led(self._master_multi_led(0.0))
        except (_Silent, _Refused):
            return False
        return panel.switch != 0 and not panel.dmc_dedicated and panel.minimum <= panel.actual <= panel.maximum

    def _probe_setting(self, setting : int) -> bool:
        if setting in self.FLAG_SETTINGS:
            return False
        try:
            self._setting_scale(setting)
            low, high = self._setting_limit(setting)
            return low <= self._raw_setting(setting) <= high
        except (_Silent, _Unsupported):
            return False


    # ------------------------------------------------------------------ #
    # polling                                                              #
    # ------------------------------------------------------------------ #

    def _dispatch(self, command : int) -> bytes:
        ''' maps one register number of the map to its exchange; raises _Unsupported or _Silent '''
        kind: int = command >> 8
        argument: int = command & 0xFF
        if command == self.CMD_VERSION:
            return self._read_version()
        if command == self.CMD_LED:
            return self._read_led()
        if command == self.CMD_SOFTWARE_VERSION:
            return self._read_software_version()
        if command == self.CMD_DEVICE_STATE:
            return self._read_device_state()
        if command == self.CMD_INFO_DC:
            return self._read_dc_info()
        if self.CMD_INFO_AC_L1 <= command <= self.CMD_INFO_AC_L4:
            if self._phase_absent(argument):
                raise _Unsupported
            return self._read_ac_info(argument)
        if command == self.CMD_MASTER_MULTI_LED:
            return self._read_master_multi_led()
        if command == self.CMD_PANEL:
            return self._read_panel_state()
        if kind == self.W_READ_RAM_VAR:
            return self._read_ram_variable(argument)
        if kind == self.W_READ_SETTING:
            return self._read_setting(argument)
        self._log.warning(f"serial_vebus has no command 0x{command:04X}; row ignored")
        raise _Unsupported

    def _reset_connection_state(self) -> None:
        ''' forgets everything learned about the device: it may have been replaced, or the interface power cycled '''
        self._ram_info.clear()
        self._setting_info.clear()
        self._phases = None
        self._master_multi_led_cache = None
        self._setting_limits.clear()
        if self._client is not None and isinstance(self._client, serial_mk2_client):
            with self._get_port_lock():
                self._client.current_address = None

    def _read_command(self, command : int) -> bytes | None:
        '''
        One map command: its normalized payload, or None. Never raises: a missing, garbled or unsupported answer is
        logged and the row is skipped. self._last_outcome says which of those it was.
        '''
        with self._lock:
            try:
                payload: bytes = self._dispatch(command)
            except _Unsupported:
                self._last_outcome = "unsupported"
                self._log.debug(f"VE.Bus device {self.address} does not support command 0x{command:04X}")
                return None
            except _Silent:
                self._last_outcome = "silent"
                self._log.debug(f"VE.Bus device {self.address} did not answer command 0x{command:04X}")
                return None
            except (serial.SerialException, OSError, ValueError) as err:
                self._log.warning(f"Serial error on {self.port}: {err}; the port will be reopened")
                self._last_outcome = "silent"
                self.connected = False
                self._release_client()
                return None
            except Exception:
                self._log.exception(f"Unexpected error reading VE.Bus command 0x{command:04X}; row skipped")
                self._last_outcome = "silent"
                return None

            self._last_outcome = "ok"
            return payload

    def _handshake(self) -> bool:
        '''
        On connect: forget what was learned about the device, select its address and ask for its software
        version, which proves a VE.Bus device answers at that address. True when it did.
        '''
        with self._lock:
            self._reset_connection_state()
            try:
                version: bytes = self._read_software_version()
            except _Silent:
                self._log.warning(f"No VE.Bus device answered at address {self.address} on {self.port}")
                return False
            except _Unsupported:
                return True
            except (serial.SerialException, OSError, ValueError) as err:
                self._log.warning(f"Serial error on {self.port}: {err}")
                return False
            self._log.info(f"VE.Bus device {self.address} software version {struct.unpack('>I', version)[0]}")
            if self._write_requested:
                self.enable_write()
            return True

    def _poll_commands_iter(self) -> Iterator[tuple[int, bytes | None]]:
        '''
        As serial_base's, with three differences: the device is (re)connected once at the start of a cycle, a device
        that says it does not support a command is not a failed command (the cycle stays complete), and the cycle is
        abandoned after MAX_CONSECUTIVE_FAILURES silent commands in a row, so an absent device costs a few timeouts, not one per row.
        '''
        commands: list[int] = self._poll_commands()
        self._cycle_expect_unit(len(commands))

        if not self.connected and not self.connect():
            for command in commands:
                self.cycle_mark_incomplete()
                yield command, None
            return

        silent: int = 0
        for command in commands:
            if silent >= self.MAX_CONSECUTIVE_FAILURES:
                self.cycle_mark_incomplete()
                yield command, None
                continue

            payload: bytes | None = self._read_command_cached(command)
            if self._last_outcome == "silent":
                silent += 1
                self.cycle_mark_incomplete()
                if silent >= self.MAX_CONSECUTIVE_FAILURES:
                    self._log.warning(f"VE.Bus device {self.address} stopped answering; the rest of this cycle is skipped and it will reconnect")
                    self.connected = False
            else:
                silent = 0
                self._cycle_mark_unit_complete()
            yield command, payload

