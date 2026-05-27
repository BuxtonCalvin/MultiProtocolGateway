# HDHK AC Module - MQTT Integration Guide

> **Supported Models:** HDHK / HDXXAXXA16GK 16-channel AC current and frequency module
> **Protocol:** Modbus RTU over RS485
> **Interface:** RS485 terminal block
> **Status:** Protocol available in MPG

---

## Table of Contents

1. [Overview](#1-overview)
2. [Supported Models & Protocol Versions](#2-supported-models--protocol-versions)
3. [Hardware Requirements](#3-hardware-requirements)
4. [Wiring & Serial Settings](#4-wiring--serial-settings)
5. [Module Setup Notes](#5-module-setup-notes)
6. [MPG Configuration](#6-mpg-configuration)
7. [Troubleshooting](#7-troubleshooting)
8. [Source Documents](#8-source-documents)

---

## 1. Overview

The HDHK 16-channel AC module is a compact RS485 Modbus RTU current and frequency acquisition module. MPG can poll each channel and publish the current, frequency, CT ratio, and module metadata to MQTT.

This device is a measurement module, not an inverter or battery. Install it in a suitable enclosure and route CT wiring according to local electrical code.

---

## 2. Supported Models & Protocol Versions

| Model | Protocol Version | Interface | Notes |
| --- | --- | --- | --- |
| HDHK / HDXXAXXA16GK 16-channel AC module | `hdhk_16ch_ac_module` | RS485 | 16 current inputs plus per-channel frequency |

The MPG register map includes channel A through P current, channel A through P frequency, CT ratio settings, address, baud, parity, and frequency division settings.

---

## 3. Hardware Requirements

### Option A - USB to RS485 Adapter

Use this for a module installed near the MPG host.

**What you need:**

- USB to RS485 adapter with A/B terminals
- 8-28 VDC power supply for the HDHK module
- Twisted pair cable for RS485
- Current transformers matched to the module range

### Option B - RS485 to Ethernet Bridge

Use this when the module is mounted in a panel away from the MPG host.

| Product | Description | Best For |
| --- | --- | --- |
| Waveshare RS485 TO ETH (B) | RS485 to Ethernet bridge | Wired LAN installs |
| Waveshare RS485 TO POE ETH (B) | Ethernet bridge with PoE | One-cable panel installs |
| Waveshare RS485 TO WIFI ETH | RS485 to Wi-Fi/Ethernet | Where Ethernet is not practical |

Configure the bridge for transparent serial mode and match the module baud, parity, data bits, and stop bits.

---

## 4. Wiring & Serial Settings

### Power

``` text
8 to +28 VDC supply   ->  Module V+
DC supply negative    ->  Module GND
```

### RS485

``` text
RS485 adapter A+      ->  Module RS485 A
RS485 adapter B-      ->  Module RS485 B
RS485 adapter GND     ->  Module GND (recommended)
```

### Default Serial Settings

| Setting | Value |
| --- | --- |
| Baud | 9600 |
| Data bits | 8 |
| Parity | None |
| Stop bits | 1 |
| Default unit ID | 1 |

The module supports baud codes for 1200, 2400, 4800, 9600, 19200, 38400, 57600, and 115200. If the module has been reconfigured, read register `3` or reset it to the expected settings before troubleshooting MPG.

---

## 5. Module Setup Notes

The vendor document lists a maximum of 32 nodes on the same RS485 network. Keep the bus linear, terminate long runs, and avoid star wiring.

Important register-backed settings:

| Setting | Register | Notes |
| --- | --- | --- |
| Device address | `3` low byte | `0x01` through `0xFF`; `0x00` is broadcast |
| Baud code | `3` high byte low nibble | Default maps to 9600 |
| Parity/stop format | `3` high byte high nibble | Default `N,8,1` |
| Frequency division coefficient | `6` | Valid range 1-5 |
| CT ratio per channel | `40` through `55` | Match installed CT hardware |

Use writes carefully. Changing address or baud immediately affects communication and can make the module appear offline until MPG is updated to match.

---

## 6. MPG Configuration

Follow the MultiProtocolGateway Modbus RTU to MQTT configuration guide:
<https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki/Configuration-Examples#modbus-rtu-to-mqtt>

### Direct USB RS485

``` ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 1
protocol_version = hdhk_16ch_ac_module
```

### RS485 Hardware Bridge

``` ini
transport = modbus_rtu
host = 192.168.1.200
port = 502
baud = 9600
unit_id = 1
protocol_version = hdhk_16ch_ac_module
```

---

## 7. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| No response | A/B reversed | Swap A and B at the adapter |
| Responds once then fails after a setting write | Baud or unit ID changed | Reconfigure MPG to match the new module setting |
| Values are scaled incorrectly | CT ratio does not match installed CTs | Verify registers `40` through `55` |
| Frequency reads unstable | Frequency division coefficient mismatch | Check register `6` and input waveform quality |
| Intermittent timeouts | RS485 bus wiring or noise | Use shielded twisted pair, termination, and isolated adapter |

---

## 8. Source Documents

- `documentation/3rdparty/protocols/HDHK 16ch_ac_module_modbus_rtu_translated_english.pdf`
- `protocols/hdhk/hdhk_16ch_ac_module.json`
- `protocols/hdhk/hdhk_16ch_ac_module.holding_registry_map.csv`
