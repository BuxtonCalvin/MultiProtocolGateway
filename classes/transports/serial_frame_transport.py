# Description: scraper transport for serial communication with SOI/EOI framing, supporting both synchronous and asynchronous modes.
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

# scraper transport for serial communication with SOI/EOI framing, supporting both synchronous and asynchronous modes.
from classes.protocol_settings import Registry_Type, registry_map_entry
from classes.transports.serial_frame_client import serial_frame_client
from classes.transports.transport_base import TransportWriteMode, transport_base
from defs.common import TransportSettings


class serial_frame_transport(transport_base):
    transport_type = "scraper"
    """
    Transport that communicates over a serial port using an SOI/EOI framing
    protocol, implemented via serial_frame_client.

    Required config keys (in addition to transport_base keys):
        port        - serial device path, e.g. /dev/ttyUSB0
        baud        - baud rate, e.g. 9600
        soi         - start-of-information byte(s) as a hex string, e.g. 0x7E or 7E
        eoi         - end-of-information byte(s) as a hex string, e.g. 0x0D or 0D

    Optional config keys:
        timeout     - read timeout in seconds (default 5.0)
        async_mode  - if true, run the background read thread (default false)
    """

    _client: serial_frame_client | None = None

    # ------------------------------------------------------------------ #
    # Construction                                                         #
    # ------------------------------------------------------------------ #

    def __init__(self, settings: TransportSettings) -> None:
        # Pull serial-specific settings before calling super().__init__,
        # because super raises if protocolSettings cannot be loaded.
        self._port: str    = settings.get("port", fallback="/dev/ttyUSB0")
        self._baud: int    = int(settings.get("baud", fallback="9600"))
        self._timeout: float = float(settings.get("timeout", fallback="5.0"))
        self._async_mode: bool = settings.getboolean("async_mode", fallback=False)

        # SOI / EOI may be provided as hex strings ("7E", "0x7E") or raw chars.
        self._soi: bytes = self._parse_frame_marker(settings.get("soi", fallback="7E"))
        self._eoi: bytes = self._parse_frame_marker(settings.get("eoi", fallback="0D"))

        # transport_base.__init__ loads protocolSettings and device metadata.
        super().__init__(settings)

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

    # ------------------------------------------------------------------ #
    # transport_base interface                                             #
    # ------------------------------------------------------------------ #

    def connect(self) -> bool:
        """Open the serial port and (optionally) start the async read thread."""
        try:
            self._client = serial_frame_client(port=self._port, baud=self._baud, soi=self._soi, eoi=self._eoi)
            self._client.timeout    = self._timeout
            self._client.asynchronous = self._async_mode

            if self._async_mode:
                self._client.on_message = self._on_async_frame

            result = self._client.connect()
            self.connected = bool(result)

            if self.connected:
                self._log.info("Connected to serial port %s @ %d baud (async=%s)", self._port, self._baud, self._async_mode)
            else:
                self._log.warning("serial_frame_client.connect() returned falsy for %s", self._port)

        except Exception:
            self._log.exception("Failed to connect to serial port %s", self._port)
            self.connected = False

        return self.connected


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
            raw: bytes | list[bytes] | None = self._client.read(reset_buffer=True, frames=1)
        except Exception:
            self._log.exception("Error reading from serial port %s", self._port)
            self.connected = False
            return {}

        if not raw or isinstance(raw, list):
            self._log.debug("No frame received from %s", self._port)
            return {}

        return self._decode_frame(raw)

    def write_data(self, data: dict[str, int | float | str], from_transport: "transport_base") -> None:
        """Write registry values to the device as a raw frame."""
        if self.write_mode in (None, TransportWriteMode.READ):
            self._log.debug("Write skipped — transport is in READ mode.")
            return

        if not self.connected or self._client is None:
            if not self.connect():
                return

        payload = self._encode_frame(data)
        if payload:
            try:
                if self._client is not None:
                    self._client.write(payload)
                    self._log.debug("Wrote %d bytes to %s", len(payload), self._port)
            except Exception:
                self._log.exception("Error writing to serial port %s", self._port)
                self.connected = False

    def cleanup(self) -> None:
        """Stop the async thread (if running) and close the serial port."""
        self._log.debug("Cleaning up serial_frame_transport %s", self.transport_name)

        if self._client is not None:
            try:
                # Stop the background thread if async mode was used.
                self._client.running = False

                if hasattr(self._client, "thread") and self._client.thread is not None:
                    self._client.thread.join(timeout=2.0)

                # Close the underlying pyserial port.
                if self._client.client and self._client.client.is_open:
                    self._client.client.close()
                    self._log.info("Serial port %s closed.", self._port)

            except Exception:
                self._log.exception("Error during cleanup of %s", self._port)
            finally:
                self._client = None

        self.connected = False
        super().cleanup()

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

        registry_type = Registry_Type.INPUT
        reg_map = self.protocolSettings.registry_map.get(registry_type, [])

        if not reg_map:
            return {"raw_frame": raw.hex()}

        try:
            info = self.protocolSettings.process_registery(registry, reg_map)
        except Exception:
            self._log.exception("process_registery failed for frame: %s", raw.hex())
            return {"raw_frame": raw.hex()}

        # Fire the on_message callback for each decoded entry (matching transport_base convention).
        if self.on_message:
            entry_map: dict[str, registry_map_entry] = {e.variable_name: e for e in reg_map}
            for key, value in info.items():
                entry = entry_map.get(key)
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
