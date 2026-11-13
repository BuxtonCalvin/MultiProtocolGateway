# Description: scraper transport for Victron VE.Direct devices (MPPT solar chargers, BMV battery monitors, inverters) using the VE.Direct text protocol
# File: serial_vedirect.py
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

# VE.Direct devices push a block of text about once a second, with nothing to ask for:
#
#   \r\n<label>\t<value>\r\n<label>\t<value> ... \r\nChecksum\t<one byte>
#
# The block is valid when every byte from the opening \r\n through the checksum byte sums to zero
# (modulo 256). The same wire also carries HEX messages (":" to a newline), which can arrive at any
# moment; they are not part of the text block and are left out of the checksum. A ":" that is itself
# the checksum byte must not be taken for the start of one.
#
# vedirect_client is a port of Victron's reference state machine for that, so the two agree on every
# edge case, including that a HEX message arriving in the middle of a block spoils that block (the
# checksum restarts) rather than being skipped. Records are matched on the protocol's register_key
# (the label), and each value is encoded by the data type the protocol map declares for it.
#
# This framing has no start or end markers to scan for, so it is its own family under serial_base.
# The transport only reads: the HEX protocol (commands, settings) is not implemented.
import time
from enum import Enum
from typing import Any

import serial

from classes.protocol_settings import Data_Type, registry_map_entry
from defs.common import TransportSettings

from .serial_base import SerialClient, serial_base


class vedirect_state(Enum):
    IDLE = 0
    RECORD_BEGIN = 1
    RECORD_NAME = 2
    RECORD_VALUE = 3
    CHECKSUM = 4
    RECORD_HEX = 5


class vedirect_client():
    '''
    The wire layer for VE.Direct text blocks: one instance per physical serial port. Feed it the
    byte stream and it returns each block whose checksum is valid, as the records only (the
    Checksum record is removed). Knows nothing about protocols or settings.
    '''

    timeout : float = 5
    ''' seconds to wait for a valid block '''

    READ_SLICE : float = 0.05
    ''' seconds the port waits for data on each read, so the overall timeout is honored '''

    MAX_FRAME : int = 4096
    ''' a block larger than this is noise; start again '''
    MAX_NAME : int = 32
    ''' longest label kept while looking for the Checksum record '''

    client : serial.Serial
    ''' the serial port '''

    def __init__(self, port : str, baud : int, **kwrgs : Any) -> None:
        self.port: str = port
        self.baud: int = baud
        self._state: vedirect_state = vedirect_state.IDLE
        self._checksum: int = 0
        self._frame: bytearray = bytearray()
        self._name: bytearray = bytearray()
        self._records_end: int = 0
        self._pending: bytearray = bytearray()
        self.client = serial.Serial(port, baud, timeout=self.READ_SLICE, **kwrgs)

    def connect(self) -> bool:
        ''' nothing to start: the device speaks on its own and blocks are read on demand '''
        return True

    def close(self) -> None:
        ''' release the serial port; safe to call more than once '''
        if self.client.is_open:
            self.client.close()

    def write(self, data : bytes) -> None:
        ''' send bytes (for the HEX protocol, which this transport does not use) '''
        self.client.write(data)

    def _reset(self) -> None:
        self._state = vedirect_state.IDLE
        self._checksum = 0
        self._frame.clear()
        self._name.clear()
        self._records_end = 0

    def feed(self, byte : int) -> bytes | None:
        '''
        Advances the state machine by one byte. Returns the records of a block when this byte
        completes a block whose checksum is valid, otherwise None (including when the block was
        complete but its checksum was wrong).
        '''
        if byte == 0x3A and self._state is not vedirect_state.CHECKSUM:  # ":" starts a HEX message
            self._state = vedirect_state.RECORD_HEX

        if self._state is not vedirect_state.RECORD_HEX:
            self._checksum = (self._checksum + byte) & 0xFF
            self._frame.append(byte)
            if len(self._frame) > self.MAX_FRAME:
                self._reset()
                return None

        state: vedirect_state = self._state
        if state is vedirect_state.IDLE:
            if byte == 0x0A:
                self._state = vedirect_state.RECORD_BEGIN
        elif state is vedirect_state.RECORD_BEGIN:
            self._name = bytearray([byte])
            self._state = vedirect_state.RECORD_NAME
        elif state is vedirect_state.RECORD_NAME:
            if byte == 0x09:
                if bytes(self._name).upper() == b"CHECKSUM":
                    # the record began with \r\n, so the records end before that
                    self._records_end = max(0, len(self._frame) - 1 - len(self._name) - 2)
                    self._state = vedirect_state.CHECKSUM
                else:
                    self._state = vedirect_state.RECORD_VALUE
            elif len(self._name) < self.MAX_NAME:
                self._name.append(byte)
        elif state is vedirect_state.RECORD_VALUE:
            if byte == 0x0A:
                self._state = vedirect_state.RECORD_BEGIN
        elif state is vedirect_state.CHECKSUM:
            valid: bool = self._checksum == 0
            records: bytes = bytes(self._frame[:self._records_end])
            self._reset()
            return records if valid else None
        elif byte == 0x0A:  # the end of a HEX message: the checksum restarts and the block in progress is lost
            self._reset()
        return None

    def read(self, reset_buffer : bool = True, frames : int = 1) -> bytes | None:
        '''
        Wait for one valid text block and return its records, or None if none arrived within the
        timeout. reset_buffer discards what the port has already buffered and any block in
        progress, so the block returned is a fresh one rather than seconds old.
        '''
        if reset_buffer:
            self.client.reset_input_buffer()
            self._reset()
            self._pending.clear()

        deadline: float = time.monotonic() + self.timeout
        while True:
            consumed: int = 0
            for byte in self._pending:
                consumed += 1
                records: bytes | None = self.feed(byte)
                if records is not None:
                    del self._pending[:consumed]
                    return records
            self._pending.clear()

            if time.monotonic() >= deadline:
                return None
            self._pending += self.client.read(max(1, self.client.in_waiting))


class serial_vedirect(serial_base):

    transport_type : str = "scraper"
    ''' declared here, not inherited: the web UI scanner reads this file with ast and does not follow inheritance '''

    LISTEN : int = 0
    ''' the single pseudo-command a read uses: the device speaks on its own '''

    DEFAULT_BAUDRATE : int = 19200
    MAX_ADDRESS : int = 0
    ''' VE.Direct is point to point: there is no address '''
    OPEN_ON_INIT : bool = False
    EXCLUSIVE_PORT : bool = True

    TEXT_TYPES : tuple[Data_Type, ...] = (Data_Type.ASCII, Data_Type.STRING, Data_Type.STRING16, Data_Type.STRING32)
    SIGNED_TYPES : tuple[Data_Type, ...] = (Data_Type.SHORT, Data_Type.INT, Data_Type.INT64)

    def __init__(self, settings : TransportSettings) -> None:
        # Set first: serial_base opens the port during construction when OPEN_ON_INIT is True.
        self._timeout: float = float(settings.get("timeout", fallback="5.0"))
        super().__init__(settings)

    # ------------------------------------------------------------------ #
    # serial_base interface                                                #
    # ------------------------------------------------------------------ #

    def _create_client(self) -> vedirect_client:
        return vedirect_client(self.port, self.baudrate, **self._serial_parameters())

    def _check_shared_client(self, shared : SerialClient) -> None:
        if not isinstance(shared, vedirect_client):
            # ValueError, not TypeError: serial_base.connect() treats it as "cannot share this port" and retries
            msg: str = f"{type(self).__name__} on {self.port} cannot share a client that does not read VE.Direct blocks"
            raise ValueError(msg)  # noqa: TRY004

    def _prepare_client(self, client : SerialClient) -> None:
        if not isinstance(client, vedirect_client):
            msg: str = f"{type(self).__name__} needs a vedirect_client, got {type(client).__name__}"
            raise TypeError(msg)
        client.timeout = self._timeout

    def _client_timeout(self) -> float:
        return self._timeout

    # ------------------------------------------------------------------ #
    # reading                                                              #
    # ------------------------------------------------------------------ #

    def _poll_commands(self) -> list[int]:
        ''' nothing is requested: one read waits for one block, however big the map is '''
        return [self.LISTEN]

    def _read_command(self, command : int) -> bytes | None:
        return self._listen()

    @staticmethod
    def parse_records(block : bytes) -> dict[str, str]:
        ''' the block's records as label -> value, with labels upper-cased (VE.Direct labels are case-insensitive) '''
        records: dict[str, str] = {}
        for line in block.split(b"\r\n"):
            label, separator, value = line.partition(b"\t")
            if separator and label:
                records[label.decode("ascii", "replace").upper()] = value.decode("ascii", "replace")
        return records

    def _encode_value(self, entry : registry_map_entry, text : str) -> bytes | None:
        '''
        The bytes the protocol map decodes this entry's value from, or None if the text is not a
        value of that type (a numeric field that reads "---", for example).
        '''
        data_type: Data_Type = entry.data_type
        if data_type in self.TEXT_TYPES:
            return text.encode("ascii", "replace")

        try:
            if data_type is Data_Type.HEX:
                number: int = int(text, 16)
                return number.to_bytes(max(1, (number.bit_length() + 7) // 8), "big")

            number = int(text)
            size: int = max(1, Data_Type.getSize(data_type) // 8)
            return number.to_bytes(size, "big", signed=data_type in self.SIGNED_TYPES)
        except (ValueError, OverflowError):
            return None

    def _process_raw(self, raw_data : dict[int, bytes]) -> dict[str, int | float | str]:
        if self.protocolSettings is None:
            return {}

        block: bytes | None = raw_data.get(self.LISTEN)
        if not block:
            return {}

        records: dict[str, str] = self.parse_records(block)
        entries: list[registry_map_entry] = self._registry_map()

        values: dict[int, bytes] = {}
        for entry in entries:
            if entry.description_source or not entry.register_key:
                continue
            text: str | None = records.get(entry.register_key.upper())
            if text is None:
                continue
            encoded: bytes | None = self._encode_value(entry, text)
            if encoded is not None:
                values[entry.register] = encoded

        return self.protocolSettings.process_registery(values, registry_map=entries) if values else {}
