# Next Power Victor NM RE - MQTT Integration Guide

> **Supported Models:** Victor NM-ECO-LV 3.6KW Plus, Victor NM-ECO-LV 6.2KW Plus, related Next Power NM ECO models
> **Protocol:** Modbus RTU over serial
> **Interface:** Battery communication / RS485 port
> **Status:** Protocol available in MPG; hardware pinout should be verified on the target inverter

---

## Table of Contents

1. [Overview](#1-overview)
2. [Supported Models & Protocol Versions](#2-supported-models--protocol-versions)
3. [Hardware Requirements](#3-hardware-requirements)
4. [Port Identification & Wiring](#4-port-identification--wiring)
5. [MPG Configuration](#5-mpg-configuration)
6. [Troubleshooting](#6-troubleshooting)
7. [Source Documents](#7-source-documents)

---

## 1. Overview

Next Power Victor NM ECO inverters expose system operating values over a low-speed serial register map. MPG includes the `next_power_victor_nm_re` protocol for inverter state, PV input, battery voltage/current, output load, temperatures, fault codes, and selected configuration registers.

The public user manual identifies both a Wi-Fi/RS232 communication port and a battery communication/RS485 port. Use the RS485 port for this MPG protocol unless your exact inverter revision documents a different service port for the register map.

---

## 2. Supported Models & Protocol Versions

| Model | Protocol Version | Baud Rate | Notes |
| --- | --- | --- | --- |
| Victor NM-ECO-LV 3.6KW Plus | `next_power_victor_nm_re` | 2400 | Verify port pinout before connection |
| Victor NM-ECO-LV 6.2KW Plus | `next_power_victor_nm_re` | 2400 | Listed in MPG tested-device notes |
| Related NM ECO / rebranded units | `next_power_victor_nm_re` | 2400 | Register compatibility likely but unconfirmed |

The MPG protocol reads holding registers beginning around `4501`, including working mode, mains voltage/frequency, PV voltage/current/power, battery state, load power, output mode, priorities, and charge settings.

---

## 3. Hardware Requirements

### Option A - USB to RS485 Adapter

**What you need:**

- USB to RS485 adapter with A/B terminals
- Correct cable for the inverter communication port
- Linux/Raspberry Pi host with an available USB port

Use an isolated adapter for permanent installs, especially where the inverter and host are powered from different circuits.

### Option B - RS485 to Ethernet/Wi-Fi Bridge

For a remote inverter, use a transparent RS485 bridge and set the serial side to 2400 baud, 8 data bits, no parity, 1 stop bit unless your inverter manual says otherwise.

---

## 4. Port Identification & Wiring

The NM ECO user manual labels:

| Port | Manual Description | MPG Use |
| --- | --- | --- |
| Wi-Fi communication / RS232 | Monitoring dongle or serial accessory | Do not use unless your hardware revision exposes the register map there |
| Battery communication / RS485 | Battery/BMS communication port | Preferred port for MPG |

Because pinouts vary across NM ECO rebrands, confirm the RJ45 or terminal pinout from the label, service manual, or continuity-tested cable before connecting.

Typical RS485 wiring:

``` text
RS485 adapter A+      ->  Inverter RS485 A
RS485 adapter B-      ->  Inverter RS485 B
RS485 adapter GND     ->  Signal GND, if provided
```

If the inverter uses an RJ45 port, do not assume standard Ethernet wiring. Only the RS485 pair should be connected to the adapter.

---

## 5. MPG Configuration

Follow the MultiProtocolGateway Modbus RTU to MQTT configuration guide:
<https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki/Configuration-Examples#modbus-rtu-to-mqtt>

``` ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 2400
unit_id = 1
protocol_version = next_power_victor_nm_re
```

If you use a network bridge, keep the same baud rate on the serial side and point MPG at the bridge host/port.

---

## 6. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| No Modbus response | Wrong port or wrong pinout | Verify the battery communication / RS485 port and cable pinout |
| Timeouts at 9600 baud | Protocol requires 2400 baud | Set `baud = 2400` |
| Garbage values | Rebrand uses a different register map | Compare live registers against `next_power_victor_nm_re.holding_registry_map.csv` |
| Only battery data appears | Connected to BMS-only port | Check whether another serial port exposes inverter telemetry |
| Writes do not persist | Setting register may be read-only on your firmware | Treat write-capable entries as model-dependent |

---

## 7. Source Documents

- `protocols/next_power/next_power_victor_nm_re.json`
- `protocols/next_power/next_power_victor_nm_re.holding_registry_map.csv`
- Next Power NM ECO Solar Inverter User Manual: <https://manuals.plus/ae/1005008122667693>
