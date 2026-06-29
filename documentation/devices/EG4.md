# EG4 Inverter — MQTT Integration Guide

> **Supported Models:** EG4 6000XP · EG4 12000XP · EG4 18KPV · EG4 3000EHV
> **Protocol:** Modbus RTU over RS485
> **Interface:** RJ45 RS485 port · terminal block (18KPV)
> **Status:** Confirmed working on MPG v1.0+

---

## Table of Contents

1. [Overview](#1-overview)
2. [Supported Models & Protocol Versions](#2-supported-models--protocol-versions)
3. [Hardware Requirements](#3-hardware-requirements)
   - 3.1 [Option A — USB to RS485 Adapter (Direct)](#31-option-a--usb-to-rs485-adapter-direct)
   - 3.2 [Option B — Waveshare USB to RS485 (Industrial Grade)](#32-option-b--waveshare-usb-to-rs485-industrial-grade)
   - 3.3 [Option C — Waveshare RS485 to Ethernet/Wi-Fi Bridge](#33-option-c--waveshare-rs485-to-ethernetwi-fi-bridge)
   - 3.4 [Option D — Raspberry Pi RS485 HAT](#34-option-d--raspberry-pi-rs485-hat)
4. [Port Identification by Model](#4-port-identification-by-model)
   - 4.1 [EG4 6000XP](#41-eg4-6000xp)
   - 4.2 [EG4 18KPV](#42-eg4-18kpv)
   - 4.3 [EG4 3000EHV](#43-eg4-3000ehv)
5. [Wiring & Pinout](#5-wiring--pinout)
6. [EG4 Local API Access](#6-eg4-local-api-access)
7. [MPG Configuration](#7-mpg-configuration)
8. [Troubleshooting](#8-troubleshooting)

---

## 1. Overview

EG4 inverters expose a Modbus RTU interface over RS485, allowing real-time monitoring of power flow, battery state, grid parameters, and fault codes. This guide covers wiring, hardware options, API access, and MPG configuration for publishing EG4 data to an MQTT broker.

> **Note:** EG4's Wi-Fi and Ethernet dongles operate via a proprietary cloud mechanism on port 8899 and are **not** compatible with direct Modbus monitoring. The RS485 wired interface is the recommended and most reliable integration path.

---

## 2. Supported Models & Protocol Versions

| Model | Protocol Version | Baud Rate | Notes |
| --- | --- | --- | --- |
| EG4 6000XP | `eg4_v58` | 19200 | Use CT1 or INV485 RJ45 port |
| EG4 12000XP | `eg4_v58` | 19200 | CT1 RJ45 port confirmed |
| EG4 18KPV | `eg4_v58` | 19200 | Dedicated RS485 terminal block |
| EG4 3000EHV | `eg4_3000ehv_v1` | 9600 | See model-specific notes below |

> Protocols listed may not be limited to the models shown. Community reports indicate additional EG4 models respond correctly to `eg4_v58`.

---

## 3. Hardware Requirements

### 3.1 Option A — USB to RS485 Adapter (Direct)

The simplest path for a single inverter connected to a local host (Raspberry Pi, NUC, etc.).

**What you need:**

- USB to RS485 adapter with RJ45 connector *(sold by EG4 directly)*, **or**
- Generic USB to RS485 adapter + RJ45 Ethernet cable (only pins 7 & 8 used) + ferrule crimp terminals

**Recommended adapters (community-tested):**

| Adapter | Notes |
| --- | --- |
| EG4-branded USB to RS485 | Plug-and-play, correct pinout out of the box |
| DSD TECH SH-U11F | Isolated, includes termination resistors — recommended for reliability |
| CH340-based adapters | Inexpensive; choose a quality brand for long-term stability |

> ⚠️ Avoid the cheapest no-brand CH340 adapters. Community reports cite frequent dropped connections requiring physical re-plug. Spend slightly more for a quality chip or use an isolated adapter.

**Connection:**

``` text
USB Adapter    →  EG4 RJ45 Port
A+ (or TXD+)   →  Pin 8 (brown wire)
B−  (or TXD−)  →  Pin 7 (brown/white wire)
GND (optional) →  Pin 3 or Pin 6
```

---

### 3.2 Option B — Waveshare USB to RS485 (Industrial Grade)

For improved reliability and electrical isolation over generic adapters.

| Product | Description | Best For |
| --- | --- | --- |
| **USB TO RS485 (B)** | FT232RNL chipset, industrial rail case, isolated | Single inverter, direct USB host |
| **USB TO RS485/422** | Dual-protocol, built-in TVS protection, 120Ω termination | Single inverter, noisy environments |

**Key advantages:**

- Onboard power and signal isolation reduces risk of ground loop damage
- TVS surge protection and resettable fuses
- Compatible with Linux, macOS, Windows, Raspberry Pi OS out-of-the-box
- Baud rates up to 921600 bps supported

Wiring is identical to Option A — Waveshare adapters use standard A/B screw terminals.

---

### 3.3 Option C — Waveshare RS485 to Ethernet/Wi-Fi Bridge

For multi-inverter setups or installations where running a USB cable to the monitoring host is not practical.

| Product | Description | Best For |
| --- | --- | --- |
| **RS485 TO ETH (B)** | RS485 ↔ Ethernet, Modbus/MQTT gateway, rail-mount | Wired LAN environments |
| **RS485 TO POE ETH (B)** | Same + PoE power input, isolated | Clean single-cable deployments |
| **RS485 TO WIFI ETH** | RS485 ↔ Wi-Fi + Ethernet | Where running Ethernet is not practical |
| **RS232/485 TO WIFI ETH (B)** | Dual RS232/485, Wi-Fi + Ethernet, metal case | Mixed-protocol environments |

**Configuration notes:**

- Use VirCom software (Windows) for initial IP assignment; default IP is `192.168.1.200`
- Set TCP Client mode pointing at your MPG host
- Match baud rate to inverter model (19200 for 6000XP/18KPV; see Section 2)
- Multiple inverters on an RS485 daisy chain can share one bridge if each has a unique Modbus address

> **Tip:** Waveshare 2-CH and 4-CH variants allow multiple inverters per bridge, reducing cabling complexity. The RS485 wiring pinout is identical regardless of whether you use USB or Ethernet bridge variants.

---

### 3.4 Option D — Raspberry Pi RS485 HAT

When using a Raspberry Pi RS485 HAT (e.g., Waveshare RS485 CAN HAT), the A/B differential pair orientation may be reversed relative to a standard USB adapter.

**Symptom of reversed wiring:**

``` text
ERROR:.transport_base[transport.0]:<bound method ModbusException.__str__ of ModbusIOException()>
```

**Fix:** Swap the A and B wires at the screw terminal on the HAT.

---

## 4. Port Identification by Model

### 4.1 EG4 6000XP

The 6000XP uses an internal communication board where the RS485 interface is shared with the Wi-Fi dongle slot.

**Primary port:** `CT1` — RJ45 jack on the internal communication board

**Alternative port:** `INV485` — shared with Wi-Fi dongle slot; remove dongle before use

> The Wi-Fi dongle and the RS485 wired connection **cannot be used simultaneously** on the same bus. Remove the dongle when using wired RS485 monitoring.

**Pinout (CT1 / INV485 RJ45):**

| Pin | Wire Color (T568B) | Signal |
| --- | --- | --- |
| 7 | Brown/White | RS485 B− |
| 8 | Brown | RS485 A+ |

Only 2 wires are required. GND is optional but improves noise immunity on long cable runs.

---

### 4.2 EG4 18KPV

The 18KPV exposes RS485 on a **dedicated terminal block** located below the RJ45 connector bank on the internal wiring panel.

**Primary port:** Terminal block labeled `RS485` (A and B terminals)

**Alternative:** RJ45 port pinout per EG4 documentation Section 3.5.2

For third-party RS485 monitoring, use the dedicated RS485 terminal block with 2 stripped wires connected to a screw-terminal RS485 adapter. CAT5/5e/6 cable is suitable.

---

### 4.3 EG4 3000EHV

Connects via the standard RS485/Modbus port. Uses a separate protocol version from the 6000XP/18KPV family:

``` ini
protocol_version = eg4_3000ehv_v1
```

Refer to the EG4 3000EHV manual for port location. Hardware options A–C in Section 3 all apply.

---

## 5. Wiring & Pinout

### Standard RJ45 RS485 Pinout (EG4 6000XP / 12000XP/ 18kPV)

``` text
RJ45 Pin   Wire (T568B)    Signal
────────   ────────────    ──────────
  7        Brown/White     RS485 B−
  8        Brown           RS485 A+

or

RJ45 Pin   Wire (T568B)    Signal
────────   ────────────    ──────────
  1        Orange/White     RS485 B−
  2        Orange           RS485 A+

┌─────────────────────────────────────────────────────────────────┐
│                                                                 │
│  EG4 BMS RJ45 Port (looking into the socket, tab down)          │
│  ┌─────────────────────────┐                                    │
│  │  1  2  3  4  5  6  7  8 │                                    │
│  │  │  │  │  │  │  │  │  │ │                                    │
│  └──┼──┼──┼──┼──┼──┼──┼──┼─┘                                    │
│     │  │  │  │  │  │  │  │                                      │
│     │  │  │  │  │  │  │  └── RS485 B  (Brown)                   │
│     │  │  │  │  │  │  └───── RS485 A  (Brown/White)             │
│     │  │  │  │  │  └──────── GND      (Green)                   │
│     │  │  │  │  └─────────── (unused) (Blue/White)              │
│     │  │  │  └────────────── (unused) (Blue)                    │
│     │  │  └───────────────── GND      (Green/White)             │
│     │  └──────────────────── RS485 A  (Orange)                  │
│     └─────────────────────── RS485 B  (Orange/White)            │
│                                                                 │
│  Pins 1/8 (B) and 2/7 (A) are bridged — use either end.         │
│  Pins 4/5 are not RS485 on this port. Do not tap them.          │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘  

```

### USB RS485 Adapter Terminal Mapping

``` text
Adapter Terminal   →   EG4 RJ45
      A+           →   Pin 8 (brown)
      B−           →   Pin 7 (brown/white)
     GND (opt.)    →   Pin 3 or bare shield

or

Adapter Terminal   →   EG4 RJ45
      A+           →   Pin 1 (orange)
      B−           →   Pin 2 (orange/white)
     GND (opt.)    →   Pin 3 or bare shield

```

### 18KPV Terminal Block Wiring

``` text
Inverter Terminal   →   RS485 Adapter
      RS485-A       →   A+
      RS485-B       →   B−
      GND (opt.)    →   GND
```

> Use CAT5e or CAT6 cable. Only 2 signal wires are electrically required. Do not use the parallel-port RJ45 jacks for RS485 monitoring — those are for inter-inverter communication only.

---

## 6. EG4 Local API Access

EG4 inverters running current firmware expose a **local web interface** on their LAN IP address when the Wi-Fi or Ethernet dongle is connected. While the primary integration path for MPG is RS485 Modbus, the local web interface provides a JSON data endpoint that can serve as the basis for a custom HTTP-based MPG module — useful when RS485 cabling is not practical.

> **Note:** The Wi-Fi dongle and RS485 cannot be used simultaneously on models that share the port (6000XP). If you are using the local API path, the dongle must remain installed. If you are using RS485 direct, remove the dongle.

### Discovering the Inverter IP

The EG4 dongle joins your Wi-Fi network and obtains a DHCP address. Find it in your router's DHCP client table. Assign a static IP or DHCP reservation for reliable access.

### Local Web Interface

``` text
http://<inverter-ip>/
```

The web dashboard displays real-time power flow, battery status, and grid parameters. On most firmware versions the underlying data is fetched from a JSON endpoint that can be polled directly.

### JSON Data Endpoint

``` text
GET http://<inverter-ip>/status
```

or

``` text
GET http://<inverter-ip>/api/realtime
```

> Endpoint paths vary by firmware version. Use your browser's developer tools (Network tab) while loading the EG4 web dashboard to identify the exact endpoint and response schema for your firmware version.

**Example response structure (firmware-dependent):**

``` json
{
  "pvPower":      3420,
  "batteryPower": -500,
  "gridPower":    1250,
  "loadPower":    2170,
  "batterySoc":   85,
  "batteryVoltage": 51.2,
  "gridVoltage":  241.3,
  "outputVoltage": 120.0
}
```

### EG4 Cloud API (EG4 Connect / Solarman-Based)

EG4 dongles use the **Solarman V5** cloud backend (same protocol as Deye/Sunsynk). The local TCP port 8899 and the Solarman V5 framing protocol described in the Deye/Sunsynk guide apply equally to EG4 dongles.

**Connection:**

``` text
Host:     <dongle-ip>
Port:     8899
Protocol: Solarman V5 (see Deye_Sunsynk.md Section 8 for framing details)
```

The `pysolarmanv5` Python library handles the framing:

``` python
from pysolarmanv5 import PySolarmanV5

inverter = PySolarmanV5(
    address="192.168.1.xxx",
    serial=2312345678,    # Dongle serial number (from label)
    port=8899,
    mb_slave_id=1,
    verbose=False
)

# Read AC output power (register varies by firmware — verify against EG4 register map)
ac_power = inverter.read_holding_registers(register_addr=186, quantity=1)
print(f"AC Power: {ac_power[0]}W")
```

> **MPG module note:** A `eg4_solarman_v1` protocol module using `pysolarmanv5` over port 8899 would provide full Modbus register access for EG4 inverters without requiring a physical RS485 cable, using the existing Wi-Fi dongle as the transport. Refer to the Deye/Sunsynk guide for full Solarman V5 framing documentation.

---

## 7. MPG Configuration

Follow the MultiProtocolGateway Modbus RTU to MQTT configuration guide:
<https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki/Configuration-Examples#modbus-rtu-to-mqtt>

### EG4 6000XP, 12000XP, 18KPV

``` ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 19200
unit_id = 1
protocol_version = eg4_v58
```

### EG4 3000EHV

``` ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 1
protocol_version = eg4_3000ehv_v1
```

> Baud rate defaults to 9600 if not specified. Always set it explicitly to avoid mismatches, particularly for `eg4_v58` models which require 19200.

---

## 8. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| `ModbusIOException` on Raspberry Pi HAT | A/B wires reversed at HAT | Swap A and B at the HAT screw terminal |
| No response, connection timeouts | Wi-Fi dongle still inserted in shared port | Remove dongle; RS485 and dongle share the same bus on 6000XP |
| Intermittent disconnects | Low-quality CH340 USB adapter | Replace with isolated adapter (DSD SH-U11F or Waveshare) |
| Only one battery visible in a multi-battery bank | RS485 reads the directly-connected device only | Use a Modbus hub or switch to CAN bus for multi-battery visibility |
| Wrong baud rate errors | Protocol/baud mismatch | Confirm baud is `19200` for `eg4_v58` models; `9600` for `eg4_3000ehv_v1` |
| Waveshare bridge device not reachable | IP conflict after power cycle | Use VirCom (Windows) to assign a static IP to the bridge |
