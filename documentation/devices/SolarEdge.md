# SolarEdge — MQTT Integration Guide

> **Supported Models:** HD-Wave SE Series (residential) · StorEdge · Home Hub · Synergy Tri-Phase · Commercial Gateway (CCG)
> **Protocol:** Modbus TCP (primary, via Ethernet) · Modbus RTU over RS485 (RS485-1, RS485-2, RS485-E terminals) · SolarEdge Monitoring API (cloud + local)
> **Interface:** Ethernet (LAN) · RS485 terminal block
> **Status:** Well-supported — SunSpec-compliant, community-confirmed

---

## Table of Contents

1. [Overview](#1-overview)
2. [Supported Models & Protocol Matrix](#2-supported-models--protocol-matrix)
3. [Enabling Modbus on SolarEdge Inverters](#3-enabling-modbus-on-solaredge-inverters)
   - 3.1 [SetApp Models (Current Generation)](#31-setapp-models-current-generation)
   - 3.2 [LCD Models (Older Generation)](#32-lcd-models-older-generation)
4. [Hardware Requirements](#4-hardware-requirements)
   - 4.1 [Option A — Ethernet Direct (Modbus TCP)](#41-option-a--ethernet-direct-modbus-tcp)
   - 4.2 [Option B — RS485 Direct (USB Adapter)](#42-option-b--rs485-direct-usb-adapter)
   - 4.3 [Option C — Waveshare USB to RS485 (Industrial Grade)](#43-option-c--waveshare-usb-to-rs485-industrial-grade)
   - 4.4 [Option D — Waveshare RS485 to Ethernet Bridge](#44-option-d--waveshare-rs485-to-ethernet-bridge)
5. [RS485 Port Identification by Model](#5-rs485-port-identification-by-model)
6. [RS485 Pinout & Wiring](#6-rs485-pinout--wiring)
7. [Modbus TCP Configuration Details](#7-modbus-tcp-configuration-details)
8. [Modbus RS485 Configuration Details](#8-modbus-rs485-configuration-details)
9. [Key Modbus Registers (SunSpec)](#9-key-modbus-registers-sunspec)
10. [Local & Cloud API Access](#10-local--cloud-api-access)
11. [MPG Configuration](#11-mpg-configuration)
12. [Multi-Inverter (Leader/Follower) Setups](#12-multi-inverter-leaderfollower-setups)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. Overview

SolarEdge inverters implement standard **SunSpec Modbus** over both Ethernet (TCP) and RS485 (RTU). All current-generation SetApp-configured inverters have **two built-in RS485 ports** (RS485-1 and RS485-2) in addition to an Ethernet port, making them highly flexible for monitoring integration.

**Critical notes before starting:**

> - Modbus is **disabled by default** — it must be activated via SetApp or the LCD menu
> - SolarEdge's default Modbus TCP port is **1502**, not the standard 502 — this is the most common source of connection failures
> - SolarEdge supports **only one Modbus TCP connection at a time**
> - RS485-1 is typically reserved for SolarEdge Leader/Follower synchronization — use **RS485-2** for third-party monitoring on multi-inverter sites

---

## 2. Supported Models & Protocol Matrix

| Model | Modbus TCP | RS485 Modbus | RS485 Ports | Local API | Notes |
| --- | --- | --- | --- | --- | --- |
| SE HD-Wave (SetApp, current gen) | ✅ | ✅ | RS485-1, RS485-2 | ✅ | SetApp required for activation |
| SE HD-Wave (LCD, older gen) | ✅ | ✅ | RS485-1 | ❌ | LCD menu activation |
| StorEdge / Home Hub | ✅ | ✅ | RS485-1 | ✅ | Battery registers available |
| Synergy Tri-Phase (3-phase US) | ✅ | ✅ | RS485-2, RS485-E | ✅ | Use RS485-2 for monitoring |
| Commercial Gateway (CCG) | ✅ | ✅ | Multiple | ✅ | Concentrator for large sites |

> **Synergy note:** On Synergy Tri-Phase inverters, the Synergy Manager presents as a single logical inverter in Modbus, even though it consists of multiple physical units.

---

## 3. Enabling Modbus on SolarEdge Inverters

### 3.1 SetApp Models (Current Generation)

SetApp configuration requires the **SolarEdge SetApp** mobile application (iOS or Android). Installer-level access is required.

**Enabling Modbus TCP:**

1. Toggle the red switch on the inverter to the **"P"** position briefly (less than 5 seconds) to activate inverter Wi-Fi
2. Open SetApp and scan the QR code on the inverter label
3. Phone connects to the inverter's temporary Wi-Fi access point (`SEDGxxxxxx`)
4. Navigate to: **Commissioning → Site Communication → Modbus TCP**
5. Set **Modbus TCP** to **Enabled**
6. Note the port number — default is **1502**
7. Save and allow the inverter to reconnect to LAN

**Enabling Modbus RS485:**

1. Connect via SetApp as above
2. Navigate to: **Commissioning → Site Communication → RS485-X** (choose port 1 or 2)
3. Set the RS485 port function to **Non-SE Logger (Modbus)**
4. Set a unique **Device ID** for each inverter on the bus (default: 1)

> **Note:** Setting RS485-1 to monitoring mode disables Leader/Follower chaining on that port. Use RS485-2 for monitoring on multi-inverter sites.

### 3.2 LCD Models (Older Generation)

1. Press and hold **OK** for 5 seconds to enter installer mode
2. Enter installer password: `12312312` (maps to ⬆⬇✅⬆⬇✅⬆⬇ on directional buttons)
3. Navigate to: **Communications → LAN Setup → Modbus TCP**
4. Set port to **1502** and enable
5. For RS485: navigate to **Communications → RS485** and set to non-SE device mode

---

## 4. Hardware Requirements

### 4.1 Option A — Ethernet Direct (Modbus TCP)

Recommended for all current-generation SetApp inverters.

**What you need:**

- Standard Ethernet cable (CAT5e/CAT6)
- Monitoring host on the same LAN segment

**Connection:**

1. Connect inverter Ethernet port to your LAN switch
2. Assign a static IP or DHCP reservation for the inverter
3. Enable Modbus TCP via SetApp (Section 3.1)
4. Connect MPG to `<inverter-ip>:1502`

> ⚠️ SolarEdge uses **port 1502**, not the standard Modbus TCP port 502. Always specify port 1502 explicitly.

---

### 4.2 Option B — RS485 Direct (USB Adapter)

For installations where Ethernet access to the inverter is impractical, or for multi-inverter daisy chains.

**What you need:**

- USB to RS485 adapter with screw terminals
- Twisted-pair cable (CAT5e/CAT6 or dedicated shielded 2-wire) to the RS485-2 terminal block

**Community-recommended adapters:**

| Adapter | Notes |
| --- | --- |
| **DSD TECH SH-U11F** | Isolated, includes TVS and termination resistor — most recommended |
| FTDI-based USB RS485 | Reliable driver support on all platforms |
| Waveshare USB TO RS485 (B) | Industrial grade — see Option C |

**Important:** Use RS485-2 (not RS485-1) on multi-inverter sites to preserve the Leader/Follower bus. On single-inverter sites, either port works.

---

### 4.3 Option C — Waveshare USB to RS485 (Industrial Grade)

For permanent installations requiring isolation and surge protection.

| Product | Description | Best For |
| --- | --- | --- |
| **USB TO RS485 (B)** | FT232RNL chipset, isolated, rail-mount | Single inverter, USB host |
| **USB TO RS485/422** | Dual-protocol, TVS surge, 120Ω termination | Long cable runs, noisy environments |

> **Baud rate note:** SolarEdge RS485 defaults to **115200 bps** on SetApp models — significantly faster than the 9600 bps common on other inverter brands. Confirm the Waveshare adapter supports this rate (all current Waveshare RS485 adapters do).

---

### 4.4 Option D — Waveshare RS485 to Ethernet Bridge

For remote monitoring or multi-inverter chains connected over the network.

| Product | Description | Best For |
| --- | --- | --- |
| **RS485 TO ETH (B)** | RS485 ↔ Ethernet; Modbus TCP gateway | Wired LAN |
| **RS485 TO POE ETH (B)** | Same + PoE power; isolated | Single-cable installations |
| **RS485 TO WIFI ETH** | RS485 ↔ Wi-Fi + Ethernet | Wireless environments |

**Setup:**

1. Wire RS485-2 A/B from inverter terminal block to bridge screw terminals
2. Configure IP via VirCom (Windows); default IP: `192.168.1.200`
3. Set to Modbus TCP gateway mode
4. Set baud rate to **115200** (SetApp models) or **9600** (LCD models)

---

## 5. RS485 Port Identification by Model

### SetApp Models (Current Generation — HD-Wave)

| Port | Location | Default Use | Monitoring Use |
| --- | --- | --- | --- |
| **RS485-1** | Communication board, 3-pin terminal | Leader/Follower chaining | Avoid on multi-inverter sites |
| **RS485-2** | Communication board, 3-pin terminal | Meter / third-party | ✅ Preferred for monitoring |
| **RS485-E** | Optional RS485 Plug-in Kit | Extended bus | Second chain or additional meter |

Access the communication board by removing the inverter's communication cover (bottom of unit). Terminals are labeled on the PCB silkscreen.

### LCD Models (Older Generation)

- Single RS485 port only (RS485-1)
- Can be set to either Leader/Follower or third-party mode — not simultaneously

### Home Hub / StorEdge

- RS485-1 available for monitoring
- Battery communications use a separate SolarEdge Home Network protocol — these buses do not conflict

---

## 6. RS485 Pinout & Wiring

SolarEdge RS485 uses a **3-pin screw terminal block** (not RJ45).

``` text
Terminal   Signal
────────   ──────────
   A+      RS485 A+ (data positive)
   B−      RS485 B− (data negative)
   GND     Signal ground (optional but recommended)
```

**Wiring to USB RS485 adapter:**

``` text
Inverter A+   →   Adapter A+
Inverter B−   →   Adapter B−
Inverter GND  →   Adapter GND (optional)
```

**Cable specification:**

- Use shielded twisted-pair (STP) for runs over 5 meters
- CAT6 is acceptable for shorter runs (use only one twisted pair for A/B)
- Maximum bus length: ~1000 meters at 9600 bps; ~100 meters at 115200 bps

**Termination:** Add a 120Ω resistor across A+ and B− at the **far end** of the RS485 bus. Most quality adapters (Waveshare, DSD SH-U11F) include this resistor.

---

## 7. Modbus TCP Configuration Details

| Parameter | Value |
| --- | --- |
| Protocol | Modbus TCP |
| Default Port | **1502** (not the standard 502) |
| Default Device ID | **1** |
| Max concurrent connections | **1** |
| SunSpec Base Register Address | 40000 |

---

## 8. Modbus RS485 Configuration Details

| Parameter | Value |
| --- | --- |
| Protocol | Modbus RTU |
| Default Baud Rate | **115200 bps** (SetApp models) |
| Default Baud Rate | **9600 bps** (LCD / older models) |
| Data bits | 8 |
| Parity | None |
| Stop bits | 1 |
| Default Device ID | **1** |

> For long RS485 cable runs (>30m), reduce baud rate to 9600 or 19200 via SetApp to improve signal integrity.

---

## 9. Key Modbus Registers (SunSpec)

SolarEdge implements SunSpec Modbus with a base address of 40000. Scale factors are in adjacent registers.

| Register | Description | Scale Factor Register |
| --- | --- | --- |
| 40071 | AC Power output (W) | 40075 |
| 40083 | AC Energy lifetime (Wh) | 40087 |
| 40100 | DC Voltage string 1 (V) | 40104 |
| 40101 | DC Power string 1 (W) | 40104 |
| 40093 | Inverter operating status | — |
| 40736 | Battery SOC (%) — StorEdge | 40737 |

> **Scale factor rule:** Multiply the raw register value by 10^(scale factor). For example, AC power register = 3420 with scale factor register = -1 → 342.0 W.

Full SunSpec register documentation:
<https://knowledge-center.solaredge.com/sites/kc/files/sunspec-implementation-technical-note.pdf>

---

## 10. Local & Cloud API Access

SolarEdge provides both a **local API** (on SetApp models) and a **cloud monitoring API** via the Monitoring Portal. Either can serve as the basis for a custom MPG module.

### 10.1 Local API (SetApp Models — LAN)

SetApp-generation inverters expose a local HTTPS API on the inverter's LAN IP. This provides real-time data without cloud dependency.

**Base URL:**

``` text
https://<inverter-ip>/api/v1/
```

> Uses a self-signed certificate. Use `-k` in curl or disable certificate verification in your HTTP client.

**Authentication:**

``` http
POST https://<inverter-ip>/api/v1/login
Content-Type: application/json

{"username": "admin", "password": "YOUR_PASSWORD"}
```

Response contains a session cookie or token used for subsequent requests.

**Key Local Endpoints:**

| Endpoint | Method | Description |
| --- | --- | --- |
| `/api/v1/status` | GET | Inverter operating state and basic data |
| `/api/v1/power_flow` | GET | Real-time power flow (PV, battery, grid, load) |
| `/api/v1/meters` | GET | Connected meter readings |
| `/api/v1/storage` | GET | Battery SOC, state, power (StorEdge/Home Hub) |

**Example — Real-time power flow:**

``` bash
curl -k "https://192.168.1.xxx/api/v1/power_flow" \
  -H "Cookie: session=SESSION_TOKEN"
```

**Example response:**

``` json
{
  "LOAD": {"currentPower": 1850},
  "PV": {"currentPower": 3420},
  "GRID": {"currentPower": -1570},
  "STORAGE": {"currentPower": 0, "chargeLevel": 85}
}
```

### 10.2 SolarEdge Monitoring Portal Cloud API

For cloud-based monitoring, SolarEdge provides a REST API with an API key issued per account.

**Base URL:**

``` text
https://monitoringapi.solaredge.com/
```

**Authentication:** API key as a query parameter:

``` text
https://monitoringapi.solaredge.com/site/{siteId}/overview?api_key=YOUR_KEY
```

**Key Cloud Endpoints:**

| Endpoint | Description |
| --- | --- |
| `/site/{siteId}/overview` | Site-level production overview |
| `/site/{siteId}/powerDetails` | Time-series power (15-min intervals) |
| `/site/{siteId}/storageData` | Battery charge/discharge data |
| `/site/{siteId}/currentPowerFlow` | Real-time power flow snapshot |
| `/equipment/{siteId}/list` | All inverters in the site |

> **Rate limit:** The cloud API is rate-limited to 300 requests per day per API key. For real-time monitoring, the **local API** is strongly preferred.
> **MPG module note:** A `solaredge_local_v1` protocol module could poll the local `/api/v1/power_flow` endpoint for real-time data on SetApp models, providing a zero-Modbus-configuration path to MQTT for all LAN-connected SolarEdge inverters.

---

## 11. MPG Configuration

MPG connects to SolarEdge inverters via Modbus TCP or RTU. Modbus must be enabled per Section 3 before configuration.

**Modbus TCP (recommended):**

``` ini
transport = modbus_tcp
host = 192.168.1.xxx
port = 1502
unit_id = 1
protocol_version = solaredge_sunspec_v1
```

**RS485 RTU:**

``` ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 115200
unit_id = 1
protocol_version = solaredge_sunspec_v1
```

Configuration reference:
<https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki/Configuration-Examples#modbus-tcp-to-mqtt>

---

## 12. Multi-Inverter (Leader/Follower) Setups

**Modbus TCP — multiple inverters:**

- Each inverter has its own IP address and Device ID
- Query each inverter independently — SolarEdge does not support multi-drop over TCP
- All inverters can share a single MPG instance with multiple device entries

**RS485 daisy-chain — multi-inverter:**

1. Use RS485-2 on each inverter (leaving RS485-1 for Leader/Follower sync)
2. Daisy-chain RS485-2 from inverter to inverter with twisted-pair cable
3. Assign each inverter a unique Device ID (1–247) via SetApp
4. Add 120Ω termination at the last inverter in the chain
5. A single RS485 adapter or Waveshare bridge serves the entire chain

> ⚠️ A Follower inverter **cannot simultaneously** communicate Leader sync on RS485-1 and third-party monitoring on RS485-1. Always use RS485-2 for third-party monitoring in Leader/Follower installations.

---

## 13. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| Connection refused | Wrong port (502 vs 1502) | SolarEdge uses port **1502** — specify explicitly |
| Connection refused on correct port | Modbus TCP not enabled | Enable via SetApp on-site per Section 3.1 |
| Second client gets connection refused | Single-connection limit | Disconnect all other Modbus clients before connecting |
| Modbus TCP stops working after time | Known firmware hang | Re-enable/disable Modbus via SetApp; consider scheduled reconnect automation |
| RS485 no response | RS485-1 in Leader/Follower mode | Use RS485-2; or change RS485-1 to Non-SE mode via SetApp |
| Baud rate mismatch | 115200 vs 9600 | Verify actual baud via SetApp; SetApp model default is 115200 |
| Duplicate Device IDs | Common multi-inverter setup error | Set unique Device ID per inverter individually in SetApp |
| Scale factor = 0 gives wrong value | Scale factor register not read | Read both data register and scale factor register; apply per Section 9 |
| Local API returns 401 | Session expired | Re-authenticate via `/api/v1/login` and refresh session token |
