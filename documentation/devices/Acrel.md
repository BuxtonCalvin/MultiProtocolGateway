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
5. [Word Order, Byte Order and Scaling](#5-word-order-byte-order-and-scaling)
6. [MPG Configuration](#6-mpg-configuration)
7. [Manufacturer Manual Errata](#7-manufacturer-manual-errata)
8. [Verification Status](#8-verification-status)
9. [Not Mapped](#9-not-mapped)
10. [Source Documents](#10-source-documents)

---

## 1. Overview

The DTSD1352 is a three-phase DIN-rail meter (three-phase 3-wire or 4-wire) sold under the Acrel name and also supplied for Sungrow inverters as the "Sungrow DTSD1352". Everything is read with Modbus function 03. Measurements are raw integers that the meter expects the reader to scale with the PT and CT ratios, so the map applies those ratios itself.

## 2. Meter Settings That Affect Scaling

| Setting | Where | Effect in MPG |
| --- | --- | --- |
| PT ratio (menu `In` > `Pt`, register `0x008D`) | Meter keys or Modbus | Multiplies every voltage, power and energy value. Use 1 for direct voltage connection. |
| CT ratio (menu `In` > `Ct`, register `0x008E`) | Meter keys or Modbus | Multiplies every current, power and energy value. Use 1 for a direct-connect (80A) meter; for a CT-operated meter use the CT ratio, for example 40 for 200/5. |

**Set both correctly on the meter.** The map publishes `pt_ratio` and `ct_ratio` and reads them every cycle; if a ratio is wrong, all scaled values are wrong by the same factor. If the ratio rows are not read in a polling pass, the rows that depend on them publish the raw (unscaled) value for that pass.

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
| Voltage and current unbalance, status words, PT/CT ratios, pulse constant, communication settings | `0x003F`, `0x0040`, `0x008D`-`0x0097` | Polled |
| Energy by tariff (sharp, peak, flat, valley) | `0x0002`-`0x003A` | Read Disabled |
| Maximum demand, daily maximum demand, current demand | `0x007B`-`0x0086`, `0x0180`-`0x019B` | Read Disabled |
| Max/Min voltage, current and power records | `0x0200`-`0x0280` | Read Disabled |
| Temperature (optional NTC probes) | `0x2000`-`0x2002` | Read Disabled |
| Real-time clock | `0x003C`-`0x003E` | Read Disabled |
| Harmonic data (`-CH` models): THD, harmonics 2-31, fundamental/harmonic V, I, P, Q | `0x05DD`-`0x06B2` | Read Disabled |
| Second RS485 path (dual-RS485 models) | `0x0096`, `0x0097` | Read Disabled |

Read Disabled rows (`RD` in the `writable` column) stay in the map but are not polled. Enable them in the protocol editor when you have the option fitted or want them. Tariff energies need the meter's multi-tariff mode; the harmonic rows exist only on harmonic models.

## 5. Word Order, Byte Order and Scaling

- **32-bit values (energies and power) are read high word first, big-endian bytes (ABCD)**, the MPG default, so no `Register_Endian` adjustment is used. The manual does not state the order; it was taken from AggSoft's DTSD1352 map (big-endian dwords). Confirm it on your meter (section 8).
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
  
- **Byte-packed registers.** Several settings share one register: register `0x003F` holds the address in its high byte and the baud code in its low byte; `0x0095` holds parity (high) and stop bits (low); `0x0091` holds two running-state bytes; the clock is three registers of two bytes. These use byte offsets in the register column (`0x003F.0` is the high byte, `0x003F.1` the low byte).

## 6. MPG Configuration

``` ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 1
protocol_version = acrel_dtsd1352_c
```

## 7. Manufacturer Manual Errata

| Manual (V3.2) | Used |
| --- | --- |
| Min apparent power of phase C listed at `027EH`; the total follows at `0280H` and every other record has a 4-register pitch | `0x027C` |
| Line voltages labelled "A-B", "C-B", "A-C" in that order | `voltage_l1_l2`, `voltage_l2_l3` (labelled C-B), `voltage_l3_l1` (labelled A-C) |
| "Spike" energy | Named `sharp`, matching the tariff rate list ("1: sharp") |
| Baud code table has no code for 19200 | Not mapped as a code; the value is published as the raw code |
| `0x019B`-`0x01FF` listed as Reserved while `0x019B` is also a demand row | `0x019B` mapped as reverse reactive demand |
| Current imbalance has no unit | 0.1 %, as the voltage imbalance above it |

## 8. Verification Status

The map follows the manufacturer's table but has not been run against a meter. Three points rely on inference and should be checked once:

1. **Word order of 32-bit values.** Taken from AggSoft's map (big-endian dwords). Check that total active energy matches the meter display.
2. **Demand scaling.** The manual says only "keep 3 decimal places"; the map applies PT × CT by analogy with the other power values. These rows are Read Disabled until checked.
3. **Clock byte placement.** Inferred from the manual's SOE example (first-named field in the high byte). Read Disabled until checked.

## 9. Not Mapped

Time-zone and tariff-period tables (`0x0041`-`0x00C3` and the second block at `0x00B2`), history energy and demand "assemblages" for the last 48 months and 90 days (`0x1001`-`0x115A`, read as whole 60-register blocks), frozen-time settings (`0x0121`, `0x0122`), the SOE event log (`0x3001`-`0x3064`), and the time-of-occurrence registers that follow demand and Max/Min values. The bit layout of the DIDO/over-voltage/loss-voltage word (`0x008F`) is not documented by the manufacturer, so it is published as a raw word.

## 10. Source Documents

- Acrel, *DTSD1352 Installation and operation instruction V3.2* (section 9, communication description)
- Acrel, *DTSD1352 Installation and operation instruction V2.19* and *V3.1*
- AggSoft, *Acrel (DTSD1352) - MODBUS Maps* (register list, big-endian dword types)
- `protocols/acrel/acrel_dtsd1352_c.json` and `acrel_dtsd1352_c.holding_registry_map.csv`
