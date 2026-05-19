# SMA Solar — MQTT Integration Guide

> **Supported Models:** Sunny Boy (all generations) · Sunny Tripower · Sunny Island · Sunny Boy Storage · Sunny Boy TL-US
> **Protocol:** Modbus TCP (primary) · Modbus RTU over RS485 (select models via add-on module) · Speedwire JSON API (Data Manager M)
> **Interface:** Ethernet (LAN) — primary · RS485 — optional via DM-485CB-10 module
> **Status:** Well-supported — community-confirmed

---

## Table of Contents

1. [Overview](#1-overview)
2. [Supported Models & Interface Matrix](#2-supported-models--interface-matrix)
3. [Enabling Modbus on SMA Inverters](#3-enabling-modbus-on-sma-inverters)
   - 3.1 [Sunny Boy / Tripower — Modern Firmware (Web UI)](#31-sunny-boy--tripower--modern-firmware-web-ui)
   - 3.2 [Older LCD Models](#32-older-lcd-models)
4. [Hardware Requirements](#4-hardware-requirements)
   - 4.1 [Option A — Ethernet Direct (Modbus TCP)](#41-option-a--ethernet-direct-modbus-tcp)
   - 4.2 [Option B — RS485 via DM-485CB-10 Module](#42-option-b--rs485-via-dm-485cb-10-module)
   - 4.3 [Option C — Waveshare USB to RS485 (Industrial Grade)](#43-option-c--waveshare-usb-to-rs485-industrial-grade)
   - 4.4 [Option D — Waveshare RS485 to Ethernet Bridge](#44-option-d--waveshare-rs485-to-ethernet-bridge)
   - 4.5 [Option E — SMA Data Manager M (Multi-Inverter)](#45-option-e--sma-data-manager-m-multi-inverter)
5. [Modbus TCP Connection Details](#5-modbus-tcp-connection-details)
6. [RS485 Connection Details](#6-rs485-connection-details)
7. [Modbus Unit IDs](#7-modbus-unit-ids)
8. [Key Modbus Registers](#8-key-modbus-registers)
9. [Local API Access (Speedwire / Data Manager M)](#9-local-api-access-speedwire--data-manager-m)
10. [MPG Configuration](#10-mpg-configuration)
11. [Multi-Inverter Setups](#11-multi-inverter-setups)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Overview

SMA inverters implement Modbus over **Ethernet (TCP)** as the primary monitoring interface. Most models do not expose Modbus natively over RS485 — the RS485 interface on older SMA hardware used the proprietary **SMA Data 1** protocol, not standard Modbus RTU.

> **Key distinction from other brands:** RS485 Modbus RTU is available only via the optional `DM-485CB-10` add-on module, which replaces the Ethernet module on Tripower TL-30 models — meaning you cannot have both Ethernet and RS485 simultaneously on that series.

**Modbus must be manually enabled** in the inverter settings — it is disabled by default on all models.

---

## 2. Supported Models & Interface Matrix

| Model Series | Modbus TCP | RS485 Modbus | Speedwire API | Notes |
| --- | --- | --- | --- | --- |
| Sunny Boy 3.0–7.7 (TL-US) | ✅ Ethernet | ❌ | ❌ | Enable in settings; unit ID = 3 |
| Sunny Boy 3.0–6.0 (1AV-40) | ✅ Ethernet | ❌ | ❌ | SunSpec-compatible |
| Sunny Tripower 3–10 kW (3AV-40) | ✅ Ethernet | Optional (DM-485CB-10) | ❌ | RS485 replaces Ethernet module |
| Sunny Tripower 15–25 kW (TL-30) | ✅ Ethernet | Optional (DM-485CB-10) | ❌ | Requires SMA Data Manager M for Wi-Fi |
| Sunny Boy Storage | ✅ Ethernet | ❌ | ❌ | Storage registers available |
| Sunny Island | ✅ Ethernet | Via external adapter | ❌ | Often paired with Victron CAN |
| SMA Data Manager M | ✅ Aggregated | ✅ (downstream) | ✅ JSON | Gateway for up to 50 inverters |

> **SunSpec compatibility:** All modern SMA inverters with Ethernet support SunSpec Modbus (unit ID 126) in addition to the proprietary SMA Modbus profile (unit ID 3). SunSpec unit ID 126 is preferred for cross-vendor compatibility.

---

## 3. Enabling Modbus on SMA Inverters

**Modbus TCP is disabled by default.** It must be activated before any Modbus connection will succeed.

### 3.1 Sunny Boy / Tripower — Modern Firmware (Web UI)

1. Connect to the inverter's web interface at its LAN IP (find via router DHCP table or SMA Energy App)
2. Log in as **Installer** (default password: `1234` or as set during commissioning)
3. Navigate to: **Device Parameters → External Communication → Modbus TCP server**
4. Set **Modbus TCP** to **Enabled**
5. Default port: **502**
6. Save — the inverter begins accepting Modbus TCP connections immediately

> **Note:** SMA inverters accept only **one Modbus TCP connection at a time.** Additional connection attempts are refused until the existing connection closes.

### 3.2 Older LCD Models

For LCD-based inverters (e.g., older Sunny Boy TL-US):

1. Press and hold **OK** for 5 seconds to enter installer mode
2. Enter installer password: `1234` (or as commissioned)
3. Navigate to: **Communications → LAN Setup → Modbus TCP**
4. Enable Modbus TCP; note default port (502)

---

## 4. Hardware Requirements

### 4.1 Option A — Ethernet Direct (Modbus TCP)

The simplest and recommended approach for all modern SMA models.

**What you need:**

- Standard Ethernet cable (CAT5e/CAT6)
- Monitoring host (Raspberry Pi, NUC, Linux server) on the same LAN

**Connection:**

1. Connect inverter Ethernet port to your LAN switch
2. Assign a static IP or DHCP reservation for the inverter
3. Enable Modbus TCP per Section 3
4. Connect from MPG or bridge software to `<inverter-ip>:502`

No additional hardware is required beyond a standard Ethernet connection.

---

### 4.2 Option B — RS485 via DM-485CB-10 Module

For the Sunny Tripower TL-30 series (15–25 kW), an optional RS485 module adds wired RS485 connectivity.

> ⚠️ **Important:** Installing the `DM-485CB-10` module on a Sunny Tripower TL-30 **physically replaces** the Ethernet module. The inverter cannot have both interfaces simultaneously on this model. On Tripower 3AV-40 models, a dedicated RS485-2 port is available without sacrificing Ethernet.

**Module:** Sunny Tripower RS485 Data Module (`DM-485CB-10`)

**Wiring:** Standard RS485 A/B differential pair to a USB RS485 adapter or Waveshare bridge (see Options C and D).

---

### 4.3 Option C — Waveshare USB to RS485 (Industrial Grade)

For permanent RS485 installations requiring isolation and surge protection.

| Product | Chipset | Isolation | Best For |
| --- | --- | --- | --- |
| **USB TO RS485 (B)** | FT232RNL | ✅ | Single inverter, direct USB host |
| **USB TO RS485/422** | FT232RNL | ✅ | Long cable runs, noisy environments |

**Key advantages:**

- Built-in power and signal isolation minimizes ground loop risk
- TVS transient voltage suppression
- Wide OS support: Linux, Raspberry Pi OS, macOS, Windows

---

### 4.4 Option D — Waveshare RS485 to Ethernet Bridge

For monitoring over the network without a USB host co-located at the inverter.

| Product | Description | Best For |
| --- | --- | --- |
| **RS485 TO ETH (B)** | RS485 ↔ Ethernet; Modbus TCP gateway | Wired LAN |
| **RS485 TO POE ETH (B)** | Same + PoE power; electrically isolated | Clean single-cable installations |
| **RS485 TO WIFI ETH** | RS485 ↔ Wi-Fi + Ethernet | Wireless environments |

> **Note:** The Waveshare bridge operates as a serial-over-TCP bridge. Ensure MPG or your bridge software is configured for RTU-over-TCP mode, or use `mbusd` to convert to native Modbus TCP.

**Setup:**

1. Wire RS485 A/B from inverter DM-485CB-10 terminals to bridge screw terminals
2. Configure IP via VirCom (Windows); default IP: `192.168.1.200`
3. Set TCP Client mode, target your MPG host
4. Set baud rate to match inverter setting (default: 9600)

---

### 4.5 Option E — SMA Data Manager M (Multi-Inverter)

For installations with multiple SMA inverters, the **SMA Data Manager M** aggregates data from up to 50 inverters via Speedwire (SMA's proprietary LAN protocol) and exposes a single Modbus TCP endpoint and local JSON API.

**Data access methods:**

- Modbus TCP at the Data Manager's IP (port 502)
- Local JSON API (see Section 9)
- SMA Sunny Portal cloud (optional)

---

## 5. Modbus TCP Connection Details

| Parameter | Value |
| --- | --- |
| Protocol | Modbus TCP |
| Default Port | **502** |
| SMA Modbus Unit ID | **3** |
| SunSpec Unit ID | **126** |
| Max concurrent connections | **1** |
| Baud rate | N/A (TCP) |

> Use **unit ID 3** for the full SMA-specific Modbus profile.
> Use **unit ID 126** for SunSpec-standard registers (preferred for cross-vendor tools).

**Example connection string:**

``` ini
host = 192.168.1.xxx
port = 502
unit_id = 3
```

---

## 6. RS485 Connection Details

Applies only to models with the `DM-485CB-10` module installed (Tripower TL-30) or Tripower models with a dedicated RS485-2 port.

| Parameter | Value |
| --- | --- |
| Protocol | Modbus RTU |
| Default Baud Rate | **9600** (changeable to 115200 on newer firmware) |
| Data bits | 8 |
| Parity | None |
| Stop bits | 1 |
| Default Slave ID | **3** |

**RS485 Wiring (3-wire):**

``` text
Inverter Terminal   →   RS485 Adapter
     RS485-A        →   A+
     RS485-B        →   B−
     GND            →   GND (recommended)
```

Use shielded twisted-pair cable for runs over 3 meters.

---

## 7. Modbus Unit IDs

SMA inverters expose two separate register maps depending on the unit ID used:

| Unit ID | Profile | Use |
| --- | --- | --- |
| **3** | SMA proprietary Modbus | Full SMA device data, all parameters |
| **126** | SunSpec (standard) | Cross-vendor compatibility |

For MPG integration, **unit ID 3** provides the most complete dataset for SMA-specific parameters.

---

## 8. Key Modbus Registers

All registers use unit ID 3 unless noted. Values are 32-bit unsigned integers unless otherwise specified.

| Register | Description | Scale | Unit |
| --- | --- | --- | --- |
| 30775 | AC Power output | ÷100 | W |
| 30783 | Grid voltage L1 | ÷100 | V |
| 30977 | Daily energy yield | ÷1 | Wh |
| 30529 | Total lifetime yield | ÷1 | Wh |
| 30201 | Operating state | — | Enum |
| 30867 | Battery SOC (Sunny Boy Storage) | ÷100 | % |
| 30775 | AC Power (SunSpec, unit ID 126) | ÷1 | W |

> **Sentinel value:** A returned value of `2147483648` (0x80000000) means the inverter has no valid data for that register — typically during night mode or a fault condition. Treat this as `null` rather than a real measurement.

Full SMA Modbus register documentation:
<https://www.sma.de/en/products/product-features-interfaces/modbus-protocol-interface>

---

## 9. Local API Access (Speedwire / Data Manager M)

The **SMA Data Manager M** exposes a local REST JSON API via the Speedwire interface that can be used to develop a custom MPG module. All modern SMA inverters connected to a Data Manager are accessible through this API without requiring Modbus.

### Base URL

``` text
https://<data-manager-ip>/api/v1/
```

> The Data Manager uses a self-signed TLS certificate. Use `-k` in curl or configure your HTTP client to skip certificate verification.

### Authentication

The API uses session-based authentication via a login endpoint:

``` http
POST https://<data-manager-ip>/api/v1/token
Content-Type: application/json

{"right": "usr", "pass": "YOUR_PASSWORD"}
```

The response returns a session token. Pass this token as a Bearer header on all subsequent requests:

``` http
Authorization: Bearer <session-token>
```

### Key Endpoints

| Endpoint | Method | Description |
| --- | --- | --- |
| `/api/v1/plants` | GET | List all connected inverters and plants |
| `/api/v1/measurements/live` | POST | Real-time live measurements (power, voltage, current) |
| `/api/v1/measurements/overview` | GET | System-level production and consumption summary |
| `/api/v1/devices/{deviceId}` | GET | Device-specific detail and status |
| `/api/v1/plants/{plantId}/measurements` | GET | Time-series energy data for a plant |

### Live Measurement Request Example

``` json
POST /api/v1/measurements/live
{
  "destDev": [],
  "channels": [
    {"channelId": "Metering_TotWhOut", "componentId": "IGULD:SELF"},
    {"channelId": "Metering_GridMs_TotW_Cur", "componentId": "IGULD:SELF"}
  ]
}
```

### Individual Inverter via Speedwire (No Data Manager)

For single Sunny Boy / Tripower units without a Data Manager, a basic web API is available on the inverter directly:

``` text
https://<inverter-ip>/dyn/getDashValues.json
```

This endpoint returns current AC power, daily yield, and inverter status. No authentication is required on some firmware versions; others require the installer password via HTTP Basic auth.

**Example response (abbreviated):**

``` json
{
  "result": {
    "0199-xxxxx": {
      "6100_40263F00": {"val": 3420},
      "6400_00260100": {"val": 12345678}
    }
  }
}
```

Key identifiers: `6100_40263F00` = current AC power (W); `6400_00260100` = total yield (Wh).

> **MPG module note:** A future `sma_speedwire_v1` protocol module could poll `getDashValues.json` on individual inverters or the Data Manager's `/api/v1/measurements/live` endpoint, avoiding the need for Modbus TCP entirely and supporting all SMA models with a network connection.

---

## 10. MPG Configuration

MPG connects to SMA inverters via Modbus TCP. Modbus must be enabled per Section 3 before configuration.

**Modbus TCP (recommended):**

``` ini
transport = modbus_tcp
host = 192.168.1.xxx
port = 502
unit_id = 3
protocol_version = sma_modbus_v1
```

**RS485 RTU (DM-485CB-10 module required):**

``` ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 3
protocol_version = sma_modbus_v1
```

Configuration reference:
<https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki/Configuration-Examples#modbus-tcp-to-mqtt>

---

## 11. Multi-Inverter Setups

**Modbus TCP — multiple inverters:**

- Each inverter connects individually via Ethernet with its own IP address
- Each must have a unique unit ID
- Query each inverter separately — SMA does not support multi-drop on TCP
- Consider using the SMA Data Manager M to expose all inverters under a single TCP endpoint

**RS485 bus — daisy-chain:**

- Daisy-chain inverters on the RS485 bus (DM-485CB-10 module per inverter)
- Assign each inverter a unique slave ID (1–247)
- A single RS485 adapter or Waveshare bridge serves the entire chain
- Add 120Ω termination at the far end of the bus

---

## 12. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| Connection refused on port 502 | Modbus TCP not enabled | Enable in inverter web UI or LCD menu per Section 3 |
| Second client connection refused | Single-connection limit | Close all other Modbus clients before connecting (SMA Energy App, other tools) |
| Register reads return `NaN` | Wrong unit ID | Try unit ID 3 (SMA native) or 126 (SunSpec) |
| `2147483648` returned for a register | Inverter in night mode or fault | Known sentinel value — treat as null; not a wiring issue |
| RS485 no response | DM-485CB-10 not installed or Ethernet module still present | Physically confirm RS485 module is installed and Ethernet module is removed |
| Modbus stops responding intermittently | Known firmware bug on some models | Re-enable/disable Modbus via web UI to reset; schedule periodic checks via automation |
| API returns 401 Unauthorized | Session token expired | Re-authenticate via `/api/v1/token` and refresh Bearer token |
