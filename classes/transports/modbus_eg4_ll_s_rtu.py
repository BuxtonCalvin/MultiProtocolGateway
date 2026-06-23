# Description: Scraper for EG4 LL rack batteries (PDF V01.06 protocol) via Modbus TCP through a Waveshare RS485 bridge, inheriting from modbus_tcp but adding
# File: modbus_eg4_ll_s_rtu.py
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
# Scraper for EG4 LL rack batteries (PDF V01.06 protocol) via Modbus TCP
# through a Waveshare RS485 bridge, inheriting from modbus_tcp but adding
# EG4-specific derived-field logic on top of the TCP transport.
"""
EG4 LL Battery - Modbus TCP Transport (via Waveshare RS485 bridge)
==================================================================

Connects to a Waveshare (or similar) serial-to-TCP bridge that tunnels
RS485 Modbus RTU to Modbus TCP. pymodbus handles all framing and CRC
transparently — this class adds only EG4-specific derived-field logic
on top of modbus_tcp.

Protocol notes
--------------
This class targets the EG4-LL battery using the PDF V01.06 register map
(eg4_ll_pdf_holding_registry_map.csv / eg4_ll_pdf_input_registry_map.csv).
That protocol uses a SINGLE Modbus address space, ALL accessed via FC 0x03
(Read Holding Registers). There is no FC 0x04 input space — do not use the
input CSV for live reads.

Flag decoding (warning, protection, error_code registers) is handled
entirely by protocol_settings using Data_Type._16BIT_FLAGS together with
the bit-label JSON codes in eg4_ll_pdf.json.  This class does NOT manually
re-decode those registers.  The decoded info dict will already contain
string values like "Pack_OV, Cell_UV" for those fields by the time
read_data() sees them — attempting int() on those strings would raise
ValueError.

Status and heater_state are _8BIT enum registers. protocol_settings
resolves those to human-readable strings via the status_codes and
heater_state_codes entries in eg4_ll_pdf.json before this class sees them.

Derived fields
--------------
Three sets of fields are computed here after the register decode:

  cell_voltage_max_v    max individual cell voltage in V
  cell_voltage_min_v    min individual cell voltage in V
  cell_voltage_diff_mv  spread between max and min in mV

  balancing_state       integer: 0=Idle 1=Balancing 2=Finished
  balancing_state_text  human-readable label

These are injected into the returned info dict so the parent class and any
downstream consumers treat them identically to actual register values.

Holding register cache
----------------------
Protection thresholds (balance_volt, cell_ov_release, etc.) are read once
on startup from the holding registry map and cached.  They are re-read on
reconnect.  The balancing state inference uses these cached values; if the
holding read fails, safe built-in defaults are used instead.

Variable names used from the holding cache match the variable_name column
in eg4_ll_pdf_holding_registry_map.csv exactly:
  balance_volt        (reg 56)
  balance_volt_diff   (reg 57)
  cell_ov_release     (reg 69)

Waveshare device setup (via its web interface)
----------------------------------------------
  Work Mode  : TCP Server
  Baud Rate  : 19200 (match battery DIP baud setting)
  Data Bits  : 8
  Stop Bits  : 1
  Parity     : None
  Local Port : 502 (default; match config `port`)
  Protocol   : Modbus TCP <-> Modbus RTU (if supported by model)
               "None/Transparent" also works with pymodbus

DIP switch / slave address notes
---------------------------------
Per ll firmware, DIP address 1 activates inverter closed-loop RS485 mode on
the master battery — full BMS register data is unavailable at that address.
Set batteries to DIP addresses 2-64 for full data access.  If using CAN for
inverter comms, all RS485 ports are free and all addresses 2-64 are usable.

Sequential read mode is mandatory. All batteries share one physical RS485 bus
through the Waveshare. Set read_mode = "sequential" or "interleaved" (not
"concurrent") in the gateway config to prevent request collisions on the wire.

Config keys (in addition to modbus_tcp / transport_base keys)
-------------------------------------------------------------
  host             Waveshare IP address
  port             Waveshare TCP port, default 502
  slave_id         Battery DIP switch address (2-64 for full BMS data)
  protocol_version must be set to eg4_ll_pdf

Manual verification commands (send to RS485 bus to confirm addressing):
  Battery 1 (DIP=01): 01 03 00 00 00 01 84 0A
  Battery 2 (DIP=02): 02 03 00 00 00 01 84 39
  Battery 3 (DIP=03): 03 03 00 00 00 01 85 E8
  Battery 4 (DIP=04): 04 03 00 00 00 01 84 5F
  Battery 5 (DIP=05): 05 03 00 00 00 01 85 8E
  Battery 6 (DIP=06): 06 03 00 00 00 01 85 BD
  (Each asks for pack voltage at register 0 — a safe probe register.)
"""

from __future__ import annotations

from classes.protocol_settings import Registry_Type
from classes.transports.modbus_rtu import modbus_rtu
from defs.common import TransportSettings


class modbus_eg4_ll_s_rtu(modbus_rtu):

    transport_type: str = "scraper"
    """
    EG4 LL battery transport over Modbus TCP (Waveshare RS485 bridge),
    targeting the PDF V01.06 register map.

    Inherits all connection, retry, and register-read logic from modbus_tcp.
    Adds EG4-specific derived fields: cell voltage statistics and balancing
    state inference from cached holding register thresholds.

    Flag registers (warning, protection, error_code) and enum registers
    (status, heater_state) are decoded to human-readable strings by
    protocol_settings via _16BIT_FLAGS data type and JSON codes — this class
    does not re-decode them.
    """

    # Defaults used for balancing inference when holding registers are unavailable.
    # Values are in the same units as the decoded holding register fields:
    #   balance_volt      V  (raw mV * 0.001)
    #   balance_volt_diff V  (raw mV * 0.001)
    #   cell_ov_release   V  (raw mV * 0.001)
    _BALANCE_VOLT_DEFAULT:      float = 3.400
    _BALANCE_VOLT_DIFF_DEFAULT: float = 0.040
    _CELL_OV_RELEASE_DEFAULT:   float = 3.450

    _holding_cache:  dict[str, int | float | str]
    _holding_loaded: bool

    def __init__(self, settings: TransportSettings) -> None:
        self._holding_cache  = {}
        self._holding_loaded = False
        super().__init__(settings)
        self._slave_id: str = settings.get("slave_id", fallback="")


    # ------------------------------------------------------------------
    # transport_base interface
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        super().connect()
        if not self.connected:
            # Drop cached thresholds so they are re-read after reconnect.
            self._holding_loaded = False
            self._holding_cache  = {}
            return False
        return True

    def read_data(self) -> dict[str, int | float | str]:
        if self.protocolSettings is None:
            self._log.warning("No protocolSettings for %s", self.transport_name)
            return {}

        # One-time startup: read BMS config/threshold registers and cache them.
        if not self._holding_loaded:
            self._load_holding_registers()

        # Per-poll: delegate register read + protocol_settings decode to parent.
        # On return, info already contains:
        #   - All decoded register values (pack_voltage, pack_current, soc, etc.)
        #   - warning / protection / error_code as comma-separated flag name strings
        #     (decoded by _16BIT_FLAGS + JSON codes in eg4_ll_pdf.json)
        #   - status / heater_state as human-readable enum strings
        #     (decoded by _8BIT + JSON codes in eg4_ll_pdf.json)
        info: dict[str, int | float | str] = super().read_data()
        if not info:
            return {}

        # Merge cached holding register values (thresholds, config) into the
        # poll result so downstream consumers see the full picture in one dict.
        # Must happen before _compute_cell_stats and _compute_balancing_state
        # because balancing inference reads from _holding_cache directly.
        info.update(self._holding_cache)

        # Derived aggregations — must run in dependency order:
        #   cell_stats first (produces cell_voltage_max_v / min_v)
        #   balancing_state second (consumes cell_voltage_max_v / min_v)
        info.update(self._compute_cell_stats(info))
        info.update(self._compute_balancing_state(info))

        return info

    # ------------------------------------------------------------------
    # Holding register startup load
    # ------------------------------------------------------------------

    def _load_holding_registers(self) -> None:
        """
        Read BMS config/threshold registers once on startup and cache
        the decoded values. This is used for balancing state inference and to expose config values in the info dict.

        All PDF V01.06 registers use FC 0x03 (Read Holding Registers),
        so Registry_Type.HOLDING is correct here.  modbus_tcp.read_registers()
        with HOLDING maps to pymodbus read_holding_registers() (FC 0x03),
        which the Waveshare bridge passes through to the battery transparently.
        """
        if self.protocolSettings is None:
            self._holding_loaded = True
            return

        try:
            self._log.info("Reading EG4 BMS holding registers for %s (%d entries)...", self.transport_name)
            self._holding_cache = self.read_registry(Registry_Type.HOLDING)
        except Exception:
            self._log.exception("process_registery failed for HOLDING registers on %s", self.transport_name)

        self._holding_loaded = True
        self._log.info("EG4 BMS holding registers loaded for %s: %d values cached.", self.transport_name,len(self._holding_cache))

    # ------------------------------------------------------------------
    # Derived field computations
    # ------------------------------------------------------------------

    def _compute_cell_stats(
        self, info: dict[str, int | float | str]
    ) -> dict[str, int | float | str]:
        """
        Compute per-poll cell voltage statistics from the decoded register values.

        Cell voltage registers (cell_01_voltage through cell_16_voltage) are
        decoded by protocol_settings as raw mV integers (USHORT, unit_mod=1,
        unit=1mV).  This method converts them to volts and computes spread.

        Skips any cell reporting 0 mV (absent or unpopulated cell slot).

        Produces:
          cell_voltage_max_v    float V   highest individual cell voltage
          cell_voltage_min_v    float V   lowest individual cell voltage
          cell_voltage_diff_mv  float mV  spread between max and min
        """
        derived:        dict[str, int | float | str] = {}
        cell_voltages_v: list[float] = []

        for i in range(1, 17):
            raw = info.get(f"cell_{i:02d}_voltage")
            if raw is not None:
                mv = float(raw)
                if mv > 0:
                    cell_voltages_v.append(mv / 1000.0)

        if cell_voltages_v:
            derived["cell_voltage_max_v"]   = round(max(cell_voltages_v), 3)
            derived["cell_voltage_min_v"]   = round(min(cell_voltages_v), 3)
            derived["cell_voltage_diff_mv"] = round(
                (max(cell_voltages_v) - min(cell_voltages_v)) * 1000.0, 1
            )

        return derived

    def _compute_balancing_state(
        self, info: dict[str, int | float | str]
    ) -> dict[str, int | float | str]:
        """
        Infer the pack balancing state from cell voltage stats and BMS thresholds.

        Thresholds come from the holding register cache (read once on startup).
        Variable names match variable_name in eg4_ll_pdf_holding_registry_map.csv:
          balance_volt       reg 56  minimum cell voltage to enable balancing
          balance_volt_diff  reg 57  delta threshold to consider cells imbalanced
          cell_ov_release    reg 69  OV release voltage used for hysteresis

        All three are decoded by protocol_settings with unit_mod=0.001, so their
        cached values are already in volts (e.g. 3.400, 0.040, 3.450).

        State logic:
          0 Idle       cell_min is below balance_volt — not ready to balance
          1 Balancing  cell spread exceeds balance_volt_diff — actively balancing
          2 Finished   all cells at or above ov_release with spread within delta

        The ov_release hysteresis prevents "Balancing" flickering when a cell
        touches the threshold and then dips slightly under load from the
        balancing resistor itself.

        Produces:
          balancing_state       int   0=Idle 1=Balancing 2=Finished
          balancing_state_text  str   human-readable label
        """
        derived: dict[str, int | float | str] = {}

        cell_min_raw = info.get("cell_voltage_min_v")
        cell_max_raw = info.get("cell_voltage_max_v")

        if cell_min_raw is None or cell_max_raw is None:
            # cell stats not available — nothing to infer
            return derived

        cell_min: float = float(cell_min_raw)
        cell_max: float = float(cell_max_raw)

        # Variable names match the CSV variable_name column exactly.
        # Defaults are used when the holding register read failed on startup.
        balance_voltage: float = float(
            self._holding_cache.get("balance_volt",      self._BALANCE_VOLT_DEFAULT)
        )
        balance_delta: float = float(
            self._holding_cache.get("balance_volt_diff", self._BALANCE_VOLT_DIFF_DEFAULT)
        )
        ov_release: float = float(
            self._holding_cache.get("cell_ov_release",   self._CELL_OV_RELEASE_DEFAULT)
        )

        delta_v: float = round(cell_max - cell_min, 3)

        if cell_min < balance_voltage:
            state = 0   # Idle — cells not charged enough to balance
        elif cell_min >= ov_release and delta_v <= balance_delta:
            state = 2   # Finished — all cells high and within tolerance
        elif delta_v > balance_delta:
            state = 1   # Balancing — spread exceeds threshold
        else:
            state = 0   # Idle — default

        state_labels: dict[int, str] = {0: "Idle", 1: "Balancing", 2: "Finished"}
        derived["balancing_state"]      = state
        derived["balancing_state_text"] = state_labels[state]

        return derived
