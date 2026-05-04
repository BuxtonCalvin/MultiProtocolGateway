# Scraper for EG4 LL-S rack batteries via Modbus TCP through a Waveshare RS485 bridge,
# inheriting from modbus_tcp but adding EG4-specific decode logic on top of the TCP transport.
"""
EG4 LL-S Rack Battery - Modbus TCP Transport (via Waveshare RS485 bridge)
=========================================================================

Connects to a Waveshare (or similar) serial-to-TCP bridge that tunnels
RS485 Modbus RTU to Modbus TCP. pymodbus handles all framing and CRC
transparently — this class adds only EG4-specific decode logic on top of
modbus_tcp.

Waveshare device setup (via its web interface):
  - Work Mode      : TCP Server
  - Baud Rate      : 19200
  - Data Bits      : 8
  - Stop Bits      : 1
  - Parity         : None
  - Local Port     : 502 (default; match config `port`)
  - Protocol       : Modbus TCP <-> Modbus RTU (if supported by your model)
                     Otherwise "None/Transparent" also works with pymodbus

These commands ask each battery for its Voltage (Register 0). The Waveshare bridge transparently converts Modbus
TCP requests from pymodbus into Modbus RTU commands on the RS485 bus, and then converts the RTU responses back
into TCP responses for pymodbus to parse. You can use these same commands with a direct RS485-to-USB adapter
(bypassing the Waveshare) to verify the battery addresses and responses before connecting via TCP.

Battery	ID	Hex Command to Send
Battery 1	01	01 03 00 00 00 01 84 0A
Battery 2	02	02 03 00 00 00 01 84 39
Battery 3	03	03 03 00 00 00 01 85 E8
Battery 4	04	04 03 00 00 00 01 84 5F
Battery 5	05	05 03 00 00 00 01 85 8E
Battery 6	06	06 03 00 00 00 01 85 BD

Implementation Steps
  Cable Prep: Using a standard T568B Ethernet cable, identify the brown-white (Pin 7), brown (Pin 8) and orange (Pin 2) wires .
  Converter Connection: Connect the White/Brown wire to the B+ (or TX+/RX+) terminal on your USB-A to RS-485 converter.
  Converter Connection: Connect the Brown wire to the A- (or TX-/RX-) terminal on your converter.
  Converter Connection: Connect the Orange wire to the ground terminal on your converter.
  Battery Setup: Set the battery's dipswitch to ID: 64 (all dips ON) to enable communication.

Config keys (in addition to modbus_tcp / transport_base keys):
  host             - Waveshare IP address
  port             - Waveshare TCP port, default 4196
  slave_id         - Battery DIP switch address (2-64 for full BMS data)
  protocol_version - must be set to eg4_ll_s

Sequential mode set in the config file is mandatory. Since all 4 transports
  share one physical RS485 bus through the Waveshare, only one Modbus request
  can be in flight at a time. If two transports poll concurrently, their requests
  collide on the wire and both get garbage responses. Make sure read_mode is set to "sequential" or "interleaved" (not "concurrent")
  for all EG4 battery transports in your gateway config. The modbus_tcp shared client dictionary already serializes
  access per TCP connection via the port lock, but sequential mode at the gateway level
  is cleaner and avoids queuing delays.

Per current ll-s firmware, DIP switch address 1 is special — when the master battery is set to address 1,
  the EG4 firmware activates its inverter closed-loop protocol mode. In this mode the battery responds on
  slave ID 1 with a reduced register map containing only what the inverter needs: pack voltage, current, SOC,
  and charge/discharge limits. The individual cell voltages, per-sensor temperatures, cycle count, protection
  thresholds, and everything in the holding registry map are simply not available at that address.

So the trade-off is:

  DIP = 1 → inverter closed-loop comms work, full BMS data unavailable via RS485
  DIP = 2-64 → full BMS data available, but the battery won't act as the RS485 master for inverter comms

  If you're using CAN for inverter closed-loop, the master battery's RS485 port is completely free and uninvolved
  in inverter comms. You can set all four batteries to DIP addresses 2, 3, 4, 5 and poll all of them for full data
  without any conflict. Address 1 simply goes unused, which is why there's no [eg4_battery_1] section — there's nothing
  at that address to talk to.

If you were using RS485 for inverter comms instead of CAN, you're stuck with partial data from address 1 on the inverter bus,
and you'd need a second independent RS485 connection directly to each battery's other RS485 port to get full data — which is
exactly the messier situation we discussed earlier.
"""

from classes.protocol_settings import Registry_Type, registry_map_entry
from classes.transports.modbus_tcp import modbus_tcp
from defs.common import TransportSettings

# ---------------------------------------------------------------------------
# Status / protection decode tables  (identical to eg4_ll_s_transport)
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

_WARNING_BITS: dict[int, str] = _PROTECTION_BITS


class modbus_eg4_ll_s_tcp(modbus_tcp):
    """
    EG4 LL-S battery transport over Modbus TCP (Waveshare RS485 bridge).

    Inherits all connection, retry, and register-read logic from modbus_tcp.
    Adds EG4-specific decode: holding register startup load, status/protection
    text decoding, cell voltage stats, and balancing state inference.
    """

    # --- EG4-specific state ---
    _holding_cache: dict[str, int | float | str]
    _holding_loaded: bool

    def __init__(self, settings: TransportSettings) -> None:
        self._holding_cache  = {}
        self._holding_loaded = False
        super().__init__(settings)
        self._slave_id: str = settings.get('slave_id', fallback='')

    @property
    def scrape_target(self) -> str:
        """
        Each battery has a unique slave_id on the shared RS485 bus.
        Including slave_id in the scrape_target ensures each battery
        gets its own scrape group and its own independent read thread,
        rather than being consolidated with other batteries behind the
        same Waveshare bridge.
        """
        base: str = super().scrape_target   # "10.17.2.66:502"
        slave_id = getattr(self, '_slave_id', None)
        if slave_id:
            return f"{base}:{slave_id}"
        return base

    # ------------------------------------------------------------------
    # transport_base interface
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        super().connect()
        # Reset holding cache on reconnect so thresholds are refreshed
        if not self.connected:
            self._holding_loaded = False
            self._holding_cache  = {}
            return False
        else:
            return True

    def read_data(self) -> dict[str, int | float | str]:
        if self.protocolSettings is None:
            self._log.warning("No protocolSettings for %s", self.transport_name)
            return {}

        # --- One-time startup: holding (config/limits) registers ---
        if not self._holding_loaded:
            self._load_holding_registers()

        # --- Per-poll: delegate input register read to modbus_tcp/modbus_base ---
        info: dict[str, int | float | str] = super().read_data()
        if not info:
            return {}

        # --- Merge cached holding register values ---
        info.update(self._holding_cache)

        # --- EG4-specific derived fields ---
        info.update(self._decode_status(info))
        info.update(self._compute_cell_stats(info))
        info.update(self._compute_balancing_state(info))

        return info

    # ------------------------------------------------------------------
    # Holding register startup load
    # ------------------------------------------------------------------

    def _load_holding_registers(self) -> None:
        """
        Read the BMS holding (config) registers once on startup via the
        holding registry map and cache the processed results.

        modbus_tcp.read_registers() with Registry_Type.HOLDING maps to
        pymodbus read_holding_registers() (FC03), which is the same function
        code the EG4 uses for config data — the Waveshare bridge passes it
        through transparently.
        """
        if self.protocolSettings is None:
            self._holding_loaded = True
            return

        holding_map: list[registry_map_entry] = (self.protocolSettings.registry_map.get(Registry_Type.HOLDING, []))

        if not holding_map:
            self._log.warning("No HOLDING registry map defined for %s — protection thresholds and balance config unavailable.", self.transport_name)
            self._holding_loaded = True
            return

        self._log.info("Reading EG4 BMS holding registers for %s (%d entries)...", self.transport_name, len(holding_map))

        # Use modbus_base.read_modbus_registers() — it handles batching,
        # retries, and failure tracking exactly as for input registers.
        raw: dict[int, int] = self.read_modbus_registers(
            ranges=self.protocolSettings.get_registry_ranges(Registry_Type.HOLDING),
            registry_type=Registry_Type.HOLDING,
        )

        if not raw:
            self._log.warning(
                "Could not read HOLDING registers for %s — "
                "balancing inference will use built-in defaults.",
                self.transport_name,
            )
            self._holding_loaded = True
            return

        try:
            self._holding_cache = self.protocolSettings.process_registery(
                raw, holding_map
            )
        except Exception:
            self._log.exception(
                "process_registery failed for HOLDING registers on %s",
                self.transport_name,
            )

        self._holding_loaded = True
        self._log.info(
            "EG4 BMS holding registers loaded for %s: %d values cached.",
            self.transport_name, len(self._holding_cache),
        )

    # ------------------------------------------------------------------
    # Derived field calculations  (identical logic to eg4_ll_s_transport)
    # ------------------------------------------------------------------

    def _decode_status(self, info: dict[str, int | float | str]) -> dict[str, int | float | str]:
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

    def _compute_cell_stats(self, info: dict[str, int | float | str]) -> dict[str, int | float | str]:
        derived: dict[str, int | float | str] = {}
        cell_voltages: list[float] = []

        for i in range(1, 17):
            raw: int | float | str | None = info.get(f"cell_{i:02d}_voltage")
            if raw is not None:
                mv: float = float(raw)
                if mv > 0:
                    cell_voltages.append(mv / 1000)

        if cell_voltages:
            derived["cell_voltage_max_v"]   = round(max(cell_voltages), 3)
            derived["cell_voltage_min_v"]   = round(min(cell_voltages), 3)
            derived["cell_voltage_diff_mv"] = round(
                (max(cell_voltages) - min(cell_voltages)) * 1000, 1
            )

        return derived

    def _compute_balancing_state(self, info: dict[str, int | float | str]) -> dict[str, int | float | str]:
        derived: dict[str, int | float | str] = {}

        cell_min_raw: int | float | str | None = info.get("cell_voltage_min_v")
        cell_max_raw: int | float | str | None = info.get("cell_voltage_max_v")

        if cell_min_raw is None or cell_max_raw is None:
            return derived

        cell_min: float = float(cell_min_raw)
        cell_max: float = float(cell_max_raw)

        balance_voltage: float = float(self._holding_cache.get("balance_voltage",       3.40))
        balance_delta:   float = float(self._holding_cache.get("balance_voltage_delta", 0.040))

        # prevents the "Balancing" status from flickering if a cell touches the threshold and then drops slightly
        # due to the load of the balancing resistor itself.
        ov_release:      float = float(self._holding_cache.get("cell_ov_release",       3.45))

        delta_v: float = round(cell_max - cell_min, 3)

        state: int
        if cell_min < balance_voltage:
            state = 0
        elif cell_min >= ov_release and delta_v <= balance_delta:
            state = 2
        elif delta_v > balance_delta:
            state = 1
        else:
            state = 0

        state_labels: dict[int, str] = {0: "Idle", 1: "Balancing", 2: "Finished"}
        derived["balancing_state"]      = state
        derived["balancing_state_text"] = state_labels[state]

        return derived
