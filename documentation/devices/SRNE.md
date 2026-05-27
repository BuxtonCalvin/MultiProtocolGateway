# SRNE Inverter / Controller - MQTT Integration Guide

> **Supported Models:** ASF/ASP Split-Phase, HYP, HESP Hybrid, HF/HFP Series, SRNE charge controllers using compatible Modbus maps
> **Protocol:** Modbus RTU over RS485
> **Interface:** RS485 port, Wi-Fi/RS485 port, or model-specific communication terminal
> **Status:** Protocols available in MPG

---

## Table of Contents

1. [Overview](#1-overview)
2. [Supported Models & Protocol Versions](#2-supported-models--protocol-versions)
3. [Hardware Requirements](#3-hardware-requirements)
4. [Port Identification & Wiring](#4-port-identification--wiring)
5. [Protocol Selection](#5-protocol-selection)
6. [MPG Configuration](#6-mpg-configuration)
7. [Troubleshooting](#7-troubleshooting)
8. [Source Documents](#8-source-documents)

---

## 1. Overview

SRNE equipment uses several related Modbus RTU register maps across charge controllers and hybrid inverters. MPG includes protocol definitions for the older controller-oriented v3.9 map and the newer energy-storage inverter maps v1.7 and v1.96.

These protocols publish PV input, battery voltage/current/SOC, grid values, inverter output, load, charge state, device state, temperatures, alarms, fault flags, and selected writable settings where the vendor map allows writes.

---

## 2. Supported Models & Protocol Versions

| Device / Family | Protocol Version | Interface | Notes |
| --- | --- | --- | --- |
| SRNE charge controllers and controller-derived devices | `srne_v3.9` | RS485 | Vendor MODBUS v3.9, 2020-04-21 |
| SRNE energy-storage inverters | `srne_v1.7` | RS485 | Includes split-phase / three-phase style output registers |
| SRNE energy-storage inverters, 2021 revision | `srne_2021_v1.96` | RS485 | Adds newer product type and charge-state fields |
| ASF/ASP Split-Phase, HYP, HESP, HF/HFP families | Start with `srne_v1.96` or `srne_v3.9` | RS485 | Pick based on which register map matches live data |

If you are unsure which map applies, start with the model's vendor protocol document. If that is unavailable, scan with `srne_2021_v1.96` first for hybrid inverters and `srne_v3.9` first for charge controllers.

---

## 3. Hardware Requirements

### Option A - USB to RS485 Adapter

**What you need:**

- USB to RS485 adapter
- Model-specific SRNE RS485 cable or RJ45 breakout
- Linux/Raspberry Pi host

### Option B - RS485 to Ethernet/Wi-Fi Bridge

Use a bridge when the inverter is far from the MPG host. Configure transparent serial mode and match the serial settings below.

| Setting | Value |
| --- | --- |
| Baud | 9600 |
| Data bits | 8 |
| Parity | None |
| Stop bits | 1 |
| Address range | 1-247 |

---

## 4. Port Identification & Wiring

SRNE models vary. Some label a dedicated `RS485` port, while others share communication through a Wi-Fi/RS485 port or a terminal connector.

Typical wiring:

``` text
RS485 adapter A+      ->  SRNE RS485 A
RS485 adapter B-      ->  SRNE RS485 B
RS485 adapter GND     ->  Signal GND, if provided
```

If the connector is RJ45, confirm the model-specific pinout before crimping. Do not assume Ethernet pin assignments.

---

## 5. Protocol Selection

| Symptom / Device Type | Try First | Why |
| --- | --- | --- |
| Charge controller values: PV voltage/current, load DC power, battery SOC | `srne_v3.9` | Matches SRNE MODBUS v3.9 controller map |
| Hybrid inverter values: grid phase, inverter phase, AC load, PV1/PV2 | `srne_2021_v1.96` | Newer energy-storage inverter map |
| Older hybrid inverter with state names like "Mains powered operation" | `srne_v1.7` | Older inverter state map |

When the wrong map is selected, communication may still succeed but values will be missing, offset, or nonsensical.

---

## 6. MPG Configuration

Follow the MultiProtocolGateway Modbus RTU to MQTT configuration guide:
<https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki/Configuration-Examples#modbus-rtu-to-mqtt>

### SRNE v3.9

``` ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 1
protocol_version = srne_v3.9
```

### SRNE v1.7

``` ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 1
protocol_version = srne_v1.7
```

### SRNE 2021 v1.96

``` ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 1
protocol_version = srne_2021_v1.96
```

---

## 7. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| No response | A/B reversed or wrong port | Swap A/B and verify the RS485 connector |
| Values are shifted or impossible | Wrong SRNE protocol version | Try the other SRNE protocol maps |
| Only controller values appear on inverter | Using `srne_v3.9` against an inverter map | Try `srne_2021_v1.96` |
| Writes fail | Register not writable on that firmware | Confirm vendor manual and enable MPG write support only for known-safe registers |
| Intermittent reads | Long or noisy RS485 cable | Use shielded twisted pair, termination, and an isolated adapter |

---

## 8. Source Documents

- `documentation/3rdparty/protocols/SRNE MODBUS_v3.9.pdf`
- `documentation/3rdparty/protocols/SRNE Solar.Charge.Inverter.MODBUS.Protocol1.96.pdf`
- `protocols/srne/srne_v3.9.json`
- `protocols/srne/srne_v1.7.json`
- `protocols/srne/srne_2021_v1.96.json`
