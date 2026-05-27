# Voltronic BMS - MQTT Integration Guide

> **Supported Models:** Voltronic-compatible BMS integrations for Axpert Max, Axpert King, Infinisolar, VM II/III/IV Series and compatible batteries
> **Protocol:** Voltronic BMS Modbus RTU over RS485
> **Interface:** BMS RS485 / RJ45 communication port
> **Status:** Protocols available in MPG

---

## Table of Contents

1. [Overview](#1-overview)
2. [Supported Models & Protocol Versions](#2-supported-models--protocol-versions)
3. [Hardware Requirements](#3-hardware-requirements)
4. [Serial Settings & Addressing](#4-serial-settings--addressing)
5. [Wiring Notes](#5-wiring-notes)
6. [MPG Configuration](#6-mpg-configuration)
7. [Troubleshooting](#7-troubleshooting)
8. [Source Documents](#8-source-documents)

---

## 1. Overview

Voltronic inverter/BMS communication uses Modbus RTU over RS485 for battery telemetry and control/status exchange. MPG includes two Voltronic BMS maps: the 2020-03-25 inverter/BMS protocol and the older BMS Modbus v1.1 map.

The protocols expose cell count, cell voltages, temperature sensors, pack voltage/current, SOC/SOH, charge/discharge limits, warnings, protections, and fault/status flags where available.

---

## 2. Supported Models & Protocol Versions

| Device / Family | Protocol Version | Baud Rate | Notes |
| --- | --- | --- | --- |
| Voltronic inverter and BMS protocol 2020-03-25 | `voltronic_bms_2020_03_25` | 9600 | Used by compatible inverter/BMS integrations |
| Voltronic BMS Modbus RS485 v1.1 | `voltronic_bms_v1.1` | 9600 | Older BMS map with log registers |
| Axpert Max / Axpert King / Infinisolar / VM II/III/IV families | Start with `voltronic_bms_2020_03_25` | 9600 | Confirm against inverter and battery firmware |

Many batteries marketed for Voltronic compatibility may actually expose Pylon, PACE, Daly, or vendor-specific protocols. Select the protocol that the BMS is configured to speak.

---

## 3. Hardware Requirements

### Option A - USB to RS485 Adapter

**What you need:**

- USB to RS485 adapter
- Correct RJ45 or terminal cable for the BMS/inverter port
- Linux/Raspberry Pi host

### Option B - RS485 to Ethernet Bridge

Use a transparent RS485 bridge for remote battery rooms or inverter cabinets. Configure the serial side to 9600 baud, 8 data bits, no parity, 1 stop bit.

### Option C - Existing Inverter BMS Cable

If the inverter already uses the BMS RS485 port, avoid attaching MPG as a second master on that same bus unless the topology is explicitly supported. A passive tap can still disturb communication if the adapter drives the bus.

---

## 4. Serial Settings & Addressing

| Setting | Value |
| --- | --- |
| Baud | 9600 |
| Data bits | 8 |
| Parity | None |
| Stop bits | 1 |
| Function codes | `0x03` read, `0x10` write where supported |

The v1.1 document maps BMS module addresses `0x01` through `0x0F` to DIP switch IDs 1 through 15. The 2020 document describes battery packs communicating among themselves, with one pack communicating to the system over RS485. Make sure each BMS ID is unique in a multi-pack installation.

---

## 5. Wiring Notes

Voltronic-compatible systems often use RJ45 connectors, but pin assignments vary by inverter and battery. Use the inverter/BMS manual for the exact pinout.

Typical RS485 wiring:

``` text
RS485 adapter A+      ->  BMS RS485 A
RS485 adapter B-      ->  BMS RS485 B
RS485 adapter GND     ->  Signal GND, if available
```

If using an RJ45 cable, crimp or break out only the RS485 pair and optional ground. Do not connect Ethernet equipment to the BMS RS485 port.

---

## 6. MPG Configuration

Follow the MultiProtocolGateway Modbus RTU to MQTT configuration guide:
<https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki/Configuration-Examples#modbus-rtu-to-mqtt>

### Voltronic 2020-03-25

``` ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 1
protocol_version = voltronic_bms_2020_03_25
```

### Voltronic BMS v1.1

``` ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 1
protocol_version = voltronic_bms_v1.1
```

---

## 7. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| No response | Wrong protocol selected on BMS | Configure the BMS for Voltronic RS485 or choose the matching MPG protocol |
| Inverter loses BMS comms when MPG is attached | Two masters on one RS485 bus | Use a supported gateway/tap or poll only when inverter is disconnected |
| Only one battery appears | RS485 link reports master pack only | Configure unique BMS IDs or read packs individually |
| Cell temperatures look offset | Kelvin-based temperature scaling in map | Verify scaling against vendor app/manual |
| Timeouts | A/B reversed or missing ground reference | Swap A/B and add signal ground if available |

---

## 8. Source Documents

- `documentation/3rdparty/protocols/Voltronic Inverter and BMS 485 communication protocol 20200325.pdf`
- `documentation/3rdparty/protocols/Voltronic BMS Modbus Protocol for RS485 V1.1(2018-11-15).pdf`
- `protocols/voltronic/voltronic_bms_2020_03_25.json`
- `protocols/voltronic/voltronic_bms_v1.1.json`
