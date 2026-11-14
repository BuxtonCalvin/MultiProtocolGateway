# ABB B21 / B23 / B24 Energy Meter - MQTT Integration Guide

> **Supported Models:** ABB B21 (single phase), B23 and B24 (three phase) energy meters with RS485 Modbus
> **Protocol:** Modbus RTU over RS485 (`abb_b21`)
> **Interface:** 3-pole RS485 terminal block on the meter
> **Status:** Protocol available in MPG. Single-phase (L1) values only; some addresses are inferred (section 8)
> ABB inverters (TRIO, REACT2, VSN300) are a separate protocol family. See [ABB_Fimer.md](ABB_Fimer.md).

---

## Table of Contents

1. [Overview](#1-overview)
2. [Supported Models & Protocol Versions](#2-supported-models--protocol-versions)
3. [Hardware Requirements](#3-hardware-requirements)
4. [Wiring](#4-wiring)
5. [Serial Settings](#5-serial-settings)
6. [What the Map Publishes](#6-what-the-map-publishes)
7. [MPG Configuration](#7-mpg-configuration)
8. [Verification Status](#8-verification-status)
9. [Troubleshooting](#9-troubleshooting)
10. [Not Mapped](#10-not-mapped)
11. [Source Documents](#11-source-documents)

---

## 1. Overview

The ABB B-series are DIN-rail energy meters with an optional RS485 Modbus RTU interface. The `abb_b21` protocol reads the meter with function code 03 only. Energy counters are 64-bit values (four registers), power quantities are 32-bit, and the meter returns `FFFF...` or `7FFF...` rather than an exception for a register it does not support.

Meters sold for Victron systems must be of the "Silver" class (or better) **with Modbus RS485**; a B-series meter without the RS485 option cannot be read.

---

## 2. Supported Models & Protocol Versions

| Model | Protocol Version | Notes |
| --- | --- | --- |
| ABB B21 | `abb_b21` | Single phase. Fully covered by the map |
| ABB B23 / B24 | `abb_b21` | Three phase. Totals and L1 only; L2 / L3 are not mapped (section 10) |

The address layout is the ABB A/B-series layout (voltage at `0x5B00`, current at `0x5B0C`, power at `0x5B14`, frequency at `0x5B2C`, energies at `0x5000` onward), cross-checked against the Victron `dbus-modbus-client` driver, which has been validated on B21, B23 and B24 hardware.

---

## 3. Hardware Requirements

- USB to RS485 adapter (an isolated adapter is recommended) or an RS485 to Ethernet bridge
- Shielded twisted pair for runs longer than a few metres, with 120 ohm termination at both ends of the bus
- Linux/Raspberry Pi host

---

## 4. Wiring

The meter uses three RS485 terminals (per the SmartgridOne / Eniris wiring guide):

| Signal | Meter terminal |
| --- | --- |
| A | 37 |
| B | 36 |
| C (GND) | 35 |

Match meter A to adapter A and meter B to adapter B. One user connecting a **B23** to an EV charger found that the A and B wires had to be swapped; if a three-phase meter does not answer, swap them before suspecting anything else.

---

## 5. Serial Settings

The sources disagree about the factory serial settings, so **check the meter**:

| Source | Baud | Parity | Stop bits |
| --- | --- | --- | --- |
| Eniris / SmartgridOne B-series guide | 19200 | Even | 2 |
| ABB Terra AC charger load-management guide (factory defaults) | 19200 | Even | 1 |
| ABB EV-meter document used for the map | 9600 | Even | 1 |
| Victron (parity only) | - | Even (factory) | - |

On the meter, press **SET**, choose **RS-485**, then **Prot**, **baud** and **Parity** to see the current values. The Modbus address defaults to 1.

> **Important:** `modbus_rtu` takes its baud rate from the `baudrate` setting in your transport section (default 9600), **not** from the protocol JSON. The JSON ships `baud = 9600`, which will not match a meter still at 19200. Always set `baudrate`, `parity` (`E`) and, if needed, `stopbits` explicitly.

---

## 6. What the Map Publishes

| Group | Registers | Notes |
| --- | --- | --- |
| Active energy | `0x5000`-`0x5008` | Import, export, net (signed), 64-bit, 0.01 kWh |
| Reactive energy | `0x500C`-`0x5014` | Import, export, net, 0.01 kvarh |
| Apparent energy | `0x5018`-`0x5020` | Import, export, net, 0.01 kVAh |
| Tariff energy (B21) | `0x5170`-`0x517C`, `0x5190`-`0x519C` | Active import and export for tariffs 1-4. Zero when a tariff is unused |
| Instantaneous L1 | `0x5B00`-`0x5B3A` | Voltage (0.1 V), current (0.01 A), active power (0.01 W, negative = export), reactive power, apparent power, frequency (0.01 Hz), power factor |
| Identity and status | `0x8900`, `0x8A13` | Serial number, 64 error flags (bit 0 is an EEPROM error) |

Registers are published as holding registers (`send_holding_register = true`). The map has 26 rows, all read-only.

---

## 7. MPG Configuration

Follow the MultiProtocolGateway configuration guide:
<https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki/Configuration-Examples>

``` ini
[transport.abb_meter]
transport = modbus_rtu
protocol_version = abb_b21
port = /dev/ttyUSB0
baudrate = 19200
parity = E
address = 1
read_interval = 10
bridge = transport.mqtt
device_manufacturer = ABB
device_model = B21
```

Add `stopbits = 2` if the meter is set to two stop bits.

---

## 8. Verification Status

The map was built from ABB's EV-meter Modbus map (9AKK107991A9529 Rev B, the same A/B-series layout), the energy-register offsets listed by AggSoft and Quantum Bit Solutions, and the Victron driver. **ABB's own B-series product manual (2CDC512084D0101, chapter 4) could not be retrieved.**

Rows marked INFERRED in the CSV were placed by layout contiguity and should be confirmed on a meter: **reactive power at `0x5B1C` and apparent power at `0x5B24`**. The power factor register is read as signed so that a negative value (export) is not lost; ABB's EV-meter document shows it unsigned.

---

## 9. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| No response | Baud or parity mismatch, A/B swapped, wrong address | Match `baudrate`, `parity`, `stopbits` to the meter menu; swap A and B |
| Values of `65535` / `-1` / `32767` | Register not supported by this meter | Expected for tariff or reactive/apparent rows on some models |
| Reactive or apparent power looks wrong | Inferred addresses | Compare with the meter display and report back |
| L2 / L3 missing on a B23 / B24 | Not mapped | See section 10 |

---

## 10. Not Mapped

On B23 / B24: L2 and L3 voltage (`0x5B02`, `0x5B04`), current (`0x5B0E`, `0x5B10`), power (`0x5B18`, `0x5B1A`), power factor (`0x5B3C`, `0x5B3D`) and per-phase energy from `0x5460`. These can be added as rows if you need them.

---

## 11. Source Documents

- ABB, EV-meter Modbus map 9AKK107991A9529 Rev B
- Victron Energy, [ABB B21, B23 and B24 energy meter](https://www.victronenergy.com/media/pg/Energy_Meter_ABB_B21_B23_and_B24/sv/abb-b21,-b23-and-b24-energy-meter.html) and the `dbus-modbus-client` `abb.py` driver
- Eniris / SmartgridOne, [ABB energy meters](https://docs.eniris.com/en/Controller/Devices/Energy-meter/Abb) (terminals and default serial settings)
- Teltonika Energy Wiki, [ABB settings](https://wiki.teltonika-energy.com/view/ABB_settings)
- ABB, Terra AC load-management quick installation guide
- `protocols/abb/abb_b21.json`
- `protocols/abb/abb_b21.holding_registry_map.csv`
