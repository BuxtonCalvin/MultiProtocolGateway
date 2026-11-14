# Eastron SDM120M - MQTT Integration Guide

> **Supported Models:** Eastron SDM120M single-phase DIN-rail energy meter (also sold under the B+G e-tech name); the same map should apply to the other SDM120 Modbus variants (see section 2)
> **Protocol:** Modbus RTU over RS485 (`eastron_eastron_sdm120m`)
> **Interface:** RS485 terminals on the meter
> **Status:** Protocol available in MPG

---

## Table of Contents

1. [Overview](#1-overview)
2. [Supported Models & Protocol Versions](#2-supported-models--protocol-versions)
3. [Hardware Requirements](#3-hardware-requirements)
4. [Wiring](#4-wiring)
5. [Serial Settings](#5-serial-settings)
6. [What the Map Publishes](#6-what-the-map-publishes)
7. [MPG Configuration](#7-mpg-configuration)
8. [Troubleshooting](#8-troubleshooting)
9. [Not Mapped](#9-not-mapped)
10. [Source Documents](#10-source-documents)

---

## 1. Overview

The SDM120M is a single-phase energy meter with an RS485 Modbus RTU port. Every value is an IEEE 754 32-bit float stored in two registers, big-endian. Measurements are **input registers** (function code 04); the meter's settings are **holding registers** (function code 03, writable with 06 / 10). The `eastron_eastron_sdm120m` protocol therefore ships two CSV files, an input map and a holding map, and the JSON polls only the input registers (`send_input_register = true`, `send_holding_register = false`).

---

## 2. Supported Models & Protocol Versions

| Model | Protocol Version | Notes |
| --- | --- | --- |
| SDM120M (Modbus) | `eastron_eastron_sdm120m` | Mapped |
| SDM120CT-M and other SDM120 Modbus variants | `eastron_eastron_sdm120m` | Same layout; the CT variants add CT1 primary and CT2 secondary settings |
| SDM120 with M-Bus or pulse only | Not supported | No RS485 Modbus port |
| SDM230 / SDM630 | Not covered | Different register tables |

---

## 3. Hardware Requirements

- USB to RS485 adapter (an isolated adapter is recommended) or an RS485 to Ethernet bridge
- Shielded twisted pair for long runs, 120 ohm termination at both ends of the bus
- Linux/Raspberry Pi host

---

## 4. Wiring

The meter datasheet lists terminals 8, 9 and 10 as the RS485 port (signals G, B and A; confirm against the label on your unit). Terminals 1-2 carry the load current, 3-4 are neutral, and 5-7 are the pulse output. Connect meter A to adapter A and B to B. Ground is optional on short runs.

If you get no reply, swap A and B at the adapter first.

---

## 5. Serial Settings

| Setting | Factory default | Options |
| --- | --- | --- |
| Baud | **2400** | 1200, 2400, 4800, 9600 |
| Parity | None | None, even, odd |
| Stop bits | 1 | 1 or 2 (2 only without parity) |
| Data bits | 8 | Fixed |
| Address | 1 | 1-247 |

Many software templates assume 9600 baud, but the meter ships at 2400. Either change the meter or set `baudrate = 2400` in MPG. The JSON file ships `baud = 2400`, but `modbus_rtu` reads `baudrate` from your transport section (default 9600), so **set it explicitly**.

Settings can be changed from the meter's set-up menu or over Modbus (holding registers). The baud-rate register uses codes: 0 = 2400, 1 = 4800, 2 = 9600, 5 = 1200. The parity / stop-bit register uses 0 = one stop bit, no parity; 1 = one stop bit, even; 2 = one stop bit, odd; 3 = two stop bits, no parity. Changes take effect after a restart.

---

## 6. What the Map Publishes

### Input registers (function code 04)

| Group | Registers | Unit |
| --- | --- | --- |
| Voltage, current | `0x0000`, `0x0006` | V, A |
| Active, apparent, reactive power | `0x000C`, `0x0012`, `0x0018` | W, VA, var |
| Power factor | `0x001E` | - |
| Frequency | `0x0046` | Hz |
| Import and export active energy | `0x0048`, `0x004A` | kWh |
| Import and export reactive energy | `0x004C`, `0x004E` | kvarh |
| Power demand (total, import, export) and their maximums | `0x0054`-`0x005E` | W |
| Current demand and maximum | `0x0102`, `0x0108` | A |
| Total active and reactive energy | `0x0156`, `0x0158` | kWh, kvarh |

### Holding registers (settings, read-only in MPG)

Demand time and period, relay pulse width, parity / stop bits, meter ID (address), baud rate, and the CT1 primary / CT2 secondary ratings for the CT variants. The vendor marks these read/write; MPG publishes them as read-only so a stray write cannot change the meter's address or baud rate.

---

## 7. MPG Configuration

Follow the MultiProtocolGateway configuration guide:
<https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki/Configuration-Examples>

``` ini
[transport.eastron_meter]
transport = modbus_rtu
protocol_version = eastron_eastron_sdm120m
port = /dev/ttyUSB0
baudrate = 2400
address = 1
read_interval = 10
bridge = transport.mqtt
device_manufacturer = Eastron
device_model = SDM120M
```

The protocol file asks for 20 registers per read. Reads slower than a few registers per second are normal at 2400 baud (roughly 3-5 registers per second), so keep `read_interval` at 10 seconds or more, or raise the meter to 9600 baud.

---

## 8. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| No response, or all zeros | Baud / parity mismatch | Match the meter's settings. The default is 2400 8N1 |
| Timeouts after changing settings | Meter needs a restart | Power-cycle the meter |
| Nothing from holding registers | The JSON polls input registers only | Enable `send_holding_register` if you want the settings rows |
| Slow updates | Low baud rate | Raise the meter to 9600 baud and update `baudrate` |
| Address conflict on a shared bus | Two meters with the same ID | Give each meter a unique address |

---

## 9. Not Mapped

Three-phase quantities (this is a single-phase map), per-tariff or time-of-use data, the pulse output configuration and the password registers.

---

## 10. Source Documents

- Eastron, SDM120 Modbus protocol and SDM120 series datasheet / manual (RS485 parameters, terminals, register table)
- B+G e-tech / Eastron, SDM120-Modbus user manual: <https://iot-kmutnb.github.io/blogs/sensors/power_meter_sdm120_modbus/sdm120_mid_user_manual.pdf>
- ESPHome, [sdm_meter](https://esphome.io/components/sensor/sdm_meter.html) (factory baud rate)
- `protocols/eastron/eastron_eastron_sdm120m.json`
- `protocols/eastron/eastron_eastron_sdm120m.input_registry_map.csv`
- `protocols/eastron/eastron_eastron_sdm120m.holding_registry_map.csv`
