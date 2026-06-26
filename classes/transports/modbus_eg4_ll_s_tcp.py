# Description: Scraper for EG4 LL rack batteries (PDF V01.06 protocol) via Modbus TCP through a Waveshare RS485 bridge,
# inheriting from modbus_tcp
# File: modbus_eg4_ll_s_tcp.py
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
"""
EG4 LL Battery - Modbus TCP Transport (via Waveshare RS485 bridge)
==================================================================

Connects to a Waveshare (or similar) serial-to-TCP bridge that tunnels
RS485 Modbus RTU to Modbus TCP.  pymodbus handles all framing and CRC
transparently — this class adds only EG4-specific derived-field logic
on top of modbus_tcp.

Protocol notes
--------------
This class targets the EG4-LL battery using the PDF V01.06 register map
(eg4_ll_pdf_holding_registry_map.csv).
That protocol uses a SINGLE Modbus address space, ALL accessed via FC 0x03
(Read Holding Registers).  There is no FC 0x04 input space — do not use the
input CSV for live reads.

Flag decoding (warning, protection, error_code registers) is handled
entirely by protocol_settings using Data_Type._16BIT_FLAGS together with
the bit-label JSON codes in eg4_ll_pdf.json.  This class does NOT manually
re-decode those registers.  The decoded info dict will already contain
string values like "Pack_OV, Cell_UV" for those fields by the time
post_process_data() sees them.

Status and heater_state are _8BIT enum registers.  protocol_settings
resolves those to human-readable strings via the status_codes and
heater_state_codes entries in eg4_ll_pdf.json before this class sees them.

Derived fields
--------------
Three sets of fields are computed by post_process_data() after each cycle:

  cell_voltage_max_v    max individual cell voltage (V)
  cell_voltage_min_v    min individual cell voltage (V)
  cell_voltage_diff_mv  spread between max and min (mV)

  balancing_state       int: 0=Idle  1=Balancing  2=Finished
  balancing_state_text  human-readable label

These are injected into the info dict returned to the gateway so any
downstream bridge (timescaledb, influxdb, MQTT) receives them as
first-class metrics alongside the register-decoded values.

Post-processing hook
--------------------
Derived field computation is implemented in post_process_data(), which
modbus_base calls via _finish_cycle_tracking() at the end of every scrape
cycle regardless of read mode (sequential, group, or interleaved).  This
replaces the previous read_data() override that only fired on the
sequential path.

Holding register cache
----------------------
Protection thresholds (balance_volt, balance_volt_diff, cell_ov_release)
are read once after each successful connection via the on_first_connect_read()
hook and cached.  The cache uses the same mask/screen filter applied to the
main scrape (because read_registry() calls get_registry_map(), which already
reflects the filtered map), so variables excluded from the mask will not be
present in the cache and the built-in defaults will be used instead.

If the holding register read fails on startup, safe built-in defaults are
used throughout the connection lifetime.  The cache is cleared and refreshed
on every reconnect so stale threshold values from a previous session never
persist.

Variable names used from the holding cache match the variable_name column
in eg4_ll_pdf_holding_registry_map.csv exactly:
  balance_volt        (reg 56)  minimum cell voltage to enable balancing
  balance_volt_diff   (reg 57)  delta threshold to consider cells imbalanced
  cell_ov_release     (reg 69)  OV release voltage used for hysteresis

Waveshare device setup (via its web interface)
----------------------------------------------
  Work Mode  : TCP Server
  Baud Rate  : 9600 (match battery DIP baud setting)
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

Sequential or interleaved read mode is mandatory.  All batteries share one
physical RS485 bus through the Waveshare.  Set read_mode = "sequential" or
"interleaved" (not "concurrent") in the gateway config to prevent request
collisions on the wire.

Config keys (in addition to modbus_tcp / transport_base keys)
-------------------------------------------------------------
  host             Waveshare IP address
  port             Waveshare TCP port, default 502
  slave_id         Battery DIP switch address (2-64 for full BMS data)
  protocol_version must be set to eg4_ll_s

Manual verification commands (send to RS485 bus to confirm addressing):
  Battery 1 (DIP=01): 01 03 00 00 00 01 84 0A
  Battery 2 (DIP=02): 02 03 00 00 00 01 84 39
  Battery 3 (DIP=03): 03 03 00 00 00 01 85 E8
  Battery 4 (DIP=04): 04 03 00 00 00 01 84 5F
  Battery 5 (DIP=05): 05 03 00 00 00 01 85 8E
  Battery 6 (DIP=06): 06 03 00 00 00 01 85 BD
  (Each asks for pack voltage at register 0 — a safe probe register.)
  Typical opening commands per BMS tools:
                      02 03 00 00 00 27 05 E3

                      02 03 00 69 00 17 D5 EB
                      02    Slave ID = 2
                      03    Function = Read Holding Registers
                      0069  Starting register = 105
                      0017  Number of registers = 23
                      D5EB  CRC (RTU only)

                      01 03 00 2D 00 5B 94 38
                      Device: 0x01
                      Function: 0x03 (Read Holding Registers)
                      Start: 0x002D (45)
                      Count: 0x005B (91 registers)
                      CRC: 0x94 0x38


                      02 03 00 00 00 27 CRC
                      02 03 00 2D 00 5B CRC
                      02 03 00 69 00 17 CRC
"""

from __future__ import annotations

from classes.protocol_settings import Registry_Type
from classes.transports.modbus_tcp import modbus_tcp
from defs.common import TransportSettings


class modbus_eg4_ll_s_tcp(modbus_tcp):

    transport_type: str = "scraper"
    """
    EG4 LL battery transport over Modbus TCP (Waveshare RS485 bridge),
    targeting the PDF V01.06 register map.

    Inherits all connection, retry, and register-read logic from modbus_tcp.
    Adds EG4-specific derived fields — cell voltage statistics and balancing
    state inference from cached holding register thresholds — through the
    post_process_data() and on_first_connect_read() hooks defined in
    modbus_base / transport_base.
    """

    # Fallback thresholds used for balancing inference when the holding
    # register cache is empty (mask excluded the registers, or the startup
    # read failed).  Units match the decoded holding register values:
    #   balance_volt      V  (CSV raw mV × 0.001)
    #   balance_volt_diff V  (CSV raw mV × 0.001)
    #   cell_ov_release   V  (CSV raw mV × 0.001)
    _BALANCE_VOLT_DEFAULT:      float = 3.400
    _BALANCE_VOLT_DIFF_DEFAULT: float = 0.040
    _CELL_OV_RELEASE_DEFAULT:   float = 3.450

    def __init__(self, settings: TransportSettings) -> None:
        # Initialise cache and one-time warning flags before super().__init__()
        # because modbus_base may attempt a connection during init on some
        # configurations.
        self._holding_cache: dict[str, int | float | str] = {}
        self._cell_stats_inputs_warned: bool = False
        super().__init__(settings)

    # ------------------------------------------------------------------
    # modbus_base / transport_base hook: post-connection startup read
    # ------------------------------------------------------------------

    @property
    def synthetic_field_names(self) -> frozenset[str]:
        """Declare all fields injected by post_process_data.

        ``_filter_for_member`` in ``protocol_gateway`` uses this set to pass
        these fields through the variable mask / registry map filter so they
        reach the bridge layer even though they have no corresponding row in
        the protocol CSV and cannot appear in the mask file.

        Fields produced by _compute_cell_stats:
          cell_voltage_max_v    highest individual cell voltage (V)
          cell_voltage_min_v    lowest individual cell voltage (V)
          cell_voltage_diff_mv  spread between max and min (mV)

        Fields produced by _compute_balancing_state:
          balancing_state       int  0=Idle  1=Balancing  2=Finished
          balancing_state_text  str  human-readable label
        """
        return frozenset({
            "cell_voltage_max_v",
            "cell_voltage_min_v",
            "cell_voltage_diff_v",
            "balancing_state",
            "balancing_state_text",
        })

    def on_first_connect_read(self) -> None:
        """Load BMS configuration thresholds once per connection.

        Called by modbus_base.connect() after self.connected is True —
        fires on every connect and reconnect, ensuring the cache always
        reflects the device's current configuration values.

        The cache is populated from Registry_Type.HOLDING because the
        EG4-LL V01.06 protocol exposes all registers (measurement and
        configuration alike) via FC 0x03.  The result reflects whatever
        mask / screen filter the transport has configured — if threshold
        registers have been excluded, built-in defaults are used during
        balancing state inference.
        """
        super().on_first_connect_read()
        self._holding_cache = {}
        # Reset the one-time scrape-input warning so it fires again after
        # reconnect (in case the mask changed between sessions).
        self._cell_stats_inputs_warned: bool = False

        if self.protocolSettings is None:
            return

        try:
            self._log.info(
                "Loading EG4 BMS holding registers for %s (%s)...",
                self.transport_name,
                self.scrape_target,
            )
            self._holding_cache = self.read_registry(Registry_Type.HOLDING)
            self._log.info(
                "EG4 BMS holding registers loaded for %s: %d values cached.",
                self.transport_name,
                len(self._holding_cache),
            )
        except Exception:
            self._log.exception(
                "Failed to load holding registers for %s — "
                "balancing inference will use built-in defaults.",
                self.transport_name,
            )

        # Check which holding-register inputs to _compute_balancing_state are
        # absent from the cache.  Missing entries mean the variable_mask for
        # this transport excludes them — the method will fall back to built-in
        # defaults, which may not match the device's actual configuration.
        _BALANCING_STATE_INPUTS: dict[str, str] = {
            "balance_volt":      "minimum cell voltage to enable balancing",
            "balance_volt_diff": "cell voltage delta threshold to start balancing",
            "cell_ov_release":   "OV release voltage used for balancing hysteresis",
        }
        missing_balancing: list[str] = [
            k for k in _BALANCING_STATE_INPUTS if k not in self._holding_cache
        ]
        if missing_balancing:
            self._log.info(
                "[%s] To enable accurate 'balancing_state' / 'balancing_state_text' "
                "synthetic metrics, add the following register(s) to the variable mask: %s  "
                "(%s).  Built-in defaults will be used until then.",
                self.transport_name,
                ", ".join(missing_balancing),
                " | ".join(
                    f"{k}: {_BALANCING_STATE_INPUTS[k]}" for k in missing_balancing
                ),
            )

    # ------------------------------------------------------------------
    # modbus_base / transport_base hook: per-cycle post-processing
    # ------------------------------------------------------------------

    def post_process_data(self, info: dict[str, int | float | str]) -> dict[str, int | float | str]:
        """Inject EG4-specific derived metrics after every scrape cycle.

        Called by _finish_cycle_tracking() which is the single convergence
        point for all three read modes (sequential, group, interleaved),
        so this fires exactly once per cycle regardless of gateway config.

        Derived fields are computed in dependency order:
          1. cell_stats   — produces cell_voltage_max_v / min_v
          2. balancing    — consumes cell_voltage_max_v / min_v

        On the first cycle after connect, emits a one-time INFO log if the
        cell voltage registers needed by _compute_cell_stats are absent from
        the scrape data (e.g. excluded by the variable mask).

        Returns ``info`` unchanged (including the injected keys) so the
        gateway's bridge layer sees derived metrics alongside register values.
        """
        if not info:
            return info

        # One-time check on the first cycle: warn if cell voltage registers
        # are absent from the scrape data so the user knows which metrics to
        # add to the mask to enable cell_voltage_* synthetic metrics.
        if not getattr(self, '_cell_stats_inputs_warned', False):
            self._cell_stats_inputs_warned = True
            missing_cell: list[str] = [
                f"cell_{i:02d}_voltage"
                for i in range(1, 17)
                if f"cell_{i:02d}_voltage" not in info
            ]
            if missing_cell:
                self._log.info(
                    "[%s] To enable 'cell_voltage_max_v', 'cell_voltage_min_v', and "
                    "'cell_voltage_diff_mv' synthetic metrics, add the following "
                    "register(s) to the variable mask: %s",
                    self.transport_name,
                    ", ".join(missing_cell),
                )

        info.update(self._compute_cell_stats(info))
        info.update(self._compute_balancing_state(info))
        return info

    # ------------------------------------------------------------------
    # Derived field computations
    # ------------------------------------------------------------------

    def _compute_cell_stats(self, info: dict[str, int | float | str]) -> dict[str, int | float | str]:
        """Compute per-poll cell voltage statistics from decoded register values.

        Cell voltage registers (cell_01_voltage through cell_16_voltage) are
        decoded by protocol_settings as raw V integers (USHORT, unit_mod=1,
        unit=0.001V).  This method converts them to volts and computes spread.
        Skips any cell reporting 0 mV (absent or unpopulated cell slot).

        Produces:
          cell_voltage_max_v    float V   highest individual cell voltage
          cell_voltage_min_v    float V   lowest individual cell voltage
          cell_voltage_diff_v  float mV  spread between max and min
        """
        derived: dict[str, int | float | str] = {}
        cell_voltages_v: list[float] = []

        for i in range(1, 17):
            raw: int | float | str | None = info.get(f"cell_{i:02d}_voltage")
            if raw is not None:
                mv = float(raw)
                if mv > 0:
                    cell_voltages_v.append(mv)

        if cell_voltages_v:
            derived["cell_voltage_max_v"]   = round(max(cell_voltages_v), 3)
            derived["cell_voltage_min_v"]   = round(min(cell_voltages_v), 3)
            derived["cell_voltage_diff_v"] = round(
                (max(cell_voltages_v) - min(cell_voltages_v)), 3
            )

        return derived

    def _compute_balancing_state(self, info: dict[str, int | float | str]) -> dict[str, int | float | str]:
        """Infer the pack balancing state from cell voltage stats and BMS thresholds.

        Thresholds come from the holding register cache loaded by
        on_first_connect_read().  Variable names match variable_name in
        eg4_ll_pdf_holding_registry_map.csv exactly:
          balance_volt       reg 56  minimum cell voltage to enable balancing
          balance_volt_diff  reg 57  delta threshold to consider cells imbalanced
          cell_ov_release    reg 69  OV release voltage used for hysteresis

        All three are decoded by protocol_settings with unit_mod=0.001, so their
        cached values are already in volts (e.g. 3.400, 0.040, 3.450).
        If any are absent from the cache (masked out or load failed), the
        class-level _*_DEFAULT fallbacks are used.

        State logic:
          0 Idle       cell_min is below balance_volt — not ready to balance
          1 Balancing  cell spread exceeds balance_volt_diff — actively balancing
          2 Finished   all cells at or above ov_release with spread within delta

        The ov_release hysteresis prevents "Balancing" flickering when a cell
        touches the threshold and then dips slightly under load from the
        balancing resistor itself.

        Produces:
          balancing_state       int   0=Idle  1=Balancing  2=Finished
          balancing_state_text  str   human-readable label
        """
        derived: dict[str, int | float | str] = {}

        cell_min_raw: int | float | str | None = info.get("cell_voltage_min_v")
        cell_max_raw: int | float | str | None = info.get("cell_voltage_max_v")

        if cell_min_raw is None or cell_max_raw is None:
            return derived

        cell_min: float = float(cell_min_raw)
        cell_max: float = float(cell_max_raw)

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
