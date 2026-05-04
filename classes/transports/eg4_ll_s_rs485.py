# Scraper for EG4 LL-S rack batteries via RS485 Modbus RTU, using serial_frame_client for the physical
# connection but handling Modbus framing and retries here.
"""
EG4 LL-S Rack Battery - RS485 Modbus RTU Transport
====================================================

Protocol notes (from EG4-LL-MODBUS-Communication-Protocol and community
reverse-engineering):

  - Physical layer : RS485, 9600 baud, 8N1
  - Application    : Modbus RTU (function code 0x03 - Read Input and Holding Registers)
  - Slave address  : Set by DIP switch on each battery (1-64).
                     Master battery for inverter comms is typically ID 1.
                     For direct PC/gateway polling of individual modules,
                     use their assigned DIP-switch ID.
  - Frame          : standard Modbus RTU - [ADDR][FC][REG_HI][REG_LO][CNT_HI][CNT_LO][CRC_LO][CRC_HI]
  - CRC            : CRC-16/IBM (poly 0xA001, init 0xFFFF) - standard Modbus CRC
  - Inter-frame gap: 100 ms recommended by EG4 documentation
  - Register maps  : eg4_ll_s_input_registry_map.csv   (live telemetry, read every poll)
                     eg4_ll_s_holding_registry_map.csv  (BMS config/limits, read once on connect)

Important addressing note
--------------------------
When DIP switches are set to address 1 (master/host for inverter comms),
the battery switches to a reduced register map used for inverter closed-loop
communication. For full BMS data (cell voltages, temps, etc.) address the
battery at its DIP-switch ID (2-64) directly. The config key `slave_id`
controls this.

Multi-battery polling
----------------------
To poll multiple batteries, create one transport per battery in config.cfg,
each with a different `slave_id`. The RS485 bus is shared (daisy-chained
via the battery-to-battery RJ45 cables); only one device may transmit at a
time — use sequential (non-concurrent) mode in Protocol_Gateway.

Config keys (in addition to transport_base keys)
--------------------------------------------------
    port              - serial device, e.g. /dev/ttyUSB0
    baud              - default 9600
    slave_id          - Modbus slave address (DIP switch ID), default 2
    timeout           - read timeout seconds, default 1.0
    inter_frame_gap   - seconds to wait after sending before reading, default 0.1
    retries           - number of retry attempts per register block, default 3
    settling_delay    - seconds to wait after port open for adapter to settle, default 3.0
                        CH341-based USB-RS485 adapters can need up to 40s; the port
                        is kept open between polls so this only applies on first connect.
    protocol_version  - must be set to eg4_ll_s (loads the CSV registry maps)
"""
import struct
import time

from classes.protocol_settings import Registry_Type, registry_map_entry
from classes.transports.serial_frame_client import serial_frame_client
from classes.transports.transport_base import transport_base
from defs.common import TransportSettings

# ---------------------------------------------------------------------------
# CRC-16/IBM  (standard Modbus CRC)
# ---------------------------------------------------------------------------

def _crc16(data: bytes) -> int:
    crc: int = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def _append_crc(frame: bytes) -> bytes:
    crc: int = _crc16(frame)
    return frame + struct.pack("<H", crc)   # little-endian (lo byte first)


def _check_crc(frame: bytes) -> bool:
    if len(frame) < 2:
        return False
    payload, received = frame[:-2], frame[-2:]
    expected: bytes = struct.pack("<H", _crc16(payload))
    return received == expected


# ---------------------------------------------------------------------------
# Status / protection code tables
# Derived from eg4_ll.py lookup_status() and protection bit documentation.
# ---------------------------------------------------------------------------

_STATUS_CODES: dict[str, str] = {
    "0000": "Standby",
    "0100": "Charging",
    "0200": "Discharging",
    "0008": "Protection Active",
    "0800": "Charge Current Limited",
}

_PROTECTION_BITS: dict[int, str] = {
    0:  "Cell Overvoltage",
    1:  "Cell Undervoltage",
    2:  "Pack Overvoltage",
    3:  "Pack Undervoltage",
    4:  "Charge Overcurrent",
    5:  "Discharge Overcurrent",
    6:  "Charge Overtemperature",
    7:  "Discharge Overtemperature",
    8:  "Charge Undertemperature",
    9:  "Discharge Undertemperature",
    10: "MOSFET Overtemperature",
    11: "Short Circuit",
    12: "IC Fault",
    13: "Software Lock",
}

# Warning register uses the same bit layout as protection — pre-fault thresholds
_WARNING_BITS: dict[int, str] = _PROTECTION_BITS

# ---------------------------------------------------------------------------
# SOI/EOI are not used by Modbus RTU; null bytes satisfy serial_frame_client's
# constructor while we bypass its framing entirely.
# ---------------------------------------------------------------------------
_NULL_MARKER: bytes = bytes([0x00])


class eg4_ll_s_rs485(transport_base):
    """
    Transport for EG4 LL-S rack batteries via RS485 Modbus RTU.

    Uses serial_frame_client for the physical serial connection only.
    Modbus framing, CRC, and request/response are handled here.

    Improvements over base serial_frame_transport informed by community
    driver eg4_ll.py (tuxntoast/eg4-ll) https://github.com/tuxntoast/eg4-ll/blob/main/config.ini:
      - Per-request retry loop with buffer flush between attempts
      - Port kept open between polls (avoids CH341 adapter settling penalty)
      - Startup-only holding register read (protection thresholds, balance config)
      - Cell voltage validation (zero-volt dropout filtering)
      - Status / protection / warning hex decoded to human-readable strings
      - Balancing state inferred from cell voltages and config thresholds
    """

    # --- Serial connection ---
    _client: serial_frame_client | None
    _port: str
    _baud: int
    _slave_id: int
    _timeout: float
    _inter_frame_gap: float
    _retries: int
    _settling_delay: float

    # --- Holding register cache ---
    _holding_cache: dict[str, int | float | str]
    ''' BMS config/limit registers, read once on first connect via holding registry map '''
    _holding_loaded: bool

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self, settings: TransportSettings) -> None:
        self._port              = settings.get("port",            fallback="/dev/ttyUSB0")
        self._baud              = int(settings.get("baud",         fallback="9600"))
        self._slave_id          = int(settings.get("slave_id",     fallback="2"))
        self._timeout           = float(settings.get("timeout",    fallback="1.0"))
        self._inter_frame_gap   = float(settings.get("inter_frame_gap", fallback="0.1"))
        self._retries           = int(settings.get("retries",      fallback="3"))
        self._settling_delay    = float(settings.get("settling_delay",  fallback="3.0"))

        self._client         = None
        self._holding_cache  = {}
        self._holding_loaded = False

        super().__init__(settings)

    # ------------------------------------------------------------------
    # transport_base interface
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        # Keep the existing port open between polls — closing and reopening
        # resets the CH341 USB-RS485 adapter settling clock (can take 40+ s).
        if self._client is not None and self._client.client.is_open:
            self.connected = True
            return True

        try:
            self._client = serial_frame_client(
                port=self._port,
                baud=self._baud,
                soi=_NULL_MARKER,
                eoi=_NULL_MARKER,
            )
            self._client.timeout     = self._timeout
            self._client.asynchronous = False
            self._client.client.timeout = self._timeout

            self._log.info(
                "Connected to EG4 LL-S on %s @ %d baud (slave_id=%d) — "
                "waiting %.1fs for adapter to settle",
                self._port, self._baud, self._slave_id, self._settling_delay,
            )
            time.sleep(self._settling_delay)
            self.connected = True

        except Exception:
            self._log.exception("Failed to connect to EG4 LL-S on %s", self._port)
            self.connected = False

        return self.connected

    def read_data(self) -> dict[str, int | float | str]:
        if not self.connected or self._client is None:
            if not self.connect():
                return {}

        if self._client is None:
            return {}

        if self.protocolSettings is None:
            self._log.warning("No protocolSettings for %s", self.transport_name)
            return {}

        # --- One-time startup: read holding (config) registers ---
        if not self._holding_loaded:
            self._load_holding_registers()

        # --- Per-poll: read live telemetry (input) registers ---
        input_map: list[registry_map_entry] = (
            self.protocolSettings.registry_map.get(Registry_Type.INPUT, [])
        )
        if not input_map:
            self._log.warning("No INPUT registry map entries for %s", self.transport_name)
            return {}

        registry: dict[int, int] = {}
        for start_reg, count in self._build_read_windows(input_map):
            block: list[int] | None = self._read_registers(start_reg, count)
            if block is None:
                self._log.warning(
                    "No response from slave %d for INPUT reg 0x%04X count %d",
                    self._slave_id, start_reg, count,
                )
                continue
            for i, value in enumerate(block):
                registry[start_reg + i] = value

        if not registry:
            return {}

        try:
            info: dict[str, int | float | str] = self.protocolSettings.process_registery(
                registry, input_map
            )
        except Exception:
            self._log.exception("process_registery failed for %s", self.transport_name)
            return {}

        # --- Merge holding register values ---
        info.update(self._holding_cache)

        # --- Derived / decoded fields ---
        info.update(self._decode_status(info))
        info.update(self._compute_cell_stats(info))
        info.update(self._compute_balancing_state(info))

        # --- Fire on_message callbacks ---
        if self.on_message:
            entry_map: dict[str, registry_map_entry] = {
                e.variable_name: e for e in input_map
            }
            for key, value in info.items():
                entry: registry_map_entry | None = entry_map.get(key)
                if entry:
                    try:
                        self.on_message(self, entry, str(value))
                    except Exception:
                        self._log.exception("on_message raised for key '%s'", key)

        return info

    def cleanup(self) -> None:
        self._log.debug("Cleaning up eg4_ll_s_transport %s", self.transport_name)
        if self._client is not None:
            try:
                if self._client.client and self._client.client.is_open:
                    self._client.client.close()
                    self._log.info("Serial port %s closed.", self._port)
            except Exception:
                self._log.exception("Error closing %s", self._port)
            finally:
                self._client = None

        self.connected       = False
        self._holding_loaded = False
        self._holding_cache  = {}
        super().cleanup()

    # ------------------------------------------------------------------
    # Holding register (startup config) block
    # ------------------------------------------------------------------

    def _load_holding_registers(self) -> None:
        """
        Read the BMS holding (config) registers once on startup via the
        holding registry map and cache the processed results.

        If no holding map is defined in protocolSettings the method logs a
        warning and marks loading complete so it is not retried every poll.
        """
        if self.protocolSettings is None:
            self._holding_loaded = True
            return

        holding_map: list[registry_map_entry] = (
            self.protocolSettings.registry_map.get(Registry_Type.HOLDING, [])
        )

        if not holding_map:
            self._log.warning(
                "No HOLDING registry map defined for %s — "
                "protection thresholds and balance config will not be available. "
                "Add eg4_ll_s_holding_registry_map.csv to your protocol settings.",
                self.transport_name,
            )
            self._holding_loaded = True
            return

        self._log.info(
            "Reading BMS holding registers from slave %d (%d entries)...",
            self._slave_id, len(holding_map),
        )

        registry: dict[int, int] = {}
        for start_reg, count in self._build_read_windows(holding_map):
            block: list[int] | None = self._read_registers(start_reg, count)
            if block is None:
                self._log.warning(
                    "No response from slave %d for HOLDING reg 0x%04X count %d",
                    self._slave_id, start_reg, count,
                )
                continue
            for i, value in enumerate(block):
                registry[start_reg + i] = value

        if not registry:
            self._log.warning(
                "Could not read any HOLDING registers from slave %d — "
                "balancing inference will use built-in defaults.",
                self._slave_id,
            )
            self._holding_loaded = True
            return

        try:
            self._holding_cache = self.protocolSettings.process_registery(
                registry, holding_map
            )
        except Exception:
            self._log.exception(
                "process_registery failed for HOLDING registers on %s",
                self.transport_name,
            )

        self._holding_loaded = True
        self._log.info(
            "BMS holding registers loaded: %d values cached.",
            len(self._holding_cache),
        )

    # ------------------------------------------------------------------
    # Derived field calculations
    # ------------------------------------------------------------------

    def _decode_status(
        self, info: dict[str, int | float | str]
    ) -> dict[str, int | float | str]:
        """Decode status, protection and warning registers to human-readable strings."""
        derived: dict[str, int | float | str] = {}

        status_raw: int | float | str | None = info.get("battery_status")
        if status_raw is not None:
            hex_str: str = f"{int(status_raw):04X}"
            derived["battery_status_text"] = _STATUS_CODES.get(
                hex_str, f"Unknown (0x{hex_str})"
            )

        for field, bit_table in (
            ("protection_status", _PROTECTION_BITS),
            ("warning_status",    _WARNING_BITS),
        ):
            raw: int | float | str | None = info.get(field)
            if raw is not None:
                active: list[str] = [
                    label for bit, label in bit_table.items()
                    if int(raw) & (1 << bit)
                ]
                derived[f"{field}_text"] = ", ".join(active) if active else "None"

        return derived

    def _compute_cell_stats(
        self, info: dict[str, int | float | str]
    ) -> dict[str, int | float | str]:
        """
        Derive cell_voltage_max_v, cell_voltage_min_v, cell_voltage_diff_mv
        from individual cell voltage registers.

        Filters out zero-volt readings which indicate sensor dropout, not a
        dead cell. Mirrors the validated cell filtering in eg4_ll.py
        read_cell_details().
        """
        derived: dict[str, int | float | str] = {}
        cell_voltages: list[float] = []

        for i in range(1, 17):
            raw: int | float | str | None = info.get(f"cell_{i:02d}_voltage")
            if raw is not None:
                mv: float = float(raw)
                if mv > 0:                          # filter sensor dropouts
                    cell_voltages.append(mv / 1000) # mV → V

        if cell_voltages:
            derived["cell_voltage_max_v"]   = round(max(cell_voltages), 3)
            derived["cell_voltage_min_v"]   = round(min(cell_voltages), 3)
            derived["cell_voltage_diff_mv"] = round(
                (max(cell_voltages) - min(cell_voltages)) * 1000, 1
            )

        return derived

    def _compute_balancing_state(
        self, info: dict[str, int | float | str]
    ) -> dict[str, int | float | str]:
        """
        Infer balancing state from cell voltages and holding register thresholds.
        Returns balancing_state: 0 = idle, 1 = balancing, 2 = finished.

        Logic ported from eg4_ll.py balancingStat(). Uses thresholds loaded
        from the holding registry map; falls back to conservative defaults if
        the holding registers were not available at startup.

          - All cells below balance_voltage          → idle (0)
          - Delta > balance_voltage_delta            → balancing (1)
          - All cells >= cell_ov_release, delta OK   → finished (2)
          - Otherwise                                → idle (0)
        """
        derived: dict[str, int | float | str] = {}

        cell_min_raw: int | float | str | None = info.get("cell_voltage_min_v")
        cell_max_raw: int | float | str | None = info.get("cell_voltage_max_v")

        if cell_min_raw is None or cell_max_raw is None:
            return derived

        cell_min: float = float(cell_min_raw)
        cell_max: float = float(cell_max_raw)

        # Use values from holding cache if available, else safe defaults
        balance_voltage: float = float(
            self._holding_cache.get("balance_voltage", 3.40)
        )
        balance_delta: float = float(
            self._holding_cache.get("balance_voltage_delta", 0.040)
        )
        ov_release: float = float(
            self._holding_cache.get("cell_ov_release", 3.45)
        )

        delta_v: float = round(cell_max - cell_min, 3)

        state: int
        if cell_min < balance_voltage:
            state = 0   # cells too low to enter balancing
        elif cell_min >= ov_release and delta_v <= balance_delta:
            state = 2   # top balance complete
        elif delta_v > balance_delta:
            state = 1   # actively balancing
        else:
            state = 0   # idle / monitoring

        state_labels: dict[int, str] = {0: "Idle", 1: "Balancing", 2: "Finished"}
        derived["balancing_state"]      = state
        derived["balancing_state_text"] = state_labels[state]

        return derived

    # ------------------------------------------------------------------
    # Modbus RTU helpers
    # ------------------------------------------------------------------

    def _read_registers(self, start: int, count: int) -> list[int] | None:
        """
        Send a Modbus FC03 request and return a list of register values,
        or None if all retry attempts fail.

        Retries up to self._retries times, flushing the serial buffer between
        each attempt. This mirrors the retry loop in eg4_ll.py read_eg4ll_command()
        which proved necessary for reliable communication on noisy RS485 buses.
        """
        if self._client is None:
            return None

        serial_port = self._client.client
        request: bytes = _append_crc(
            bytes([self._slave_id, 0x03]) + struct.pack(">HH", start, count)
        )
        expected_len: int = 3 + count * 2 + 2

        for attempt in range(1, self._retries + 1):
            try:
                serial_port.reset_input_buffer()
                serial_port.reset_output_buffer()
                serial_port.write(request)
                self._log.debug(
                    "TX attempt=%d slave=%d reg=0x%04X count=%d : %s",
                    attempt, self._slave_id, start, count, request.hex(),
                )

                time.sleep(self._inter_frame_gap)

                response: bytes = serial_port.read(expected_len)

            except Exception:
                self._log.exception(
                    "Serial I/O error on %s (attempt %d/%d)",
                    self._port, attempt, self._retries,
                )
                self.connected = False
                return None

            if not response:
                self._log.debug(
                    "No response from slave %d reg=0x%04X attempt %d/%d",
                    self._slave_id, start, attempt, self._retries,
                )
                continue

            self._log.debug("RX attempt=%d : %s", attempt, response.hex())

            if len(response) < 5:
                self._log.warning(
                    "Response too short (%d bytes) attempt %d/%d",
                    len(response), attempt, self._retries,
                )
                continue

            # Modbus exception response — won't improve on retry
            if response[1] == 0x83:
                self._log.warning(
                    "Modbus exception from slave %d: code 0x%02X",
                    self._slave_id, response[2],
                )
                return None

            if not _check_crc(response):
                self._log.warning(
                    "CRC mismatch attempt %d/%d: %s",
                    attempt, self._retries, response.hex(),
                )
                continue

            if response[0] != self._slave_id or response[1] != 0x03:
                self._log.warning(
                    "Unexpected header addr=0x%02X fc=0x%02X attempt %d/%d",
                    response[0], response[1], attempt, self._retries,
                )
                continue

            byte_count: int = response[2]
            if byte_count != count * 2:
                self._log.warning(
                    "Byte count mismatch: expected %d got %d attempt %d/%d",
                    count * 2, byte_count, attempt, self._retries,
                )
                continue

            return list(struct.unpack_from(f">{count}H", response, 3))

        self._log.error(
            "All %d attempts failed for slave=%d reg=0x%04X count=%d",
            self._retries, self._slave_id, start, count,
        )
        return None

    @staticmethod
    def _build_read_windows(
        reg_map: list[registry_map_entry],
        max_gap: int = 4,
        max_count: int = 125,
    ) -> list[tuple[int, int]]:
        """
        Collapse a registry map into minimal contiguous Modbus read windows.

        max_gap   : tolerated hole between registers before splitting into a
                    new window — avoids a round-trip for a single missing reg.
        max_count : Modbus spec limit of 125 registers per FC03 request.
        """
        addresses: list[int] = sorted({
            e.register for e in reg_map
            if isinstance(e.register, int)   # skip bit-offset entries e.g. 77.b2
        })

        if not addresses:
            return []

        windows: list[tuple[int, int]] = []
        window_start: int = addresses[0]
        window_end: int   = addresses[0]

        for addr in addresses[1:]:
            if addr - window_end <= max_gap and (addr - window_start + 1) <= max_count:
                window_end = addr
            else:
                windows.append((window_start, window_end - window_start + 1))
                window_start = addr
                window_end   = addr

        windows.append((window_start, window_end - window_start + 1))
        return windows
