# Description: Bluetooth Low Energy transport for JBD (Jiabaida, Xiaoxiang, Overkill Solar) smart BMS: the JBD UART frames carried over the FF00 notify/write service
# File: serial_bluetooth_jbd.py
#
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

from __future__ import annotations

# JBD smart BMS over Bluetooth Low Energy. The Bluetooth module on a JBD board is a UART bridge, so the
# frames are the ones serial_jbd reads from the UART, and the protocol map is the same one (jbd_bms):
#
#   request    DD  A5  <command>  <length>  <data...>  <checksum:2>  77
#   response   DD  <command>  <status>  <length>  <data...>  <checksum:2>  77
#
# The checksum is 0x10000 minus the sum of every byte from the third byte through the data, high byte
# first. A BLE notification carries 20 bytes at most, so a response arrives in several slices;
# extract_frame joins them by the length byte, never by looking for the end byte, because 0x77 can occur
# inside the data.
#
# Many packs sold under other names use this BMS and this service (Overkill Solar, Xiaoxiang, SBL, Supervolt,
# Lossigy, Nordstrom and others; EG4 12V packs advertise as JBD too). Set protocol_version = jbd_bms. This
# transport only reads; writing to the pack is not implemented.
#
# Some newer packs will not answer until they are given a password (the "secret" of the manufacturer's app).
# It is the password setting, and is sent when the connection is made:
#
#   request    FF  AA  15  <length>  <password, ASCII>  <checksum>
#   response   FF  AA  15  01  <status>  <checksum>             status 0 means accepted
#
# The checksum is the low byte of the sum of the bytes from the third byte on. A pack that does not ask for
# a password is not sent one, so leave the setting empty for it.
from types import SimpleNamespace

from defs.common import TransportSettings

from .serial_bluetooth import serial_bluetooth


class serial_bluetooth_jbd(serial_bluetooth):

    transport_type : str = "scraper"
    ''' declared here, not inherited: the web UI scanner reads this file with ast and does not follow inheritance '''

    SERVICE_UUID : str = "ff00"
    NOTIFY_UUID : str = "ff01"
    WRITE_UUID : str = "ff02"

    START : int = 0xDD
    END : int = 0x77
    READ : int = 0xA5
    ''' the second byte of a read request '''
    STATUS_OK : int = 0x00
    STATUS_ERROR : int = 0x80
    MIN_FRAME : int = 7
    ''' DD, command, status, length, 2 checksum bytes and the end marker, with no data '''
    MAX_FRAME : int = 128
    ''' the largest read reply is 71 bytes (32 cell voltages); a stray start byte with a bigger length is noise '''

    AUTH_HEAD : bytes = b"\xff\xaa"
    COMMAND_AUTH : int = 0x15
    AUTH_REPLY_SIZE : int = 6
    ''' the password exchange has its own framing: FF AA instead of DD '''

    PRUNE_SHORT_PAYLOADS : bool = True
    ''' a pack with fewer cells or temperature sensors than the map has rows for simply has no such variables '''

    def __init__(self, settings : TransportSettings) -> None:
        password: str = settings.get(["password", "secret"], fallback="")
        if not password.isascii() or len(password) > 32:
            msg: str = "JBD password must be ASCII text of at most 32 characters"
            raise ValueError(msg)
        self._password: bytes = password.encode("ascii")
        ''' sent when the connection is made; empty for a pack that does not ask for one. Never logged. '''
        super().__init__(settings)

    @staticmethod
    def calculate_checksum(data : bytes) -> int:
        ''' 0x10000 minus the sum of the bytes, as the 16-bit value the frame carries '''
        return (0x10000 - sum(data)) & 0xFFFF

    @staticmethod
    def extract_frame(buffer : bytearray) -> bytes | None:
        start: int = serial_bluetooth_jbd.START
        head: bytes = serial_bluetooth_jbd.AUTH_HEAD
        while True:
            candidates: list[int] = [position for position in (buffer.find(bytes([start])), buffer.find(head)) if position >= 0]
            if not candidates:
                keep: int = 1 if buffer.endswith(head[:1]) else 0  # the FF of an FF AA may be split across notifications
                del buffer[:len(buffer) - keep]
                return None
            if min(candidates) > 0:
                del buffer[:min(candidates)]

            if buffer[0] == head[0]:
                # the pack's answer to the password
                if len(buffer) < serial_bluetooth_jbd.AUTH_REPLY_SIZE:
                    return None
                if buffer[2] != serial_bluetooth_jbd.COMMAND_AUTH or buffer[3] != 1:
                    del buffer[:1]
                    continue
                reply: bytes = bytes(buffer[:serial_bluetooth_jbd.AUTH_REPLY_SIZE])
                del buffer[:serial_bluetooth_jbd.AUTH_REPLY_SIZE]
                return reply

            if len(buffer) < 4:
                return None

            total: int = buffer[3] + serial_bluetooth_jbd.MIN_FRAME
            if buffer[2] not in (serial_bluetooth_jbd.STATUS_OK, serial_bluetooth_jbd.STATUS_ERROR) or total > serial_bluetooth_jbd.MAX_FRAME:
                del buffer[:1]  # not a real frame start: look for the next one
                continue
            if len(buffer) < total:
                return None
            if buffer[total - 1] != serial_bluetooth_jbd.END:
                del buffer[:1]
                continue

            frame: bytes = bytes(buffer[:total])
            del buffer[:total]
            return frame

    def build_frame(self, command : int, info : bytes = b"") -> bytes:
        if command == self.COMMAND_AUTH:
            auth: bytes = bytes([command, len(info)]) + info
            return self.AUTH_HEAD + auth + bytes([sum(auth) & 0xFF])

        body: bytes = bytes([command, len(info)]) + info
        return bytes([self.START, self.READ]) + body + self.calculate_checksum(body).to_bytes(2, "big") + bytes([self.END])

    def decode_frame(self, raw_frame : bytes) -> SimpleNamespace | None:
        frame: bytes = bytes(raw_frame)

        if len(frame) < self.MIN_FRAME or frame[0] != self.START or frame[-1] != self.END:
            self._log.warning(f"JBD frame has the wrong start, end or size: {frame.hex()}")
            return None

        length: int = frame[3]
        if len(frame) != length + self.MIN_FRAME:
            self._log.warning(f"JBD frame length byte says {length} data bytes but the frame is {len(frame)} bytes: {frame.hex()}")
            return None

        calculated: int = self.calculate_checksum(frame[2:4 + length])
        received: int = int.from_bytes(frame[4 + length:6 + length], "big")
        if calculated != received:
            self._log.warning(f"JBD checksum error, expected {calculated:04X}, got {received:04X}; frame discarded")
            return None

        status: int = frame[2]
        if status != self.STATUS_OK:
            self._log.warning(f"JBD command {frame[1]:#04x} was refused with status {status:#04x}")
            return None

        return SimpleNamespace(command=frame[1], status=status, payload=frame[4:4 + length])

    def _read_command(self, command : int) -> bytes | None:
        # a pack can push a frame of its own, so only the reply that echoes this command is taken
        frame: bytes | None = self._exchange_until(command, lambda candidate: len(candidate) > 1 and candidate[1] == command)
        if frame is None:
            return None

        decoded: SimpleNamespace | None = self.decode_frame(frame)
        return bytes(decoded.payload) if decoded is not None else None

    def _handshake(self) -> bool:
        ''' sends the password, if one is set, and checks that the pack accepted it '''
        if not self._password:
            return True

        reply: bytes | None = self._exchange_until(self.COMMAND_AUTH, lambda candidate: candidate[:2] == self.AUTH_HEAD, self._password)
        if reply is None:
            self._log.warning(f"JBD pack {self.port} did not answer the password")
            return False
        if reply[4] != 0 or sum(reply[2:5]) & 0xFF != reply[5]:
            self._log.error(f"JBD pack {self.port} refused the password; check the password setting")
            return False
        return True
