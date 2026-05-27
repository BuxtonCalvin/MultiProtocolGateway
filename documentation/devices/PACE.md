# PACE BMS - MQTT Integration Guide

> **Supported Models:** PACE BMS RS485 v1.3 compatible rack and battery-pack BMS units
> **Protocol:** PACE BMS Modbus RTU over RS485
> **Interface:** RS485 port, often RJ45
> **Status:** Protocol available in MPG

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

PACE BMS units expose battery telemetry using Modbus RTU over RS485. MPG can publish pack voltage, pack current, SOC, cell voltages, temperatures, warning flags, protection flags, fault flags, MOSFET status, and battery serial/register information.

This is the generic PACE guide. For SOK-specific wiring and configuration notes, see [SOK.md](SOK.md).

---

## 2. Supported Models & Protocol Versions

| Device | Protocol Version | Baud Rate | Notes |
| --- | --- | --- | --- |
| PACE BMS RS485 v1.3 | `pace_bms_v1.3` | 9600 | Generic PACE Modbus map |
| SOK / Jakiper PACE-based batteries | `pace_bms_v1.3` | 9600 | See SOK guide for battery-specific notes |

The v1.3 PACE protocol revision adds PACK serial-number registers and additional protection registers compared with earlier v1.0-v1.2 maps.

---

## 3. Hardware Requirements

### Option A - USB to RS485 Adapter

**What you need:**

- USB to RS485 adapter
- RJ45 breakout or model-specific RS485 cable
- Linux/Raspberry Pi host

### Option B - Isolated RS485 Adapter

Use an isolated adapter for battery systems. PACE BMS units are installed in high-current DC environments where ground differences and switching noise can cause unreliable communication or adapter damage.

### Option C - RS485 Hub for Multiple Batteries

For multiple packs, each BMS needs a unique address. Use a proper RS485 bus or hub and configure one MPG device entry per unit ID.

---

## 4. Serial Settings & Addressing

| Setting | Value |
| --- | --- |
| Baud | 9600 |
| Data bits | 8 |
| Parity | None |
| Stop bits | 1 |
| Timeout | 200 ms minimum recommended |
| Frame interval | Greater than 100 ms recommended |

PACE BMS addressing is normally controlled by DIP switches. The protocol document maps addresses `0x01` through `0x0F` to BMS module IDs 1 through 15, with all switches off commonly representing address `0x00` on some hardware.

Use the address documented for your battery, then set MPG `unit_id` to the same value.

---

## 5. Wiring Notes

PACE-based batteries often use RJ45 ports, but the pinout is not universal across every pack enclosure. Verify the battery label or manual before connecting.

Common PACE/SOK-style RS485A pinout:

| RJ45 Pin | Signal | Alternate Pin |
| --- | --- | --- |
| 1 | RS485 A+ | 8 |
| 2 | RS485 B- | 7 |
| 3 | GND | 6 |

``` text
RS485A Pin 1 or 8    ->  Adapter A+
RS485A Pin 2 or 7    ->  Adapter B-
RS485A Pin 3 or 6    ->  Adapter GND (optional)
```

---

## 6. MPG Configuration

Follow the MultiProtocolGateway Modbus RTU to MQTT configuration guide:
<https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki/Configuration-Examples#modbus-rtu-to-mqtt>

### Single BMS

``` ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 1
protocol_version = pace_bms_v1.3
```

### Multiple BMS Units

``` ini
[pace_bms_1]
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 1
protocol_version = pace_bms_v1.3

[pace_bms_2]
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 2
protocol_version = pace_bms_v1.3
```

---

## 7. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| No response | Wrong BMS address | Match `unit_id` to the DIP switch address |
| Timeouts | RS485 A/B reversed | Swap A and B at the adapter |
| Some packs invisible | Duplicate addresses | Assign a unique BMS module ID to each pack |
| Reads fail intermittently | Polling too fast | Increase interval or reduce batch pressure |
| Values look like another BMS family | Wrong protocol selected | Confirm the battery is set to PACE Modbus mode |

---

## 8. Source Documents

- `documentation/3rdparty/protocols/PACE BMS-Modbus-Protocol-for-RS485-V1.3-20170627.pdf`
- `documentation/3rdparty/protocols/PACE BMS-RS485-communication-protocol-20180615.pdf`
- `protocols/pace/pace_bms_v1.3.json`
- `protocols/pace/pace_bms_v1.3.holding_registry_map.csv`
