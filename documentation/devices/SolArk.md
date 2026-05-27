# Sol-Ark Inverter — MQTT Integration Guide

> **Supported Models:** Sol-Ark 5K · 8K · 12K (Indoor & Outdoor) · 15K-2P · 15K-2P-N
> **Protocol:** Modbus RTU over RS485 · Solarman V5 TCP (port 8899 via dongle)
> **Interface:** RJ45 RS485 port · screw terminal block (15K)
> **Status:** Untested / Unconfirmed — community reports available. Requires MPG v1.0.0 or higher.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Supported Models & Protocol Versions](#2-supported-models--protocol-versions)
3. [Hardware Requirements](#3-hardware-requirements)
   - 3.1 [Option A — USB to RS485 Adapter (Direct)](#31-option-a--usb-to-rs485-adapter-direct)
   - 3.2 [Option B — Waveshare USB to RS485 (Industrial Grade)](#32-option-b--waveshare-usb-to-rs485-industrial-grade)
   - 3.3 [Option C — Waveshare RS485 to Ethernet/Wi-Fi Bridge](#33-option-c--waveshare-rs485-to-ethernetwi-fi-bridge)
4. [Port Identification by Model](#4-port-identification-by-model)
   - 4.1 [Sol-Ark 15K-2P / 15K-2P-N](#41-sol-ark-15k-2p--15k-2p-n)
   - 4.2 [Sol-Ark 12K / 8K / 5K Outdoor](#42-sol-ark-12k--8k--5k-outdoor)
   - 4.3 [Sol-Ark 12K / 8K Indoor](#43-sol-ark-12k--8k-indoor)
5. [Wiring & Pinout](#5-wiring--pinout)
6. [BMS Port Considerations](#6-bms-port-considerations)
7. [Sol-Ark Local API & Solarman Access](#7-sol-ark-local-api--solarman-access)
8. [MPG Configuration](#8-mpg-configuration)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. Overview

Sol-Ark inverters share hardware lineage with Deye and Sunsynk and expose a Modbus RS485 interface for monitoring. This guide covers hardware connection options, port identification, wiring, API access, and MPG configuration for publishing Sol-Ark data to an MQTT broker.

> **Important:** Sol-Ark's monitoring dongle port is **separate** from the Modbus RS485 port on models that have both. This integration does not interfere with the Sol-Ark cloud dongle on those models — both can operate simultaneously. On models with a combined BMS/RS485 port, a splitter cable is required (see Section 6).
> **Register map compatibility:** Sol-Ark shares its Modbus register map with Deye and Sunsynk. If `solark_v1.1` is not recognized, the Deye/Sunsynk protocol selector may also work. Refer to the Deye/Sunsynk guide for full register documentation.

---

## 2. Supported Models & Protocol Versions

| Model | Protocol Version | RS485 Port | Min MPG Version | Notes |
| --- | --- | --- | --- | --- |
| Sol-Ark 15K-2P | `solark_v1.1` | "Modbus RS485" screw terminal | v1.1.3 | Dedicated open RS485 port |
| Sol-Ark 15K-2P-N | `solark_v1.1` | "Modbus RS485" terminal | v1.1.3 | Community confirmed |
| Sol-Ark 12K Outdoor | `solark_v1.1` | "Battery CANBus" RJ45 | v1.1.3 | Use Battery Monitoring pin pair |
| Sol-Ark 8K Outdoor | `solark_v1.1` | "Battery CANBus" RJ45 | v1.1.3 | See port notes |
| Sol-Ark 5K Outdoor | `solark_v1.1` | "Battery CANBus" RJ45 | v1.1.3 | See port notes |
| Sol-Ark 12K Indoor | `solark_v1.1` | `RJ45_485` port | v1.1.3 | Separate CAN and RS485 ports |
| Sol-Ark 8K Indoor | `solark_v1.1` | `RJ45_485` port | v1.1.3 | Separate CAN and RS485 ports |

---

## 3. Hardware Requirements

### 3.1 Option A — USB to RS485 Adapter (Direct)

The most common approach. Connect from the inverter's RS485 port via an RJ45 cable to a USB RS485 adapter at the monitoring host.

**What you need:**

- USB to RS485 adapter with screw terminals
- RJ45 Ethernet cable (CAT5e or CAT6) — custom pinout required (see Section 5)
- Ferrule crimp tool recommended for reliable terminal connections

**Community-recommended adapter:**

| Adapter | Notes |
| --- | --- |
| **DSD TECH SH-U11F** | Isolated, includes termination resistors and GND terminal — widely confirmed for Sol-Ark |

> ⚠️ Generic USB RS485 RJ45 cables that do not specifically state Deye/Sol-Ark compatibility are unlikely to have the correct pinout. Build your own cable per Section 5 or source a confirmed adapter.

**Connection:**

1. Build or source a compatible RJ45 cable per Section 5
2. Connect RJ45 end to the appropriate inverter RS485 port (see Section 4)
3. Connect screw terminals to RS485 adapter A+ and B−
4. Connect adapter USB to monitoring host
5. Verify detection: `dmesg | grep tty`

---

### 3.2 Option B — Waveshare USB to RS485 (Industrial Grade)

| Product | Description | Best For |
| --- | --- | --- |
| **USB TO RS485 (B)** | FT232RNL chipset, isolated, industrial rail case | Single inverter, direct USB host |
| **USB TO RS485/422** | Dual-protocol, TVS surge protection, 120Ω termination resistor | Long cable runs, noisy environments |

Waveshare adapters provide isolation that reduces the risk of ground loop damage between the inverter and monitoring host — particularly valuable in the high-power environment of an inverter cabinet.

---

### 3.3 Option C — Waveshare RS485 to Ethernet/Wi-Fi Bridge

For installations where the monitoring host is remote from the inverter.

| Product | Description | Best For |
| --- | --- | --- |
| **RS485 TO ETH (B)** | RS485 ↔ Ethernet; Modbus/MQTT gateway | Wired LAN environments |
| **RS485 TO POE ETH (B)** | Same + PoE power; electrically isolated | Clean single-cable installations |
| **RS485 TO WIFI ETH** | RS485 ↔ Wi-Fi + Ethernet | Wireless environments |

**Setup:**

1. Connect RS485 A/B wires from inverter to bridge screw terminals
2. Assign static IP via VirCom software (default: `192.168.1.200`)
3. Configure TCP Client mode, target IP of MPG host
4. Set baud rate to 9600

---

## 4. Port Identification by Model

### 4.1 Sol-Ark 15K-2P / 15K-2P-N

The 15K has two relevant communication ports on the internal wiring board.

| Port Label | Use |
| --- | --- |
| **Modbus RS485** | ✅ Use this for third-party monitoring — open and dedicated |
| **Battery CANBus** | Battery BMS communication; may also carry RS485 but avoid using for monitoring |

**Recommended:** Use the labeled **"Modbus RS485"** screw terminal block directly. This leaves the Battery CANBus port free for battery communications and avoids the need for a splitter cable.

**Wiring:** The port is a screw terminal block. Wire per the Sol-Ark 15K wiring diagram — connect green (GND), brown/white (B−), and brown (A+).

---

### 4.2 Sol-Ark 12K / 8K / 5K Outdoor

These outdoor models use a single RJ45 port labeled **"Battery CANBus"** that carries both RS485 and CAN bus signals on different pin pairs.

**Monitoring port:** "Battery CANBus" RJ45

**Active pins for Modbus RS485:**

| RJ45 Pin | Signal |
| --- | --- |
| 6 | RS485 GND |
| 7 | RS485 B− |
| 8 | RS485 A+ |

> If the Battery CANBus port is occupied by battery BMS communications, a **splitter cable** is required to simultaneously route both CAN (battery, pins 1–4) and RS485 (monitoring, pins 6–8). Solar Assistant sells a pre-made splitter. See Section 6.

---

### 4.3 Sol-Ark 12K / 8K Indoor

Indoor models have two separate RJ45 ports — no splitter needed.

| Port Label | Use |
| --- | --- |
| `RJ45_485` | ✅ Modbus RS485 — use for monitoring |
| `RJ45_CAN` | CANBus — for battery BMS only |

Connect monitoring hardware to `RJ45_485` only.

---

## 5. Wiring & Pinout

Sol-Ark uses a non-standard RJ45 pinout for RS485. **Do not use a straight-through Ethernet cable without verifying pin mapping against this section.**

### 15K Outdoor / Indoor RS485 Port

``` text
RJ45 Pin   Wire Color (T568B)   Signal
────────   ──────────────────   ──────────
  6        Green                GND
  7        Brown/White          RS485 B−
  8        Brown                RS485 A+
```

### Building the Custom Cable

**Steps:**

1. Take a CAT5e/CAT6 cable and cut one end
2. Strip and identify wires by T568B color code
3. Connect pin 8 (brown) → adapter A+
4. Connect pin 7 (brown/white) → adapter B−
5. Connect pin 6 (green) → adapter GND
6. Leave all other wires unconnected or cut back

Use ferrule crimp connectors at the screw terminal end for the most reliable connection.

---

## 6. BMS Port Considerations

If your batteries are connected via the **Battery CANBus** port and you need to add RS485 monitoring simultaneously, use the appropriate approach for your model.

| Model Type | Approach |
| --- | --- |
| 15K-2P / 15K-2P-N | Use the dedicated open **Modbus RS485** terminal — no conflict |
| 12K / 8K / 5K Outdoor | Build or buy a **splitter cable** routing CAN (pins 1–4) and RS485 (pins 6–8) separately |
| 12K / 8K Indoor | Connect to `RJ45_485` — `RJ45_CAN` handles battery separately |

### Splitter Cable Wiring (Outdoor Models)

``` text
Battery CANBus RJ45 (inverter)
├── Pins 1–4: CAN-H, CAN-L, GND, GND  →  Battery BMS RJ45 cable
└── Pins 6–8: GND, RS485 B−, RS485 A+ →  Monitoring RS485 adapter
```

---

## 7. Sol-Ark Local API & Solarman Access

Sol-Ark inverters use the **Solarman V5** dongle and cloud backend — the same infrastructure as Deye and Sunsynk. The local TCP port 8899 on the dongle provides Modbus RTU-over-TCP access using Solarman V5 framing, without requiring a physical RS485 cable.

### Solarman V5 Local Access (Port 8899)

**Connection details:**

``` text
Host:     <dongle-ip>
Port:     8899
Protocol: Solarman V5 (Modbus RTU wrapped in Solarman framing)
```

**Requirements:**

- Sol-Ark Wi-Fi or LAN dongle installed and connected to your LAN
- Dongle IP address (from router DHCP table)
- Dongle serial number (from dongle label or SolarmanPV app)

**Python access via pysolarmanv5:**

``` python
from pysolarmanv5 import PySolarmanV5

inverter = PySolarmanV5(
    address="192.168.1.xxx",
    serial=2312345678,    # Dongle serial number (integer)
    port=8899,
    mb_slave_id=1,
    verbose=False
)

# Read battery SOC (register 103 — verify against Sol-Ark register map)
soc = inverter.read_holding_registers(register_addr=103, quantity=1)
print(f"Battery SOC: {soc[0]}%")
```

The Sol-Ark/Deye/Sunsynk register map is shared. Refer to the Deye/Sunsynk guide (Section 8) for the full Solarman V5 framing specification and register map.

### Sol-Ark Monitoring Portal

Sol-Ark provides cloud monitoring via the Sol-Ark Customer Portal, which uses the Solarman backend. The SolarmanPV cloud API (documented in the Deye/Sunsynk guide, Section 8) applies equally to Sol-Ark systems.

> **MPG module note:** A `solark_solarman_v1` protocol module using `pysolarmanv5` over port 8899 would provide full Modbus register access for Sol-Ark inverters via the existing Wi-Fi dongle, without requiring a physical RS485 cable. The Sol-Ark and Deye/Sunsynk register maps are compatible — a shared `deye_sunsynk_solark_v1` module is likely sufficient for all three brands.

---

## 8. MPG Configuration

Follow the MultiProtocolGateway Modbus RTU to MQTT configuration guide:
<https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki/Configuration-Examples#modbus-rtu-to-mqtt>

### RS485 Direct

``` ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 1
protocol_version = solark_v1.1
```

> **Note:** Set the inverter's Modbus slave ID to `1` in the Sol-Ark settings menu. Some battery BMS devices claim address `0` on the same bus — setting the inverter to `1` avoids address conflicts.

---

## 9. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| No Modbus response at all | Wrong RS485 port selected | Verify correct port per Section 4 for your model |
| Every request times out | Wiring reversed or wrong pinout | Use DSD TECH SH-U11F (confirmed adapter); verify cable per Section 5 |
| Works intermittently, then stops | Missing termination resistor | Use an adapter with built-in 120Ω termination (DSD SH-U11F includes this) |
| Register values read as 0 | Incorrect Modbus slave ID | Set inverter Modbus ID to `1` in Sol-Ark settings; confirm no ID conflict |
| BMS communication drops after adding monitoring | CAN and RS485 conflict on shared port | Use a splitter cable (Section 6) or the dedicated RS485 port on 15K models |
| Solarman 8899 connection refused | Dongle offline or wrong IP | Check router DHCP; ping dongle IP; verify dongle LED indicates Wi-Fi connection |
