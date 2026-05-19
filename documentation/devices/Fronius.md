# Fronius — MQTT Integration Guide

> **Supported Models:** Primo GEN24 · Primo GEN24 Plus · Symo GEN24 · Symo GEN24 Plus · Tauro · Galvo · Symo (legacy) · Primo (legacy)
> **Protocol:** Modbus TCP · Modbus RTU over RS485 (GEN24 models) · Fronius Solar API v1 (JSON REST — all models)
> **Interface:** Ethernet · Wi-Fi · RS485 terminals (GEN24 only)
> **Status:** Well-supported — SunSpec-compliant, Solar API widely used in community integrations

---

## Table of Contents

1. [Overview](#1-overview)
2. [Supported Models & Protocol Matrix](#2-supported-models--protocol-matrix)
3. [Communication Interfaces by Model Generation](#3-communication-interfaces-by-model-generation)
4. [Enabling Modbus on Fronius Inverters](#4-enabling-modbus-on-fronius-inverters)
   - 4.1 [GEN24 Models (Web UI)](#41-gen24-models-web-ui)
   - 4.2 [Legacy Symo / Primo (Datamanager 2.0 Web UI)](#42-legacy-symo--primo-datamanager-20-web-ui)
5. [Hardware Requirements](#5-hardware-requirements)
   - 5.1 [Option A — Ethernet Direct (Modbus TCP)](#51-option-a--ethernet-direct-modbus-tcp)
   - 5.2 [Option B — RS485 Direct (GEN24 Only)](#52-option-b--rs485-direct-gen24-only)
   - 5.3 [Option C — Waveshare USB to RS485](#53-option-c--waveshare-usb-to-rs485)
   - 5.4 [Option D — Waveshare RS485 to Ethernet Bridge](#54-option-d--waveshare-rs485-to-ethernet-bridge)
6. [RS485 Pinout (GEN24 Models)](#6-rs485-pinout-gen24-models)
7. [Modbus TCP Configuration Details](#7-modbus-tcp-configuration-details)
8. [Modbus RS485 Configuration Details](#8-modbus-rs485-configuration-details)
9. [Key Modbus Registers (SunSpec Float)](#9-key-modbus-registers-sunspec-float)
10. [Fronius Solar API v1 (Local REST)](#10-fronius-solar-api-v1-local-rest)
11. [MPG Configuration](#11-mpg-configuration)
12. [Multi-Inverter (Cascaded) Setups](#12-multi-inverter-cascaded-setups)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. Overview

Fronius inverters offer three communication approaches for monitoring integration:

- **Modbus TCP** — via the inverter's Ethernet port; preferred for real-time, low-latency monitoring and control
- **Modbus RTU over RS485** — available on GEN24 models via internal RS485 terminals (`COM0` / `COM1`)
- **Fronius Solar API v1** — a local HTTP JSON API built into the Datamanager 2.0 and GEN24 network interface; requires no Modbus configuration and is the easiest path for polling-based integration

> **Key note:** Unlike SMA and SolarEdge, Fronius supports **multiple simultaneous Modbus TCP connections**. You can connect both MPG and Home Assistant at the same time without conflict.

---

## 2. Supported Models & Protocol Matrix

| Model | Modbus TCP | RS485 RTU | Solar API | Notes |
| --- | --- | --- | --- | --- |
| Primo GEN24 Plus | ✅ | ✅ | ✅ | Current US residential model |
| Symo GEN24 Plus | ✅ | ✅ | ✅ | 3-phase; sold commercially in US |
| Tauro | ✅ | ✅ | ✅ | Commercial / utility scale |
| Galvo | ✅ | ✅ | ✅ | Smaller residential single-phase |
| Primo (legacy, Datamanager 2.0) | ✅ | ❌ | ✅ | RS485 uses Solar.net, not Modbus RTU |
| Symo (legacy, Datamanager 2.0) | ✅ | ❌ | ✅ | Same RS485 limitation as Primo legacy |

---

## 3. Communication Interfaces by Model Generation

### GEN24 Models (Current)

GEN24 models have the network interface built directly into the inverter — no separate Datamanager card is required.

- **Ethernet port:** Built-in; Modbus TCP on port 502
- **Wi-Fi:** Built-in; Modbus TCP available over Wi-Fi
- **RS485 terminals:** Two interfaces labeled `COM0` and `COM1` (or `RS485 interface 0` and `RS485 interface 1`) on the internal terminal strip

### Legacy Symo / Primo (Datamanager 2.0)

Older models use the **Fronius Datamanager 2.0** card installed in a communication slot on the inverter.

- **Ethernet port:** On the Datamanager 2.0 card
- **Wi-Fi:** Optional WLAN module on the Datamanager 2.0 card
- **RS485:** Present on the card, but uses **Fronius Solar.net** protocol (proprietary inter-inverter daisy chain) — **not standard Modbus RTU**

> Third-party Modbus RTU access over RS485 is **not available** on legacy models. Use Modbus TCP (via Datamanager 2.0 Ethernet) or the Solar API instead.

---

## 4. Enabling Modbus on Fronius Inverters

### 4.1 GEN24 Models (Web UI)

1. Open a browser and navigate to `http://<inverter-ip>`
2. Log in as **Service** user (password varies — check the inverter label or commissioning sheet)
3. Navigate to: **Communication → Modbus**
4. Configure the following:

    | Setting | Value |
    | --- | --- |
    | Data export via Modbus | **TCP** (for Ethernet) or **RTU** (for RS485 terminals) |
    | SunSpec Model Type | **float** (recommended for most integrations) |
    | Modbus TCP port | **502** |
    | RTU Slave address | **1** (default; increment for multi-inverter RS485 bus) |

5. For RS485 RTU, also navigate to: **Communication → Modbus → RTU interface 0** and set to **Slave**
6. Save settings

> **SunSpec Model Type:** The `float` setting uses floating-point format (SunSpec models I111/I112/I113). The `int+SF` setting uses integer registers with scale factors (models I101/I102/I103). Most integrations and MPG modules work with `float`. Use `int+SF` only if a specific tool requires it.

### 4.2 Legacy Symo / Primo (Datamanager 2.0 Web UI)

1. Open browser at `http://<datamanager-ip>`
2. Navigate to: **Settings → Modbus**
3. Set **Modbus TCP** to **Enabled**
4. Set SunSpec Model Type to **float**
5. Default port: **502**
6. Save

---

## 5. Hardware Requirements

### 5.1 Option A — Ethernet Direct (Modbus TCP)

The simplest and recommended approach for all Fronius models with network connectivity.

**What you need:**

- Standard Ethernet cable (CAT5e/CAT6)
- Monitoring host on the same LAN segment

**Connection:**

1. Connect Fronius inverter (or Datamanager 2.0) Ethernet port to your LAN switch
2. Assign a static IP or DHCP reservation for the inverter
3. Enable Modbus TCP per Section 4
4. Connect MPG to `<inverter-ip>:502`

---

### 5.2 Option B — RS485 Direct (GEN24 Only)

GEN24 models expose RS485 RTU terminals (`COM0` / `COM1`) on the internal communication terminal strip.

**What you need:**

- USB to RS485 adapter with screw terminals
- 2-wire shielded twisted-pair cable from inverter COM0/COM1 terminals to adapter

**Use cases:**

- Air-gapped installations (no LAN available)
- Pi/host physically co-located without Ethernet infrastructure
- Multiple GEN24 units on a single RS485 daisy chain

---

### 5.3 Option C — Waveshare USB to RS485

For permanent GEN24 RS485 installations.

| Product | Description | Best For |
| --- | --- | --- |
| **USB TO RS485 (B)** | FT232RNL, isolated, rail-mount | Single inverter, USB host |
| **USB TO RS485/422** | Dual-protocol, TVS surge, 120Ω termination | Long cable runs |

Both adapters provide isolation valuable in the inverter's high-power environment.

---

### 5.4 Option D — Waveshare RS485 to Ethernet Bridge

For GEN24 RS485 monitoring over the network without a local USB host.

| Product | Description | Best For |
| --- | --- | --- |
| **RS485 TO ETH (B)** | RS485 ↔ Ethernet; Modbus TCP gateway | Wired LAN |
| **RS485 TO POE ETH (B)** | Same + PoE power; isolated | Clean single-cable installations |
| **RS485 TO WIFI ETH** | RS485 ↔ Wi-Fi + Ethernet | Wireless environments |

**Setup:**

1. Wire COM0 DATA+/DATA− terminals to bridge screw terminals
2. Configure IP via VirCom (Windows); default IP: `192.168.1.200`
3. Set baud rate to 9600 (Fronius GEN24 RS485 default)
4. Set to Modbus TCP gateway mode

---

## 6. RS485 Pinout (GEN24 Models)

GEN24 RS485 interfaces are on the internal terminal strip. Interfaces are labeled `COM0` and `COM1` (or `RS485 interface 0` / `1`).

``` text
Terminal Label   Signal
──────────────   ──────────────────────────────
    DATA+        RS485 A+ (positive differential)
    DATA−        RS485 B− (negative differential)
    GND          Signal ground
    +12V         12V power supply output (for external devices — use with caution)
```

**Standard 2-wire wiring to USB adapter:**

``` text
Inverter DATA+  →  Adapter A+
Inverter DATA−  →  Adapter B−
Inverter GND    →  Adapter GND (recommended for noise immunity)
```

> Fronius recommends `COM0` for meter communication and `COM1` for battery or third-party device communication. Verify per your specific model's installation manual.

---

## 7. Modbus TCP Configuration Details

| Parameter | Value |
| --- | --- |
| Protocol | Modbus TCP |
| Default Port | **502** |
| Inverter Unit ID | **1** (default) |
| Smart Meter Unit ID | **200** (legacy Datamanager) or **240** (GEN24) |
| Battery Storage Unit ID | **126** |
| Max concurrent connections | Multiple allowed |

---

## 8. Modbus RS485 Configuration Details

| Parameter | Value |
| --- | --- |
| Protocol | Modbus RTU |
| Default Baud Rate | **9600 bps** |
| Data bits | 8 |
| Parity | None |
| Stop bits | 1 |
| Default Slave Address | **1** |

Baud rate and slave address are configurable from the GEN24 web UI under Communication → Modbus → RTU settings.

---

## 9. Key Modbus Registers (SunSpec Float)

Using SunSpec float format (default). Base address: 40000.

| Register | Description | Unit |
| --- | --- | --- |
| 40070 | AC Power output | W |
| 40072 | AC Frequency | Hz |
| 40078 | AC Energy total (lifetime) | Wh |
| 40102 | DC Voltage string 1 | V |
| 40103 | DC Current string 1 | A |
| 40240 | Battery SOC (if storage connected) | % |

> **Known issue — `NaN` on startup:** Fronius inverters may return NaN or 0 for scale factors during the first 30–60 seconds after boot. Wait for full initialization before relying on register reads.

Full GEN24 Modbus register documentation:
<https://manuals.fronius.com/html/4204102649/en-US.html>

---

## 10. Fronius Solar API v1 (Local REST)

The Fronius Solar API v1 is built into every Datamanager 2.0 and GEN24 network interface. It requires no authentication for read-only local access by default, making it the **easiest integration path** for a custom MPG module.

### Base URL

``` text
http://<inverter-ip>/solar_api/v1/
```

No authentication is required for local read access on most firmware versions. HTTPS is not enforced — plain HTTP is used.

### Key Endpoints

| Endpoint | Description |
| --- | --- |
| `GetInverterRealtimeData.cgi?Scope=Device&DeviceId=1&DataCollection=CommonInverterData` | AC power, energy, voltage, current, inverter status |
| `GetPowerFlowRealtimeData.fcgi` | Full site power flow: PV, grid, load, battery |
| `GetMeterRealtimeData.cgi?Scope=System` | Smart meter data (if a Fronius Smart Meter is connected) |
| `GetStorageRealtimeData.cgi?Scope=System` | Battery storage data (if BYD, LG, or Fronius battery connected) |
| `GetInverterInfo.cgi` | Inverter model, serial number, firmware version |
| `GetActiveDeviceInfo.cgi?DeviceClass=Inverter` | Enumerate all connected inverters by DeviceId |

### GetInverterRealtimeData Example

``` bash
curl "http://192.168.1.xxx/solar_api/v1/GetInverterRealtimeData.cgi?Scope=Device&DeviceId=1&DataCollection=CommonInverterData"
```

**Response:**

``` json
{
  "Body": {
    "Data": {
      "PAC":           {"Value": 3420,     "Unit": "W"},
      "TOTAL_ENERGY":  {"Value": 12345678, "Unit": "Wh"},
      "DAY_ENERGY":    {"Value": 8765,     "Unit": "Wh"},
      "UAC":           {"Value": 241.3,    "Unit": "V"},
      "IAC":           {"Value": 14.2,     "Unit": "A"},
      "UDC":           {"Value": 385.2,    "Unit": "V"},
      "IDC":           {"Value": 9.1,      "Unit": "A"},
      "DeviceStatus":  {"StatusCode": 7,   "InverterState": "Running"}
    }
  },
  "Head": {"Status": {"Code": 0, "Reason": "", "UserMessage": ""}}
}
```

### GetPowerFlowRealtimeData Example

``` bash
curl "http://192.168.1.xxx/solar_api/v1/GetPowerFlowRealtimeData.fcgi"
```

**Response (abbreviated):**

``` json
{
  "Body": {
    "Data": {
      "Site": {
        "P_Grid":    -1250.0,
        "P_Load":    -2170.0,
        "P_PV":       3420.0,
        "P_Akku":        0.0,
        "E_Day":      8765.0,
        "E_Total": 12345678.0
      }
    }
  }
}
```

> **Sign convention:** Negative `P_Grid` means exporting to grid; negative `P_Load` means consuming load. `P_Akku` is battery power (positive = charging, negative = discharging on some firmware versions — verify against your unit).

### Multi-Inverter Enumeration

To query multiple inverters daisy-chained via Solar.net (legacy) or on a multi-inverter GEN24 site, first enumerate DeviceIds:

``` bash
curl "http://192.168.1.xxx/solar_api/v1/GetActiveDeviceInfo.cgi?DeviceClass=Inverter"
```

Then query each DeviceId individually using `&DeviceId=N` in the CommonInverterData endpoint.

> **MPG module note:** A `fronius_solar_api_v1` protocol module polling `GetPowerFlowRealtimeData.fcgi` and `GetInverterRealtimeData.cgi` would provide comprehensive site data for all Fronius models — including legacy units — with zero Modbus configuration required. The unauthenticated local HTTP endpoint makes implementation straightforward.

---

## 11. MPG Configuration

MPG connects to Fronius inverters via Modbus TCP or RS485 RTU. Modbus must be enabled per Section 4.

**Modbus TCP (recommended for all models):**

``` ini
transport = modbus_tcp
host = 192.168.1.xxx
port = 502
unit_id = 1
protocol_version = fronius_sunspec_v1
sunspec_model_type = float
```

**RS485 RTU (GEN24 only):**

``` ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 1
protocol_version = fronius_sunspec_v1
```

**Smart meter (separate unit ID):**

``` ini
[meter]
transport = modbus_tcp
host = 192.168.1.xxx
port = 502
unit_id = 240
protocol_version = fronius_meter_v1
```

Configuration reference:
<https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki/Configuration-Examples#modbus-tcp-to-mqtt>

---

## 12. Multi-Inverter (Cascaded) Setups

**Modbus TCP — multiple inverters:**

- Each GEN24 inverter has its own IP and Device ID (unit ID)
- Query each inverter independently using separate MPG device entries
- Fronius allows multiple simultaneous TCP connections — no conflict with Home Assistant running in parallel

**RS485 daisy chain (GEN24 only):**

- Connect up to 31 inverters on a single RS485 bus
- Assign unique slave addresses (1–31) per inverter via each unit's web UI
- Add 120Ω termination at the far end of the bus
- One USB adapter or Waveshare bridge serves the entire chain

**Legacy Solar.net daisy chain (Datamanager 2.0):**

- Legacy inverters connect via Solar.net RS485 with the Datamanager 2.0 at the head
- Up to 100 inverters per Datamanager
- The Datamanager presents all inverters via a single Ethernet/Modbus TCP endpoint and via the Solar API (using `DeviceId=1`, `DeviceId=2`, etc.)

---

## 13. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| Modbus TCP no response | Modbus not enabled | Enable Modbus TCP in web UI and confirm SunSpec model type is set |
| Register reads return 0 | Wrong SunSpec model type | Try switching between `float` and `int+SF` in inverter settings |
| NaN on startup | Inverter not fully initialized | Wait 60 seconds after boot before polling; `NaN` on scale factor is a known startup condition |
| Solar API not reachable | IP wrong or Wi-Fi issue | Ping the inverter; check DHCP; switch to Ethernet if on Wi-Fi |
| RS485 no response | Slave mode not configured | Set `RS485 interface 0` to **Slave** in GEN24 web UI under Communication → Modbus |
| Smart meter data missing | Wrong unit ID | Try unit ID 200 (legacy Datamanager) or 240 (GEN24) for the Fronius Smart Meter |
| Battery/storage registers empty | Storage not provisioned | Verify battery communication is configured in the inverter's storage settings |
| Multi-inverter: only DeviceId=1 responds | Legacy Solar.net enumeration issue | Query `GetActiveDeviceInfo.cgi` first to confirm active DeviceIds on that Datamanager |
