# Sungrow Inverter - MQTT Integration Guide

> **Supported Models:** SG Series string inverters, SH5.0/6.0/8.0/10RT hybrids, SG350HX and related Sungrow families
> **Protocol:** Modbus RTU over RS485 or Modbus TCP over Ethernet, model dependent
> **Interface:** COM/RS485 port or Ethernet
> **Status:** Device documentation added; MPG protocol folder exists but no Sungrow protocol JSON is currently committed

---

## Table of Contents

1. [Overview](#1-overview)
2. [Supported Models & Protocol Status](#2-supported-models--protocol-status)
3. [Hardware Requirements](#3-hardware-requirements)
4. [RS485 Wiring](#4-rs485-wiring)
5. [Ethernet / Modbus TCP Setup](#5-ethernet--modbus-tcp-setup)
6. [MPG Configuration](#6-mpg-configuration)
7. [Troubleshooting](#7-troubleshooting)
8. [Source Documents](#8-source-documents)

---

## 1. Overview

Sungrow inverters commonly support third-party monitoring through Modbus RTU over RS485 and, on many models, Modbus TCP over Ethernet. This guide documents the physical connection and MPG setup pattern for Sungrow equipment.

Important MPG status: the repository contains a `protocols/sungrow/` folder, but it is currently empty. A Sungrow register map must be added before a `protocol_version` can be selected in MPG.

---

## 2. Supported Models & Protocol Status

| Model Family | Modbus RTU RS485 | Modbus TCP Ethernet | MPG Protocol |
| --- | --- | --- | --- |
| SGxxRT | Supported by Sungrow third-party compatibility docs | Supported | Not yet committed |
| SGxxRS | Supported | Supported | Not yet committed |
| SHxxRS | Supported | Supported | Not yet committed |
| SHxxRT | Supported | Supported | Not yet committed |
| SGxxCX / SGxxCX-P2 | Supported | Supported | Not yet committed |
| SG350HX / related utility string inverters | RS485 available per product documentation | Model/accessory dependent | Not yet committed |

Once a Sungrow JSON protocol is added under `protocols/sungrow/`, this guide should be updated with the exact `protocol_version`.

---

## 3. Hardware Requirements

### RS485 Path

- USB to RS485 adapter or RS485 bridge
- Sungrow COM/RS485 cable or terminal breakout
- Shielded twisted pair for longer runs

### Ethernet Path

- Inverter connected to LAN
- Modbus TCP enabled in the inverter communication settings
- Static IP or DHCP reservation

Use Ethernet/Modbus TCP when available. It avoids serial adapter wiring issues and is easier to isolate from inverter cabinet electrical noise.

---

## 4. RS485 Wiring

A common Sungrow RS485 connection pattern is:

``` text
RS485 adapter A+      ->  Sungrow COM pin 1 / RS485 A+
RS485 adapter B-      ->  Sungrow COM pin 2 / RS485 B-
RS485 adapter GND     ->  Sungrow COM pin 3 / GND, if available
```

Always confirm the exact COM connector pinout in the inverter manual. Different Sungrow product families use different physical connectors.

Recommended serial defaults for Modbus RTU:

| Setting | Value |
| --- | --- |
| Baud | Model setting; commonly 9600 |
| Data bits | 8 |
| Parity | None unless configured otherwise |
| Stop bits | 1 |
| Unit ID | Inverter communication address, 1-247 |

---

## 5. Ethernet / Modbus TCP Setup

On models that support Modbus TCP:

1. Open the inverter display, commissioning app, or installer interface.
2. Navigate to communication parameters.
3. Enable Modbus TCP.
4. Confirm the device address / unit ID.
5. Assign a static IP or DHCP reservation.
6. Use the configured TCP port, commonly `502`.

Network monitoring should be kept on the local LAN. Do not expose Modbus TCP directly to the internet.

---

## 6. MPG Configuration

This section is intentionally marked as pending because no Sungrow protocol JSON is currently present in the repository.

Expected Modbus TCP shape after a protocol is added:

``` ini
transport = modbus_tcp
host = 192.168.1.50
port = 502
unit_id = 1
protocol_version = sungrow_<map_name>
```

Expected RS485 shape after a protocol is added:

``` ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 1
protocol_version = sungrow_<map_name>
```

Replace `sungrow_<map_name>` with the actual protocol file name once it exists under `protocols/sungrow/`.

---

## 7. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| MPG cannot load protocol | No Sungrow protocol JSON exists yet | Add a Sungrow register map under `protocols/sungrow/` |
| Modbus TCP connection refused | Modbus TCP disabled or wrong port | Enable Modbus TCP and verify port `502` or configured port |
| RS485 timeouts | A/B reversed or wrong COM pins | Confirm Sungrow pinout and swap A/B if needed |
| Reads work from another tool but not MPG | Unit ID mismatch | Match MPG `unit_id` to inverter communication address |
| Values differ by model | Different Sungrow register map | Use the register map for the exact inverter family |

---

## 8. Source Documents

- Sungrow third-party compatibility overview: <https://ger.sungrowpower.com/upload/file/20250212/EN_Third-party_Compatibility_Overview.pdf>
- Sungrow SG350HX product documentation: <https://en.sungrowpower.com/productDetail/2305/string-inverter-sg350hx>
- SmartgridOne Sungrow connection notes: <https://docs.eniris.io/en/Controller/Devices/PV-hybrid-and-battery-inverters/Sungrow/Sungrow%20Inverters>
- `protocols/sungrow/` (currently empty)
