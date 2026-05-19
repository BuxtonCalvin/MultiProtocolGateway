# Deye / Sunsynk — MQTT Integration Guide

> **Supported Models:** Deye SUN-xK-SG04LP3 · SUN-xK-SG05LP3 · SUN-xK-SG01LP1 · Sunsynk 5kW / 8kW / 12kW / 16kW
> **Protocol:** Modbus RTU over RS485 · Modbus TCP via Solarman logger (port 8899) · Solarman Local API (JSON)
> **Interface:** RJ45 RS485 port · terminal block RS485 · Solarman Wi-Fi / LAN dongle
> **Status:** Well-supported — large community, multiple open-source integrations

---

## Table of Contents

1. [Overview](#1-overview)
2. [Brand Relationships & Compatibility](#2-brand-relationships--compatibility)
3. [Supported Models & Protocol Matrix](#3-supported-models--protocol-matrix)
4. [Hardware Requirements](#4-hardware-requirements)
   - 4.1 [Option A — USB to RS485 Adapter (Direct)](#41-option-a--usb-to-rs485-adapter-direct)
   - 4.2 [Option B — Waveshare USB to RS485 (Industrial Grade)](#42-option-b--waveshare-usb-to-rs485-industrial-grade)
   - 4.3 [Option C — Waveshare RS485 to Ethernet/Wi-Fi Bridge](#43-option-c--waveshare-rs485-to-ethernetwi-fi-bridge)
   - 4.4 [Option D — Solarman Wi-Fi / LAN Dongle (TCP Port 8899)](#44-option-d--solarman-wi-fi--lan-dongle-tcp-port-8899)
5. [RS485 Port Identification by Model](#5-rs485-port-identification-by-model)
   - 5.1 [SG04LP3 and SG05LP3 Series](#51-sg04lp3-and-sg05lp3-series)
   - 5.2 [SG01LP1 Series (Single Phase)](#52-sg01lp1-series-single-phase)
   - 5.3 [2-in-1 BMS Port Models (Newer Firmware)](#53-2-in-1-bms-port-models-newer-firmware)
6. [RS485 Pinout & Wiring](#6-rs485-pinout--wiring)
   - 6.1 [Standard RJ45 Pinout](#61-standard-rj45-pinout)
   - 6.2 [Splitter Cable for 2-in-1 BMS Ports](#62-splitter-cable-for-2-in-1-bms-ports)
7. [Modbus Configuration Details](#7-modbus-configuration-details)
8. [Solarman Local API Access](#8-solarman-local-api-access)
9. [MPG Configuration](#9-mpg-configuration)
   - 9.1 [RS485 Direct](#91-rs485-direct)
   - 9.2 [Solarman TCP Port 8899](#92-solarman-tcp-port-8899)
10. [Multi-Inverter Setups](#10-multi-inverter-setups)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Overview

Deye and Sunsynk inverters share the same hardware platform. Sunsynk is the primary brand name in North America; Deye is the manufacturer name used in other markets. Sol-Ark inverters (covered in a separate guide) also use the same underlying hardware and Modbus register map.

Two distinct MQTT integration paths are available:

- **RS485 direct** — physically wire to the inverter's RS485 port; full read/write Modbus RTU access; most reliable
- **Solarman TCP 8899** — use the existing Wi-Fi/LAN dongle's undocumented local TCP port; no additional hardware; read-only or limited write

> **RS485 direct is strongly preferred** for reliable, low-latency monitoring with full register access. The Solarman 8899 path is convenient if cable routing to the inverter is not practical, but offers less control and depends on the dongle remaining operational.

---

## 2. Brand Relationships & Compatibility

| Brand Name | Manufacturer | Register Map | Notes |
| --- | --- | --- | --- |
| Deye | Ningbo Deye Inverter Technology | Deye/Sunsynk standard | Original manufacturer |
| Sunsynk | Sunsynk Ltd (rebranded Deye) | Identical | Primary US/SA brand name |
| Sol-Ark | Sol-Ark LLC (rebranded Deye) | Identical | See SolArk.md |
| Other rebrands | Various | Usually identical | Verify with community before assuming |

The Deye/Sunsynk Modbus register map is publicly documented and extensively reverse-engineered. All brands on this hardware platform share the same ~300 registers.

---

## 3. Supported Models & Protocol Matrix

| Model Series | RS485 Port | Solarman 8899 | Combined BMS Port | Notes |
| --- | --- | --- | --- | --- |
| SUN-5K-SG04LP3 | RJ45 "RS485" | ✅ | Some firmware | Separate ports on older firmware |
| SUN-8K-SG04LP3 | RJ45 "RS485" or "BMS" | ✅ | Some firmware | Check port label carefully |
| SUN-12K-SG04LP3 | RJ45 "RS485" or "BMS" | ✅ | Latest firmware | Use splitter on combined port |
| SUN-16K-SG05LP3 | RJ45 "BMS" | ✅ | Latest firmware | BMS port carries both CAN + RS485 |
| SUN-5K-SG01LP1 | RJ45 "RS485" | ✅ | ❌ | Single-phase US residential model |
| Sunsynk 5kW–16kW | Same as Deye equivalents | ✅ | Model-dependent | Identical register map |

---

## 4. Hardware Requirements

### 4.1 Option A — USB to RS485 Adapter (Direct)

**What you need:**

- USB to RS485 adapter with RJ45 connector, **or**
- Generic USB to RS485 adapter with screw terminals + RJ45 Ethernet cable (custom pinout — see Section 6)

**Community-recommended adapters:**

| Adapter | Notes |
| --- | --- |
| **DSD TECH SH-U11F** | Isolated, TVS, resettable fuse, GND pin — most recommended by Sunsynk project maintainer |
| **Waveshare USB TO RS485 (B)** | Industrial grade, isolated — see Option B |
| Generic CH340-based | Works but has no isolation or protection; susceptible to ground loops |

> **RS485 idle voltage:** The Deye/Sunsynk RS485 port idles at approximately 4–5V between A and B with no load connected. This is normal. When a 120Ω termination load is present it may drop to ~0.5V — also normal. Do not interpret low idle voltage as a wiring fault.

---

### 4.2 Option B — Waveshare USB to RS485 (Industrial Grade)

| Product | Description | Best For |
| --- | --- | --- |
| **USB TO RS485 (B)** | FT232RNL chipset, isolated, rail-mount | Single inverter, direct USB host |
| **USB TO RS485/422** | Dual-protocol, TVS surge protection, 120Ω termination | Long cable runs, noisy environments |

The Waveshare adapter's built-in isolation protects both the inverter's BMS circuitry and the monitoring host from ground loop damage.

---

### 4.3 Option C — Waveshare RS485 to Ethernet/Wi-Fi Bridge

For remote monitoring without a USB host physically co-located at the inverter.

| Product | Description | Best For |
| --- | --- | --- |
| **RS485 TO ETH (B)** | RS485 ↔ Ethernet; Modbus TCP gateway | Wired LAN |
| **RS485 TO POE ETH (B)** | Same + PoE power; isolated | Clean single-cable installations |
| **RS485 TO WIFI ETH** | RS485 ↔ Wi-Fi + Ethernet | Wireless environments |

> **Community note:** The Waveshare RS485-to-Ethernet bridge must be configured in **Modbus TCP gateway mode** (not raw serial bridge mode) when used with the Sunsynk Add-on. Raw serial bridge mode is not compatible. Use the `tcp://` port format in the add-on configuration.

**Setup:**

1. Wire RS485 A/B from inverter RJ45 port to bridge screw terminals
2. Configure IP via VirCom (Windows); default IP: `192.168.1.200`
3. Set to Modbus TCP gateway mode
4. Set baud rate to 9600

---

### 4.4 Option D — Solarman Wi-Fi / LAN Dongle (TCP Port 8899)

The Wi-Fi or LAN dongle shipped with Deye/Sunsynk inverters exposes an **undocumented local TCP port 8899** that carries Modbus RTU-over-TCP. This enables monitoring without any additional hardware.

**Requirements:**

- Solarman dongle plugged in and connected to your LAN
- Dongle IP address (check router DHCP table or use a network scanner)
- Dongle serial number (required by the Solarman protocol driver; found on dongle label or in the SolarmanPV app)

**Limitations:**

- Some registers are read-only via the 8899 interface
- Reliability is lower than direct RS485
- The dongle and a direct RS485 cable **cannot be used simultaneously** on models sharing a port — remove one or use a splitter

---

## 5. RS485 Port Identification by Model

> Port labeling is the most common source of connection failures on Deye/Sunsynk. The port labeled "RS485" on older firmware is sometimes **not** the correct port for third-party monitoring on newer firmware.

### 5.1 SG04LP3 and SG05LP3 Series

| Port Label | Correct Use |
| --- | --- |
| **RS485** | ✅ Third-party monitoring — use this one |
| **BMS** | Battery BMS communication (CAN or RS485 to battery pack) |
| **RS485-1** (some firmware) | Monitoring port on newer firmware revisions |

On the SG04LP3 family, the RJ45 port marked **"RS485"** in the bottom-right area of the inverter communication board is the correct monitoring port. A nearby port labeled differently may not respond.

### 5.2 SG01LP1 Series (Single Phase)

The single-phase SG01LP1 has:

- A USB serial port on the Wi-Fi RS232 dongle slot (standard USB serial; cannot be used simultaneously with the Wi-Fi dongle)
- An RS485 RJ45 port for wired Modbus monitoring

### 5.3 2-in-1 BMS Port Models (Newer Firmware)

Newer Deye/Sunsynk models combine battery BMS and RS485 monitoring on a single RJ45 port:

- **5kW latest firmware:** No separate CAN port — BMS port carries both CAN (battery) and RS485 (monitoring) on different pin pairs
- **16kW latest firmware:** BMS port used for both; splitter cable required
- **3-phase latest firmware:** Same combined port approach

If the battery BMS occupies this port, a splitter cable is required (see Section 6.2).

---

## 6. RS485 Pinout & Wiring

### 6.1 Standard RJ45 Pinout

On both T568A and T568B crimped cables, only pins 7 and 8 carry the RS485 signals. The crimping standard affects wire colors only, not the pin numbers.

| RJ45 Pin | Signal | Wire Color (T568B) | Wire Color (T568A) |
| --- | --- | --- | --- |
| 7 | RS485 B− | Brown/White | Brown/White |
| 8 | RS485 A+ | Brown | Brown |
| 3 or 6 | GND (optional) | Green or Green/White | Orange or Orange/White |

> **Identifying your cable crimp:** Look at the RJ45 plug already on the inverter or the supplied cable. If the two outermost wires are brown and green → T568A. If brown and orange → T568B. In both cases only pins 7 and 8 carry RS485 signals.

**Wiring to USB RS485 adapter:**

``` text
RJ45 Pin 8 (brown)        →  Adapter A+
RJ45 Pin 7 (brown/white)  →  Adapter B−
RJ45 Pin 3 or 6 (GND)    →  Adapter GND (recommended)
```

### 6.2 Splitter Cable for 2-in-1 BMS Ports

For models where the BMS port carries both CAN (battery) and RS485 (monitoring) on the same RJ45 connector.

``` text
BMS RJ45 Port (inverter)
├── Pins 1–4: CAN-H, CAN-L, GND, GND  →  Battery BMS RJ45 cable
└── Pins 7–8: RS485 A+, RS485 B−      →  Monitoring RS485 adapter
```

A custom Y-splitter cable routes pins 1–4 to a battery BMS cable and pins 7–8 to the monitoring adapter. Solar Assistant sells a pre-made version; community members build their own using the pinout above.

---

## 7. Modbus Configuration Details

| Parameter | Value |
| --- | --- |
| Protocol | Modbus RTU (RS485 direct) |
| Default Baud Rate | **9600 bps** |
| Data bits | 8 |
| Parity | None (8N1) |
| Stop bits | 1 |
| Default Slave ID | **1** |
| Solarman TCP port | **8899** |

Baud rate and slave ID are configurable in the inverter menu (SolarmanPV app → Inverter Settings → Communication, or inverter LCD on models that have one).

---

## 8. Solarman Local API Access

The Solarman Wi-Fi/LAN dongle exposes a **local JSON API** in addition to the TCP 8899 Modbus interface. This API provides structured device data and is the basis for several Home Assistant integrations. It can also be used to build a custom MPG module.

### Local API — Port 8899 (Modbus RTU over TCP)

This interface uses a proprietary Solarman framing protocol wrapping standard Modbus RTU. It is not a pure Modbus TCP connection — a Solarman-specific header prefix is required.

**Connection details:**

``` text
Host:     <dongle-ip>
Port:     8899
Protocol: Solarman V5 frame (Modbus RTU wrapped in Solarman framing)
```

**Solarman V5 Frame Structure:**

``` text
Byte offset  Length  Description
──────────── ──────  ──────────────────────────────────
0            1       Start byte: 0xA5
1–2          2       Payload length (little-endian)
3            1       Control code: 0x10
4–5          2       Sequence number
6–9          4       Logger serial number (dongle serial, little-endian)
10+          N       Modbus RTU frame (standard)
Last         1       Checksum (XOR of all bytes after 0xA5)
Last+1       1       End byte: 0x15
```

The Python `pysolarmanv5` library handles this framing automatically and is the recommended implementation basis:

``` bash
pip install pysolarmanv5
```

``` python
from pysolarmanv5 import PySolarmanV5

dongle = PySolarmanV5(
    address="192.168.1.xxx",
    serial=2312345678,     # Dongle serial number (integer)
    port=8899,
    mb_slave_id=1,
    verbose=False
)

# Read a holding register (e.g., register 672 = battery SOC)
soc = dongle.read_holding_registers(register_addr=672, quantity=1)
print(f"Battery SOC: {soc[0]}%")
```

### Local HTTP API (LAN Dongle / Stick Logger)

The Solarman LAN dongle (SOLARMAN Stick Logger LSW-3, LAN-3) also exposes a local HTTP API on port 80:

``` text
http://<dongle-ip>/status.html         # Basic status page
http://<dongle-ip>/real_time_data.json # Real-time inverter data (some firmware)
```

> This HTTP API varies significantly by dongle firmware version and model. The `pysolarmanv5` library over port 8899 is more consistent across firmware versions.

### Solarman Cloud API

For cloud-based access, Solarman provides a REST API for registered accounts:

**Base URL:**

``` text
https://globalapi.solarmanpv.com/
```

**Authentication:**

``` http
POST https://globalapi.solarmanpv.com/account/v1.0/token
Content-Type: application/json

{
  "appId": "YOUR_APP_ID",
  "appSecret": "YOUR_APP_SECRET",
  "email": "YOUR_EMAIL",
  "password": "MD5_HASHED_PASSWORD"
}
```

**Key Cloud Endpoints:**

| Endpoint | Description |
| --- | --- |
| `POST /device/v1.0/currentData` | Real-time inverter data by device serial |
| `POST /device/v1.0/historical` | Historical time-series data |
| `POST /station/v1.0/realTime` | Station-level production summary |

> **MPG module note:** A `solarman_v5_v1` protocol module using `pysolarmanv5` over port 8899 would provide full Modbus register access for all Deye/Sunsynk/Sol-Ark models without requiring a physical RS485 cable — only the existing Wi-Fi/LAN dongle and its IP address and serial number. This would make it the lowest-friction integration path for these inverters.

---

## 9. MPG Configuration

### 9.1 RS485 Direct

``` ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 1
protocol_version = deye_sunsynk_v1
```

### 9.2 Solarman TCP Port 8899

When using the Waveshare bridge in Modbus TCP gateway mode (not Solarman V5 framing):

``` ini
transport = modbus_tcp
host = 192.168.1.200
port = 8899
unit_id = 1
protocol_version = deye_sunsynk_v1
```

> **Note:** Direct Solarman V5 framing (via `pysolarmanv5`) requires a dedicated protocol module in MPG. The standard Modbus TCP transport does not handle the Solarman framing prefix.

Configuration reference:
<https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki/Configuration-Examples#modbus-rtu-to-mqtt>

---

## 10. Multi-Inverter Setups

**RS485 daisy chain:**

- Each inverter requires a unique Modbus slave ID (set per inverter in inverter communication settings or SolarmanPV app)
- Daisy-chain RS485 ports in series: adapter → inverter 1 → inverter 2 → ...
- Add 120Ω termination at the last inverter in the chain
- One adapter or Waveshare bridge serves all inverters

**Parallel installation (same site, synchronized inverters):**

- Parallel inverters do not automatically share a Modbus address
- Each parallel inverter requires its own dedicated RS485 cable or unique slave ID
- Configure separate device entries in MPG for each inverter

---

## 11. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| No response at all | Wrong port (see Section 5 for correct port by model) | Identify correct RS485 monitoring port per model family |
| Response on some registers, 0 on others | Read-only registers via 8899 interface | Switch to RS485 direct for full register read/write access |
| Timeout or garbled data | A/B wires reversed | Swap pins 7 and 8 at the adapter end |
| Works with dongle unplugged, fails when plugged in | Shared port conflict | Remove dongle when using RS485 direct; or use a splitter cable |
| Idle voltage reads ~0V between A/B | 120Ω termination pulling voltage down | Normal behavior with termination load; 4–5V is idle without load |
| Solarman 8899 connection refused | Dongle not on LAN or IP changed | Check router DHCP; ping dongle IP; verify dongle is online |
| Waveshare bridge not working | Raw serial bridge mode selected | Reconfigure Waveshare to Modbus TCP gateway mode via VirCom |
| Wrong slave ID across multi-inverter chain | ID conflict | Assign unique Modbus slave ID to each inverter individually |
