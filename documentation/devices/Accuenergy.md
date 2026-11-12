# Accuenergy Acuvim II - MQTT Integration Guide

> **Supported Models:** Acuvim II, Acuvim IIR, Acuvim IIE, Acuvim IIW power quality meters
> **Protocol:** Modbus RTU over RS485 (`accuenergy_acuvim`)
> **Status:** Protocol available in MPG

---

## Table of Contents

1. [Overview](#1-overview)
2. [Required Meter Settings](#2-meter-settings-that-affect-scaling)
3. [Serial Settings](#3-serial-settings)
4. [What the Map Publishes](#4-what-the-map-publishes)
   - [Expansion I/O Modules: Coils and Discrete Inputs](#expansion-io-modules-coils-and-discrete-inputs)
5. [Word Order and Scaling](#5-word-order-and-scaling)
6. [MPG Configuration](#6-mpg-configuration)
7. [Manufacturer Map Errata](#7-manufacturer-map-errata)
8. [Not Yet Mapped](#8-not-yet-mapped)
9. [Source Documents](#9-source-documents)

---

## 1. Overview

The Acuvim II is a three-phase power quality meter. All data is read with Modbus function 03 (holding registers), so the protocol ships a holding map only. Optional coil (relay output) and discrete (digital input) maps exist for the AXM-IO expansion modules and are Read Disabled by default.

## 2. Meter Settings That Affect Scaling

| Setting | Where | Values | Effect in MPG |
| --- | --- | --- | --- |
| Basic Parameter Mode (S28 PARA MODE, register `0x101D`) | Display or Modbus | 1 Primary, 0 Secondary (factory default) | Voltage, current, power and demand are scaled by the PT/CT ratios automatically when the meter is in Secondary mode, and published as read in Primary mode. |
| Energy Display Mode (S24 E SEL, register `0x1019`) | Display or Modbus | 0 Primary (default), 1 Secondary | Energy rows use `Rx/10` (Primary) or `Rx/1000` (Secondary) automatically. |
| PT1, PT2, CT1, CT2 (`0x1005`-`0x1009`) | Display or Modbus | Instrument transformer ratios | Used for Secondary-mode scaling. Keep them correct. |

Both modes work with no meter changes, and the map publishes `basic_parameter_mode`, `energy_display_mode`, `pt1`, `pt2`, `ct1` and `ct2` so you can see what the meter is configured for. These rows are read every cycle; the rows that depend on them show raw values for any cycle where they were not read. Setting the meter to Primary mode is still the simplest option if you control the meter, because it removes any dependence on the ratio registers.

## 3. Serial Settings

| Setting | Manufacturer default |
| --- | --- |
| Baud | 19200 (1200-38400) |
| Parity / stop | None, 1 stop (code 3) |
| Unit ID | 1 |

Parity codes: 0 Even, 1 Odd, 2 None with 2 stop bits, 3 None with 1 stop bit.

## 4. What the Map Publishes

| Group | Registers | Default |
| --- | --- | --- |
| Frequency, phase/line voltage, current, neutral current | `0x4000`-`0x401B` | Polled |
| Power (kW), reactive (kvar), apparent (kVA), power factor, unbalance, load type, demand | `0x401C`-`0x4047`, `0x4600`-`0x4605` | Polled |
| Energy (import, export, net, reactive, apparent, per phase) | `0x4048`-`0x4059`, `0x4620`-`0x463D` | Polled |
| THD, odd/even THD, crest/K factor, THFF | `0x405A`-`0x4061` and per-channel totals | Polled |
| Phase angles | `0x42A0`-`0x42A4` | Polled |
| System settings, status, clock | `0x0FFD`-`0x1045` | Polled (slowly) |
| Harmonic orders 2-63 for V1-V3, I1-I3 | `0x4062`-`0x45BF` | Read Disabled |
| Max/Min values | `0x4136`-`0x428D`, `0x4606`-`0x4614` | Read Disabled |
| TOU energy and TOU maximum demand | `0x7200`-`0x769F` | Read Disabled |
| AXM-IO digital input counters, analog in/out | `0x4349`-`0x4390` | Read Disabled |

Read Disabled rows (`RD` in the `writable` column) are kept in the map but skipped when polling. Enable them in the protocol editor when you need them. Clear commands (clear energy, demand, Max/Min and so on) are Write Only (`WO`) and still require the two write gates described in [protocols.md](../usage/protocols.md#managing-write-back-via-the-web-ui). The password register is Read Disabled so it is never published.

### Expansion I/O Modules: Coils and Discrete Inputs

The protocol ships `send_coil_register` and `send_discrete_register` set to `false`. Leave them off unless the meter has an AXM-IO expansion module fitted *and* you want that module's single-bit points. A bare Acuvim II has none, and polling coils or discrete inputs on it only produces Modbus exceptions.

Which registry type carries which point (all read from the meter's own Modbus address, over the same RS485 line):

| You want | Registry type | Function code | Flag in `config.cfg` | Rows in this protocol |
| --- | --- | --- | --- | --- |
| Digital input state (DI1-DI28) | Discrete | 02 | `send_discrete_register = true` | `accuenergy_acuvim.discrete_registry_map.csv` (`di1_status` ...) |
| Relay output state and control (RO1-RO8) | Coil | 01 read, 05 write | `send_coil_register = true` | `accuenergy_acuvim.coil_registry_map.csv` (`ro1_status` ...) |
| DI pulse counters, analog inputs, analog outputs | Holding | 03 | none (holding is already on) | `di*_pulse_counter`, `ai*_sampling_value`, `ao*_value` in the holding map |

Module points, as listed in the manufacturer's map: AXM-IO11 has DI1-6 and RO1-2; AXM-IO21 has DI7-10 and AO1-2; AXM-IO31 has DI11-14, RO3-4 and AI1-2; AXM-IO12 has DI15-20 and RO5-6; AXM-IO22 has DI21-24 and AO3-4; AXM-IO32 has DI25-28, RO7-8 and AI3-4. Read the `expansion_io_modules_status` row first: its bits show which modules the meter detects.

#### **Example scenarios (illustrative)**

- **Breaker or contactor status on a panel board.** An Acuvim II monitors an incoming feeder, and an AXM-IO11 wires six auxiliary contacts from the main breaker, a generator transfer switch and two downstream contactors to DI1-DI6. Set `send_discrete_register = true`, enable the `di1_status` to `di6_status` rows, and the on/off states are published beside the voltage, current and power values. A trip then shows up in MQTT or Home Assistant alongside the power dropping to zero.
- **Water, gas or sub-meter pulses.** A utility pulse output wired to a DI input is counted by the meter. Those counters are 32-bit holding registers (`di1_pulse_counter` and so on), so you only enable those rows. Setting `send_discrete_register` is needed only if you also want the live on/off state of the input.
- **Load shedding or alarm relays.** An AXM-IO31 relay (RO3-4) drives a contactor that sheds a non-critical load or sounds a horn. Reading the relay state needs `send_coil_register = true`. Switching it from Home Assistant or MQTT needs more: the `ro*_status` rows ship as `RD`, so change the row to `RW`, set `write_enabled = true` on the transport, and tick the W box for the register in the protocol editor (the two write gates described in [protocols.md](../usage/protocols.md#managing-write-back-via-the-web-ui)). Think through the failure mode first: a relay that sheds load should not be commanded from an automation you have not tested.
- **Not needed:** a meter that only measures a feeder, with nothing wired to the I/O modules, or a meter with no expansion module at all. Keep both flags `false`.

Setup steps once you have decided you need them:

1. Confirm the module is fitted and detected (`expansion_io_modules_status` bit set).
2. In the transport section of `config.cfg`, set the flag you need (`send_discrete_register = true` and/or `send_coil_register = true`). Config values override the protocol JSON.
3. Change the rows for the points you actually wired from `RD` to `R` (or `RW` for a relay you will control) in the protocol editor or an override, so only used points are polled.
4. Restart MPG.

Status: the coil and discrete maps have been checked against the loader and the manufacturer's address table only. They have not been run against a meter with an I/O module, so treat the first read as a commissioning test.

## 5. Word Order and Scaling

- **Floats and 32-bit energies are high word first, big-endian bytes (ABCD)**, the MPG default, so no `Register_Endian` adjustment is used. Accuenergy's example: `0x42480000` = 50 Hz.
- The real-time block (`0x4000`-`0x4047`) is IEEE-754 float32, two registers per value, at even addresses (`0x4000`, `0x4002`, ...).
- **Secondary mode ratios** are defined once in the protocol JSON under `context_variables` (`voltage_scale`, `current_scale`, `power_scale`, built from `basic_parameter_mode`, `pt1`, `pt2`, `ct1`, `ct2`). Each real-time row carries only `{"Context": {"formula": "x*{voltage_scale}"}}` (or `current_scale` / `power_scale`). A scale is 1 in Primary mode and the PT/CT ratio in Secondary mode. If you edit a scale, edit it in `accuenergy_acuvim.json`, not in the CSV.
- **CT2 of 333, 80, 100 or 200** (333mV and mA inputs): the manufacturer takes CT2 as 1, so `ct_ratio` is `CT1` alone for those values (one grouped case, `"333,80,100,200"`) and `CT1/CT2` for 1A and 5A inputs.
- Power-type values are `Rx/1000` kW/kvar/kVA; the raw float is in W/var/VA, so the map uses the `0.001` unit multiplier.
- Unbalance is `Rx*100` %, so the unit is `100%`. Load characteristic is float 76.0 = L, 67.0 = C, 82.0 = R.
- Energy (32-bit unsigned; net energy signed) is `Rx/10` or `Rx/1000` according to `energy_display_mode`. The meter allows presetting energy, but the rows are read-only here because the scale is applied on read.
- THD and harmonics are 16-bit `Rx/100` %; phase angles `Rx/10` degrees; run time `Rx/100` hours.
- Max/Min and TOU maximum-demand values are signed 16-bit. The manufacturer's table gives the PT/CT formula with no Primary/Secondary column, so the same mode-aware scaling is applied. Whether Primary mode already scales these integers has not been verified on hardware; check one value against the display before relying on them.

## 6. MPG Configuration

``` ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 19200
unit_id = 1
protocol_version = accuenergy_acuvim
```

## 7. Manufacturer Map Errata

The vendor PDF contains typos; MPG follows the hex address, which is consistent within each register run.

| PDF text | Used |
| --- | --- |
| V2 45th harmonic at `425DH` | `0x452D` |
| Decimal addresses of several I1/I3 harmonics (for example `4561H 17762`) | Hex address |
| `0x1020` listed as both "Demand Calculation Slipping Time" and Reserved | Mapped as slipping time; may not exist on all firmware |
| Rows `0x4044` / `0x4046` have the S/Q formula letters and units swapped | Row names followed (Q kvar, S kVA) |
| Max/Min frequency `Rx/1000`; older scaling page says `Rx/100` | Mapped per the map; verify on your meter |
| Crest/K factor `Rx/100`; older scaling page says `Rx/10` | Mapped per the map |
| Min table rows labeled "Max of ..." (`0x4232`, `0x4240`, `0x424E`, `0x4255`) | Named `min_*` |
| `7363H-7363H` for Prior Month Total Es | Not mapped as a separate address (`0x7362`) |

## 8. Not Yet Mapped

Other Accuenergy families use different maps and are not covered: Acuvim L / CL / EL, Acuvim 3, AcuRev 1310 / 2100 / 4100, AcuDC. The Acuvim II SunSpec block (`0xC350`) is also not mapped. Max/Min and TOU timestamps, alarm/SOE records, data logs and the event log are not mapped.

## 9. Source Documents

- Accuenergy, *Acuvim II Modbus Map* (41 pages; <https://www.accuenergy.com/support/modbus-map/>)
- Accuenergy, *Acuvim II Modbus Registers - Scaling Data*
- `protocols/accuenergy/accuenergy_acuvim.json` and the `.holding`, `.coil`, `.discrete` registry maps
