# Description: Scraper for EG4 LL rack batteries (PDF V01.06 protocol) via Modbus RTU through a Waveshare RS485 bridge,
# inheriting from modbus_rtu
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
"""
EG4 LL Battery - Modbus RTU Transport
=====================================

Connects to RS485 to Modbus RTU.  pymodbus handles all framing and CRC
transparently — this class adds only EG4-specific derived-field logic
on top of modbus_rtu.

Protocol notes
--------------
This class targets the EG4-LL battery using the EG4 PDF V01.06 register map
(eg4_ll_s.holding_registry_map.csv).
That protocol uses a SINGLE Modbus address space, ALL accessed via FC 0x03
(Read Holding Registers).

Flag decoding (warning, protection, error_code registers) is handled
entirely by protocol_settings using Data_Type._16BIT_FLAGS together with
the bit-label JSON codes in eg4_ll_s.json.  This class does NOT manually
re-decode those registers.  The decoded info dict will already contain
string values like "Pack_OV, Cell_UV" for those fields by the time
post_process_data() sees them.

Status and heater_state are _8BIT enum registers.  protocol_settings
resolves those to human-readable strings via the status_codes and
heater_state_codes entries in eg4_ll_s.json before this class sees them.

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
modbus_base calls via finish_cycle_tracking() at the end of every scrape
cycle regardless of read mode (sequential, group, or interleaved).  This
replaces the previous read_data() override that only fired on the
sequential path.

Holding register cache
----------------------
Protection thresholds (balance_volt, balance_volt_diff, cell_ov_release)
are read once after each successful connection via the on_first_connect_read()
hook (deferred to the first successful scrape cycle — see that hook's
docstring) and cached.  Read directly at their fixed holding addresses
(56-57, 69) via self.read_registers(), bypassing mask/screen entirely —
same convention eg4_metadata.py already uses for serial-number/device-type
reads — since these 3 values aren't exposed as their own scraped metrics,
only consumed internally for balancing_state inference.  A device's
mask/screen configuration has no effect on whether this cache loads.

If the holding register read fails, safe built-in defaults are used until
it succeeds — see _load_holding_cache()'s docstring for why this is
tracked entirely independently of the main scrape's per-cycle completeness
state, so a failure here can never suppress data the main scrape already
collected.  The cache is cleared and re-armed on every reconnect so stale
threshold values from a previous session never persist.

Variable names used from the holding cache match the variable_name column
in eg4_ll_s.holding_registry_map.csv exactly:
  balance_volt        (reg 56)  minimum cell voltage to enable balancing
  balance_volt_diff   (reg 57)  delta threshold to consider cells imbalanced
  cell_ov_release     (reg 69)  OV release voltage used for hysteresis

DIP switch / slave address notes
---------------------------------
Per ll firmware, DIP address 1 activates inverter closed-loop RS485 mode on
the master battery — full BMS register data is unavailable at that address.
Note that many values are averages from across all batteries and some slave
addresses do not produce the same type of data as the master.
Set batteries to DIP addresses 2-64 for full data access.  If using CAN for
inverter comms, all RS485 ports are free and all addresses 2-64 are usable
however, limited data will come through the rs485 port on battery 1.

Sequential or interleaved read mode is mandatory.  All batteries share one
physical RS485 bus through the Waveshare.  Set read_mode = "sequential" or
"interleaved" (not "concurrent") in the gateway config to prevent request
collisions on the wire.

Config keys (in addition to modbus_rtu / transport_base keys)
-------------------------------------------------------------

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
                      02 03 00 69 00 17 D5 EB
                      02    Slave ID = 2
                      03    Function = Read Holding Registers
                      0069  Starting register = 105
                      0017  Number of registers = 23
                      D5EB  CRC (RTU only)
"""

from __future__ import annotations

import logging
from typing import NoReturn

from pymodbus.pdu import ModbusPDU

from classes.protocol_settings import Registry_Type
from classes.transports.modbus_rtu import modbus_rtu
from defs.common import TransportSettings


class modbus_eg4_ll_s_rtu(modbus_rtu):

    transport_type: str = "scraper"
    """
    EG4 LL battery transport over Modbus RTU
    targeting the PDF V01.06 register map.

    Inherits all connection, retry, and register-read logic from modbus_rtu.
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
        self._holding_cache_loaded: bool = False
        self._holding_cache_load_failures: int = 0
        ''' Local, cycle-tracking-independent failure counter for
        _load_holding_cache() — see that method's docstring. Reset on every
        (re)connect by on_first_connect_read(), same as the other cache
        flags. '''
        super().__init__(settings)

    # ------------------------------------------------------------------
    # modbus_base / transport_base hook: post-connection startup read
    # ------------------------------------------------------------------
    @property
    def synthetic_fields_metadata(self) -> list[tuple[str, str, float, str, str]]:
        """Declare data types for fields injected by post_process_data.

        Used by TimescaleDB init_bridge to create wide table columns with
        the correct PostgreSQL types at schema registration time, so
        _validate_wide_row never sees these as unknown extra_keys.

        Tuple format: (variable_name, data_type, unit_mod, note)
        data_type strings match Data_Type enum names in protocol_settings.
        """
        return [
            ("cell_voltage_max_v",   "FLOAT32",  1.0, "Highest individual cell voltage (V)", "holding"),
            ("cell_voltage_min_v",   "FLOAT32",  1.0, "Lowest individual cell voltage (V)", "holding"),
            ("cell_voltage_diff_v",  "FLOAT32",  1.0, "Cell voltage spread max-min (V)", "holding"),
            ("balancing_state",      "USHORT",   1.0, "0=Idle  1=Balancing  2=Finished", "holding"),
            ("balancing_state_text", "ASCII",    1.0, "Human-readable balancing state", "holding"),
        ]

    def on_first_connect_read(self) -> None:
        """Schedule the holding register cache load for the first scrape cycle.

        Rather than loading the cache synchronously here — which would block
        the main connection loop for the full retry duration (up to 2.5 minutes
        at 5 retries x 29s timeout) and prevent other transports in the
        interleaved scheduler from running — we set a flag that causes
        post_process_data to load the cache on the first successful cycle.

        This means synthetic metrics use built-in defaults on cycle 1, then
        switch to device-sourced thresholds from cycle 2 onward.  That is
        always acceptable — one cycle of default-based balancing inference
        is harmless.

        The flag is reset here (not just in __init__) so it also re-arms
        on reconnect, ensuring the cache is refreshed after each reconnection
        without ever blocking the scheduler.
        """
        super().on_first_connect_read()
        self._holding_cache = {}
        self._cell_stats_inputs_warned = False
        self._holding_cache_loaded = False  # arm the deferred load
        self._holding_cache_load_failures = 0

        # Log which threshold inputs will use defaults until cache loads
        _BALANCING_STATE_INPUTS: dict[str, str] = {
            "balance_volt":      "minimum cell voltage to enable balancing",
            "balance_volt_diff": "cell voltage delta threshold to start balancing",
            "cell_ov_release":   "OV release voltage used for balancing hysteresis",
        }
        self._log.info(
            "[%s] Holding register cache will load on first successful scrape cycle. "
            "Balancing inference will use built-in defaults until then: %s",
            self.transport_name,
            ", ".join(
                f"{k}={v}" for k, v in [
                    ("balance_volt",      self._BALANCE_VOLT_DEFAULT),
                    ("balance_volt_diff", self._BALANCE_VOLT_DIFF_DEFAULT),
                    ("cell_ov_release",   self._CELL_OV_RELEASE_DEFAULT),
                ]
            ),
        )

    # Fixed holding-register addresses for the 3 threshold values this cache
    # exists to provide (see class docstring): balance_volt (56) and
    # balance_volt_diff (57) are contiguous and read together in one call;
    # cell_ov_release (69) is separate.
    _BALANCE_VOLT_REGISTER: int = 56
    _CELL_OV_RELEASE_REGISTER: int = 69

    @staticmethod
    def _raise_incomplete_threshold_read(low_regs: ModbusPDU | None, ov_release_regs: ModbusPDU | None) -> NoReturn:
        """Raises for _load_holding_cache() when either threshold register
        read came back empty. Pulled out into its own function (rather than
        a bare `raise` inside the try block) per Ruff TRY301/EM102 — keeps
        the try block itself to statements that can actually fail, and
        avoids constructing the f-string message directly inside `raise`.
        """
        msg: str = (
            "one or both threshold register reads returned no data "
            f"(balance_volt/diff={getattr(low_regs, 'registers', None)}, "
            f"cell_ov_release={getattr(ov_release_regs, 'registers', None)})"
        )
        raise IOError(msg)

    def _load_holding_cache(self) -> None:
        """Load the 3 BMS configuration threshold registers into the cache.

        Reads ONLY holding registers 56-57 (balance_volt, balance_volt_diff)
        and 69 (cell_ov_release) — the exact fixed addresses this cache
        exists for — via self.read_registers(), the same low-level PDU read
        modbus_rtu uses for the main scrape.

        Deliberately NOT self.read_registry(HOLDING) / read_modbus_registers():
        those pull the ENTIRE mask/screen-filtered holding map (136+
        registers on a typical device) and are wired into the shared
        per-cycle completeness tracker (_cycle_expect_unit() /
        cycle_mark_incomplete()) — a single failed range anywhere in that
        much larger read used to be able to mark the WHOLE cycle incomplete,
        silently suppressing data the main scrape had already successfully
        collected moments earlier in that same cycle, over nothing more than
        a hiccup loading these 3 optional threshold values. self.
        read_registers() never touches cycle-tracking state at all, so a
        failure here genuinely cannot affect the main scrape's data. This
        matters at least as much on RTU as on TCP — likely more, since this
        transport shares one physical RS485 bus across every battery on it
        (see class docstring), so a bus collision affecting one register
        range is not a rare event here.

        Consequence of reading fixed addresses directly: these 3 registers
        are now read regardless of the device's mask/screen configuration
        (same convention eg4_metadata.py already uses for serial-number/
        device-type reads — "fixed address, bypassing mask/screen"). They
        aren't exposed as their own scraped metrics, only consumed
        internally for balancing_state inference, so bypassing mask/screen
        for them is intentional, not an oversight — unlike before, a user
        excluding balance_volt/etc. from their mask no longer affects
        whether balancing_state uses live values or the built-in defaults.

        Failures are tracked entirely in the local
        _holding_cache_load_failures counter — never in cycle-tracking
        state — and simply retry on the next cycle via
        _holding_cache_loaded staying False.
        """
        if self.protocolSettings is None:
            return

        try:
            self._log.info(
                "Loading EG4 BMS threshold registers for %s (%s)...",
                self.transport_name,
                self.scrape_target,
            )

            low_regs: ModbusPDU | None = self.read_registers(
                start=self._BALANCE_VOLT_REGISTER, count=2, registry_type=Registry_Type.HOLDING,
            )
            ov_release_regs: ModbusPDU | None = self.read_registers(
                start=self._CELL_OV_RELEASE_REGISTER, count=1, registry_type=Registry_Type.HOLDING,
            )

            if (
                low_regs is None or not getattr(low_regs, "registers", None)
                or len(low_regs.registers) < 2
                or ov_release_regs is None or not getattr(ov_release_regs, "registers", None)
            ):
                self._raise_incomplete_threshold_read(low_regs, ov_release_regs)

            # Raw register values are mV; this map's convention is 0.001V
            # per LSB (see _BALANCE_VOLT_DEFAULT / _BALANCE_VOLT_DIFF_DEFAULT
            # / _CELL_OV_RELEASE_DEFAULT above).
            self._holding_cache = {
                "balance_volt":      low_regs.registers[0] * 0.001,
                "balance_volt_diff": low_regs.registers[1] * 0.001,
                "cell_ov_release":   ov_release_regs.registers[0] * 0.001,
            }
            self._holding_cache_loaded = True
            self._holding_cache_load_failures = 0
            self._log.info(
                "EG4 BMS threshold registers loaded for %s: %s",
                self.transport_name,
                self._holding_cache,
            )

        except Exception:
            self._holding_cache_load_failures += 1
            self._log.warning(
                "[%s] Failed to load EG4 BMS threshold registers (attempt %d) "
                "— balancing inference will use built-in defaults "
                "(balance_volt=%.3f, balance_volt_diff=%.3f, "
                "cell_ov_release=%.3f) until this succeeds. This does NOT "
                "affect the main scrape data already collected this cycle "
                "— only these 3 threshold registers are involved. "
                "Will retry next cycle.",
                self.transport_name,
                self._holding_cache_load_failures,
                self._BALANCE_VOLT_DEFAULT,
                self._BALANCE_VOLT_DIFF_DEFAULT,
                self._CELL_OV_RELEASE_DEFAULT,
                exc_info=self._log.isEnabledFor(logging.DEBUG),
            )
            # Leave _holding_cache_loaded = False so we retry next cycle.
            # No cycle-tracking state is touched anywhere in this except
            # block — this failure is fully local to this cache.

    # ------------------------------------------------------------------
    # modbus_base / transport_base hook: per-cycle post-processing
    # ------------------------------------------------------------------

    def post_process_data(self, info: dict[str, int | float | str]) -> dict[str, int | float | str]:
        """Inject EG4-specific derived metrics after every scrape cycle.

        Called by finish_cycle_tracking() which is the single convergence
        point for all three read modes (sequential, group, interleaved),
        so this fires exactly once per cycle regardless of gateway config.

        On the first successful cycle after connect, triggers the deferred
        holding register cache load (_load_holding_cache).  This avoids
        blocking the main connection loop — see on_first_connect_read.
        If the cache load fails, it retries on the next cycle until it
        succeeds.

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

        # Deferred cache load — runs on the first successful scrape cycle
        # (and retries on subsequent cycles if the previous load failed).
        # This keeps on_first_connect_read non-blocking.
        if not self._holding_cache_loaded:
            self._load_holding_cache()

        # One-time check on the first cycle: warn if cell voltage registers
        # are absent from the scrape data so the user knows which metrics to
        # add to the mask to enable cell_voltage_* synthetic metrics.
        if not self._cell_stats_inputs_warned:
            self._cell_stats_inputs_warned = True
            missing_cell: list[str] = [
                f"cell_{i:02d}_voltage"
                for i in range(1, 17)
                if f"cell_{i:02d}_voltage" not in info
            ]
            if missing_cell:
                self._log.info(
                    "[%s] To enable 'cell_voltage_max_v', 'cell_voltage_min_v', and "
                    "'cell_voltage_diff_v' synthetic metrics, add the following "
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

    def _compute_cell_stats(
        self,
        info: dict[str, int | float | str],
    ) -> dict[str, int | float | str]:
        """Compute per-poll cell voltage statistics from decoded register values.

        Cell voltage registers (cell_01_voltage through cell_16_voltage) are
        decoded by protocol_settings as raw V integers (USHORT, unit_mod=1,
        unit=1mV).  This method converts them to volts and computes spread.
        Skips any cell reporting 0 mV (absent or unpopulated cell slot).

        Produces:
          cell_voltage_max_v    float V   highest individual cell voltage
          cell_voltage_min_v    float V   lowest individual cell voltage
          cell_voltage_diff_v  float V  spread between max and min
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
            derived["cell_voltage_diff_v"] = round((max(cell_voltages_v) - min(cell_voltages_v)), 3)

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
