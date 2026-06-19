# Growatt Inverter — MQTT Integration Guide

> **Supported Models:** SPF 5000 · SPF 5000TL HVM-WPV · SPF 6000ES · SPF 12000T DVM-US MPV · and other SPF-series models
> **Protocol:** Modbus RTU over USB or RS485 · Growatt ShineWifi / ShineLink Local API
> **Interface:** USB-B · USB-A · RS485 (select models) · DB9 RS232 (legacy models)
> **Status:** Confirmed working

---

## Table of Contents

1. [Overview](#1-overview)
2. [Supported Models & Protocol Versions](#2-supported-models--protocol-versions)
3. [Hardware Requirements](#3-hardware-requirements)
   - 3.1 [Option A — USB-A or USB-B Cable (Direct)](#31-option-a--usb-a-or-usb-b-cable-direct)
   - 3.2 [Option B — DB9 RS232 Adapter (Legacy Models)](#32-option-b--db9-rs232-adapter-legacy-models)
   - 3.3 [Option C — RS485 Adapter with RJ45 (Best Long-Term Reliability)](#33-option-c--rs485-adapter-with-rj45-best-long-term-reliability)
   - 3.4 [Option D — Waveshare USB to RS485 (Industrial Grade)](#34-option-d--waveshare-usb-to-rs485-industrial-grade)
   - 3.5 [Option E — Waveshare RS485 to Ethernet/Wi-Fi Bridge](#35-option-e--waveshare-rs485-to-ethernetwi-fi-bridge)
   - 3.6 [Optional: USB Isolator (Recommended)](#36-optional-usb-isolator-recommended)
4. [Port Identification by Model](#4-port-identification-by-model)
5. [Parallel Installations](#5-parallel-installations)
6. [Growatt ShineWifi / ShineLink Local API](#6-growatt-shinewifi--shinelink-local-api)
7. [MPG Configuration](#7-mpg-configuration)
8. [Home Assistant Cards](#8-home-assistant-cards)
   - 8.1 [PV1 & PV2 Card](#81-pv1--pv2-card)
   - 8.2 [Output Card](#82-output-card)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. Overview

Growatt SPF-series inverters expose a Modbus RTU interface via USB (and RS485 on some models), enabling monitoring of PV input, battery state, output load, and grid parameters. Data is published to an MQTT broker for use with Home Assistant, Node-RED, or other automation platforms.

> **Important:** Growatt's USB port carries Modbus RTU directly over USB D+/D− on most SPF models — a standard USB cable to a host is all that is needed. Some legacy models with a DB9 port require an RS232 adapter. For best long-term reliability, use an RS485 adapter with an RJ45 cable instead of the USB path.

---

## 2. Supported Models & Protocol Versions

| Model | Protocol Version | Interface | Notes |
| --- | --- | --- | --- |
| SPF 5000 | `v0.14` | USB-B | Standard USB cable |
| SPF 5000TL HVM-WPV | `v0.14` | USB-B | Confirmed |
| SPF 6000 ES | `v0.14` | USB-B | Community confirmed |
| SPF 12000T DVM-US MPV | `v0.14` | USB-B | Confirmed |
| DB9 port models | `v0.14` | RS232 (DB9) | PIN1 and PIN2 must be OFF before use |

### Eniris / grid-tied RS485 series (stub protocols)

[SmartgridOne / Eniris](https://docs.eniris.io/en/Controller/Devices/PV-hybrid-and-battery-inverters/Growatt) also documents RS485 for these Growatt lines. MPG has transport-only stubs pending full register maps:

| Eniris series | Example model | Stub `protocol_version` |
| --- | --- | --- |
| MID | MID 11-30K-TL3-XH | `growatt_mid_series` |
| MIN | MIN 2500-6000TL-X | `growatt_min_series` |
| MOD | MOD 3-10K-TL3-XH | `growatt_mod_series` |
| SPA | SPA 1000-3000TL BL | `growatt_spa_series` |
| SPH | SPH 3000-6000TL BL-UP | `growatt_sph_series` |
| UE | 4000-6000 UE | `growatt_ue_series` |
| WIT | WIT 100K-TL3-H | `growatt_wit_series` |

> Other SPF-series models likely respond to `v0.14`. Try it before assuming incompatibility.

---

## 3. Hardware Requirements

### 3.1 Option A — USB-A or USB-B Cable (Direct)

For most SPF-series models, a standard USB cable to the host is all that is required.

**What you need:**

- USB-A to USB-B or USB-A to USB-A cable (depending on inverter model)
- Linux/Raspberry Pi host with an available USB port

**Connection:**

1. Connect the USB cable from the inverter's USB port to your monitoring host
2. Verify the device is detected on Linux: `dmesg | grep tty`
3. Expected output: `usb X-X: cp210x converter now attached to ttyUSB0`
4. Note the device path (e.g., `/dev/ttyUSB0`) for MPG configuration
5. If a secondary USB port exists on the inverter, try it first — it is often more stable

---

### 3.2 Option B — DB9 RS232 Adapter (Legacy Models)

Some older Growatt models use a DB9 connector for RS232 communication instead of USB.

**What you need:**

- DB9 RS232 to USB adapter

> ⚠️ **Critical setting:** Before using RS232 communication, ensure **PIN1 and PIN2 are set to OFF** on the inverter DIP switch. Failure to do so will prevent communication or cause errors.

---

### 3.3 Option C — RS485 Adapter with RJ45 (Best Long-Term Reliability)

For the most reliable, interference-resistant permanent deployment, an RS485 adapter with an RJ45 Ethernet cable is preferred over the USB path. This avoids the USB power stability issues described in Section 3.6.

**What you need:**

- USB to RS485 adapter (screw terminal or RJ45 style)
- RJ45 Ethernet cable (CAT5e or CAT6)

**Connection:**

1. Connect RJ45 cable from inverter RS485 port to adapter
2. Connect adapter USB to monitoring host
3. Verify device detection via `dmesg | grep tty`

---

### 3.4 Option D — Waveshare USB to RS485 (Industrial Grade)

For improved reliability and electrical isolation over generic USB adapters.

| Product | Description | Best For |
| --- | --- | --- |
| **USB TO RS485 (B)** | FT232RNL chipset, isolated, industrial rail case | Single inverter, direct USB host |
| **USB TO RS485/422** | Dual-protocol, built-in TVS surge protection | Noisy environments, long cable runs |

**Key advantages over generic adapters:**

- Power and signal isolation reduces ground loop risk
- TVS transient voltage suppression
- Resettable fuses for overcurrent protection
- Wide OS support — Linux, macOS, Windows, Raspberry Pi OS

---

### 3.5 Option E — Waveshare RS485 to Ethernet/Wi-Fi Bridge

For network-connected monitoring without a USB cable to the host.

| Product | Description | Best For |
| --- | --- | --- |
| **RS485 TO ETH (B)** | RS485 ↔ Ethernet; Modbus/MQTT gateway | Wired LAN |
| **RS485 TO POE ETH (B)** | Same + PoE power; electrically isolated | Single-cable clean installations |
| **RS485 TO WIFI ETH** | RS485 ↔ Wi-Fi + Ethernet | Wireless environments |

**Setup:**

1. Wire RS485 A/B from inverter RS485 port to bridge screw terminals
2. Configure IP via VirCom (Windows); default IP: `192.168.1.200`
3. Set TCP Client mode pointing at your MPG host
4. Set baud rate to 9600

---

### 3.6 Optional: USB Isolator (Recommended)

Growatt inverters have a known USB power quality issue that causes intermittent disconnections in the USB path over time.

**Recommended isolator:** ADUM3160 chipset USB isolator

Example: [AliExpress — ADUM3160 USB Isolator](https://www.aliexpress.com/item/1005002959825296.html)

Insert the isolator between the inverter USB port and your host USB port. This significantly improves connection reliability and is strongly recommended for all direct USB deployments (Options A and D).

---

## 4. Port Identification by Model

### SPF 5000 / 5000TL / 6000ES / 12000T

**Primary port:** USB-B port labeled "Wi-Fi dongle port" or similar on the inverter face

**Alternative port:** If a second USB-A port is present on the communication board, try that first — it is often more electrically stable than the dongle slot

### DB9 Models

**Port:** DB9 connector on the inverter face

Ensure PIN1 and PIN2 are OFF on the inverter DIP switch before connecting.

### RS485 Terminal Block (select models)

Some SPF models expose RS485 via screw terminals labeled **RS485-A** and **RS485-B**. Connect these directly to an RS485 adapter's A and B terminals. This is the preferred connection path when available.

---

## 5. Parallel Installations

Each Growatt inverter in a parallel stack requires its own dedicated cable back to the monitoring host. Growatt does not implement Modbus slave addressing in the standard way for parallel setups, so daisy-chaining is not natively supported.

For multi-inverter setups with a Waveshare multi-channel bridge, each inverter connects to a separate RS485 channel on the bridge, reducing overall cabling to a single network connection.

---

## 6. Growatt ShineWifi / ShineLink Local API

Growatt's Wi-Fi module (ShineWifi-X) and LAN module (ShineLink) expose a **local HTTP API** when connected to your network. This provides an alternative integration path that does not require RS485 cabling and can serve as the basis for a custom MPG module.

> **Note:** The ShineWifi/ShineLink module and the USB RS485 connection can generally be used simultaneously on Growatt inverters, as they use separate physical interfaces on the communication board.

### Discovering the Module IP

The ShineWifi-X or ShineLink obtains a DHCP address on your network. Find it in your router's DHCP client table. Assign a static IP or DHCP reservation.

### ShineWifi-X Local API

The ShineWifi-X module (firmware 3.x+) exposes a local API accessible via HTTP:

**Base URL:**

``` text
http://<shinewifi-ip>/
```

**Real-time data endpoint:**

``` text
GET http://<shinewifi-ip>/status.html
```

On newer firmware, a JSON endpoint is available:

``` text
GET http://<shinewifi-ip>/inverter/status
```

**Authentication:** Some firmware versions require HTTP Basic authentication using the ShineWifi admin credentials (default: `admin` / `123456`).

**Example response:**

``` json
{
  "InverterStatus": "Normal",
  "Ppv": 3420,
  "Pac": 3350,
  "Soc": 85,
  "BatteryPower": -500,
  "Vac": 241.3,
  "Fac": 60.01,
  "TodayEnergy": 8.76,
  "TotalEnergy": 1234.56
}
```

### Growatt Cloud API (ShineServer)

Growatt provides a cloud API via their ShineServer platform for registered accounts:

**Base URL:**

``` text
https://openapi.growatt.com/
```

**Authentication:**

``` http
POST https://openapi.growatt.com/newToceanApi/login
Content-Type: application/x-www-form-urlencoded

account=YOUR_EMAIL&password=MD5_HASH_OF_PASSWORD
```

**Key Cloud Endpoints:**

| Endpoint | Description |
| --- | --- |
| `POST /newToceanApi/login` | Authenticate and obtain session token |
| `GET /newToceanApi/device/getDeviceInfo` | Device list and status |
| `POST /newToceanApi/storage/storageData` | Real-time inverter and battery data |
| `POST /newToceanApi/storage/storageDataByDay` | Historical daily energy data |

> **MPG module note:** A `growatt_shine_local_v1` protocol module polling the ShineWifi-X local HTTP JSON endpoint would provide real-time inverter and battery data for all Growatt SPF models with a ShineWifi-X module installed — without any USB or RS485 cabling requirement. The unauthenticated local endpoint on current firmware makes this a straightforward implementation.

---

## 7. MPG Configuration

Follow the MultiProtocolGateway Modbus RTU to MQTT configuration guide:
<https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki/Configuration-Examples#modbus-rtu-to-mqtt>

### All SPF Models (USB or RS485)

``` ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 1
protocol_version = v0.14
```

> Default baud rate for Growatt SPF series via USB is 9600 bps. Verify via the Growatt app or inverter display settings if communication fails.

---

## 8. Home Assistant Cards

The following example cards can be added directly to your Home Assistant dashboard. Adjust entity names to match your configured MQTT topic structure.

### 8.1 PV1 & PV2 Card

![PV1 and PV2 Power Card](https://github.com/BuxtonCalvin/MultiProtocolGateway/assets/2180145/372980f9-f2d6-48e5-9acd-ee519badb61f)

<details>
<summary>Card YAML Code</summary>

``` yaml
type: horizontal-stack
cards:
  - type: gauge
    needle: false
    name: PV1 Voltage
    entity: sensor.growatt_inverter_pv1_voltage
    severity:
      green: 150
      yellow: 50
      red: 0
  - type: gauge
    entity: sensor.growatt_inverter_pv2_voltage
    name: PV2 Voltage
    severity:
      green: 125
      yellow: 50
      red: 0
  - type: gauge
    needle: false
    entity: sensor.growatt_inverter_pv1_watts
    name: PV1 Watts
    severity:
      green: 750
      yellow: 250
      red: 0
  - type: gauge
    entity: sensor.growatt_inverter_pv2_watts
    name: PV2 Watts
    severity:
      green: 750
      yellow: 250
      red: 0
```

</details>

---

### 8.2 Output Card

![Output Voltage and Current Card](https://github.com/BuxtonCalvin/MultiProtocolGateway/assets/2180145/9a129dad-73bc-4401-9746-d7a0dd22cf0a)

<details>
<summary>Card YAML Code</summary>

``` yaml
type: horizontal-stack
cards:
  - type: gauge
    needle: true
    entity: sensor.growatt_inverter_output_voltage
    name: Output Voltage
    max: 270
    min: 210
    segments:
      - from: 0
        color: '#db4437'
      - from: 220
        color: '#ffa600'
      - from: 235
        color: '#43a047'
      - from: 245
        color: '#ffa600'
      - from: 250
        color: '#db4437'
  - type: gauge
    entity: sensor.growatt_inverter_output_hz
    name: Output Hertz
    unit: hz
    needle: true
    max: 62
    min: 58
    segments:
      - from: 0
        color: '#db4437'
      - from: 59
        color: '#ffa600'
      - from: 59.5
        color: '#43a047'
      - from: 60.5
        color: '#ffa600'
      - from: 61
        color: '#db4437'
  - type: gauge
    needle: false
    entity: sensor.growatt_inverter_output_wattage
    name: Output Watts
    severity:
      green: 0
      yellow: 1200
      red: 8000
    max: 12000
  - type: gauge
    entity: sensor.growatt_inverter_output_current
    name: Output Current
    severity:
      green: 0
      yellow: 10
      red: 40
    max: 50
```

</details>

---

## 9. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| Device not detected (`/dev/ttyUSB*` missing) | Driver not loaded or wrong USB port | Run 'dmesg &#124; grep tty` after plugging in; try the secondary USB port if present' |
| Intermittent disconnections | Growatt USB power instability | Add ADUM3160 USB isolator between inverter and host (see Section 3.6) |
| No Modbus response | Baud rate mismatch | Default is 9600; verify via Growatt app settings |
| DB9 model not responding | PIN1/PIN2 not OFF | Flip DIP switches to OFF position per Section 3.2 |
| Data stuck or stale after long runtime | RS485 termination missing on long runs | Add 120Ω termination resistor at far end of RS485 cable runs |
| Multiple inverters — only one responds | Parallel setup requires individual addressing | Use a separate cable per inverter or a Waveshare multi-channel bridge |
