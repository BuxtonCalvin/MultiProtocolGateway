# JK (Jikong) BMS - MQTT Integration Guide

> **Supported Models:** Jikong JK-B / JK-BD series and JK-PB inverter / server-rack BMS
> **Protocols:** two wired protocols, selected by the BMS UART setting: **`jk_bms_modbus_rtu`** (RS485 Modbus RTU) and **`jk_bms_4g_gps_serial`** (JK's 4G-GPS `4E 57` binary protocol)
> **Interface:** UART1 / RS485 port on the BMS through a USB serial adapter
> **Status:** both protocols available in MPG, read-only. **Bluetooth is not yet supported**

---

## Table of Contents

1. [Overview](#1-overview)
2. [Which Protocol Should I Choose?](#2-which-protocol-should-i-choose)
3. [Supported Models](#3-supported-models)
4. [Hardware Requirements](#4-hardware-requirements)
5. [BMS Port Setup](#5-bms-port-setup)
6. [MPG Configuration](#6-mpg-configuration)
7. [`jk_bms_modbus_rtu` Map Notes](#7-jk_bms_modbus_rtu-map-notes)
8. [`jk_bms_4g_gps_serial` Map Notes](#8-jk_bms_4g_gps_serial-map-notes)
9. [How the Two Maps Differ](#9-how-the-two-maps-differ)
10. [Troubleshooting](#10-troubleshooting)
11. [Source Documents](#11-source-documents)

---

## 1. Overview

A JK BMS can be read three different ways, and each uses a **different protocol with different data**. MPG supports the first two and names each protocol after the interface it reads, so you can pick the right one from your BMS settings:

| Interface | JK name in the BMS settings | MPG protocol | MPG transport |
| --- | --- | --- | --- |
| RS485 Modbus RTU | **JK BMS RS485 Modbus V1.0** (UART protocol `001`, or `013` at 9600 baud) | `jk_bms_modbus_rtu` | `modbus_rtu` |
| 4G-GPS binary serial | **4G-GPS Remote Module Common Protocol** (UART protocol `000`) | `jk_bms_4g_gps_serial` | `serial_jk` |
| Bluetooth (BLE) | The phone-app link | *not supported yet* | - |

> **Upgrade note:** the 4G-GPS protocol was previously called `jk_bms_rs485`. That name suggested RS485 Modbus, which it is not, so it was renamed `jk_bms_4g_gps_serial` (files `jk_bms_4g_gps_serial.json` and `jk_bms_4g_gps_serial.registry_map.csv`). If you already use it, change `protocol_version` in your configuration.

---

## 2. Which Protocol Should I Choose?

| If your BMS UART port is set to... | Use `protocol_version` | Transport | Baud |
| --- | --- | --- | --- |
| `001` JK BMS RS485 Modbus V1.0 | `jk_bms_modbus_rtu` | `modbus_rtu` | 115200 |
| `013` (9600) JK BMS RS485 Modbus V1.0 | `jk_bms_modbus_rtu` | `modbus_rtu` | **9600** (set `baudrate = 9600`) |
| `000` 4G-GPS Remote Module Common Protocol V4.2 | `jk_bms_4g_gps_serial` | `serial_jk` | 115200 |
| Bluetooth only | *none yet* | - | - |

**Recommendation:** use `jk_bms_modbus_rtu` whenever your BMS supports it (JK-PB and newer boards). It is a standard Modbus protocol, exposes far more data (per-cell balance-line resistance, balancing current and state, state of health, remaining and full capacity, runtime, five temperature sensors, 24 alarm bits and the protection settings) and can serve several packs on one bus. Use `jk_bms_4g_gps_serial` for older JK-B / JK-BD boards, or where the port cannot be set to Modbus.

A BMS port runs only **one** protocol at a time, and the two protocols cannot be read from the same port at once. If an inverter already talks to the BMS on that port, use a different UART or the second port for MPG.

### How to tell what a port is set to

- The two protocols are not interchangeable. Pointing `modbus_rtu` at a port set to `000`, or `serial_jk` at a port set to `001`/`013`, will not work.
- The Modbus map itself publishes `uart1_protocol`, `uart2_protocol` and `can_protocol` (with names), so once Modbus works you can see how each port is configured.

---

## 3. Supported Models

| Model | Protocol Version | Notes |
| --- | --- | --- |
| JK-PB series (PB1A16S, PB2A16S, ...) | `jk_bms_modbus_rtu` (preferred) or `jk_bms_4g_gps_serial` | UART1 can be set to either; UART2 offers Modbus only |
| JK-B / JK-BD series | `jk_bms_4g_gps_serial`, or `jk_bms_modbus_rtu` where the firmware offers it | Check the available UART protocols on your board |

The Modbus map is built from JK's published register table and from addresses tested in the ESPHome JK-PB integration. It has **not been run against real hardware by its author**. Please report differences you find on your model.

---

## 4. Hardware Requirements

- A USB to RS485 or USB to TTL adapter, matching the BMS port. Many JK boards expose the "RS485-TTL" jack as a **4-pin JST 1.25 mm** connector that carries 3.3 V TTL signals even though it is labelled RS485. Check the board manual, and use an RS485 adapter only where the port truly is RS485
- An adapter that runs 115200 baud reliably
- An isolated adapter is recommended, because the BMS ground is the battery negative
- A cable matched to your port pinout (RJ45 RS485 ports vary by model)

---

## 5. BMS Port Setup

On boards with several UART ports, choose the protocol on the port you connect to. UART1 options listed by community documentation for the JK-PB:

| Code | Protocol | MPG protocol |
| --- | --- | --- |
| `000` | 4G-GPS / GPS Remote Module Common Protocol V4.2 | `jk_bms_4g_gps_serial` |
| `001` | JK BMS RS485 Modbus V1.0 | `jk_bms_modbus_rtu` (115200) |
| `013` | (9600) JK BMS RS485 Modbus V1.0 | `jk_bms_modbus_rtu` (9600) |
| `004` | PACE RS485 Modbus V1.3 | [PACE](PACE.md) |
| `005` / `014` | Pylon low-voltage RS485 V3.5 (`014` at 9600) | [Pylon](Pylon.md) |
| `006` | Growatt BMS RS485 1xSxxP ESS Rev 2.01 | [Growatt](Growatt.md) |
| `007` | Voltronic inverter and BMS RS485 | [Voltronic](Voltronic.md) |

Other options (NIU U series, China Tower, WOW, JK LCD V2.0, user customization) are not covered by MPG.

**Modbus slave address:** set by the BMS DIP switches. On **UART2**, address `0` makes the pack a Modbus *master* that polls other packs, so use addresses `1`-`15` for a pack you want MPG to read. On **UART1** any address `0`-`15` works because UART1 is always a slave. UART2 offers only protocol `001` (and `015`).

---

## 6. MPG Configuration

Follow the MultiProtocolGateway configuration guide:
<https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki/Configuration-Examples>

### RS485 Modbus RTU (`jk_bms_modbus_rtu`)

``` ini
[transport.jk_battery]
transport = modbus_rtu
protocol_version = jk_bms_modbus_rtu
port = /dev/ttyUSB0
baudrate = 115200
address = 1
read_interval = 15
bridge = transport.mqtt
device_manufacturer = Jikong
device_model = JK-PB2A16S20P
```

Use `baudrate = 9600` if the port is set to protocol `013`. Several packs on one UART2 bus each need their own `[transport.xxx]` section with a unique `address` (1-15).

### 4G-GPS serial (`jk_bms_4g_gps_serial`)

``` ini
[transport.jk_battery]
transport = serial_jk
protocol_version = jk_bms_4g_gps_serial
port = /dev/ttyUSB0
baudrate = 115200
address = 0
read_interval = 15
bridge = transport.mqtt
device_manufacturer = Jikong
device_model = JK-BD
```

For `serial_jk`, `address` is the JK *terminal number* (0 to 0xFFFFFF, the low 24 bits are used), sent in the frame's 4-byte terminal field. Use the device address configured on your BMS (for example `0`). The web UI default is 115200 baud and a 5 second timeout.

---

## 7. `jk_bms_modbus_rtu` Map Notes

The map has 159 rows and publishes:

| Group | Contents |
| --- | --- |
| Pack | total voltage, power, current, state of charge, state of health, remaining / full-charge / cycle capacity, cycle count, runtime |
| Cells | up to 32 cell voltages, cell presence flags, average voltage, voltage difference, highest and lowest cell number |
| Temperatures | MOSFET temperature and five battery temperatures (0.1 °C) |
| Balancing | balancing current and state, balance-line resistance for 32 cells (hourly), balance-line alarm flags |
| Status | 24-bit alarm flags, charge / discharge / precharge status, charger plugged, timers |
| Settings (daily, read-only) | cell and pack voltage limits, charge and discharge current limits, protection delays, temperature limits, cell count, switches, design capacity |
| Device info (daily) | model, hardware and software version, serial number, power-on count, UART1 / UART2 / CAN protocol numbers with names |

Things to know:

- **Byte addressing.** JK's table uses byte offsets (16-bit values step by 2, 32-bit by 4), so the protocol JSON sets `register_address_step = 2` and MPG places word *i* of a block read at `first address + 2 * i`. The CSV keeps JK's own addresses (the hex address is also at the start of each note). Do not change `register_address_step` or the even addresses. See [Byte-Addressed Devices](../usage/protocols.md#byte-addressed-devices-register_address_step).
- **Few requests, safe masks.** Neighbouring rows are read together as blocks of up to 64 words (`batch_size = 128` addresses). The first poll takes about 8 requests; once the slow rows settle (settings and device info once a day, balance-line resistance hourly) a normal poll is about 3. A `variable_mask` allowlist is safe for any selection, because MPG always starts a block at the first row it still reads. This keeps traffic to the BMS low, which matters because JK boards can stall under heavy polling.
- **Byte order.** 32-bit values are standard big-endian (high word first). For registers that hold two 8-bit fields, JK lists the **high byte first**: for example `0x12A6` is balancing state (high byte) and state of charge (low byte). The map uses `.b8` and `.b0` accordingly.
- **Everything is read-only.** JK accepts only Modbus function codes 03 (read) and 16 (write multiple registers), and several settings share one register as two bytes, so a careless write would also change the other byte. The settings block is mapped for monitoring only. The `0x1600` control block (calibration, shutdown, chemistry presets, bulk-frame download) is not mapped.
- **Power is unsigned.** JK lists `power` as unsigned; take the direction (charge or discharge) from the sign of `current`.
- **Not covered:** per-cell connection-line resistance settings, the temperature-sensor presence byte, the voltage-correction float, buzzer and dry-node settings, and the Bluetooth name, PIN, password and user-data registers (deliberately left out). Alarm bits 22 and 23 are numbered by their order in JK's table. The two "user alarm" words have no published bit meanings.

---

## 8. `jk_bms_4g_gps_serial` Map Notes

- Register numbers are the JK data identifiers (for example `128` is `0x80`, MOSFET temperature, and `131` is `0x83`, total voltage). Cell voltages come from identifier `0x79` as groups of three bytes (cell number, millivolts), written as `121.1`, `121.4`, and so on.
- The map carries 46 rows: up to 32 cell voltages plus MOSFET, battery-box and battery temperatures, total voltage, current, state of charge, sensor count, cycle count, total cycle capacity, cell count, warning and status flags, software version and the protocol version byte.
- Total voltage is in units of 0.01 V and current in 0.01 A (10 mA).
- The transport normalizes two values before the map decodes them: current (identifier `0x84`) has two encodings selected by the protocol version byte (`0xC0`), and temperatures above 100 mean negative degrees. The current is rewritten so that charging is positive.
- `warning_status` and `battery_status` are 16-bit flag words. Names of each bit are in `jk_bms_4g_gps_serial.json`:
  - Warning bits include low capacity, MOSFET over-temperature, charge over-voltage, discharge under-voltage, battery over-temperature, charge/discharge over-current, cell voltage difference, battery-box over-temperature, low temperature and cell over/under-voltage.
  - Battery-status bits are charging MOSFET on, discharging MOSFET on, balancing on and battery connected.
- Rows for cells beyond the cell count the pack reports are dropped.
- Writing parameters (limits, balancing, MOSFET control) is not implemented.

---

## 9. How the Two Maps Differ

Variable names match where the quantity is the same, so Home Assistant entities and MQTT topics stay familiar if you switch protocol:

| Value | `jk_bms_modbus_rtu` | `jk_bms_4g_gps_serial` |
| --- | --- | --- |
| Cell voltages | `cell_1_voltage` ... `cell_32_voltage` | same names |
| MOSFET temperature | `mosfet_temperature` (0.1 °C) | same name |
| Total voltage | `total_voltage` (1 mV resolution) | same name (10 mV resolution) |
| Current | `current` (1 mA resolution) | same name (10 mA resolution) |
| State of charge, cycle count, total cycle capacity | same names | same names |
| Battery temperatures | `battery_temperature_1` ... `_5` | `battery_temperature`, `battery_box_temperature` |
| Alarms | `alarm_status` (24 named bits) | `warning_status`, `battery_status` |
| State of health, balancing, capacity, power, runtime | published | not available |
| Protection settings, device info | published (read-only) | not available |
| Several packs on one bus | yes (addresses 1-15) | one pack per port |
| Write support | none yet | none yet |

Pick one protocol per BMS. Running both against the same pack would publish duplicate entities.

---

## 10. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| No response | Wrong port, TTL/RS485 mismatch or TX/RX swapped | Confirm the connector is 3.3 V TTL or RS485 as labelled; swap TX/RX or A/B |
| No response with the right wiring | The BMS port is set to a different protocol | Match the table in Section 2 to the UART setting in the BMS |
| Modbus: timeouts at 115200 | Port is set to `013` | Set `baudrate = 9600` |
| Modbus: nothing from a pack on UART2 | DIP address is `0` (master mode) | Use an address 1-15 |
| Modbus: BMS stops answering or reads time out | Too much polling, or another master on the same bus | Raise `read_interval`; use a `variable_mask` allowlist; make sure only one master polls the port |
| Modbus: exception responses or wrong values | Edited addresses, step or `batch_size` | Leave `register_address_step` at 2 and keep addresses even. If the BMS rejects long reads, lower `batch_size` (for example to 64, i.e. 32 words per read) |
| Modbus: cell numbers look off by one | JK does not state the numbering base | Compare against the BMS app; the number is published as JK reports it |
| 4G-GPS: no reply on JK-PB | The port is not set to protocol `000` | Set 4G-GPS / GPS Remote Module Common Protocol, or use Modbus |
| 4G-GPS: current sign looks wrong | Older firmware protocol version | The transport normalizes this using the protocol version byte; update the BMS firmware if values still look wrong |
| 4G-GPS: no reply with `address = 0` | BMS uses another terminal number | Set `address` to the device address configured on the BMS |
| Looking for a Bluetooth connection | MPG has no Bluetooth support for JK | Use a wired port |

---

## 11. Source Documents

- `protocols/jk/jk_bms_modbus_rtu.json` and `protocols/jk/jk_bms_modbus_rtu.holding_registry_map.csv` (header comments record the sources and caveats)
- `protocols/jk/jk_bms_4g_gps_serial.json` and `protocols/jk/jk_bms_4g_gps_serial.registry_map.csv`
- `classes/transports/serial_jk.py` (4G-GPS frame format, checksum and normalization are documented in its header)
- Multiple JK-BMS over Modbus RS485, with the "JK BMS RS485 Modbus Universal Protocol V1.1" register table, UART1/UART2 protocol lists and wiring: <https://github.com/phinix-org/Multiple-JK-BMS-by-Modbus-RS485>
- ESPHome JK-BMS component (RS485 Modbus and BLE), the source of the tested JK-PB register addresses: <https://github.com/syssi/esphome-jk-bms>
- JK-PB Modbus support discussion: <https://github.com/syssi/esphome-jk-bms/issues/390>
- JK BLE protocol implementation, for the future Bluetooth map: <https://github.com/patman15/BMS_BLE-HA>
