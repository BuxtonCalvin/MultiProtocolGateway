# Pylon / Pylontech Battery - MQTT Integration Guide

> **Supported Models:** Pylontech low-voltage batteries (US2000, US3000, US5000 and compatibles) on Pylon RS485 v2.8 / v3.3 or Pylon CAN v1.2, and Pylontech high-voltage systems (Force H1 / H2 and compatibles such as Dyness) on Pylon HV CAN v1.17
> **Protocol:** Pylon RS485 serial protocol or Pylon CAN bus
> **Interface:** RS485 console/link port or CAN port
> **Status:** Protocols available in MPG. Verified against the vendor documents and their worked examples; not yet run on every battery model

---

## Table of Contents

1. [Overview](#1-overview)
2. [Supported Models & Protocol Versions](#2-supported-models--protocol-versions)
3. [Hardware Requirements](#3-hardware-requirements)
4. [RS485 Setup](#4-rs485-setup)
5. [CAN Bus Setup](#5-can-bus-setup)
6. [Addressing & DIP Switch Notes](#6-addressing--dip-switch-notes)
7. [MPG Configuration](#7-mpg-configuration)
8. [Troubleshooting](#8-troubleshooting)
9. [Source Documents](#9-source-documents)

---

## 1. Overview

Pylontech batteries publish battery state through RS485 and CAN bus, depending on model and port. MPG includes three protocols:

| Protocol | For | Style |
| --- | --- | --- |
| `pylon_rs485_v3.3` | Low-voltage batteries (also V2.8 firmware) | MPG asks, the battery answers (master / slave) |
| `pylon_can` | Low-voltage batteries, CAN V1.2 | The battery broadcasts once a second; MPG listens |
| `pylon_hv_can` | High-voltage systems, CAN V1.17 | The battery answers only when asked; MPG sends two request frames |

Use CAN when you want the aggregate values an inverter would consume: charge voltage, current limits, SOC, SOH, pack voltage and current, alarms, protections and request flags. Use RS485 when you need the per-cell voltages, per-sensor temperatures, capacities, cycle count and serial number that only the serial protocol reports.

---

## 2. Supported Models & Protocol Versions

| Battery Family | Protocol Version | Transport | Baud |
| --- | --- | --- | --- |
| Pylon low-voltage RS485 v3.3 (V2.8 compatible) | `pylon_rs485_v3.3` | `serial_pylon` | set `baudrate` to match the battery (MPG default 9600) |
| Pylon low-voltage CAN v1.2 | `pylon_can` | `canbus` | 500000 |
| Pylon high-voltage CAN v1.17 | `pylon_hv_can` | `canbus` | 500000 |

RS485 V2.8 and V3.3 use the same commands and layouts; V3.3 adds three-byte capacities for batteries over 65 Ah, and MPG handles both. A Pylon CAN V1.3 variant exists (frames 0x35A and 0x35F) but is not mapped: the only descriptions found are community reverse-engineering notes that disagree on the alarm bit layout, and no vendor document.

The RS485 protocol document notes DIP-selectable RS485 speeds of 9600 and 115200. The `serial_pylon` transport uses 9600 unless `baudrate` is set in the config (the protocol definition file lists 115200), so set `baudrate` explicitly to match the battery's DIP setting.

---

## 3. Hardware Requirements

### RS485 Path

- USB to RS485 adapter or RS485 serial bridge
- Correct Pylontech communication cable
- Access to the battery RS485 communication port

### CAN Path

- CAN interface supported by the MPG host, such as Waveshare USB-CAN-A, CANable, or PEAK PCAN-USB
- Correct RJ45/CAN cable for the battery
- CAN termination at the physical ends of the bus

Do not connect RS485 adapters to CAN ports or CAN adapters to RS485 ports. The connectors may look similar, but the electrical interfaces are different.

---

## 4. RS485 Setup

The Pylon RS485 protocol is master/slave. MPG acts as the master and the first battery or selected battery acts as the slave.

``` ini
[transport.pylon_battery]
transport = serial_pylon
port = /dev/ttyUSB0
baudrate = 115200
address = 2
protocol_version = pylon_rs485_v3.3
```

Pylon documentation states that battery addresses start from 2 when communicating with an inverter or upper computer. If address 1 does not respond, try address 2 and verify the DIP settings. `unit_id` and `baud` are accepted as aliases of `address` and `baudrate`.

---

### What the RS485 map publishes

| Group | Command | Variables |
| --- | --- | --- |
| Analog values | `0x4642` | Battery number, number of cells and temperatures, current (positive is charge), module voltage, remaining and total capacity (Ah), cycle count, state of charge |
| Cell voltages | from `0x4642` | `cell_1_voltage` to `cell_16_voltage` |
| Temperatures | from `0x4642` | BMS, four cell-group averages and (US3000B) MOSFET, in degrees C |
| Charge / discharge management | `0x4692` | Charge and discharge voltage limits, charge and discharge current limits, status flags (charge enable, discharge enable, charge immediately 1 and 2, full charge request) |
| System parameters | `0x4647` | The battery's own alarm and protection thresholds (cell, module, temperature, current) |
| Alarm info | `0x4644` | Alarm state of each cell and temperature sensor, charge current, module voltage and discharge current, and the five status bytes with named bits |
| Identity | `0x464F`, `0x4651`, `0x4693`, `0x4696` | Protocol version, battery name, software version, manufacturer, serial number |

Notes:

- **A battery with fewer cells or sensors simply has fewer variables.** A 15-cell battery has no `cell_16_voltage`.
- **`state_of_charge` is derived** by MPG as remaining / total capacity. The battery does not send it over RS485.
- **Capacity:** batteries over 65 Ah put the real values in the three-byte "capacity 2" fields and `FFFF` in the two-byte ones; MPG uses whichever is valid.
- **Not mapped:** the set and control commands (`0x94` set charge / discharge management, `0x95` turn off), which change the battery's behavior. Earlier versions of this map listed a "turn off" row; it has been removed.

### How MPG talks to the battery

Most commands need the battery number as a one-byte INFO field, and MPG sends it (the battery's address for a single battery). The system query is the exception: set `command_info = ff` to ask for the totals of the whole stack, which the battery answers with the number of batteries as the command value.

``` ini
[transport.pylon_stack]
transport = serial_pylon
port = /dev/ttyUSB0
baudrate = 115200
address = 2
command_info = ff
protocol_version = pylon_rs485_v3.3
```

Analog and alarm answers vary in length with the number of cells and sensors, so MPG cuts each one into fixed-offset blocks before decoding. That is why the map's `register` column shows pseudo commands `0x46F1` to `0x46F4`; they are never sent on the bus.

## 5. CAN Bus Setup

The Pylon CAN protocol uses standard CAN frames at 500 kbps with a nominal 1 second transmission cycle.

All 16-bit values are **little endian** (low byte first). The `pylon_can` map reads:

| CAN ID | Data |
| --- | --- |
| `0x351` | Charge voltage, charge current limit, discharge current limit, and discharge voltage limit (bytes 6-7; blank in the V1.2 document, sent by US2000 / US3000) |
| `0x355` | SOC and SOH |
| `0x356` | Voltage, current, average cell temperature |
| `0x359` | Protection and alarm flags, module count |
| `0x35C` | Charge/discharge enable and force/full charge requests |
| `0x35E` | Manufacturer name |
| `0x372`, `0x373`, `0x379` | Extension frames from the Victron / SMA CAN-bus BMS spec: module status counts, min/max cell voltage and temperature (kelvin), installed capacity |

``` ini
transport = canbus
channel = can0
baud = 500000
protocol_version = pylon_can
```

The V1.2 document says the inverter answers with `0x305` once a second. Some batteries do not transmit unless they hear it. If MPG is the only device on the bus (no inverter), ask it to send the keep-alive:

``` ini
poll_frames = 0x305:0000000000000000
poll_interval = 1
```

Do not set this when an inverter is connected; it already sends the frame.

### High-voltage CAN (`pylon_hv_can`)

The high-voltage protocol uses 29-bit IDs and is request / response: the host sends the broadcast frame `0x4200` (byte 0 = 0 for the ensemble information, 2 for the system equipment information) and the battery answers on `0x4210` to `0x4290`, `0x42E0`, `0x42F0`, `0x7310` and `0x7320`. The protocol file therefore asks the CAN transport to send those two requests every 5 seconds.

``` ini
transport = canbus
channel = can0
baud = 500000
protocol_version = pylon_hv_can
```

| Frame | Data |
| --- | --- |
| `0x4210` | Pile voltage, current (offset -3000 A), BMS temperature (offset -100 C), SOC, SOH |
| `0x4220` | Charge and discharge cutoff voltage, maximum charge and discharge current |
| `0x4230`, `0x4240` | Max / min cell voltage and temperature, with the cell numbers |
| `0x4250` | State (sleep, charge, discharge, idle), forced charge and balance requests, cycle period, fault, alarm and protection words with named bits |
| `0x4260`, `0x4270` | Max / min module voltage and temperature, with the module numbers |
| `0x4280`, `0x4290` | Charge and discharge forbidden marks, fault extension |
| `0x42E0`, `0x42F0` | Serial number, manufacturer |
| `0x7310`, `0x7320` | Hardware and software versions, module count, series modules, cells per module, voltage level, capacity |

- **Several towers:** each tower answers on its base ID plus its address (`0x4210` for address 0, `0x4211` for address 1, up to 15). Add one section per tower with the same `channel` and its own `address`; each is a separate device with its own name, bridges and data. The request frames are broadcasts, so they are sent once per section and every tower answers them.

``` ini
[transport.pylon_tower_0]
transport = canbus
channel = can0
protocol_version = pylon_hv_can
address = 0

[transport.pylon_tower_1]
transport = canbus
channel = can0
protocol_version = pylon_hv_can
address = 1
poll_frames = none
```

  With several sections on one interface, set `poll_frames = none` on all but one so the requests are not sent repeatedly.

- Request frames are sent only because the protocol asks for them, and they change nothing on the battery. To stop them (an inverter or BMS is already polling), set `poll_frames = none` in the transport section. Replace them with your own as `poll_frames = 0x4200:0000000000000000`, and change the rate with `poll_interval` (seconds).
- The control commands (sleep / wake `0x8200`, charge / discharge `0x8210`, mask communication error `0x8240`) are not mapped. The vendor document warns that improper use is a serious safety risk.

--- | --- |
| `0x351` | Charge voltage, charge current limit, discharge current limit |
| `0x355` | SOC and SOH |
| `0x356` | Voltage, current, average cell temperature |
| `0x359` | Protection and alarm flags |
| `0x35C` | Charge/discharge enable and force/full charge requests |
| `0x35E` | Manufacturer name |
| `0x372` | Module status counts |
| `0x373` | Min/max cell voltage and temperature |

``` ini
transport = canbus
channel = can0
baud = 500000
protocol_version = pylon_can
```

---

## 6. Addressing & DIP Switch Notes

For RS485, Pylon documentation describes single-group and multi-group modes. The master battery DIP switch setting determines the group address, and the final address combines the battery address and group address.

Practical setup:

1. Start with the vendor-recommended single-group master setting.
2. Try `address = 2` first for RS485.
3. If multiple groups are installed, calculate the address from the group setting and battery position.
4. Restart the battery after changing DIP switch baud or group settings.

### Multiple batteries on one RS485 bus

Create one section per battery address, all pointing at the same `port` and `baudrate`. Each section is its own device, with its own name, bridges and data. MPG opens the serial port once and shares it between the sections (the sharing comes from the `serial_base` class, which `serial_pylon` reaches through `serial_frame_transport`), and each request/response exchange is serialized so the batteries never interleave on the bus.

``` ini
[transport.pylon_battery_1]
transport = serial_pylon
port = /dev/ttyUSB0
baudrate = 9600
address = 2
protocol_version = pylon_rs485_v3.3

[transport.pylon_battery_2]
transport = serial_pylon
port = /dev/ttyUSB0
baudrate = 9600
address = 3
protocol_version = pylon_rs485_v3.3
```

`address` must be 0-255. Sections that share a port must use the same baud rate.

How the batteries are scheduled depends on `read_mode` in `[general]`:

| `read_mode` | Behavior with several batteries on one port |
| --- | --- |
| `sequential` | Batteries are polled one after another. Recommended for a shared bus. |
| `interleaved` | Batteries take turns on the bus between individual commands, so a slow battery does not hold up the others for a whole poll. |
| `concurrent` | Safe, because exchanges are serialized, but no faster than sequential. This mode is meant for devices on independent connections. |

Two sections that point at the same port, protocol and address are treated as the same device (see [ScrapeGroup consolidation](../architecture/mermaid-diagrams.md#10-flowchart--scrapegroup-consolidation)). In `sequential` and `concurrent` modes they share a single poll; in `interleaved` mode each section currently polls the battery separately.

### Protocol version and checksums

On connect MPG asks the battery for its protocol version and uses it in every later request (0x20 until the battery has answered). Responses that are truncated or fail the Pylon checksum are discarded with a warning rather than decoded. If a battery's firmware is known to send a bad checksum, `ignore_checksum_errors = true` restores the old behavior of accepting those frames.

---

## 7. MPG Configuration

Follow the relevant MPG configuration guide:

- RS485/serial: <https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki/Configuration-Examples#modbus-rtu-to-mqtt>
- CAN bus: <https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki/Configuration-Examples#canbus-to-mqtt>

### RS485

``` ini
transport = serial_pylon
port = /dev/ttyUSB0
baud = 115200
unit_id = 2
protocol_version = pylon_rs485_v3.3
```

### CAN

``` ini
transport = canbus
channel = can0
baud = 500000
protocol_version = pylon_can
```

---

## 8. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| RS485 no response | Wrong address | Try `address = 2` and confirm DIP settings |
| RS485 no response after DIP change | Battery not restarted | Power-cycle or restart the battery stack |
| `Serial Pylon checksum error` in the log, no data | Corrupted frames (noise, wrong baud, missing termination) | Check baud rate, cabling and termination; `ignore_checksum_errors = true` only if the firmware is known to send bad checksums |
| CAN interface receives nothing | Wrong bitrate or no termination | Set 500000 baud and check termination |
| Low-voltage CAN battery silent when no inverter is connected | It waits for the inverter's 0x305 frame | Set `poll_frames = 0x305:0000000000000000` and `poll_interval = 1` |
| High-voltage CAN shows no data | The battery answers only when asked | Leave the protocol's default `poll_frames` in place and check there is no other master |
| RS485 voltages look right but cell rows are missing | Battery has fewer cells than the map | Normal: rows beyond the battery's cell count are not published |
| Pylon CAN values were wildly wrong in older versions | 16-bit values were read big endian | Fixed: the map now reads them little endian |
| CAN values update once per second | Normal protocol behavior | Pylon CAN transmits on a 1 second cycle |
| Inverter comms fail after adding MPG | Two masters on same bus | Avoid placing MPG as a second active master on an inverter-controlled link |

---

## 9. Source Documents

- `documentation/3rdparty/protocols/PYLON RS485-protocol-pylon-low-voltage-V3.3-20180821.pdf`
- `documentation/3rdparty/protocols/PYLON LFP Battery communication protocol - RS485 V2.8 20161216.pdf`
- `documentation/3rdparty/protocols/PYLON CAN-Bus-protocol-PYLON-low-voltage-V1.2-20180408.pdf`
- Pylontech, *CANBUS Protocol of High Voltage system* V1.17 (2020-09-09)
- Open-source decoders used to cross-check the RS485 layouts: [python-pylontech](https://github.com/Frankkkkk/python-pylontech)
- `protocols/pylon/pylon_rs485_v3.3.json` and its map
- `protocols/pylon/pylon_can.json` and its map
- `protocols/pylon/pylon_hv_can.json` and its map
