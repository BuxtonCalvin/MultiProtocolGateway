# Bluetooth Batteries (JBD, EG4, PACE, JK-PB, Daly, Renogy, LiTime, ANT) - MQTT Integration Guide

> **Supported Models:** JBD / Xiaoxiang / Overkill Solar, EG4 LL / LL-S / LifePower4, PACEEX smart BMS, JK (Jikong) BMS including the JK-PB series, Daly smart BMS, Renogy (BT-1 / BT-2), LiTime and its rebrands, ANT BMS, Seplos V2
> **Protocols:** `jbd_bms`, `eg4_ll_s`, `paceex_bms`, `jk_bms_ble`, `daly_bms_d2`, `daly_bms_x81`, `renogy_bms`, `litime_bms`, `ant_bms`, `seplos_bms_v2`
> **Transports:** `serial_bluetooth_jbd`, `_eg4`, `_pace`, `_jk`, `_daly`, `_renogy`, `_litime`, `_ant`, `_seplos` (all built on `serial_bluetooth`; EG4, Daly and Renogy share `serial_bluetooth_modbus`)
> **Status:** Protocols available in MPG, **read-only**. Decoding is tested against frames captured from real batteries; the radio path has not been exercised on hardware by the author of this change

---

## Table of Contents

1. [Overview](#1-overview)
2. [Supported Devices](#2-supported-devices)
3. [Requirements](#3-requirements)
4. [Settings](#4-settings)
5. [MPG Configuration](#5-mpg-configuration)
6. [How It Works](#6-how-it-works)
7. [What Each Protocol Publishes](#7-what-each-protocol-publishes)
8. [Troubleshooting](#8-troubleshooting)
9. [Not Implemented and Known Limits](#9-not-implemented-and-known-limits)
10. [Writing Another Bluetooth Transport](#10-writing-another-bluetooth-transport)
11. [Source Documents](#11-source-documents)

---

## 1. Overview

Most battery management systems with Bluetooth put a UART behind a Bluetooth Low Energy (BLE) module: the battery exposes one characteristic it notifies on and one the gateway writes to, and the bytes on them are the same frames the BMS would speak on a wire. `serial_bluetooth` is the base transport for that. It is built on `serial_base`, so it inherits the shared client per device, the per-device lock, startup rows, scheduling and cleanup, and adds the radio side (the [bleak](https://github.com/hbldh/bleak) library, run on its own asyncio loop in a thread).

Each manufacturer speaks its own protocol, so each has a transport of its own on top of `serial_bluetooth`. They differ in framing and in what they send, not in how they connect.

---

## 2. Supported Devices

| Device | Transport | `protocol_version` | Service / notify / write | Map |
| --- | --- | --- | --- | --- |
| JBD / Xiaoxiang / Overkill Solar / SBL / Supervolt and other JBD white-labels (12V and 48V) | `serial_bluetooth_jbd` | `jbd_bms` | `ff00` / `ff01` / `ff02` | shared with the UART transport |
| EG4 LL, LL-S and LifePower4 with the Bluetooth module (Modbus RTU over BLE) | `serial_bluetooth_eg4` | `eg4_ll_s` | `1000` / `1002` / `1001` | shared with `modbus_eg4_ll_s_rtu` |
| PACEEX smart BMS (advertises as `PC-xxxx`) | `serial_bluetooth_pace` | `paceex_bms` | `fff0` / `fff1` / `fff2` | new |
| JK (Jikong) BMS, JK-PB series and other firmware 11 or later | `serial_bluetooth_jk` | `jk_bms_ble` | `ffe0` / `ffe1` / `ffe1` | new |
| Daly smart BMS, older boards (advertises as `DL-xxxx`) | `serial_bluetooth_daly` | `daly_bms_d2` | `fff0` / `fff1` / `fff2` | new |
| Daly smart BMS, newer boards | `serial_bluetooth_daly` | `daly_bms_x81` | `fff0` / `fff1` / `fff2` | new |
| Renogy Smart Lithium with a BT-1 / BT-2 module (`BT-TH-xxxx`) | `serial_bluetooth_renogy` | `renogy_bms` | `ffd0`+`fff0` / `fff1` / `ffd1` | new |
| LiTime, Redodo, ECO-WORTHY T-series, Starry Sea (`L-12...`, `LT-...`, `P-...`, `R-...`, `T-...`, `S-...`) | `serial_bluetooth_litime` | `litime_bms` | `ffe0` / `ffe1` / `ffe2` | new |
| ANT BMS (`ANT-BLE24...`, `ANT-BLE3...`) | `serial_bluetooth_ant` | `ant_bms` | `ffe0` / `ffe1` / `ffe1` | new |
| Seplos V2 BMS (`BP02...` to `BP29...`) | `serial_bluetooth_seplos` | `seplos_bms_v2` | `ff00` / `ff01` / `ff02` | new |

`paceex_bms` is not the Modbus protocol of the PACE RS485 batteries (see [PACE.md](PACE.md)) and `jk_bms_ble` is not the RS485 protocol of [JK.md](JK.md); they are the protocols those manufacturers' Bluetooth modules speak.

The UART protocol files for JBD and EG4 are reused, so the protocol file's own `transport` value still names the wired transport. A config section names the Bluetooth transport itself, as in the examples below.

---

## 3. Requirements

- **bleak**, installed by `pip install -r requirements.txt`.
- A Bluetooth 4.0+ adapter. On Linux, BlueZ must be running; on macOS and Windows bleak uses the system Bluetooth. A container needs access to the host's Bluetooth stack (on Linux, the host's D-Bus socket and normally host networking).
- **The battery must not be connected to anything else.** Most batteries accept a single BLE connection, so close the manufacturer's phone app (and any other gateway) before starting MPG, and the other way round.
- The battery in range: BLE is a few metres through walls, and a metal battery rack is a poor place for an antenna. An ESP32 or a USB adapter on an extension cable helps.

---

## 4. Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `bluetooth_address` | none (required) | The battery's address: `AA:BB:CC:DD:EE:FF`, or on macOS the UUID it shows instead, or `name:<local name>` to find it by its advertised name. `port` is accepted as an alias |
| `adapter` | system default | Bluetooth adapter, such as `hci1` on Linux |
| `timeout` | `5.0` | Seconds a response may take |
| `scan_timeout` | `10.0` | Seconds to look for the battery before giving up |
| `connect_timeout` | `20.0` | Seconds to connect and discover services |
| `service_uuid`, `notify_uuid`, `write_uuid` | the protocol's own | Override for a rebranded battery that uses different characteristics |
| `password` | empty | JBD and ANT only: the battery's connection password (the secret of the manufacturer's app), for a battery that is locked. Leave empty for one that is not; `secret` is accepted as an alias. It is never logged |
| `address` | `0` | Only for EG4: the Modbus unit id, `1` to `247` (the battery answers on `1`) |

Common settings (`read_interval`, `bridge`, `variable_mask`, `variable_screen`, ...) are as for any transport. A battery that is out of range when MPG starts is simply looked for again on every cycle.

---

## 5. MPG Configuration

``` ini
[transport.jbd_battery]
transport = serial_bluetooth_jbd
protocol_version = jbd_bms
bluetooth_address = A4:C1:38:12:34:56
read_interval = 15
bridge = transport.mqtt
device_manufacturer = JBD
device_model = Smart BMS

[transport.eg4_battery]
transport = serial_bluetooth_eg4
protocol_version = eg4_ll_s
bluetooth_address = 84:C2:E4:12:34:56
address = 1
read_interval = 15
bridge = transport.mqtt
device_manufacturer = EG4
device_model = LL-S

[transport.pace_battery]
transport = serial_bluetooth_pace
protocol_version = paceex_bms
bluetooth_address = name:PC-12345678
read_interval = 15
bridge = transport.mqtt
device_manufacturer = PACE
device_model = PACEEX BMS

[transport.jbd_locked_battery]
transport = serial_bluetooth_jbd
protocol_version = jbd_bms
bluetooth_address = A4:C1:38:12:34:57
password = 123456
read_interval = 15
bridge = transport.mqtt

[transport.daly_battery]
transport = serial_bluetooth_daly
protocol_version = daly_bms_d2        ; or daly_bms_x81 for the newer boards
bluetooth_address = 41:18:12:34:56:78
read_interval = 15
bridge = transport.mqtt

[transport.renogy_battery]
transport = serial_bluetooth_renogy
protocol_version = renogy_bms
bluetooth_address = name:BT-TH-12345678
read_interval = 15
bridge = transport.mqtt

[transport.litime_battery]
transport = serial_bluetooth_litime
protocol_version = litime_bms
bluetooth_address = name:L-12100BNNA70-A00123
read_interval = 15
bridge = transport.mqtt

[transport.ant_battery]
transport = serial_bluetooth_ant
protocol_version = ant_bms
bluetooth_address = name:ANT-BLE24
password = 12345678                   ; only if the board is locked
read_interval = 15
bridge = transport.mqtt

[transport.seplos_battery]
transport = serial_bluetooth_seplos
protocol_version = seplos_bms_v2
bluetooth_address = name:BP02-12345
read_interval = 15
bridge = transport.mqtt

[transport.jk_battery]
transport = serial_bluetooth_jk
protocol_version = jk_bms_ble
bluetooth_address = C8:47:80:12:34:56
read_interval = 15
bridge = transport.mqtt
device_manufacturer = JK
device_model = JK-PB2A16S20P
```

To find a battery's address, scan with any BLE tool (`bluetoothctl scan on` on Linux, `python -m bleak` style scanners elsewhere); the advertised name is usually `xiaoxiang BMS`, `SP..`, `PC-...` or `JK_...` or `JK-...`.

Two sections may read one battery (for example with different `variable_mask`) if they use the same transport and characteristics; they then share one connection.

---

## 6. How It Works

- **Connection.** The transport scans for the address, connects, checks that the service's two characteristics exist and subscribes to notifications. A failed attempt is logged and retried on the next cycle. If the link drops, the next read marks the transport disconnected and reconnects; startup rows (model, versions, serial number) are read again after every reconnect.
- **Frames.** A BLE notification carries 20 bytes unless the link negotiated more, so a reply arrives in slices. Each protocol has a frame finder that joins the slices by the frame's own length and check bytes, skips noise, and recovers from a corrupt frame.
- **Requests.** A request clears anything received before it, so a stale reply is never mistaken for the answer. A battery that also pushes frames of its own (JK does) has them skipped until the reply to the request arrives.
- **Writes.** Whether to write with or without response comes from the characteristic's properties; if the battery rejects it the other mode is tried once and remembered.

Per protocol:

| Transport | Requests per cycle | Notes |
| --- | --- | --- |
| `serial_bluetooth_jbd` | `0x03` (pack), `0x04` (cells), `0x05` (hardware version, once) | Frames are those of the UART transport, byte for byte |
| `serial_bluetooth_eg4` | Modbus read of registers 0 to 38; registers 105 to 150 once | Modbus RTU frames, CRC low byte first |
| `serial_bluetooth_pace` | `0x0A00` stack, `0x0A01` pack, `0x0A02` cells, `0x0A03` balance; versions and serial number once | `9A ... 9D` frames, CRC-16 |
| `serial_bluetooth_daly` | D2: registers 0 to 61 and 62 to 70, versions once; X81: 0 to 63 and 65 to 126, versions and serial once | Modbus RTU; D2 uses unit `0xD2`; X81 asks `0x81` and is answered from `0x51` |
| `serial_bluetooth_renogy` | registers 5042 to 5048, 5000 to 5033, 5100 to 5107; serial, name and firmware once | Modbus RTU on unit `0x30`; characteristics in two services |
| `serial_bluetooth_litime` | one request (`0x13`) | One little-endian status frame; sensors are counted as they show non-zero readings |
| `serial_bluetooth_seplos` | one request (`0x61`, telemetry of one pack) | `7E ... 0D` frames with CRC-16/XMODEM; the reply has no fixed layout, so fixed blocks are built for the map |
| `serial_bluetooth_ant` | one request (`0x01`) | The reply has no fixed layout; the transport builds fixed blocks for the map, and sends the password first if set |
| `serial_bluetooth_jk` | `0x97` device information once (also the handshake), then `0x96` status | 300 byte status frames; a 20 byte "ready" message and settings frames are skipped |

---

## 7. What Each Protocol Publishes

**JBD** and **EG4** publish what their existing maps do (see [JBD.md](JBD.md) and [EG4.md](EG4.md)). The EG4 transport drops cell rows that report 0 V, so a 4-cell 12V pack does not get 12 phantom cells.

**PACEEX** (`paceex_bms`): pack voltage, current (positive is charging), remaining and design capacity, state of charge, state of health, cycle count, cell count, up to 16 cell voltages, four cell temperatures plus MOSFET and ambient when fitted, the balancing mask, serial number and versions. Rows named `stack_*` come from the system command and describe the whole stack when the connected battery is the master of several; on a single battery they match the pack values. Cells beyond the cell count and sensors that report 0 are not published.

**Daly**, **Renogy**, **LiTime** and **ANT** publish voltage, current (positive is charging), state of charge, remaining capacity, cycle or charge counters, every cell voltage up to the cell count the battery reports, and its temperature sensors, plus what each protocol adds: MOSFET states and balancer flags and the fault code (Daly), serial number, model and firmware (Daly, Renogy), charge and discharge MOSFET and heater state (Renogy), state of health, design capacity and the fault code (LiTime), MOSFET and balancer temperatures, power and total charge (ANT). Cells and sensors beyond the counts the battery reports, or that read 0 V, are not published. Daly current is stored as 30000 plus tenths of an amp; the maps handle it.

**JK** (`jk_bms_ble`): total voltage, current (positive is charging), state of charge, state of health, remaining, nominal and cycle capacity, cycle count, runtime, up to 32 cell voltages and wire resistances, average and delta cell voltage, MOSFET, charge and discharge state, balancing action and current, alarm flags, heater state, and up to six temperatures, plus model, hardware and software version, device name and serial number. The map numbers each value by its byte offset into the whole 300 byte frame (the numbering of the public protocol notes) and every multi-byte value is little-endian. Cells that are not in the cell mask and sensors that are not in the sensor mask, or that report -200.0 C, are not published; the third and fourth battery sensors exist from firmware 14.

---

## 8. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| "was not found; is it powered, in range and not connected to another app?" | Out of range, asleep, wrong address, or another central is connected | Close the phone app; move the adapter closer; confirm the address with a scan |
| "has no characteristic ..." | The battery is not the model this transport is for, or a rebranded one | Check the table in section 2; set `service_uuid`, `notify_uuid`, `write_uuid` |
| Connects, then times out on every read | The battery is connected to something else, or the pairing is stale | Disconnect the app; remove the device in `bluetoothctl` and retry |
| "checksum error" in the log | Weak signal dropping notification slices | Move the adapter closer; the frame is discarded and the next cycle tries again |
| JK: "supports firmware 11 and later" | Old firmware with the 24-cell layout | Not supported (see section 9) |
| Works for a while, then stops | A Linux BlueZ or adapter fault | Restart `bluetooth.service`; try a different adapter with `adapter = hci1` |
| EG4 current looks ten times too small on a 12V battery | See section 9 | Override the unit of `pack_current` to `0.1A` in the web UI |

---

## 9. Not Implemented and Known Limits

- **Read-only.** MPG sends no write commands to any of these batteries.
- **Not tested on a radio.** The frame layouts and decoded values are checked against captures from real batteries (a JK-PB2A16S20P on firmware 15.38, a PACEEX 16S 100Ah, an EG4 12V pack, and the JBD manufacturer's examples), but the Bluetooth connection itself has only been exercised against a simulated device. Expect to report an issue or two on first use with real hardware.
- **EG4 current scale.** The `eg4_ll_s` map reads current in 0.01 A, verified on a 48V LL-S. A capture from an EG4 12V pack in another open-source project reads the same register in 0.1 A. If your pack's current is ten times too small, set the unit of `pack_current` to `0.1A`.
- **JK firmware before 11** (the 24-cell layout, JK02_24S) is refused on connection because its offsets differ.
- **JK temperature sensors 3 to 5.** Sensors beyond the first two are named by position in the frame; other projects number them differently, so a label may not match the JK app.
- **Passwords.** JBD and ANT take a `password`; a wrong JBD password is refused with a message in the log and the link is dropped. ANT never answers its password, so a wrong one shows up as no data.
- **One connection.** The battery cannot be read by MPG and a phone app at the same time.
- **Daly variants.** Choose `daly_bms_d2` or `daly_bms_x81` yourself; a board of the other kind does not answer and the transport returns no data. Fault codes are published as numbers, not decoded.
- **Renogy.** Alarm bits are not decoded, and the newer Renogy Pro batteries (`RNGRBP...`) use another protocol that is not covered.
- **ANT.** The device information request (hardware and software version) is not implemented.
- **Frames from open-source test suites.** Several Daly values are synthetic in the capture used for testing, so they prove the register arithmetic rather than a real pack.
- **Seplos.** Only the V2 boards (`BP02`-`BP29`) are covered, not the V3 protocol. At most 16 cells and 8 sensors are read; MOSFET states, alarms, the pack count and the model are not decoded.
- **Other batteries** have their own Bluetooth protocols and are not covered yet (see the end of this change's notes for the ones I know of).

---

## 10. Writing Another Bluetooth Transport

Subclass `serial_bluetooth`, set `SERVICE_UUID`, `NOTIFY_UUID` and `WRITE_UUID`, declare `transport_type = "scraper"`, and implement `extract_frame()` (a static method that joins notification slices into whole frames), `build_frame()`, `decode_frame()` and `_read_command()` (use `_exchange_until()` to wait for the right reply), as in `serial_bluetooth_pace.py`. Then add an entry to `classes/WebServer/transport_defaults.json` that extends `_bluetooth_base`, a protocol file and map under `protocols/`, and tests: `pytests/bluetooth_support.py` provides a fake device that drives the real client. See [Writing a New Serial Transport](../usage/transports.md#writing-a-new-serial-transport) for the parts it shares with the other serial transports.

---

## 11. Source Documents

- aiobmsble (Apache-2.0), for the Bluetooth protocols of PACEEX and EG4 and for captured frames: <https://github.com/patman15/aiobmsble>
- aiobmsble, also for the Daly, Renogy, LiTime and ANT protocols
- esphome-jk-bms, for the JK02 frame layout: <https://github.com/syssi/esphome-jk-bms>
- Jiabaida, *Software board general protocol V4* (UART / RS232 / RS485), the JBD frames: <https://forum.mypower.cz/download/file.php?id=27163>
- `classes/transports/serial_bluetooth.py`, `serial_bluetooth_jbd.py`, `serial_bluetooth_modbus.py`, `serial_bluetooth_eg4.py`, `serial_bluetooth_pace.py`, `serial_bluetooth_jk.py`, `serial_bluetooth_daly.py`, `serial_bluetooth_renogy.py`, `serial_bluetooth_litime.py`, `serial_bluetooth_ant.py`
- `protocols/jk/jk_bms_ble.json`, `protocols/jk/jk_bms_ble.registry_map.csv`, `protocols/pace/paceex_bms.json`, `protocols/pace/paceex_bms.registry_map.csv`
- `pytests/test_serial_bluetooth*.py`, `pytests/bluetooth_support.py`
