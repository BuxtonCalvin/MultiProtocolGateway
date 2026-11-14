# JBD (Xiaoxiang / Overkill Solar) BMS - MQTT Integration Guide

> **Supported Models:** JBD / Jiabaida "software board" smart BMS, sold as Xiaoxiang, Overkill Solar and many white-label packs
> **Protocol:** JBD general protocol V4 over UART / RS232 / RS485 (`jbd_bms`)
> **Transport:** `serial_jbd` (not Modbus)
> **Status:** Protocol available in MPG, **read-only**

---

## Table of Contents

1. [Overview](#1-overview)
2. [Supported Models & Protocol Versions](#2-supported-models--protocol-versions)
3. [Hardware Requirements](#3-hardware-requirements)
4. [Serial Settings & Addressing](#4-serial-settings--addressing)
5. [How the Protocol Works](#5-how-the-protocol-works)
6. [What the Map Publishes](#6-what-the-map-publishes)
7. [MPG Configuration](#7-mpg-configuration)
8. [Troubleshooting](#8-troubleshooting)
9. [Not Implemented](#9-not-implemented)
10. [Source Documents](#10-source-documents)

---

## 1. Overview

JBD battery management systems report pack voltage, current, capacity, state of charge, cell voltages, temperatures, balancing state and protection flags over a simple binary request / response protocol. MPG reads them with the dedicated `serial_jbd` transport and the `jbd_bms` protocol. This is **not** a Modbus protocol, so there are no register numbers on the wire; the protocol CSV names each value by *command and byte offset*.

---

## 2. Supported Models & Protocol Versions

| Device | Protocol Version | Transport | Baud |
| --- | --- | --- | --- |
| JBD / Xiaoxiang / Overkill Solar smart BMS with UART, RS232 or RS485 | `jbd_bms` | `serial_jbd` | 9600 |

The Bluetooth interface of these BMS boards is not used. If your pack has only Bluetooth, you need a board with the UART header or an RS485 / RS232 module.

---

## 3. Hardware Requirements

- A USB serial adapter that matches the BMS port: **USB-TTL** for a bare UART header, USB-RS232 for an RS232 port, USB-RS485 for an RS485 module. Check the voltage level of your board's UART before connecting a TTL adapter.
- Cable to the BMS UART / communication connector
- Linux/Raspberry Pi host with a free USB port

Because the protocol has no address field, **one BMS can be connected per serial port**. To read several packs, use one adapter per pack.

---

## 4. Serial Settings & Addressing

| Setting | Value |
| --- | --- |
| Baud | 9600 (the V4 protocol document also allows customer-defined rates) |
| Data / parity / stop | 8N1 |
| Address | None |

Unlike Modbus devices, the protocol has no address field (the transport's maximum address is 0), so no `address` is needed.

---

## 5. How the Protocol Works

Every frame starts with `0xDD` and ends with `0x77`:

- **Request:** `DD A5 <command> <length> <data> <checksum:2> 77`
- **Response:** `DD <command> <status> <length> <data> <checksum:2> 77`

The checksum is 0x10000 minus the sum of the bytes from the third byte through the data, sent high byte first. A response status other than `0x00` means the BMS refused the command and carries no data. Because the data is binary, `0x77` can appear inside it (a 2.935 V cell is `0B 77`), so the transport reads a frame by its length byte rather than by scanning for the end marker.

MPG polls three commands:

| Command | Content |
| --- | --- |
| `0x03` | Pack status: voltage, current, capacities, cycles, production date, balance and protection flags, software version, SOC, MOSFET state, cell and sensor counts, temperatures |
| `0x04` | Individual cell voltages (up to 32) |
| `0x05` | Hardware version string (up to 31 characters) |

---

## 6. What the Map Publishes

The register column in `jbd_bms.registry_map.csv` is `<command>.<byte offset>`, for example `3.0` is command `0x03` at offset 0 and `4.2` is command `0x04` at offset 2.

| Group | Entries | Notes |
| --- | --- | --- |
| Pack | `total_voltage`, `current`, `residual_capacity`, `nominal_capacity`, `cycle_count`, `state_of_charge` | Voltage 0.01 V, current 0.01 A (**positive is charging, negative is discharging**), capacity 0.01 Ah |
| Identity | `production_date_raw`, `software_version`, `hardware_version`, `cell_count`, `temperature_sensor_count` | Date is packed: bits 15-9 year since 2000, bits 8-5 month, bits 4-0 day. Software `0x10` is version 1.0 |
| Flags | `balance_status_1_16`, `balance_status_17_32`, `protection_status`, `mosfet_status` | Bit names come from the code tables in `jbd_bms.json` (cell n balancing; over / under voltage, over / under temperature, charge / discharge over-current, short circuit, front-end IC error, software MOSFET lock) |
| Temperatures | `temperature_1` to `temperature_6` | Raw value is tenths of a kelvin; the adjustment converts to degrees C |
| Cells | `cell_1_voltage` to `cell_32_voltage` | Millivolts |

Rows beyond the cell count or sensor count that the pack reports are dropped, so a 16-cell pack publishes 16 cell voltages.

---

## 7. MPG Configuration

Follow the MultiProtocolGateway configuration guide:
<https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki/Configuration-Examples>

``` ini
[transport.jbd_battery]
transport = serial_jbd
protocol_version = jbd_bms
port = /dev/ttyUSB0
baudrate = 9600
read_interval = 15
bridge = transport.mqtt
device_manufacturer = JBD
device_model = Smart BMS
```

`baud` is accepted as an alias for `baudrate`. The web UI default timeout is 5 seconds.

---

## 8. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| No data | TX / RX swapped, wrong adapter type, wrong baud | Swap TX and RX on a UART; confirm the port level (TTL / RS232 / RS485) |
| Log shows "checksum error" | Noise or wrong baud | Shorten the cable, confirm 9600 baud |
| Log shows "refused with status" | BMS rejected the command | Check that the BMS firmware supports commands `0x03`, `0x04` and `0x05` |
| Only one of two packs appears | Two packs on one port | One pack per port (no addressing) |
| Temperature looks 273 degrees too high | Raw kelvin shown | The adjustment on `temperature_N` rows was removed or overridden |

---

## 9. Not Implemented

Writing to the BMS (MOSFET control, parameter changes, factory mode) is **not implemented**. The V4 protocol defines write commands (`0xDD 0x5A ...`); MPG does not send them, so a JBD pack cannot be controlled from MPG.

---

## 10. Source Documents

- Jiabaida, *Software board general protocol V4* (UART / RS232 / RS485): <https://forum.mypower.cz/download/file.php?id=27163>
- powermon protocol notes: <https://jblance.github.io/powermon/protocol_docs/jkv4/>
- `classes/transports/serial_jbd.py` and `pytests/test_serial_jbd.py`
- `protocols/jbd/jbd_bms.json`
- `protocols/jbd/jbd_bms.registry_map.csv`
