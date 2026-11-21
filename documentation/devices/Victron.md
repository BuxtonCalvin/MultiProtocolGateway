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

**Serial data available via MK3-USB** (see Section 11 and `victron_mk3usb_vebus.holding_registry_map.csv`):

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

Unit IDs are assigned by **device instance**, so they differ between installations. The system overview
is always unit ID 100; every other device (MultiPlus, battery monitor, solar charger, meter) has the
unit ID shown in the GX device under **Settings > Services > Modbus-TCP > Available services**. Use that
value for `address` in the MPG configuration.

**Key registers** (Victron CCGX Modbus-TCP register list 3.30; the register is divided by the scale factor):

| Service | Register | Description | Scale |
| --- | --- | --- | --- |
| VE.Bus (MultiPlus / Quattro) | 3 | AC input voltage L1 (V) | divide by 10 |
| VE.Bus | 6 | AC input current L1 (A) | divide by 10 |
| VE.Bus | 9 | AC input frequency L1 (Hz) | divide by 100 |
| VE.Bus | 15 | AC output voltage L1 (V) | divide by 10 |
| VE.Bus | 18 | AC output current L1 (A) | divide by 10 |
| VE.Bus | 23 | AC output power L1 (VA or W) | multiply by 10 |
| System (unit ID 100) | 840 | Battery voltage (V) | divide by 10 |
| System | 841 | Battery current (A) | divide by 10 |
| System | 842 | Battery power (W) | 1 |
| System | 843 | Battery state of charge (%) | 1 |

Full register maps are provided in the CSV files described in Section 15.

Full Modbus TCP register map (official Victron documentation):
<https://www.victronenergy.com/live/ccgx:modbustcp_faq>

---

### CAN-bus BMS batteries (`victron_gx_generic_canbus`)

Batteries that talk to a GX device over its **BMS-Can** port send a fixed set of CAN frames at **500 kbit/s**, little endian: `0x351` charge and discharge limits, `0x355` SOC and SOH, `0x356` voltage, current and temperature, `0x35A` alarms and warnings, `0x35E` manufacturer, `0x35F` battery information, and `0x370` to `0x381` names, module status, cell extremes, energy and capacity. The map is for the **battery's output**, which is the same set Pylontech V1.3 and SMA's Sunny Island use (see [Pylon.md](Pylon.md) and [SMA.md](SMA.md)).

Victron does not publish this protocol (its staff cite an NDA). The frame layouts come from SMA's *Connecting batteries with external BMS to Sunny Island* document, the Pylontech documents and open implementations such as SimpBMS, which agree. Earlier versions of the map read 16-bit values big endian (a SOC of 87 came out as 22272), declared 250 kbit/s, and included the Sunny Island's own frames (`0x305` to `0x307`) that a battery never sends; those are in `sma_sunny_island_canbus`.

`0x35A` carries each alarm or warning as a pair of bits, "arrive" (even bit) and "leave" (odd bit), and the variables list only the conditions that have arrived. `0x373` temperatures are in kelvin and `0x378` energies in units of 100 Wh; both are converted.

---

## 11. VE.Bus Serial Access via MK3-USB

The **Victron Interface MK3-USB (VE.Bus to USB)** gives MPG direct access to MultiPlus, Multi, Quattro and Phoenix
inverter operating data over USB serial, with no GX device or Venus OS. MPG speaks Victron's **MK2 serial protocol**
through the `serial_vebus` transport (`victron_mk3usb_vebus` protocol). It is **read-only by default**. Writes (the
switch position, the input current limit and five settings) exist but are off until you opt in, twice; see
[VE.Bus writes](#vebus-writes).

> **Source and status.** Built from Victron's *Interfacing with VE.Bus products - MK2 Protocol*, version 3.14
> (section numbers below refer to it). The request frames match the document's Appendix 1 examples byte for byte, and
> the DC, AC and MasterMultiLED frames of its Appendix 2 trace decode to the values the document prints. **Nothing has
> been run on real hardware.** "What is and is not verified" below lists what rests on the document and what is inferred.

### Wiring and serial parameters

1. MultiPlus / Quattro VE.Bus RJ45 socket to the MK3-USB RJ45 socket (a normal UTP cable).
2. MK3-USB USB plug to the MPG host. The device appears as `/dev/ttyUSBx` (use a hardware-ID `port` if you have several adapters).
3. Do not connect a generic RS485 adapter to VE.Bus.

| Parameter | Value |
| --- | --- |
| Baud rate | 2400 (section 3.1, which lists the MK2.2, MK2 USB and MK3 USB) |
| Data bits / parity / stop bits | 8 / none / 1 |
| Flow control | None. A serial MK2.2 needs DTR driven high (3.1); the USB interfaces do not. |
| MK2 firmware | The document applies to MK2 version 1130125 or later. Keep the MK2/MK3 firmware current (VE Configure updates it). |

If the MK2 is powered before the VE.Bus device, power-cycle the MK2 (3.6). MPG sends no reset.

### Configuration

```ini
[transport.vebus]
transport = serial_vebus
protocol_version = victron_mk3usb_vebus
port = /dev/ttyUSB0
baudrate = 2400
address = 0
read_interval = 15
bridge = transport.mqtt
```

`address` is the VE.Bus device address, 0 to 31. In a properly configured system the first device is address 0 (Appendix 2).
A parallel or three-phase installation has one VE.Bus device per unit, so add **one section per device** with the same
`port` and different `address` values; the transport selects the address (`A` command) only when it changes and keeps the
select-and-read sequence atomic, so devices on one port can be read in any scheduling mode. Settings are in
[Serial VE.Bus](../usage/transports.md#serial-vebus).

If you do not know the addresses, the document's discovery method (7.2) is: set an address, send any `W` command, and a
reply means a device is there; stop after three silent addresses. MPG does not run discovery itself: a section whose
address has no device logs "No VE.Bus device answered" once per cycle and publishes nothing.

### What is published

Per section (protocol `victron_mk3usb_vebus`; read-only except the rows marked `W`, see [VE.Bus writes](#vebus-writes)):

| Group | Source | Variables |
| --- | --- | --- |
| System, from the **info frames** (6.1, 6.2) | `F` 0, 1-3, 5 | `dc_voltage`, `dc_current_inverting`, `dc_current_charging`, `dc_current` (charging minus inverting, as in the vendor example), `ac_out_frequency`; per phase `ac_in_voltage_lN`, `ac_in_current_lN`, `ac_out_voltage_lN`, `ac_out_current_lN`, `ac_in_frequency_lN` (L1 to L3), BF/inverter factors, phase info and state; input current limits and AC-input configuration, switch register |
| Per device, **RAM variables** (7.3.3, 7.3.13.1) | `W` 0x30, IDs 0-19 | `device_*`: mains, inverter and battery voltage and current, ripple, mains and inverter frequency, AC load current, virtual switch, ignore-AC-input and multi-functional relay bits, state of charge (battery monitor), power values |
| Per device, **state and identity** | `W` 0x0E, 0x05/0x06; `V`, `L` | `device_state` (+ `_desc`), `charge_sub_state`, `software_version`, `mk2_interface_version`, LED on/blink flags |
| Per device, **settings** (7.3.4, 7.3.13.2) | `W` 0x31 | absorption/float voltage, bulk current, input current limits, absorption timings, low-battery limits, mains acceptance limits, battery monitor settings, three-phase mode, `Flags0/1/2` as named flags. Virtual-switch settings (IDs 15-43, 52-62) and ID 63 are not mapped. |

The info frames describe the **whole system**; the RAM variables and settings belong to **one device**. In a multi-device
setup every section publishes the system rows, so mask them (`variable_mask`) on all but one section.
L2 and L3 are only requested when the L1 frame says the system has that many phases.

Values the device does not support (reported as such by the device, 0x90/0x91, or a scale of 0) are left out, and do not make the cycle incomplete.

The scale and offset of each value come from the device (7.3.8, 7.3.9) and are fetched once per connection; the map
receives them already applied in fixed units. `startup` rows (identity, settings) are read once per connection.
A full cycle is about twenty exchanges at 2400 baud: use `read_interval` of 10 s or more.

### What is and is not verified

Verified against the vendor document (byte for byte or value for value, in `pytests/test_serial_vebus*.py`):

- Frame layout, checksum rule, and the request frames of Appendix 1 (`A`, `W` 0x05/0x06/0x30/0x36, `F` 0, `L`).
- The scale and offset interpretation (7.3.9, 7.3.8) against every scale answer in the Appendix 2 trace, including the
  signed current, the 0.1 s period time with offset 256, and the three bit variables.
- The DC, AC L1 and MasterMultiLED frames of Appendix 2: 26.4 V, 0.0 A, 50.0 Hz; 213.0 V, 0.7 A, 213.0 V, 0.4 A, 50.1 Hz; limit 50.0 A in 6.2 A to 100.0 A. (The trace is printed without checksums; the test frames carry computed ones.)

Built from the stated layouts, with no vendor example to compare: device state and sub-state, RAM variable and setting
values, the software version assembly, LED flags, the switch register, and the three-phase frames.

**Inferred or a judgment call** (not stated by the document):

- The DC info currents are unsigned 24-bit values scaled with the IBat scale (the document says "same scales as the
  equivalent W values"; the trace's currents were zero).
- Unit of `state_of_charge` (RAM 13): percent, from its 0.005 scale and the document's own example title "State of Charge".
  Unit of the power variables 14-19: watt (the document only says "power"). The document lists IDs 14 and 15 with the same
  description, so which is which is not known; both are published under neutral names. The sign of IBat (RAM 5) is not stated.
- `Flags2` (setting 60) is read as Flags[32] to Flags[47] by extension of the document's numbering.
- An inverter factor of 0 (reported by some inverter firmware, footnote c) is counted as 1.
- Charge sub-state 255 ("Not charging") is MPG's own value for "state is not Charge"; the vendor protocol has no such value.
- Default timeout 1.0 s (the document advises 500 ms, more than 750 ms for `F` 5), and giving up after three silent commands.
- That requesting L2/L3 on a single-phase system gets no answer is not stated; MPG avoids the question by not asking.
- Differences between document versions: 3.12 showed the software version replies as 0x81/0x82 in its example; 3.14 and MPG use 0x82/0x83.

Not implemented: RAM variable writes (the document does not say which are writable), the flag settings, forced
charge states (`W` 0x0E with a state other than 0), `W` 0x37, `F` 8 reset, `F` 9 battery operational limits, snapshots (`F` 6, `W` 0x38; the document warns a CCGX may use them),
long Winmon frames and the `X/Y/Z` commands, reading several RAM IDs in one command, device discovery, and L4 rows.

### VE.Bus writes

Two kinds of change exist, each behind its own switch. Neither does anything unless the section also has
`write_enabled = true`.

| Kind | Opt-in | Command | Rows | Stored? |
| --- | --- | --- | --- | --- |
| Panel | `allow_panel_writes = true` | `S` (section 5), variant 2 | `switch_state` (1 charger only, 2 inverter only, 3 on, 4 off), `input_current_limit_control` (amps) | No |
| Settings | `allow_setting_writes = true` | `W` 0x33 then 0x34 (7.3.6, 7.3.7) | `setting_absorption_voltage`, `setting_float_voltage`, `setting_bulk_current`, `setting_input_current_limit_ac1`, `_ac2` | **Yes, EEPROM** |

Only rows marked `W` in the map can be written; change an `R` to `W` to allow another setting (the same checks apply).
RAM variables and the flag settings `Flags0` to `Flags3` can never be written: the document warns that changing unused,
unsupported or "not promoted" flags can damage the device and that `Flags[3]` and `Flags[7]` must stay consistent.

**Safety layers** (all must pass; each has a test showing it refusing and that nothing was sent):

1. `write_enabled`, and `write_type` for `relaxed` / `unsafe`, as for every transport.
2. The opt-in for that kind of change (honored in every write mode).
3. The row must be marked `W`.
4. **Validation at every connect.** At least 90 % of the probes must pass: the MasterMultiLED frame reports its limits and a switch register, and each writable setting is supported and currently holds a value inside the limits the device reports. `relaxed` waives the score; `unsafe` skips this and layers 5 to 9. A section that failed validation (device off at start-up, say) is validated again on the next connect.
5. **The device's own limits.** A setting must lie inside the minimum and maximum the Multi reports for it (7.3.8); an input current limit inside the Multi's minimum and maximum (6.2). Nothing is clamped silently: a value outside is refused. An input current limit of 0 is refused, because the document says it switches the Multi to invert or bypass. A range in the row's `values` column (applied to the published value) tightens the device's limits.
6. **Read before write.** The current value must be readable and inside the limits; writing what is already there is skipped (less EEPROM wear, and a retained MQTT message cannot rewrite on every reconnect). The `S` command carries both the switch and the limit, so a write of one re-sends the *current* value of the other (the limit actually in force, or the switch state derived from the switch register) instead of changing it.
7. `write_min_interval` (default 30 s) between writes of the same target.
8. **Read back.** A setting is read again and the log says whether the device holds it; a panel write is checked against the MasterMultiLED frame (three reads, 0.5 s apart). Neither can undo the write: they report. The cached start-up value is dropped so the new value is published.
9. Every write and every refusal is logged (`WRITE VE.Bus ...`, `WRITE_ERROR: ...`).

A refused or failed write never raises; the other values of the same batch are still tried.

Example (home-automation current limit through MQTT, nothing else allowed):

```ini
[transport.vebus]
transport = serial_vebus
protocol_version = victron_mk3usb_vebus
port = /dev/ttyUSB0
address = 0
write_enabled = true
allow_panel_writes = true
bridge = transport.mqtt
```

Publish `16` to the `input_current_limit_control` write topic to set 16 A, or `Off` / `On` / `Charger only` /
`Inverter only` to `switch_state`. **`Off` stops the inverter: loads on the AC output lose power.**

**Verified:** the `S` frame for "on, 31.5 A" is byte for byte the document's Appendix 1 example (`07 FF 'S' 03 3B 01 01 80 E7`);
the setting write follows the two-frame sequence and the 0x88 answer of 7.3.6 and 7.3.7; refusals, limits, rate limiting,
read-back and the locking against other devices on the same interface are tested against a simulated interface.

**Not verified, or inferred. Read this before enabling writes:**

- Nothing has been run against a real Multi. The simulation answers as the document says; where the document is silent (how long a Multi takes to act on `S`, whether it wants a pause between the 0x33 and 0x34 frames) the simulation is the assumption. MPG sends the two frames back to back.
- The document has no worked example for `W` 0x33/0x34 or for 0x35 answers beyond the field list; the sequence is built from the text.
- Mapping the switch register bits (6.2: bit 4 `SwitchCharge`, bit 5 `SwitchInvert`, "current state") onto the `S` numbering (charge only 1, inverter only 2, both 3, neither 4) is an inference.
- Whether a single `S` command persists in the Multi is not stated. The document calls its example a "single remote panel command" and describes a flag that makes the MK2 resend the panel state every half second; MPG never sets it. If a Multi expects a panel to keep talking, the setting may lapse.
- The document says that when `Input current overridden by panel` (6.2) is not set the internal limit is used even if a remote panel is connected, so MPG refuses limit writes in that case. A GX device, remote panel or another MK2 on the same VE.Bus can overrule or fight these commands; the read-back will then show "not confirmed".
- A Multi that does not report the switch register (older firmware) cannot have its state read or a write confirmed: standard mode refuses panel writes for it, `relaxed` allows a switch write without confirmation, and a limit-only write is always refused.
- Settings 50 and 51 take effect only after a reset of the Multi (footnote e); the read-back shows the stored value either way.
- EEPROM endurance is not given in the document; `write_min_interval` and the no-change skip are the protection.

> In installations with more than one unit, the MK3-USB plugs into any VE.Bus socket of the system; per-device data is
> selected by address (above), not aggregated.

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
| `H22` | ✓ | — | — | Yield yesterday (0.01 kWh) |
| `H19` | ✓ | — | — | Yield total, a user-resettable counter (0.01 kWh) |
| `H9` | — | ✓ | — | Seconds since the last full charge |
| `SOC` | — | ✓ | — | State of charge (0.1%) |
| `TTG` | — | ✓ | — | Time to go (minutes) |
| `CE` | — | ✓ | — | Consumed energy (mAh) |
| `Alarm` | ✓ | ✓ | ✓ | Alarm active (ON/OFF) |
| `Relay` | ✓ | ✓ | — | Relay state (ON/OFF) |

All fields are in `victron_vedirect_serial.holding_registry_map.csv`. In an earlier version `H9` was labelled "days since last full" and `H17` to `H23` were assigned to the wrong quantities; they now follow the VE.Direct protocol 3.34 table.

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
[transport.vebus]
transport = serial_vebus
port = /dev/ttyUSB0
baudrate = 2400
address = 0
protocol_version = victron_mk3usb_vebus
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

| File (in `protocols/victron/`) | Device | Source |
| --- | --- | --- |
| `victron_gx_v3.3.holding_registry_map.csv` | System and VE.Bus summary registers | CCGX Modbus-TCP list |
| `victron_venus_gx_system.holding_registry_map.csv` | Venus OS system overview (unit ID 100) | CCGX list, `com.victronenergy.system` |
| `victron_multiplus_quattro.holding_registry_map.csv` | MultiPlus / MultiPlus-II / Quattro | CCGX list, `com.victronenergy.vebus` |
| `victron_bmv_battery_monitor.holding_registry_map.csv` | BMV-700 / 702 / 712 and other battery monitors | CCGX list, `com.victronenergy.battery` |
| `victron_smartsolar_mppt.holding_registry_map.csv` | SmartSolar / BlueSolar MPPT | CCGX list, `com.victronenergy.solarcharger` |
| `victron_phoenix_inverter.holding_registry_map.csv` | Phoenix inverters (VE.Direct) | CCGX list, `com.victronenergy.inverter` |
| `victron_gx_generic_canbus.holding_registry_map.csv` | CAN-bus BMS batteries | CAN frames, see below |
| `victron_vedirect_serial.holding_registry_map.csv` | MPPT / BMV / Phoenix over VE.Direct | VE.Direct protocol 3.34 |
| `victron_mk3usb_vebus.holding_registry_map.csv` | MultiPlus / Quattro over MK3-USB | MK2 protocol 3.14, see Section 11 |

### Notes on CSV Usage

- The Modbus register maps are generated from Victron's *CCGX Modbus-TCP register list* (3.30, kept in
  `documentation/3rdparty/protocols`), one service per file, and are read-only. Victron's scale factors
  mean **divide the register by the factor** (battery voltage, factor 100: 5123 is 51.23 V; consumed Ah,
  factor -10, is negative). The vendor marks some registers writable (modes, limits, relays); the generated maps
  publish them read-only. Earlier versions of these maps used register numbers that belonged to different
  quantities in the vendor list (the system battery voltage was read from 800, which is the serial number),
  `victron_gx_v3.3` applied the factors as multipliers (230.1 V published as 23010), and
  `victron_bmv_battery_monitor` loaded no registers at all because its file was misnamed.
- VE.Direct field labels (`V`, `PPV`, `CS`, etc.) are ASCII text labels, not numeric register
  addresses. The `register` column in those CSVs carries the label string.
- The MK3-USB map is not a register list: its register numbers are MK2 commands (high byte = the vendor command byte,
  low byte = RAM variable or setting ID, or info-frame type), and the `serial_vebus` transport hands the map
  already-scaled values in fixed layouts. Earlier versions of this map used placeholder names that were not in
  Victron's document. See Section 11.
- Scale factors are in the `unit` column as the multiplier MPG applies (`0.1V` for a register that is divided by 10).

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
| `serial_vebus`: "No VE.Bus device answered" | Wrong `address`, MK3 not connected to the Multi, or MK2 powered before the Multi | Try addresses 0-2; check the cable; power-cycle the MK3-USB (3.6) |
| `serial_vebus`: values missing | The device reports them as not supported (e.g. state of charge without the battery monitor function) | Expected; see Section 11 |
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
