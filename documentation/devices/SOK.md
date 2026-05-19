# SOK Battery — MQTT Integration Guide

> **Supported Models:** SOK 48V 100AH · Jakiper 48V 100AH · Other PACE BMS-based batteries
> **Protocol:** PACE BMS Modbus RTU over RS485
> **Interface:** RS485A port (RJ45)
> **Status:** Confirmed working

---

## Table of Contents

1. [Overview](#1-overview)
2. [Supported Batteries](#2-supported-batteries)
3. [Hardware Requirements](#3-hardware-requirements)
   - 3.1 [Option A — USB to RS485 Adapter (Direct)](#31-option-a--usb-to-rs485-adapter-direct)
   - 3.2 [Option B — Waveshare USB to RS485 (Industrial Grade)](#32-option-b--waveshare-usb-to-rs485-industrial-grade)
   - 3.3 [Option C — Waveshare RS485 to Ethernet/Wi-Fi Bridge](#33-option-c--waveshare-rs485-to-ethernetwi-fi-bridge)
   - 3.4 [Option D — Modbus Hub (Multi-Battery)](#34-option-d--modbus-hub-multi-battery)
4. [Battery Protocol Configuration](#4-battery-protocol-configuration)
5. [Port Identification & Pinout](#5-port-identification--pinout)
6. [PACE BMS Local API Access](#6-pace-bms-local-api-access)
7. [Multi-Battery Limitations & Workarounds](#7-multi-battery-limitations--workarounds)
8. [MPG Configuration](#8-mpg-configuration)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. Overview

SOK batteries and other batteries based on the PACE BMS platform communicate via Modbus RTU over RS485. This guide covers hardware selection, BMS protocol configuration, API access, and MPG configuration for publishing battery data to an MQTT broker.

> **Protocol prerequisite:** The battery's RS485 protocol **must** be set to `PACE_MODBUS` on the BMS before communication will work. This is configured on the battery itself, not in software.

---

## 2. Supported Batteries

The following batteries use the PACE BMS platform and are compatible with this integration:

| Battery | Notes |
| --- | --- |
| SOK 48V 100AH | Primary tested model |
| Jakiper 48V 100AH | PACE BMS variant — confirmed compatible |
| Other PACE BMS batteries | Any battery with `PACE_MODBUS` RS485 protocol option |

If your battery uses a PACE BMS and exposes an RS485A port, it is likely compatible with `pace_bms_v1.3`.

---

## 3. Hardware Requirements

### 3.1 Option A — USB to RS485 Adapter (Direct)

The simplest approach for a single battery connected to a local host.

**What you need:**

- USB to RS485 adapter with screw terminals or RJ45 connector
- RJ45 Ethernet cable (CAT5e or CAT6) with the pinout from Section 5

**Suitable adapters:**

| Adapter | Notes |
| --- | --- |
| Generic CH340-based USB RS485 | Adequate for single battery; no isolation |
| FTDI-based USB RS485 | Better driver support on all platforms |
| **Waveshare USB TO RS485 (B)** | Industrial grade, isolated — see Option B |

---

### 3.2 Option B — Waveshare USB to RS485 (Industrial Grade)

For improved reliability and electrical isolation, recommended for permanent installations.

| Product | Description | Best For |
| --- | --- | --- |
| **USB TO RS485 (B)** | FT232RNL chipset, isolated, industrial rail case | Single battery, direct USB host |
| **USB TO RS485/422** | Dual-protocol, TVS surge protection, 120Ω termination | Long cable runs, noisy environments |

**Key advantages:**

- Power and signal isolation minimizes ground loop risk in high-current battery environments
- TVS surge protection guards against transient voltages from battery switching
- Wide OS support: Linux, Raspberry Pi OS, macOS, Windows — typically plug-and-play

---

### 3.3 Option C — Waveshare RS485 to Ethernet/Wi-Fi Bridge

For network-connected monitoring without a physically co-located host.

| Product | Description | Best For |
| --- | --- | --- |
| **RS485 TO ETH (B)** | RS485 ↔ Ethernet; Modbus/MQTT gateway | Wired LAN environments |
| **RS485 TO POE ETH (B)** | Same + PoE power; electrically isolated | Clean single-cable installations |
| **RS485 TO WIFI ETH** | RS485 ↔ Wi-Fi + Ethernet | Wireless environments |

**Setup:**

1. Wire RS485A pin 1 (A+) and pin 2 (B−) from battery to bridge screw terminals
2. Configure IP via VirCom (Windows); default IP: `192.168.1.200`
3. Set TCP Client mode pointing at your MPG host
4. Set baud rate to 9600

---

### 3.4 Option D — Modbus Hub (Multi-Battery)

The PACE BMS protocol over RS485 can only read the battery **directly connected to the host** — batteries in a parallel bank that are not the bus master are not visible without a hub.

**What a Modbus hub does:**

A Modbus RTU RS485 hub places multiple batteries on a single RS485 bus with unique slave addresses, making all packs visible to the monitoring host through a single cable.

**Requirements:**

- Modbus RTU RS485 hub or multiplexer
- Each battery assigned a unique Modbus slave address (configured per battery)
- Single RS485 cable from hub to monitoring host

> Ensure each battery in the bank is configured with a unique Modbus address before connecting through a hub.

---

## 4. Battery Protocol Configuration

Before connecting, configure the battery BMS to use the PACE Modbus protocol:

**Steps:**

1. Access the battery BMS settings (method varies by model — refer to your SOK manual)
2. Navigate to the **Communication Protocol** setting
3. Set the RS485 protocol to: **`PACE_MODBUS`**
4. Save settings and restart the battery

> **Critical:** Without `PACE_MODBUS` selected, the RS485 port will not respond to Modbus queries and all connection attempts will time out silently.

---

## 5. Port Identification & Pinout

SOK and PACE BMS batteries expose an **RS485A port** (RJ45) on the battery face or side panel. This is the port used for monitoring.

> Do not confuse the RS485A port with the battery-to-battery daisy chain ports if your model has multiple RJ45 connectors. The RS485A port is labeled and is used for external monitoring only.

**RS485A RJ45 Pinout (community verified):**

Both pin groups below carry identical signals. Use whichever group is most convenient for cable routing.

| RJ45 Pin | Signal | Alternate Pin | Notes |
| --- | --- | --- | --- |
| 1 | RS485 A+ | 8 | Either group works |
| 2 | RS485 B− | 7 | Either group works |
| 3 | GND | 6 | Optional but recommended |

**Wiring to USB RS485 adapter:**

``` text
RS485A Pin 1 (or Pin 8)  →  Adapter A+
RS485A Pin 2 (or Pin 7)  →  Adapter B−
RS485A Pin 3 (or Pin 6)  →  Adapter GND (optional)
```

![PACE BMS Port Reference](https://github.com/BuxtonCalvin/InverterModBusToMQTT/assets/2180145/1ea28956-5d74-4bdb-9732-341d492d15c3)

---

## 6. PACE BMS Local API Access

In addition to the Modbus RTU interface, some PACE BMS variants with Bluetooth or Ethernet modules expose a local data API. This varies by hardware generation.

### PACE BMS PC Software (USB / RS485)

PACE provides a Windows PC monitoring application that communicates over RS485 using a proprietary protocol alongside the Modbus register map. The application is primarily for configuration and diagnostics, not real-time MQTT integration.

### Bluetooth-Enabled PACE BMS Units

Some SOK and Jakiper batteries include a Bluetooth module. The companion mobile app (typically branded to the battery manufacturer) reads all BMS parameters wirelessly.

Community projects have reverse-engineered the Bluetooth protocol for PACE BMS units:

- **`jkbms`** / **`daly-bms`** compatible libraries may work for certain PACE variants
- The `pybms` project provides a Python interface for several BMS brands including PACE variants

**Example using pybms (if supported by your firmware):**

``` python
from bms.pace import PaceBms

bms = PaceBms(port="/dev/ttyUSB0", baudrate=9600, address=1)
data = bms.read_all()

print(f"SOC: {data['soc']}%")
print(f"Voltage: {data['voltage']}V")
print(f"Current: {data['current']}A")
print(f"Temperature: {data['temperature']}°C")
```

### Ethernet / Wi-Fi PACE BMS Variants

Some higher-capacity PACE BMS units (typically commercial rack batteries) include an Ethernet port exposing a local HTTP interface:

``` text
http://<bms-ip>/status
```

Use your browser's developer tools to identify the JSON endpoint for your specific firmware.

> **MPG module note:** A `pace_bms_http_v1` protocol module polling the local HTTP JSON endpoint on PACE BMS units with Ethernet modules would allow multi-battery rack monitoring without RS485 cabling or Modbus hub hardware, provided each battery BMS has a unique IP address on the LAN.

---

## 7. Multi-Battery Limitations & Workarounds

| Limitation | Impact | Workaround |
| --- | --- | --- |
| RS485 reads only the directly connected battery | Slave pack voltage, current, and SOC not visible | Use a Modbus hub with unique slave IDs per battery |
| Single Modbus slave address per direct connection | Cannot daisy-chain without a hub | Modbus RTU RS485 hub |
| CAN bus not available on all PACE models | CAN aggregation not universally supported | Verify your model's specs; some PACE variants support CAN |

**Recommended multi-battery setup with a Modbus hub:**

1. Connect all batteries to Modbus hub RS485 ports
2. Assign each battery a unique Modbus address (1, 2, 3, ...)
3. Connect hub RS485 output to monitoring host or Waveshare bridge
4. Configure separate MPG device entries for each battery, each with its corresponding slave address

---

## 8. MPG Configuration

Follow the MultiProtocolGateway Modbus RTU to MQTT configuration guide:
<https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki/Configuration-Examples#modbus-rtu-to-mqtt>

### Single Battery

``` ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 1
protocol_version = pace_bms_v1.3
```

### Multi-Battery via Modbus Hub

``` ini
[battery_1]
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 1
protocol_version = pace_bms_v1.3

[battery_2]
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 2
protocol_version = pace_bms_v1.3
```

---

## 9. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| No response / all timeouts | PACE_MODBUS not selected on BMS | Set BMS RS485 protocol to `PACE_MODBUS` and restart battery |
| Device not found on Linux | Wrong port or driver issue | Check `dmesg` after plugging in adapter; verify `/dev/ttyUSB*` appears |
| Only the master battery visible in multi-battery setup | RS485 protocol limitation | Deploy a Modbus hub with unique slave IDs per battery pack |
| Connection drops intermittently | Long cable run without termination | Add 120Ω termination resistor at the far end of the RS485 run |
| Correct readings but SOC drifts over time | BMS calibration needed | Perform a full charge/discharge cycle to recalibrate the BMS SOC |
| Wrong pin connected | Wiring error | Both pin groups (1,2,3 or 8,7,6) carry identical signals — use either group consistently |
