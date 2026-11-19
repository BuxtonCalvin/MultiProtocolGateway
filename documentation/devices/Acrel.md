# Acrel DTSD1352-C - MQTT Integration Guide

> **Supported Models:** DTSD1352-C (CT-operated, 3×1(6)A) and the other DTSD1352 variants that share the manufacturer's register table (direct-connect 10(80)A, `-CH` harmonic option)
> **Protocol:** Modbus RTU over RS485 (`acrel_dtsd1352_c`)
> **Status:** Protocol available in MPG

---

## Table of Contents

1. [Overview](#1-overview)
2. [Meter Settings That Affect Scaling](#2-meter-settings-that-affect-scaling)
3. [Serial Settings](#3-serial-settings)
4. [What the Map Publishes](#4-what-the-map-publishes)
5. [Startup Read, History and Event Log](#5-startup-read-history-and-event-log)
6. [Word Order, Byte Order and Scaling](#6-word-order-byte-order-and-scaling)
7. [MPG Configuration](#7-mpg-configuration)
8. [Manufacturer Manual Errata](#8-manufacturer-manual-errata)
9. [Verification Status](#9-verification-status)
10. [Not Mapped](#10-not-mapped)
11. [Source Documents](#11-source-documents)

---

## 1. Overview

The DTSD1352 is a three-phase DIN-rail meter (three-phase 3-wire or 4-wire) sold under the Acrel name and also supplied for Sungrow inverters as the "Sungrow DTSD1352". Everything is read with Modbus function 03. Measurements are raw integers that the meter expects the reader to scale with the PT and CT ratios, so the map applies those ratios itself.

Registers that rarely change (the ratios, communication settings, pulse constant) are read **once per connection** instead of being polled, and the meter's history and event log are exposed as **record blocks** read the same way. Section 5 explains both.

## 2. Meter Settings That Affect Scaling

| Setting | Where | Effect in MPG |
| --- | --- | --- |
| PT ratio (menu `In` > `Pt`, register `0x008D`) | Meter keys or Modbus | Multiplies every voltage, power and energy value. Use 1 for direct voltage connection. |
| CT ratio (menu `In` > `Ct`, register `0x008E`) | Meter keys or Modbus | Multiplies every current, power and energy value. Use 1 for a direct-connect (80A) meter; for a CT-operated meter use the CT ratio, for example 40 for 200/5. |

**Set both correctly on the meter.** The map publishes `pt_ratio` and `ct_ratio`. They are startup rows: read when MPG connects and cached, and every scaled value uses the cached ratios. If you change a ratio at the meter, MPG picks it up at the next connection (restart MPG or reconnect). If the startup read could not get the ratios (see [section 5](#5-startup-read-history-and-event-log)), scaled rows publish the raw, unscaled value until a retry succeeds.

## 3. Serial Settings

| Setting | Value |
| --- | --- |
| Address | 1-247 |
| Baud | 1200, 2400, 4800, 9600, 19200 (the shipped protocol assumes 9600) |
| Data / parity / stop | 8 data bits; parity None or Even; 1 or 2 stop bits |

The manual does not state factory defaults; set the same values in `config.cfg`. The register table gives baud codes 1-4 (9600, 4800, 2400, 1200) only, although the menu also offers 19200.

## 4. What the Map Publishes

| Group | Registers | Default |
| --- | --- | --- |
| Phase voltage, line voltage, phase current, frequency | `0x0061`-`0x007A` | Polled |
| Active, reactive, apparent power per phase and total, power factor | `0x0164`-`0x017F` | Polled |
| Total, forward and reversing active energy; total, forward and reversing reactive energy | `0x0000`-`0x003A` (totals) | Polled |
| Forward active energy per phase | `0x0087`-`0x008C` | Polled |
| Voltage and current unbalance, DIDO/running status | `0x008F`-`0x0094` | Polled |
| PT/CT ratios, communication settings (address, baud, parity, stop bits), pulse constant | `0x003F`, `0x0040`, `0x008D`, `0x008E`, `0x0095` | **Startup** (read once per connection) |
| History records: last 48 months and last 90 days | `0x1001`-`0x1030`, `0x1101`-`0x115A` | Startup, Read Disabled until enabled |
| Event log: last 100 events | `0x3001`-`0x3064` | Startup, Read Disabled until enabled |
| Energy by tariff (sharp, peak, flat, valley) | `0x0002`-`0x003A` | Read Disabled |
| Maximum demand, daily maximum demand, current demand | `0x007B`-`0x0086`, `0x0180`-`0x019B` | Read Disabled |
| Max/Min voltage, current and power records | `0x0200`-`0x0280` | Read Disabled |
| Temperature (optional NTC probes) | `0x2000`-`0x2002` | Read Disabled |
| Real-time clock | `0x003C`-`0x003E` | Read Disabled |
| Harmonic data (`-CH` models): THD, harmonics 2-31, fundamental/harmonic V, I, P, Q | `0x05DD`-`0x06B2` | Read Disabled |
| Second RS485 path (dual-RS485 models) | `0x0096`, `0x0097` | Read Disabled |

Read Disabled rows (`RD` in the `writable` column) stay in the map but are not read. Enable them in the protocol editor when you have the option fitted or want them. Tariff energies need the meter's multi-tariff mode; the harmonic rows exist only on harmonic models. Clear commands do not exist in this register table, so there are no write-only rows.

## 5. Startup Read, History and Event Log

### Startup rows

A row whose `read interval` is `startup` is read once when MPG connects (and again after a reconnect) by the startup routine in `classes/discovery/startup.py`, then cached and added to every poll cycle's data. It is otherwise an ordinary row: listed in the register map, selectable, maskable and screenable in the web UI, with its normal unit, codes and adjustments. See [Startup Rows](../usage/protocols.md#startup-rows-read-once-per-connection).

If the first read is incomplete, it is retried at the start of a poll cycle, up to three more times, a minute apart. The log line `[acrel_dtsd1352_c] startup read: N values from R requests` says how it went.

### History records (assemblages)

The meter keeps the last 48 months and the last 90 days. Each is a **120-byte (60-register) assemblage** that can only be read whole: a read of 60 registers at `0x1001` returns last month, at `0x1002` the month before, up to `0x1030`; days are `0x1101` to `0x115A`. The records are addressed by number, not consecutive addresses, so the protocol declares them as [record blocks](../usage/protocols.md#record-blocks-history-and-event-logs) in the JSON and describes the layout once. Every record has the same layout:

| Word offset | Content | Variable suffix |
| --- | --- | --- |
| `0x00` | Frozen time: year (high byte, 20YY), month (low byte) | `_year`, `_month` |
| `0x01` | Frozen time: day (high), hour (low) | `_day`, `_hour` |
| `0x02`, `0x04`-`0x0A` | Forward active energy: total, sharp, peak, flat, valley (32-bit each) | `_forward_active_energy`, `_sharp`, `_peak`, `_flat`, `_valley` |
| `0x0C`-`0x14` | Reversing active energy, same five | `_reverse_active_energy…` |
| `0x16`-`0x1E` | Forward reactive energy, same five | `_forward_reactive_energy…` |
| `0x20`-`0x28` | Reversing reactive energy, same five | `_reverse_reactive_energy…` |
| `0x2A`, `0x2C`, `0x2E` | Active energy of phase A, B, C | `_forward_active_energy_l1` … `_l3` |
| `0x30`-`0x32` | Maximum forward active demand, time of occurrence (minute-hour, day-month) | `_max_forward_active_demand`, `_minute`, `_hour`, `_day`, `_month` |
| `0x33`-`0x35` | Maximum reversing active demand and its time | `_max_reverse_active_demand…` |
| `0x36`-`0x38` | Maximum forward reactive demand and its time | `_max_forward_reactive_demand…` |
| `0x39`-`0x3B` | Maximum reversing reactive demand and its time | `_max_reverse_reactive_demand…` |

Variable names are `history_month_<n>_<suffix>` and `history_day_<n>_<suffix>`; `<n>` = 1 is the most recent month or day. Energies are scaled like the live energies (`E = data × PT × CT × 0.01`).

### Event log

The meter keeps the last 100 events (`0x3001` for the latest, up to `0x3064`), 6 registers each: date (YY-MM), time (DD-hh), time (mm-ss), event number, event details, reserved. Variables are `event_<n>_year` … `event_<n>_second`, `event_<n>_number` and `event_<n>_details`.

The event numbers are in the `event_numbers` table of `acrel_dtsd1352_c.json` and shared by every record (`values` = `@event_numbers`), so `event_<n>_number_desc` carries the text:

| Number | Event |
| --- | --- |
| `0x0100` (256) | Power on |
| `0x0101` (257) | Power off |
| `0x0200` (512) | Clear |
| `0x0300` (768) | DO action |
| `0x0400` (1024) | UI record (alarm) |
| `0x0700` (1792) | Time calibration |

The meaning of `event_<n>_details` depends on the event: for Clear, 1 clear current energy, 2 clear history energy on flash, 3 clear maximum demand, 4 clear history energy, 5 clear maximum value in a period, 6 clear out; for DO action, 0 off, 1 on; for a UI record, bit flags 0-2 over-voltage A/B/C, 3-5 lose-voltage A/B/C, 6-8 reversing A/B/C, 9-11 over-current A/B/C, 12-14 low current A/B/C.

### Enabling records

All record fields ship Read Disabled, because more than 7,000 variables would be far too many to publish by default. Enable the ones you want in the protocol editor (for example `history_month_1_*` for last month's totals). A record is requested only when at least one of its fields is enabled, so a record you do not use costs no bus time. A meter without history (a basic model) answers none of the requests; MPG stops after three unanswered records in a block, so connecting is not held up.

A newly installed meter has fewer than 48 months of history; records that do not exist yet go unanswered and are skipped.

### Reserved registers

The manual lists many reserved ranges. A meter may refuse a request that touches one, so the JSON declares them under `unreadable_registers` and no poll or startup request ever spans one. For example, the voltage-and-current window is read as `0x0061`-`0x0066` and frequency (`0x0077`) as its own request, because `0x0067`-`0x0076` is reserved. Two documented blocks lie inside ranges the manual also marks reserved, and stay readable: the frozen-time registers `0x0121`-`0x0122` and the harmonic block `0x05DD`-`0x06B2`.

## 6. Word Order, Byte Order and Scaling

- **32-bit values (energies and power) are read high word first, big-endian bytes (ABCD)**, the MPG default, so no `Register_Endian` adjustment is used. The manual does not state the order; it was taken from AggSoft's DTSD1352 map (big-endian dwords). Confirm it on your meter ([section 9](#9-verification-status)).
- Active and reactive power are **signed** 32-bit (`INT`), negative = export. Apparent power and energies are unsigned (`UINT`). Power factor is signed 16-bit.
- Scaling follows the manufacturer's formulas:

| Quantity | Manufacturer formula | MPG |
| --- | --- | --- |
| Voltage | U = data × PT × 0.1 V | unit `0.1V`, then `x*{pt_ratio}` |
| Current | I = data × CT × 0.01 A | unit `0.01A`, then `x*{ct_ratio}` |
| Power (kW, kvar, kVA) | PQS = data × PT × CT × 0.001 | unit `0.001kW`, then `x*{power_scale}` |
| Energy (kWh, kvarh) | E = data × PT × CT × 0.01 | unit `0.01kWh`, then `x*{power_scale}` |
| Frequency | F = data × 0.01 Hz | unit `0.01Hz` |
| Power factor | PF = data × 0.001 | unit `0.001` |
| Unbalance | unit 0.1 % | unit `0.1%` |
| Temperature | 0.1 °C | unit `0.1°C` |

  `power_scale` (`{pt_ratio}*{ct_ratio}`) is defined once under `context_variables` in `acrel_dtsd1352_c.json`, so the PT×CT product is written in one place.
  
- **Byte-packed registers.** Several settings and the time fields share one register: register `0x003F` holds the address in its high byte and the baud code in its low byte; `0x0095` holds parity (high) and stop bits (low); `0x0091` holds two running-state bytes; the clock and every date/time in the history and event records are two bytes per register, first-named field in the high byte (confirmed by the manual's event-log example, `12 01 08 0A 01 01` = 2018-01-08 10:01:01). These are `BYTE` rows with a byte offset: `0x003F.0` is the high byte and `0x003F.1` the low byte, on Modbus integer words and raw bytes alike.

## 7. MPG Configuration

``` ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 1
protocol_version = acrel_dtsd1352_c
```

## 8. Manufacturer Manual Errata

| Manual | Used |
| --- | --- |
| Min apparent power of phase C listed at `027EH`; the total follows at `0280H` and every other record has a 4-register pitch | `0x027C` |
| Event number, details and reserved listed at `0004H`-`0006H` of an event record, but the worked example returns 6 registers with the event number in the fourth | Word offsets 3, 4, 5 |
| Line voltages labeled "A-B", "C-B", "A-C" in that order | `voltage_l1_l2`, `voltage_l2_l3` (labeled C-B), `voltage_l3_l1` (labeled A-C) |
| "Spike" energy | Named `sharp`, matching the tariff rate list ("1: sharp") |
| Baud code table has no code for 19200 | Not mapped as a code; the value is published as the raw code |
| `0x019B`-`0x01FF` listed as Reserved while `0x019B` is also a demand row | `0x019B` mapped as reverse reactive demand; reserved from `0x019C` |
| `0x00C4`-`0x0163` reserved, yet `0x0121`-`0x0122` documented (frozen time) | Reserved ranges split around them |
| `0x0285`-`0x06FF` reserved, yet the harmonic block `0x05DD`-`0x06B2` documented | Reserved ranges split around it |
| Current imbalance has no unit | 0.1 %, as the voltage imbalance above it |

## 9. Verification Status

The map follows the manufacturer's table but has not been run against a meter. These points rely on inference and should be checked once:

1. **Word order of 32-bit values.** Taken from AggSoft's map (big-endian dwords). Check that total active energy matches the meter display.
2. **Demand scaling** (live and in the history records). The manual says only "keep 3 decimal places"; the map applies PT × CT by analogy with the other power values. These rows are Read Disabled until checked.
3. **Reserved-register handling.** The map assumes the meter refuses a request that touches a reserved register, as the manual's "Reserve" entries suggest; if your meter tolerates them, nothing is lost by the extra requests.
4. **History and event records on your model.** Basic models may have no history. The record layouts follow the manual and its worked event example; compare one month and one event with the meter's display or the vendor software.
5. **Startup values after a meter-side change.** Ratios and communication settings are cached until the next connection by design.

## 10. Not Mapped

Time-zone and tariff-period tables (`0x0041`-`0x00C3` and the second block at `0x00B2`, plus the third and fourth lists at `0x0700`-`0x0729` and the eight-rate area at `0xE000`): they are static configuration and would make good startup rows, but the manual lists some periods twice and packs three bytes per period, so they are left for a later pass. Frozen-time settings (`0x0121`, `0x0122`), the time-of-occurrence registers that follow the live demand and Max/Min values, and the bit layout of the DIDO/over-voltage word (`0x008F`, published as a raw word) are also not mapped.

## 11. Source Documents

- Acrel, *DTSD1352 Installation and operation instruction V2.19* (history assemblages, event log, reserved ranges) and *V3.2* (register table, communication description)
- AggSoft, *Acrel (DTSD1352) - MODBUS Maps* (register list, big-endian dword types)
- `protocols/acrel/acrel_dtsd1352_c.json` and `acrel_dtsd1352_c.holding_registry_map.csv`
