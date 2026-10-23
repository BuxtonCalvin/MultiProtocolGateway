# Description: listen-only scraper transport for serial communication with SOI/EOI framing, built on serial_base, supporting both synchronous and asynchronous modes.
# File: serial_frame_transport.py
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

# Listen-only scraper transport for serial communication with SOI/EOI framing, supporting
# both synchronous and asynchronous modes.
#
# Port handling, the shared serial client, locking, scheduling and shutdown live in
# serial_base; this module adds what is specific to a listen-only framed protocol: frame
# markers read from the config, one frame per read, and optional background reading.
from collections.abc import Iterator
from typing import Callable

from classes.protocol_settings import Registry_Type, registry_map_entry
from classes.transports.transport_base import TransportWriteMode, transport_base
from defs.common import TransportSettings

from .serial_base import serial_base, serial_frame_client


class serial_frame_transport(serial_base):

    transport_type: str = "scraper"
    """
    Transport that communicates over a serial port using an SOI/EOI framing
    protocol, implemented via serial_frame_client and built on serial_base.

    Required config keys (in addition to transport_base keys):
        port        - serial device path, e.g. /dev/ttyUSB0
        baudrate    - baud rate, e.g. 9600 (also accepted as "baud")
        soi         - start-of-information byte(s) as a hex string, e.g. 0x7E or 7E
        eoi         - end-of-information byte(s) as a hex string, e.g. 0x0D or 0D

    Optional config keys:
        timeout     - read timeout in seconds (default 5.0)
        async_mode  - if true, run the background read thread (default false). The
                      port then belongs to this transport; it cannot be shared.

    The port is opened in connect() rather than at start-up, so a device that is not
    plugged in yet is retried every cycle instead of stopping the gateway.
    """

    OPEN_ON_INIT = False

    # ------------------------------------------------------------------ #
    # Construction                                                         #
    # ------------------------------------------------------------------ #

    def __init__(self, settings: TransportSettings) -> None:
        # serial_base reads port, baudrate/baud and the SOI/EOI markers (see _frame_markers);
        # transport_base loads protocolSettings and device metadata.
        super().__init__(settings)

        self._timeout: float = float(settings.get("timeout", fallback="5.0"))
        self._async_mode: bool = settings.getboolean("async_mode", fallback=False)

    # Attribute names kept for subclasses written against the earlier, standalone version.
    @property
    def _port(self) -> str:
        return self.port

    @property
    def _baud(self) -> int:
        return self.baudrate

    @property
    def _soi(self) -> bytes:
        return self._markers[0]

    @property
    def _eoi(self) -> bytes:
        return self._markers[1]

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_frame_marker(value: str) -> bytes:
        """Convert a hex string like '7E' or '0x7E' to bytes."""
        value = value.strip()
        if value.startswith("0x") or value.startswith("0X"):
            value = value[2:]
        try:
            return bytes.fromhex(value)
        except ValueError:
            # Fallback: treat as a literal ASCII/UTF-8 string.
            return value.encode("utf-8")

    def _frame_markers(self, settings: TransportSettings) -> tuple[bytes, bytes]:
        # SOI / EOI may be provided as hex strings ("7E", "0x7E") or raw chars.
        return (self._parse_frame_marker(settings.get("soi", fallback="7E")),
                self._parse_frame_marker(settings.get("eoi", fallback="0D")))

    def _serial_parameters(self) -> dict[str, object]:
        # pyserial defaults (8N1) and no exclusive lock, as before this transport used serial_base
        return {}

    # ------------------------------------------------------------------ #
    # transport_base interface                                             #
    # ------------------------------------------------------------------ #

    def _prepare_client(self, client: serial_frame_client) -> None:
        """Apply the read timeout and, in async mode, hook up the frame callback."""
        client.timeout = self._timeout
        client.asynchronous = self._async_mode

        if self._async_mode:
            owner: Callable[[bytes], None] | None = client.on_message
            if owner is not None and owner != self._on_async_frame:
                msg = f"async_mode needs serial port {self.port} to itself, but another section is already listening on it"
                raise ValueError(msg)
            client.on_message = self._on_async_frame

    def _handshake(self) -> bool:
        self._log.info("Connected to serial port %s @ %d baud (async=%s)", self.port, self.baudrate, self._async_mode)
        return True

    def interleaved_cycle_timeout(self) -> float:
        """One frame per read, so the cycle is bounded by a single read timeout, not by the registry size."""
        return max(transport_base.interleaved_cycle_timeout(self), 2.0 * self._timeout)

    def read_data(self) -> dict[str, int | float | str]:
        """
        Synchronous read: send nothing (pure listen), receive one frame,
        decode it through protocolSettings.

        Override this method in a subclass when your protocol requires
        sending a request command before the device replies.
        """
        if not self.connected or self._client is None:
            if not self.connect():
                return {}

        if self._client is None:
            return {}

        try:
            with self._get_port_lock():
                raw: bytes | list[bytes] | None = self._client.read(reset_buffer=True, frames=1)
        except Exception:
            self._log.exception("Error reading from serial port %s", self.port)
            self.connected = False
            return {}

        if not raw or isinstance(raw, list):
            self._log.debug("No frame received from %s", self.port)
            return {}

        return self._decode_frame(raw)

    def read_data_iter(self) -> Iterator[bool]:
        """Interleaved mode: a read is one frame, so the whole read is a single block (the transport_base default)."""
        return transport_base.read_data_iter(self)

    def write_data(self, data: dict[str, int | float | str], from_transport: "transport_base") -> None:
        """Write registry values to the device as a raw frame."""
        # write_mode only exists when write_type is configured, hence getattr
        if getattr(self, "write_mode", None) in (None, TransportWriteMode.READ):
            self._log.debug("Write skipped — transport is in READ mode.")
            return

        if not self.connected or self._client is None:
            if not self.connect():
                return

        payload: bytes | None = self._encode_frame(data)
        if payload:
            try:
                if self._client is not None:
                    with self._get_port_lock():
                        self._client.write(payload)
                    self._log.debug("Wrote %d bytes to %s", len(payload), self.port)
            except Exception:
                self._log.exception("Error writing to serial port %s", self.port)
                self.connected = False

    # ------------------------------------------------------------------ #
    # Frame encoding / decoding                                           #
    # ------------------------------------------------------------------ #

    def _decode_frame(self, raw: bytes) -> dict[str, int | float | str]:
        """
        Convert raw frame bytes into a named-value dict using protocolSettings.

        The base implementation treats the frame as a flat byte array keyed by
        byte index.  Override this in a subclass for protocol-specific parsing
        (e.g. checksum validation, command-byte stripping, etc.).
        """
        if not self.protocolSettings:
            # No protocol map — return the raw hex string under a generic key.
            return {"raw_frame": raw.hex()}

        registry: dict[int, bytes] = {i: bytes([b]) for i, b in enumerate(raw)}

        registry_type: Registry_Type = Registry_Type.INPUT
        reg_map: list[registry_map_entry] = self.protocolSettings.registry_map.get(registry_type, [])

        if not reg_map:
            return {"raw_frame": raw.hex()}

        try:
            info: dict[str, int | float | str] = self.protocolSettings.process_registery(registry, reg_map)
        except Exception:
            self._log.exception("process_registery failed for frame: %s", raw.hex())
            return {"raw_frame": raw.hex()}

        # Fire the on_message callback for each decoded entry (matching transport_base convention).
        if self.on_message:
            entry_map: dict[str, registry_map_entry] = {e.variable_name: e for e in reg_map}
            for key, value in info.items():
                entry: registry_map_entry | None = entry_map.get(key)
                if entry:
                    try:
                        self.on_message(self, entry, str(value))
                    except Exception:
                        self._log.exception("on_message callback raised for key '%s'", key)

        return info

    def _encode_frame(self, data: dict[str, int | float | str]) -> bytes | None:
        """
        Convert a registry dict to raw bytes for transmission.

        The base implementation is intentionally minimal — override this in a
        subclass to add command bytes, checksums, etc.
        """
        if not data:
            return None

        # Default: concatenate each entry's register address as a single byte.
        # This is a placeholder; real protocols need a proper encoder.
        payload = bytearray()
        for entry in data.values():
            if isinstance(entry, registry_map_entry):
                payload += entry.register.to_bytes(2, byteorder="big")
        return bytes(payload) if payload else None

    # ------------------------------------------------------------------ #
    # Async support                                                        #
    # ------------------------------------------------------------------ #

    def _on_async_frame(self, raw: bytes) -> None:
        """
        Callback wired to serial_frame_client.on_message in async mode.
        Decodes the frame and fires transport_base.on_message for each field.
        """
        self._decode_frame(raw)  # on_message callbacks are fired inside _decode_frame
