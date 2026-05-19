# Victron Energy — MQTT and Serial Data Integration Guide

> **Supported Products:** MultiPlus · MultiPlus-II · MultiPlus-II GX · Quattro · Phoenix Inverter ·
> SmartSolar MPPT · BlueSolar MPPT · Cerbo GX · Venus GX · Color Control GX (CCGX)
>
> **Protocols:** MQTT native (Venus OS) · Modbus TCP (via GX device) · VE.Bus serial (via MK3-USB) ·
> VE.Direct serial text · VRM REST API (cloud/local)
>
> **Interfaces:** Ethernet · Wi-Fi · VE.Bus · VE.Direct · VE.Can · MK3-USB (USB serial)
>
> **Status:** Officially supported — Venus OS publishes native MQTT independently of MPG;
> MK3-USB provides direct serial access to VE.Bus devices

---

## Table of Contents

1. [Overview & MPG Compatibility Note](#1-overview--mpg-compatibility-note)
2. [Architecture: How Victron Communicates](#2-architecture-how-victron-communicates)
3. [Supported Products & Connection Paths](#3-supported-products--connection-paths)
4. [Hardware Requirements](#4-hardware-requirements)
   - [4.1 Option A — Cerbo GX (Recommended Central Hub)](#41-option-a--cerbo-gx-recommended-central-hub)
   - [4.2 Option B — Raspberry Pi with Venus OS](#42-option-b--raspberry-pi-with-venus-os)
   - [4.3 Option C — MK3-USB Adapter (Required for VE.Bus)](#43-option-c--mk3-usb-adapter-required-for-vebus)
   - [4.4 Option D — VE.Direct to USB Cable](#44-option-d--vedirect-to-usb-cable)
   - [4.5 Option E — Waveshare RS485 Adapters (Third-Party Meters)](#45-option-e--waveshare-rs485-adapters-third-party-meters)
5. [Enabling MQTT on Venus OS](#5-enabling-mqtt-on-venus-os)
6. [Integration Path 1 — Shared MQTT Broker](#6-integration-path-1--shared-mqtt-broker)
7. [Integration Path 2 — Mosquitto Bridge](#7-integration-path-2--mosquitto-bridge)
8. [Integration Path 3 — Node-RED Translator](#8-integration-path-3--node-red-translator)
9. [Venus OS MQTT Topic Structure](#9-venus-os-mqtt-topic-structure)
10. [Modbus TCP from Venus OS GX Device](#10-modbus-tcp-from-venus-os-gx-device)
11. [VE.Bus Serial Access via MK3-USB](#11-vebus-serial-access-via-mk3-usb)
12. [VE.Direct Serial Text Protocol](#12-vedirect-serial-text-protocol)
13. [VRM REST API (Cloud & Local)](#13-vrm-rest-api-cloud--local)
14. [MPG Integration Path](#14-mpg-integration-path)
15. [Register Map CSV Files](#15-register-map-csv-files)
16. [Multi-Device Setups](#16-multi-device-setups)
17. [Troubleshooting](#17-troubleshooting)

---

## 1. Overview & MPG Compatibility Note

> ⚠️ **Important:** VE.Bus is a proprietary, timing-critical protocol. Connecting a generic RS485
> adapter directly to a MultiPlus or Quattro VE.Bus RJ45 port is explicitly unsupported by Victron
> and risks destabilizing multi-inverter synchronization. Always use the Victron MK3-USB adapter,
> which provides galvanic isolation and handles the VE.Bus timing layer.

**What Victron does natively:**

A Victron GX device (Cerbo GX, Venus GX, or Raspberry Pi running Venus OS) aggregates all connected
Victron hardware and **publishes data to its own built-in MQTT broker** on port 1883. This operates
entirely independently of MPG.

**Serial data out — two paths:**

Victron hardware also exposes real-time data directly over serial connections without requiring a GX
device:

- **MK3-USB (VE.Bus):** MultiPlus and Quattro inverter-chargers expose all operating data through
  the VE.Bus RJ45 port via the MK3-USB adapter. This makes live AC, DC, battery, and state data
  available directly on a USB serial port.

- **VE.Direct (MPPT / BMV / Phoenix):** Solar chargers, battery monitors, and Phoenix inverters
  broadcast a continuous ASCII text frame over their VE.Direct port at 19200 baud. A
  VE.Direct-to-USB cable makes this stream available on a USB serial port with no GX device required.

**How Victron data coexists with MPG output:**

If you have both Victron equipment and MPG-connected equipment (EG4, Growatt, SOK, etc.) in the
same installation, each publishes to MQTT independently. Three approaches are available to bring
both into the same broker:

| Integration Path | Summary | Best For |
| --- | --- | --- |
| **Shared broker** | Configure Venus OS to forward to the same external broker MPG uses | Cleanest architecture; single broker for all devices |
| **Mosquitto bridge** | Bridge Venus OS's internal broker to your external broker | When Venus OS broker address cannot be changed |
| **Node-RED translator** | Subscribe to Venus OS MQTT, normalize topics, republish | When topic normalization to match MPG format is needed |

> **MPG module note:** A `victron_vrm_local_v1` protocol module polling the Venus OS Modbus TCP
> interface (port 502, unit IDs per Section 10) provides a conventional polling-based path to bring
> Victron data into MPG alongside other devices, without requiring any MQTT bridging configuration.

---

## 2. Architecture: How Victron Communicates

```text
MultiPlus / Quattro  ─── VE.Bus ──────┬──── GX Device (Cerbo GX / Venus Pi)
                     ─── MK3-USB ─────┤         │
SmartSolar MPPT      ─── VE.Direct ───┤         │ Ethernet / Wi-Fi
Battery Monitor BMV  ─── VE.Direct ───┤         ▼
Lynx Smart BMS       ─── VE.Can  ─────┘  Venus OS MQTT Broker :1883
                                          Venus OS Modbus TCP  :502
                     ─── VE.Direct USB ──────────────────────────────► MPG / serial host
                     ─── MK3-USB ─────────────────────────────────────► MPG / serial host
                                                   │
                       ┌───────────────────────────┴──────────────────┐
                       ▼                                               ▼
              External MQTT Broker                        VRM Portal (cloud, optional)
           (shared with MPG output)
                       │
          ┌────────────┴────────────┐
          ▼                         ▼
    Home Assistant            Node-RED / Grafana
```

The GX device is the central aggregation point for all Victron hardware via Venus OS. Alternatively,
MPG or any serial host can poll devices directly via MK3-USB (VE.Bus) or VE.Direct-to-USB cables,
bypassing the GX device entirely for raw serial data collection.

---

## 3. Supported Products & Connection Paths

| Product | Connection to GX | Direct Serial Path | Protocol |
| --- | --- | --- | --- |
| MultiPlus, MultiPlus-II, Quattro | VE.Bus RJ45 → GX VE.Bus port | MK3-USB → USB serial host | VE.Bus (proprietary) |
| MultiPlus-II GX | Built-in GX — no external GX required | MK3-USB on internal bus | Internal |
| SmartSolar / BlueSolar MPPT | VE.Direct → GX VE.Direct port | VE.Direct-to-USB → USB serial host | VE.Direct text |
| BMV Battery Monitor | VE.Direct → GX VE.Direct port | VE.Direct-to-USB → USB serial host | VE.Direct text |
| Lynx Smart BMS | VE.Can → GX VE.Can port | Not directly supported | VE.Can |
| Phoenix Inverter (Smart) | VE.Direct or Bluetooth → GX | VE.Direct-to-USB → USB serial host | VE.Direct text |
| Third-party inverters (Fronius, SolarEdge) | Ethernet → GX (SunSpec Modbus TCP) | N/A | Modbus TCP |

> All VE.Bus connections require the MK3-USB adapter. Direct serial polling via VE.Direct or
> MK3-USB does not require a GX device.

---

## 4. Hardware Requirements

### 4.1 Option A — Cerbo GX (Recommended Central Hub)

Victron's current-generation GX device. Recommended for all new Victron installations.

**Built-in ports:**

- 2× VE.Bus (RJ45) — for MultiPlus / Quattro
- 3× VE.Direct — for MPPT chargers and BMV monitors
- 2× USB — for USB-VE.Direct adapters or MK3-USB
- 1× VE.Can — for Lynx BMS and compatible battery systems
- Ethernet + Wi-Fi — for LAN connectivity and MQTT publishing

**Connection:**

1. Connect MultiPlus VE.Bus RJ45 → Cerbo GX VE.Bus port using the supplied Victron RJ45 cable
2. Connect MPPT / BMV via VE.Direct cables to the three VE.Direct ports
3. Connect Cerbo GX Ethernet port to your LAN switch
4. Assign a static IP or DHCP reservation to the Cerbo GX

> A **GX Touch 50 or 70** display is optional but useful for local status without a phone or computer.

---

### 4.2 Option B — Raspberry Pi with Venus OS

A cost-effective alternative for DIY installations. Raspberry Pi 3B+, 4B, or Zero 2W running Venus
OS provides functionality equivalent to the Cerbo GX.

**Requirements:**

- Raspberry Pi (3B+ or 4B recommended for stability)
- MicroSD card (16 GB minimum)
- Venus OS image: <https://github.com/victronenergy/venus/wiki/raspberrypi-install>
- MK3-USB adapter (for MultiPlus/Quattro VE.Bus)
- VE.Direct to USB cable(s) (one per MPPT or BMV)
- Waveshare RS485 CAN HAT (optional — for VE.Can BMS connectivity)

**Advantages:**

- Lower hardware cost (~$40 Pi + adapters vs. ~$150 Cerbo GX)
- More USB ports for additional VE.Direct devices
- Can run Node-RED, InfluxDB, Mosquitto alongside Venus OS on the same Pi

---

### 4.3 Option C — MK3-USB Adapter (Required for VE.Bus)

The **Victron Interface MK3-USB (VE.Bus to USB)** is the only supported hardware bridge between a
MultiPlus or Quattro's VE.Bus port and a USB host. It is required in two scenarios:

1. As part of a Venus OS GX setup (Cerbo GX internal connector or Raspberry Pi USB port)
2. As a **direct serial data interface** for MPG or any other serial polling host, bypassing Venus OS entirely

> ⚠️ **Never use a generic USB-to-RS485 adapter on the VE.Bus RJ45 port.** The MK3-USB provides
> galvanic isolation and handles the proprietary VE.Bus timing protocol. Bypassing it risks
> destabilizing synchronization in parallel or three-phase setups and is explicitly unsupported
> by Victron.

**Product:** Victron MK3-USB (VE.Bus to USB Interface)

**Connection:**

1. Connect MultiPlus VE.Bus RJ45 → MK3-USB RJ45 socket
2. Connect MK3-USB USB plug → USB port on Cerbo GX, Raspberry Pi, or MPG host
3. Device appears as `/dev/ttyUSBx` (Linux) at 2400 baud

**Serial data available via MK3-USB** (see Section 11 and `victron_mk3usb_vebus_serial_fields.csv`):

- AC input voltage, current, power (all phases)
- AC output voltage, current, power (all phases)
- DC battery voltage and current
- VE.Bus state and error codes
- Switch mode (read/write)
- Charger state and alarm flags

---

### 4.4 Option D — VE.Direct to USB Cable

Connects SmartSolar MPPTs, BMV battery monitors, and Phoenix Inverters directly to a USB host for
serial data polling — either via a Venus OS device or directly to MPG.

**Product:** Victron VE.Direct to USB Interface

**Connection:**

1. Connect VE.Direct port on MPPT or BMV → VE.Direct-to-USB cable
2. Connect USB plug → USB port on Cerbo GX, Raspberry Pi, or MPG host
3. Device appears as `/dev/ttyUSBx` (Linux) at 19200 baud, 8N1

Each device requires its own dedicated cable.

**Serial data available via VE.Direct** (see Section 12 and `victron_vedirect_serial_text_protocol_fields.csv`):

- Panel voltage and power (MPPT)
- Battery voltage, current, power
- State of charge, time-to-go (BMV)
- Charge state and error codes
- Daily and lifetime yield history (MPPT)

---

### 4.5 Option E — Waveshare RS485 Adapters (Third-Party Meters)

Venus OS includes built-in drivers for third-party energy meters (Carlo Gavazzi EM24, Eastron
SDM630) connected via RS485. These meters feed grid import/export data into the Venus OS system
overview.

| Product | Description | Use |
| --- | --- | --- |
| **USB TO RS485 (B)** | FT232RNL, isolated, industrial case | Meter physically close to GX device or Pi |
| **RS485 TO ETH (B)** | RS485 ↔ Ethernet; Modbus gateway | Meter located away from GX device |
| **RS485 TO POE ETH (B)** | Same + PoE power; isolated | Clean single-cable meter deployment |

**Connection:**

1. Wire meter RS485 terminals to Waveshare USB adapter A+ and B−
2. Plug USB adapter into GX device or Pi USB port
3. Venus OS auto-detects supported meter models (Carlo Gavazzi EM24, Eastron SDM630)

---

## 5. Enabling MQTT on Venus OS

MQTT is disabled by default. Enable from the GX device web UI or touchscreen.

**Via web UI:**

1. Open a browser and navigate to `http://<gx-device-ip>`
2. Navigate to: **Settings → Services**
3. Enable **MQTT on LAN (plaintext)** — port 1883
4. Optionally enable **MQTT on LAN (SSL)** — port 8883

**Via GX Touch touchscreen:**

Navigate to: Menu → Settings → Services → MQTT on LAN → Enabled

> **Security note:** The built-in MQTT broker has no authentication by default on port 1883. For
> untrusted networks, use SSL on port 8883 with credentials. For a standard home LAN, plaintext
> 1883 is generally acceptable.

---

## 6. Integration Path 1 — Shared MQTT Broker

**The cleanest architecture.** Run an external Mosquitto broker. Configure both Venus OS (via a
bridge, see Path 2) and MPG to publish to the same broker. All device data lands in one place under
consistent topic structures.

**External broker setup:**

```bash
sudo apt install mosquitto mosquitto-clients
sudo systemctl enable mosquitto
```

**MPG configuration (existing MPG-connected devices):**

```ini
[mqtt]
host = 192.168.1.xxx
port = 1883
```

**Result:** MPG topics (e.g., `eg4/battery/soc`) and Victron topics
(e.g., `N/<portal-id>/system/0/Dc/Battery/Soc`) coexist in the same broker and are available to
Home Assistant, Node-RED, and Grafana simultaneously.

---

## 7. Integration Path 2 — Mosquitto Bridge

When Venus OS cannot be configured to point at an external broker directly, add a **bridge
configuration to your external Mosquitto broker** to pull topics from the GX device's internal broker.

**Add to your external broker's config:**

```ini
# /etc/mosquitto/conf.d/victron-bridge.conf

connection victron-gx
address 192.168.1.xxx:1883
cleansession false
clientid victron-bridge-01

# Pull all Victron data topics from GX into local broker
topic N/# in 0 "" ""

# Forward write commands from local broker to GX
topic W/# out 0 "" ""

# Forward keepalive requests from local broker to GX
topic R/# out 0 "" ""
```

Restart Mosquitto after adding this config:

```bash
sudo systemctl restart mosquitto
```

All `N/<portal-id>/...` topics from the GX device are now mirrored into your external broker
alongside MPG topics.

**Keepalive:** Venus OS stops publishing after ~60 seconds without a keepalive. Send a periodic
`{}` message to `R/<portal-id>/system/0/Serial` to maintain the stream.

**Keepalive via cron (every 50 seconds):**

```bash
# /etc/cron.d/victron-keepalive
* * * * * root for i in 0 10 20 30 40 50; do sleep $i && \
  mosquitto_pub -h localhost -t "R/PORTAL_ID/system/0/Serial" -m "{}"; done
```

---

## 8. Integration Path 3 — Node-RED Translator

Node-RED can subscribe to the Venus OS MQTT broker, normalize or rename topics, and republish to
your main broker. Useful when you want Victron topics to follow the same naming convention as MPG
output.

**Example flow:**

```text
[MQTT In] broker: <gx-ip>:1883, topic: N/<portal-id>/system/0/Dc/Battery/Soc
    → [Function] parse value from {"value": 85.2}, set topic to "victron/battery/soc"
    → [MQTT Out] broker: <external-broker>:1883, topic: victron/battery/soc
```

**Example Function node (JavaScript):**

```javascript
const parsed = JSON.parse(msg.payload);
msg.payload = parsed.value;
msg.topic   = "victron/battery/soc";
return msg;
```

This publishes clean numeric values to normalized topics alongside MPG data.

---

## 9. Venus OS MQTT Topic Structure

Venus OS publishes data under a hierarchical topic tree keyed by the GX device's **VRM Portal ID**.

**Example topics:**

```text
N/<portal-id>/system/0/Ac/Grid/L1/Power          # Grid power phase 1 (W)
N/<portal-id>/system/0/Ac/Consumption/L1/Power   # Load consumption (W)
N/<portal-id>/system/0/Dc/Battery/Soc            # Battery SOC (%)
N/<portal-id>/system/0/Dc/Battery/Voltage        # Battery voltage (V)
N/<portal-id>/vebus/276/Ac/Out/L1/V              # MultiPlus output voltage L1 (V)
N/<portal-id>/solarcharger/258/Yield/Power        # MPPT solar power (W)
N/<portal-id>/battery/512/Dc/0/Soc               # Battery monitor SOC (%)
```

**Topic prefix meanings:**

| Prefix | Direction | Purpose |
| --- | --- | --- |
| `N/` | Read (subscribe) | GX device publishes sensor data |
| `W/` | Write (publish to GX) | Send commands to the GX device |
| `R/` | Request (publish `{}`) | Ask GX to re-publish all current values |

**Payload format:** All values are published as JSON objects:

```json
{"value": 85.2}
```

Extract `.value` when processing in Node-RED, Home Assistant, or a custom script.

**Finding your Portal ID:**

- GX web UI: Settings → VRM Online Portal → VRM Portal ID
- Printed on the label on the GX device body

**Full topic map and D-Bus path reference:**
<https://github.com/victronenergy/venus/wiki/dbus>

---

## 10. Modbus TCP from Venus OS GX Device

The GX device exposes all connected Victron hardware via **Modbus TCP on port 502**. This provides
a conventional polling-based access path that an MPG module can use directly.

| Device | Unit ID |
| --- | --- |
| System overview (aggregated) | 100 |
| MultiPlus / Quattro | 246 |
| BMV Battery Monitor | 245 |
| SmartSolar MPPT (first VE.Direct slot) | 229 |
| SmartSolar MPPT (second VE.Direct slot) | 230 |
| SmartSolar MPPT (third VE.Direct slot) | 231 |
| Grid meter | 210 |

**Key registers (unit ID 246 — MultiPlus):**

| Register | Description | Scale |
| --- | --- | --- |
| 3 | AC input voltage L1 (V) | ÷10 |
| 6 | AC output voltage L1 (V) | ÷10 |
| 9 | AC output current L1 (A) | ÷10 |
| 12 | AC output power L1 (W) | ÷1 |
| 843 | Battery voltage (V) | ÷100 |
| 844 | Battery current (A) | ÷10 |
| 845 | Battery SOC (%) | ÷10 |

Full register maps are provided in the CSV files described in Section 15.

Full Modbus TCP register map (official Victron documentation):
<https://www.victronenergy.com/live/ccgx:modbustcp_faq>

---

## 11. VE.Bus Serial Access via MK3-USB

The **Victron Interface MK3-USB (VE.Bus to USB)** exposes MultiPlus and Quattro operating data
directly over USB serial — no GX device or Venus OS required. This is the correct method for
direct serial polling of VE.Bus inverter-chargers by MPG or any other host.

### Serial Port Parameters

| Parameter | Value |
| --- | --- |
| Baud rate | 2400 |
| Data bits | 8 |
| Parity | None |
| Stop bits | 1 |
| Flow control | None |
| OS device path | `/dev/ttyUSBx` (Linux) |

### Protocol Notes

The MK3-USB speaks a **proprietary Victron VE.Bus binary protocol** (also referred to as the MK2
protocol internally). It is not standard Modbus RTU. Options for accessing data:

- **Venus OS / dbus (recommended for GX setups):** When the MK3-USB is connected to a Cerbo GX or
  Venus Pi, Venus OS handles the protocol layer and exposes data via Modbus TCP (Section 10) and
  MQTT (Section 9).
- **Direct serial polling (MPG / standalone):** Use the open-source `velib` Python library or the
  `mk2pkt` packet library to speak the binary VE.Bus protocol directly.
- **VE Configure / VictronConnect:** Victron's official configuration tools communicate over MK3-USB
  using the same binary protocol.

### Data Available via MK3-USB

All fields are documented in `victron_mk3usb_vebus_serial_fields.csv`. Key data groups:

- AC input: voltage, current, power (L1/L2/L3 for three-phase)
- AC output: voltage, current, power (L1/L2/L3)
- AC input and output frequency
- DC battery: voltage and current
- Inverter temperature
- VE.Bus operating state and error code
- Charge state (bulk / absorption / float / storage / equalize)
- Switch mode (read and write)
- Alarm flags: low battery, overload, high temperature
- Active AC input source (Quattro: AC1 or AC2)
- Relay states

### MPG Serial Configuration (MK3-USB Direct)

```ini
transport = serial
port = /dev/ttyUSB0
baud = 2400
protocol_version = victron_mk3_vebus_v1
```

> ⚠️ In multi-inverter (parallel or three-phase) installations, the MK3-USB connects to the **last
> unit** in the VE.Bus daisy chain. All phase data from the parallel system is aggregated and
> accessible through that single connection.

---

## 12. VE.Direct Serial Text Protocol

SmartSolar MPPT chargers, BlueSolar MPPT chargers, BMV battery monitors, and Phoenix Smart Inverters
all broadcast a continuous ASCII text data frame from their VE.Direct port. No polling is required —
the device transmits a complete set of fields approximately once per second.

### Serial Port Parameters

| Parameter | Value |
| --- | --- |
| Baud rate | 19200 |
| Data bits | 8 |
| Parity | None |
| Stop bits | 1 |
| Flow control | None |
| OS device path | `/dev/ttyUSBx` (Linux) via VE.Direct-to-USB cable |

### Frame Format

Each frame is a sequence of `label\tvalue\r\n` pairs followed by a checksum line:

```text
V\t12650\r\n
PPV\t143\r\n
CS\t3\r\n
ERR\t0\r\n
...
Checksum\t<byte>\r\n
```

The checksum byte is chosen such that the sum of all bytes in the frame (including the checksum
byte itself) equals zero modulo 256. Frames are self-delimiting; partial frames at startup can
be discarded until a valid checksum is received.

### MPG Serial Configuration (VE.Direct Direct)

```ini
transport = serial
port = /dev/ttyUSB1
baud = 19200
protocol_version = victron_vedirect_text_v1
```

### Key VE.Direct Fields by Device Type

| Field | MPPT | BMV | Phoenix | Description |
| --- | --- | --- | --- | --- |
| `V` | ✓ | ✓ | ✓ | Battery voltage (mV) |
| `I` | ✓ | ✓ | ✓ | Battery current (mA) |
| `VPV` | ✓ | — | — | PV panel voltage (mV) |
| `PPV` | ✓ | — | — | PV panel power (W) |
| `CS` | ✓ | — | ✓ | Charge / device state |
| `ERR` | ✓ | ✓ | ✓ | Error code |
| `MPPT` | ✓ | — | — | MPPT tracker mode |
| `H20` | ✓ | — | — | Yield today (0.01 kWh) |
| `H19` | ✓ | — | — | Yield yesterday (0.01 kWh) |
| `SOC` | — | ✓ | — | State of charge (0.1%) |
| `TTG` | — | ✓ | — | Time to go (minutes) |
| `CE` | — | ✓ | — | Consumed energy (mAh) |
| `Alarm` | ✓ | ✓ | ✓ | Alarm active (ON/OFF) |
| `Relay` | ✓ | ✓ | — | Relay state (ON/OFF) |

All fields are documented in `victron_vedirect_serial_text_protocol_fields.csv`.

---

## 13. VRM REST API (Cloud & Local)

The Victron VRM (Victron Remote Management) portal provides a REST API for both cloud and local
access. This is the basis for a dedicated MPG module.

### Local API (Venus OS — No Internet Required)

Venus OS exposes a local REST API directly on the GX device's LAN IP. No Victron account or
internet connection is required.

**Base URL:**

```text
http://<gx-device-ip>/
```

**Authentication:**

```bash
curl -s \
  -c /tmp/victron-cookies.txt \
  -d "username=admin&password=YOUR_PASSWORD" \
  "http://192.168.1.xxx/login"
```

Pass the session cookie on subsequent requests:

```bash
curl -b /tmp/victron-cookies.txt \
  "http://192.168.1.xxx/api/v1/vebus/276/AC/Out"
```

> Default credentials: username `admin`, password set during commissioning (or the VRM portal
> password if VRM is configured).

**Key Local Endpoints:**

| Endpoint | Description |
| --- | --- |
| `/api/v1/system/status` | System summary (SOC, PV power, grid power, battery voltage) |
| `/api/v1/vebus/{instance}/AC/Out` | MultiPlus AC output (voltage, current, power) |
| `/api/v1/solarcharger/{instance}/Yield/Power` | MPPT current power (W) |
| `/api/v1/battery/{instance}/Dc/0/Soc` | Battery monitor SOC (%) |
| `/api/v1/grid/{instance}/Ac/L1/Power` | Grid meter power (W) |

### VRM Cloud API

For remote access via the Victron VRM portal:

**Base URL:**

```text
https://vrmapi.victronenergy.com/v2/
```

**Authentication:**

```http
POST https://vrmapi.victronenergy.com/v2/auth/login
Content-Type: application/json

{"username": "YOUR_EMAIL", "password": "YOUR_PASSWORD"}
```

Response contains a Bearer token for subsequent requests.

**Key Cloud Endpoints:**

| Endpoint | Description |
| --- | --- |
| `GET /users/{userId}/installations` | List all GX installations under the account |
| `GET /installations/{siteId}/stats` | Historical production and consumption data |
| `GET /installations/{siteId}/diagnostics` | Real-time diagnostic data for all connected devices |
| `GET /installations/{siteId}/gps` | Location data (if GPS dongle connected) |

**Example — Get real-time diagnostics:**

```bash
curl -H "X-Authorization: Bearer YOUR_TOKEN" \
  "https://vrmapi.victronenergy.com/v2/installations/YOUR_SITE_ID/diagnostics"
```

> **MPG module note:** A `victron_venus_local_v1` protocol module could poll the Venus OS local
> REST API (`/api/v1/system/status` and related endpoints) for real-time data, or use the Modbus
> TCP interface (Section 10). The local REST API requires no internet connection and provides all
> key system metrics in a single request, making it the most efficient MPG integration path for
> Victron systems where a GX device is present.

---

## 14. MPG Integration Path

MPG can integrate with Victron hardware through several paths depending on which hardware is
present in the installation.

**Option A — Modbus TCP (polling the GX device, recommended when GX device is present):**

```ini
transport = modbus_tcp
host = 192.168.1.xxx
port = 502
unit_id = 246
protocol_version = victron_venus_modbus_v1
```

**Option B — Local REST API (HTTP polling, requires custom module):**

```ini
transport = http
protocol_version = victron_venus_local_v1
host = 192.168.1.xxx
port = 80
auth_type = session
username = admin
password = YOUR_PASSWORD
poll_endpoint = /api/v1/system/status
poll_interval = 10
```

**Option C — MK3-USB direct serial (MultiPlus / Quattro, no GX device required):**

```ini
transport = serial
port = /dev/ttyUSB0
baud = 2400
protocol_version = victron_mk3_vebus_v1
```

**Option D — VE.Direct direct serial (MPPT / BMV / Phoenix, no GX device required):**

```ini
transport = serial
port = /dev/ttyUSB1
baud = 19200
protocol_version = victron_vedirect_text_v1
```

Options C and D allow MPG to collect Victron device data using only USB adapters, with no Cerbo GX,
no Venus OS, and no network configuration required.

---

## 15. Register Map CSV Files

The following CSV files provide complete holding register and serial field maps for each Victron
product in the MPG column format
(`register, variable_name, documented_name, unit, data_type, values, read_interval, writable, note`).

| File | Device | Interface | Protocol |
| --- | --- | --- | --- |
| `victron_multiplus_quattro_holding_registers.csv` | MultiPlus / MultiPlus-II / Quattro | Modbus TCP (unit ID 246) | Modbus TCP via GX |
| `victron_smartsolar_mppt_holding_registers.csv` | SmartSolar / BlueSolar MPPT | Modbus TCP (unit IDs 229–231) | Modbus TCP via GX |
| `victron_bmv_battery_monitor_holding_registers.csv` | BMV-700 / 702 / 712 | Modbus TCP (unit ID 245) | Modbus TCP via GX |
| `victron_venus_gx_system_overview_registers.csv` | Venus OS system overview | Modbus TCP (unit ID 100) | Modbus TCP via GX |
| `victron_phoenix_inverter_holding_registers.csv` | Phoenix Inverter (Smart) | Modbus TCP (unit ID varies) | Modbus TCP via GX |
| `victron_mk3usb_vebus_serial_fields.csv` | MultiPlus / Quattro | MK3-USB → USB serial | VE.Bus binary (MK3) |
| `victron_vedirect_serial_text_protocol_fields.csv` | MPPT / BMV / Phoenix | VE.Direct → USB serial | VE.Direct ASCII text |

### Notes on CSV Usage

- Modbus TCP register numbers match the official Victron Modbus TCP register map. Verify against
  <https://www.victronenergy.com/live/ccgx:modbustcp_faq> for firmware-specific additions.
- VE.Direct field labels (`V`, `PPV`, `CS`, etc.) are ASCII text labels, not numeric register
  addresses. The `register` column in those CSVs carries the label string.
- MK3-USB field identifiers (`F_AC_IN_V_L1`, etc.) are logical names assigned for MPG mapping;
  the actual binary protocol uses frame IDs defined in the Victron MK2 protocol specification.
- All scaling factors (divide-by values) are documented in the `note` column of each CSV.
- Writable registers are marked `R/W` in the `writable` column; read-only registers are marked `R`.

---

## 16. Multi-Device Setups

**Parallel MultiPlus (2× or 3×):**

- All units connect via VE.Bus daisy chain
- A single VE.Bus cable from the last unit goes to the GX device or MK3-USB adapter
- The GX presents the parallel system as one logical inverter in MQTT topics and Modbus
- Direct MK3-USB polling also aggregates parallel phases into a single data stream

**Three-phase (3× MultiPlus):**

- Same VE.Bus daisy chain; GX publishes L1, L2, and L3 phase data under separate MQTT topic paths
  and Modbus registers
- MK3-USB direct serial polling also exposes all three phases

**Multiple MPPT chargers:**

- Each charger connects via its own VE.Direct port or USB-VE.Direct adapter
- Each appears as a separate device with a unique instance ID in the MQTT topic tree
- Modbus unit IDs increment per VE.Direct device slot (229, 230, 231, etc.)
- For direct serial polling, each MPPT requires its own VE.Direct-to-USB cable and serial port

**Mixed Victron + MPG-connected devices:**

- Victron data → Venus OS MQTT → bridged to external broker (Section 7)
- MPG devices → MPG → same external broker
- Both data sets available under one broker for unified dashboards in Home Assistant or Grafana
- Alternatively, MPG polls Victron hardware directly via MK3-USB and VE.Direct, sending all data to
  MQTT, InfluxDB, and TimescaleDB alongside other MPG-connected devices with no GX device needed

---

## 17. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| No MQTT topics publishing | MQTT not enabled on Venus OS | Enable under Settings → Services → MQTT on LAN |
| Topics appear then stop after ~60 s | Keepalive not being sent | Publish `{}` to `R/<portal-id>/system/0/Serial` every 50 seconds |
| Bridge not forwarding topics | Bridge config error or GX IP changed | Verify GX static IP; check `mosquitto -v` logs on broker host |
| MultiPlus data missing from GX | MK3-USB not in use | MK3-USB is required; never connect generic RS485 to VE.Bus |
| Modbus TCP returns zeros | Wrong unit ID | Verify unit IDs per Section 10; use `dbus-spy` on GX for live D-Bus inspection |
| VRM Portal offline | Internet outage or Victron VRM maintenance | Local MQTT and Modbus TCP continue operating; VRM outage does not affect local integrations |
| Venus OS and MPG topics in different formats | Expected — different systems use different conventions | Use Node-RED (Section 8) to normalize topic format if needed |
| GX IP changed; bridge broke | DHCP reassignment | Set a static IP or DHCP reservation for the GX device on your router |
| Local REST API returns 401 | Session expired or wrong password | Re-authenticate via `/login`; verify password set during commissioning |
| MK3-USB serial port not appearing | USB driver not loaded or adapter not recognized | Verify with `lsusb`; MK3-USB uses FTDI chipset (VID 0x0403); install `ftdi_sio` driver |
| VE.Direct no data / garbled output | Wrong baud rate or wrong cable | Confirm 19200 baud 8N1; use only genuine Victron VE.Direct-to-USB cable |
| VE.Direct checksum errors | Partial frame on startup | Discard frames until a frame with a valid checksum is received; normal on initial connect |
| MK3-USB data stale | Binary protocol requires active keepalive | Use Venus OS or `velib` to maintain the session; raw serial passthrough without protocol handling will not yield data |
| Wrong phase data in three-phase setup | MK3-USB connected to wrong unit | Connect MK3-USB to the last unit in the VE.Bus daisy chain |
