# Pylon / Pylontech Battery - MQTT Integration Guide

> **Supported Models:** Pylontech low-voltage batteries using Pylon RS485 v3.3 or Pylon CAN v1.2
> **Protocol:** Pylon RS485 serial protocol or Pylon CAN bus
> **Interface:** RS485 console/link port or CAN port
> **Status:** Protocols available in MPG

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

Pylontech low-voltage lithium batteries publish battery state through both RS485 and CAN bus, depending on model and port configuration. MPG includes both `pylon_rs485_v3.3` and `pylon_can` protocols.

Use CAN when you want the same aggregate values an inverter would consume: charge voltage, current limits, SOC, SOH, pack voltage/current, alarms, protections, and request flags. Use RS485 when you need Pylon's serial command protocol and battery information records.

---

## 2. Supported Models & Protocol Versions

| Battery Family | Protocol Version | Transport | Baud |
| --- | --- | --- | --- |
| Pylon low-voltage RS485 v3.3 | `pylon_rs485_v3.3` | `serial_pylon` | 115200 default in MPG |
| Pylon low-voltage CAN v1.2 | `pylon_can` | `canbus` | 500000 |

The RS485 protocol document notes DIP-selectable RS485 speeds of 9600 and 115200. MPG's current protocol definition defaults to 115200.

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
transport = serial_pylon
port = /dev/ttyUSB0
baud = 115200
unit_id = 2
protocol_version = pylon_rs485_v3.3
```

Pylon documentation states that battery addresses start from 2 when communicating with an inverter or upper computer. If unit ID 1 does not respond, try unit ID 2 and verify the DIP settings.

---

## 5. CAN Bus Setup

The Pylon CAN protocol uses standard CAN frames at 500 kbps with a nominal 1 second transmission cycle.

Common frames in the MPG map include:

| CAN ID | Data |
| --- | --- |
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
2. Try `unit_id = 2` first for RS485.
3. If multiple groups are installed, calculate the address from the group setting and battery position.
4. Restart the battery after changing DIP switch baud or group settings.

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
| RS485 no response | Wrong unit ID | Try `unit_id = 2` and confirm DIP settings |
| RS485 no response after DIP change | Battery not restarted | Power-cycle or restart the battery stack |
| CAN interface receives nothing | Wrong bitrate or no termination | Set 500000 baud and check termination |
| CAN values update once per second | Normal protocol behavior | Pylon CAN transmits on a 1 second cycle |
| Inverter comms fail after adding MPG | Two masters on same bus | Avoid placing MPG as a second active master on an inverter-controlled link |

---

## 9. Source Documents

- `documentation/3rdparty/protocols/PYLON RS485-protocol-pylon-low-voltage-V3.3-20180821.pdf`
- `documentation/3rdparty/protocols/PYLON CAN-Bus-protocol-PYLON-low-voltage-V1.2-20180408.pdf`
- `protocols/pylon/pylon_rs485_v3.3.json`
- `protocols/pylon/pylon_can.json`
