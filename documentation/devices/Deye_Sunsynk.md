# Deye / Sunsynk — MQTT Integration Guide

> **Supported Models:** Deye SUN-xK-SG01/02/03/04/05LP1 (single-phase) · SUN-xK-SG04LP3 / SG05LP3 (three-phase, low-voltage battery) · SUN-xK-SG01HP3 / SG02HP3 (three-phase, high-voltage battery) · Sunsynk 3.6–16 kW (single- and three-phase)
> **Protocols:** `deye_sunsynk`, `deye_sunsynk_hybrid` (single-phase, including US split-phase) · `deye_sunsynk_3ph_lv` · `deye_sunsynk_3ph_hv` · `deye_sunsynk_micro_ess`
> **Transport:** Modbus RTU over RS485 · Modbus TCP through an RS485 gateway · Solarman V5 on the logger (port 8899; not supported by the MPG Modbus TCP transport)
> **Not covered:** Deye grid-tie string inverters (SUN-xK-G0x) and ordinary grid-tie microinverters (SUN-M / SUN-xxxG3 / G4). They have no battery and, like the kellerza/sunsynk project, MPG does not ship a map for them. See [Section 3](#3-supported-models--protocol-matrix).
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
6. [RS485 Pinout & Wiring](#6-rs485-pinout--wiring)
7. [Modbus Configuration Details](#7-modbus-configuration-details)
8. [Solarman Local API Access](#8-solarman-local-api-access)
9. [MPG Configuration](#9-mpg-configuration)
10. [Register Maps](#10-register-maps)
    - 10.1 [Static Registers Read at Startup](#101-static-registers-read-at-startup)
    - 10.2 [Scaling, Signs and Other Adjustments](#102-scaling-signs-and-other-adjustments)
    - 10.3 [Fault, Warning and Status Registers](#103-fault-warning-and-status-registers)
    - 10.4 [Settings and Write-Back](#104-settings-and-write-back)
    - 10.5 [Known Firmware Quirks and Gaps](#105-known-firmware-quirks-and-gaps)
11. [Multi-Inverter Setups](#11-multi-inverter-setups)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Overview

Deye manufactures the inverters that Sunsynk (and several other brands) sell under their own name. Within the Deye range there is **no single Modbus register map**: the model code decides the layout.

- **Single-phase hybrids (LP1)** keep their data in registers 0–350 (battery SOC at 184, PV1 power at 186).
- **Three-phase hybrids (LP3 and HP3)** keep their data in registers 500–730 (battery SOC at 588, PV1 power at 672) and put their settings in the 80–230 range.
- **Hybrid microinverter / ESS units** keep their live data in registers 1000–1130.
- **Grid-tie string inverters and microinverters** use different registers again and are not hybrids.

Using the wrong map does not fail loudly. It returns plausible-looking numbers from the wrong registers, so choose the protocol from the hardware table in [Section 3](#3-supported-models--protocol-matrix).

Two MQTT integration paths are available:

- **RS485 direct** — physically wire to the inverter's RS485 port; full read/write Modbus RTU access; most reliable
- **Solarman TCP 8899** — use the existing Wi-Fi/LAN dongle's local TCP port; no additional hardware, but it speaks Solarman V5 framing, which the MPG Modbus TCP transport does not decode

> **RS485 direct is strongly preferred** for reliable, low-latency monitoring with full register access. The Solarman 8899 path is convenient if cable routing to the inverter is not practical, but offers less control and depends on the dongle remaining operational.

---

## 2. Brand Relationships & Compatibility

| Brand Name | Manufacturer | Register Map | Notes |
| --- | --- | --- | --- |
| Deye | Ningbo Deye Inverter Technology | Per hardware family (Section 3) | Original manufacturer |
| Sunsynk | Sunsynk Ltd (rebranded Deye) | Same as the Deye equivalent | Match the Sunsynk model to the Deye family by phase count and battery voltage |
| Sol-Ark | Sol-Ark LLC | Own Modbus document | See [SolArk.md](SolArk.md); Sol-Ark US models are split-phase and are not confirmed to match any Deye map here |
| Other rebrands | Various | Usually the Deye equivalent | Dump a few registers (for example 184/186 for single-phase, 588/672 for three-phase) and compare with the inverter display before trusting a map |

---

## 3. Supported Models & Protocol Matrix

| Hardware family | Example models | MPG protocol | Layout | Status |
| --- | --- | --- | --- | --- |
| Single-phase LV hybrid (LP1): SG01LP1, SG02LP1, SG03LP1, plus **US split-phase SG01LP1-US** | SUN-7.6/8/12/14/16K-SG01LP1-EU, SUN-5/6/7.6/8K-SG01LP1-US (120/240 V split-phase), SUN-7.6/8/10/12K-SG02LP1-EU, SUN-3/3.6/5/6K-SG03LP1-EU, Sunsynk 5–16 kW | `deye_sunsynk_hybrid` (full) or `deye_sunsynk` (core monitoring) | Registers 0–350 | Confirmed by community projects |
| Single-phase LV hybrid, newer: SG04LP1, SG05LP1 | SUN-3.6/5/6/7/7.6/8/10K-SG05LP1-EU-AM2-P, Sunsynk Ecco | `deye_sunsynk_hybrid` | Registers 0–350 | Reported working on SG05LP1; per-firmware testing recommended |
| Three-phase LV hybrid: SG04LP3, SG05LP3 | SUN-5/6/8/10/12K-SG04LP3-EU, SUN-3–20K-SG05LP3-EU | `deye_sunsynk_3ph_lv` | Registers 500–730 | SG04LP3 best confirmed; SG05LP3 shares the map |
| Three-phase HV hybrid: SG01HP3, SG02HP3 | SUN-5–50K-SG01HP3-EU, SUN-25–80K-SG02HP3-EU | `deye_sunsynk_3ph_hv` | Registers 500–730, PV/battery power in 10 W steps | SG01HP3 confirmed; SG02HP3 and units above 50 kW less tested |
| Hybrid microinverter / ESS | Deye micro hybrid ESS units | `deye_sunsynk_micro_ess` | Registers 1000–1130 and 300–420 | From the kellerza/sunsynk `micro-hybrid-ess` definition; no PV or fault registers |
| Grid-tie string inverters | SUN-xK-G0x series | — | Different | Not covered (no battery) |
| Grid-tie microinverters | SUN-M60/80/100G4, SUN-xxxG3 | — | Different | Not covered (no battery) |

**US split-phase:** Deye's US model is named SG01LP1-US and uses the single-phase register layout. The two 120 V legs appear as the existing L1/L2 rows (grid current 160/161, grid-side L2 power 168, load power 176/177), so no separate map is needed. This is also how the kellerza/sunsynk project treats it.

The family split follows the kellerza/sunsynk project, which offers one definition set per hardware family (1PH, 1PH-16kw, 3PH, 3PH-hv, micro-hybrid-ess), selects it explicitly rather than auto-detecting it, and does not cover grid-tie string or microinverters.

`deye_sunsynk` is a lean subset of `deye_sunsynk_hybrid` (about 50 rows) for slow links such as 9600 baud RS485 shared with other devices. It has the same variable names and registers as the full single-phase map.

> **Note:** Models with the same suffix can still differ in MPPT count, counter scaling and the position of the time of use block. SG03LP1 units may need reads limited to 16 registers; see [Section 10.5](#105-known-firmware-quirks-and-gaps).

---

## 4. Hardware Requirements

### 4.1 Option A — USB to RS485 Adapter (Direct)

**What you need:**

- USB to RS485 adapter with RJ45 connector, **or**
- Generic USB to RS485 adapter with screw terminals + RJ45 Ethernet cable (custom pinout — see Section 6)

**Community-recommended adapters:**

| Adapter | Notes |
| --- | --- |
| **Waveshare USB TO RS485 (B)** | Isolated, TVS, resettable fuse, GND pin — the adapter preferred by the Sunsynk add-on maintainer |
| **DSD TECH SH-U11F** | Isolated, TVS, GND pin — widely endorsed on the DIY Solar forum |
| Generic CH340-based | Works but has no isolation or protection; susceptible to ground loops |
| 2-wire USB RS485 without GND | Works on some installations; success varies |

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

> Port labeling is the most common source of connection failures on Deye/Sunsynk. The RJ45 port to use differs by model and firmware, and on some models the socket marked "RS485" is **not** the monitoring port.

| Model family | Port reported to work | Notes |
| --- | --- | --- |
| Single-phase 3.6–8 kW (Sunsynk and Deye) | **BMS485 / RS485-CAN**, top left | On a Sunsynk 5.5 kW the socket marked "RS485" at the bottom right did **not** respond |
| Three-phase LV (SG04LP3, SG05LP3) | Socket marked **RS485** or **BMS** depending on firmware | Check the manual for your firmware; if one socket is silent, try the other |
| Three-phase HV (SG01HP3) | **RS485**, bottom right | BMS1 is the CAN link to the battery |

Further points:

- The Modbus/RS485 port must be set to **slave** (not Master/Meter) in the inverter menu. In master mode the inverter expects a meter on that port.
- The Solarman logger and a third-party poller share one inverter: only one Modbus client should poll at a time.
- Newer models combine the battery BMS and RS485 on a single RJ45 socket (the "2-in-1" port): RS485 on pins 1–3 and CAN on pins 4–5. If a battery already uses that socket you need a splitter (Section 6).

---

## 6. RS485 Pinout & Wiring

### 6.1 Standard RJ45 Pinout (inverter side)

Only pins 1–3 carry RS485. The crimping standard (T568A or T568B) changes the wire colors, not the pin numbers.

| RJ45 Pin | Signal | Wire Color (T568B) | Wire Color (T568A) |
| --- | --- | --- | --- |
| 1 | RS485 B (D−) | Orange/White | Green/White |
| 2 | RS485 A (D+) | Orange | Green |
| 3 | GND | Green/White | Orange/White |

> **Identifying your cable crimp:** If the two outermost wires you see on the connector are brown and green, it is probably T568A; if brown and orange, T568B.

**Wiring to USB RS485 adapter:**

``` text
RJ45 Pin 2 (A+, D+)   →  Adapter A+
RJ45 Pin 1 (B−, D−)   →  Adapter B−
RJ45 Pin 3 (GND)      →  Adapter GND (recommended, if the adapter has a GND terminal)
```

### 6.2 Splitter Cable for 2-in-1 BMS Ports

For models where the BMS port carries both CAN (battery) and RS485 (monitoring) on the same RJ45 connector.

``` text
BMS RJ45 Port (inverter)
├── Pins 4–5: CAN-H, CAN-L (plus GND if the battery cable needs it)  →  Battery BMS cable
└── Pins 1–3: RS485 B−, A+, GND                                       →  Monitoring RS485 adapter
```

Pins 6–8 are normally unused. Do not feed all eight pins to both the battery and the RS485 adapter: RS485 signals appearing on battery pins can disrupt BMS communication. Exact pin use varies by model and firmware, so check the communication section of your inverter manual before building a splitter.

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
| Function codes | 0x03 (read holding registers), 0x10 (write multiple registers) |
| Solarman TCP port | **8899** |

Baud rate and slave ID are configurable in the inverter menu (SolarmanPV app → Inverter Settings → Communication, or inverter LCD on models that have one).

**Read size:** MPG reads in windows of up to `batch_size` registers (40 in the protocol JSON). Wi-Fi and some Ethernet RS485 gateways (USR-W610, USR-W630) need 8 or fewer, and SG03LP1 units may need 16 or fewer. Lower `batch_size` if you see timeouts on larger reads.

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

# Read a holding register (e.g., register 184 = battery SOC on single-phase models, 588 on three-phase models)
soc = dongle.read_holding_registers(register_addr=184, quantity=1)  # single-phase map; use 588 on three-phase
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

> **MPG module note:** A `solarman_v5_v1` protocol module using `pysolarmanv5` over port 8899 would provide full Modbus register access for Deye and Sunsynk models without requiring a physical RS485 cable — only the existing Wi-Fi/LAN dongle and its IP address and serial number. This would make it the lowest-friction integration path for these inverters.

---

## 9. MPG Configuration

Use the protocol that matches the hardware family from [Section 3](#3-supported-models--protocol-matrix).

### 9.1 RS485 Direct

``` ini
[transport.inverter]
protocol_version = deye_sunsynk_hybrid
address = 1
port = /dev/ttyUSB0
baudrate = 9600
read_interval = 10
```

For a three-phase inverter set `protocol_version` to `deye_sunsynk_3ph_lv` (SG04LP3 / SG05LP3) or `deye_sunsynk_3ph_hv` (SG01HP3 / SG02HP3).

### 9.2 RS485 Gateway (Modbus TCP)

When an RS485 to Ethernet gateway (Waveshare, USR) is in **Modbus TCP gateway mode**, MPG talks to it with the standard Modbus TCP transport:

``` ini
[transport.inverter]
protocol_version = deye_sunsynk_hybrid
transport = modbus_tcp
host = 192.168.1.200
port = 502
address = 1
read_interval = 10
```

> **Note:** The Solarman logger's own port 8899 is **not** Modbus TCP. It uses Solarman V5 framing (Section 8), which the MPG Modbus TCP transport does not handle; use an RS485 gateway or add a dedicated Solarman module.

Configuration reference:
<https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki/Configuration-Examples#modbus-rtu-to-mqtt>

---

## 10. Register Maps

The four protocol files live in `protocols/deye_sunsynk/`.

| Protocol | Rows | Purpose |
| --- | --- | --- |
| `deye_sunsynk` | ~50 | Core monitoring for single-phase hybrids, read-only |
| `deye_sunsynk_hybrid` | ~145 | Full single-phase map: monitoring, battery and BMS values, time of use programs, settings |
| `deye_sunsynk_3ph_lv` | ~190 | Three-phase LV map (SG04LP3 / SG05LP3) |
| `deye_sunsynk_3ph_hv` | ~195 | Three-phase HV map (SG01HP3 / SG02HP3), including PV5–PV8 power |
| `deye_sunsynk_micro_ess` | ~90 | Hybrid microinverter / ESS map: battery, BMS, grid, load, energy and time of use settings (no PV or fault registers) |

Register sources are the Deye single-phase protocol document (V118/V119), the Deye alarm list, and the community projects that have decoded the three-phase and high-voltage layouts. Deye does not publish a single official document for all families, so unconfirmed details are called out in the `note` column.

### 10.1 Static Registers Read at Startup

Rows whose `read interval` is `startup` are read once per connection (and again after a reconnect) instead of every poll cycle. The values are cached and published with every cycle. See [protocols.md](../usage/protocols.md#startup-rows-read-once-per-connection).

| Variable | Single-phase register | Three-phase register | Meaning |
| --- | --- | --- | --- |
| `device_type` | 0 | 0 | Hardware class (single-phase hybrid, three-phase hybrid, string, micro) with a `_desc` text |
| `communication_protocol_version` | 2 | 2 | Modbus protocol document version the firmware implements |
| `serial_number` | 3–7 | 3–7 | 10 ASCII characters |
| `control_board_aux_version` | 11 | — | Control board auxiliary program version |
| `control_board_version` | 13 | — | Matches the MAIN version on the LCD |
| `communication_board_version` | 14 | — | Matches the HMI version on the LCD |
| `rated_power` | 16–17 | 20–21 | Rated output power in watts (32-bit, low word first) |
| `grid_standard`, `configured_grid_frequency`, `configured_grid_phases` | — | 182–184 | Grid code settings (three-phase) |

Settings that people change (battery limits, time of use programs, grid charge) are **not** startup rows. They are polled every 60 seconds so a change made on the inverter or through MPG write-back appears within a minute. Startup rows would hold the old value until the next reconnect.

### 10.2 Scaling, Signs and Other Adjustments

| Topic | Handling |
| --- | --- |
| 32-bit counters (energy, rated power) | One row with a register pair such as `78_80` and `{"Register_Endian": "little_endian-CDAB"}`. Deye stores the **low word first**. For single-phase total grid import the pair is 78 and 80 because register 79 is the grid frequency. |
| Temperatures | Raw value has a +1000 offset: `(raw − 1000) × 0.1` °C. Rows use unit `0.1°C` with `{"Offset": -100}`. |
| Power and current | Signed 16-bit (`SHORT`). Positive battery power or current means charging; positive grid power means import. |
| Packed bytes | Inverter time (22–24 single-phase, 62–64 three-phase) is split into byte fields with `.b8` and `.b0` offsets. Year carries an offset of 2000. |
| Bit fields | Single-phase `grid_charge_enabled`, `priority_load`, `solar_export`, `use_timer` and three-phase `use_timer` and `grid_connected` read one bit, so other bits in the same register cannot change the value. |
| High-voltage scaling | HV maps use unit `10W` for PV and battery power and `0.1V` for battery voltages. |
| Time of use times | HHMM written as decimal digits (1530 = 15:30), handled in 5 minute steps. |

### 10.3 Fault, Warning and Status Registers

Fault registers (103–106 single-phase, 555–558 three-phase) are flag words. Bit *n* of word *w* is fault `F(16w + n + 1)`, and the names come from the Deye hybrid alarm list (F01–F64) in the protocol JSON as `fault_1_codes` to `fault_4_codes`. For example F58 is a **BMS communication** fault and F62 is **DRMs0 stop**. Several of those numbers mean something different on Deye string inverters, which is why those inverters are not covered by these maps.

Three-phase maps also publish `relay_status` (552), `warning_1` (553: fan warning, wrong grid phase) and `warning_2` (554: battery and parallel communication warnings).

### 10.4 Settings and Write-Back

Settings rows are marked `RW` in the CSV, so write-back can be enabled on them in the web UI (see [protocols.md](../usage/protocols.md#managing-write-back-via-the-web-ui)). Two rules:

- **`inverter_enabled` (register 43 single-phase, 80 three-phase) is read-only.** Community sources disagree on the 0/1 meaning, one test unit read 0 while running normally, and a wrong write can stop the inverter (fault F19).
- **Time of use `prog*_charge` registers are flag words.** MPG writes the whole word, so read it first and keep the other bits. Test one write at a time and compare with the inverter display. Treat the work mode (`load_limit`) as a critical setting.

### 10.5 Known Firmware Quirks and Gaps

- **Single-phase 12–16 kW and SG02LP1:** `grid_power` (169) and `grid_ct_power` (172) are reported in 10 W steps. If those values are ten times too small, set the unit to `10W` in a protocol override.
- **Month and year counters (65–67, 87–88, 98–99):** the V118 document lists 1 kWh steps for the month counters, but PV matched 0.1 kWh in hardware tests, and the year load and year grid export values read identically on one SG05LP1. Treat them as unreliable until confirmed on your firmware.
- **BMS values (313, 320, 321):** read 0 on some single-phase firmware.
- **Battery temperature (three-phase 586):** the offset is documented for most temperatures, but confirm this one against the inverter display.
- **Values above 32 kW (large HV units):** powers in the 600–700 range have optional high-word registers (687–708) that are not mapped, so totals above 32,767 W can wrap.
- **External meter block (single-phase 424–437) and parallel/battery 2 bits** are not mapped.
- **Micro hybrid / ESS family:** temperature scaling and offset, and the meaning of the state, work mode, priority load, load limit and program charge values, are not published, so those rows show raw values. There are no fault or PV registers in the source definition. The inverter power pair at 1028–1029 is a signed 32-bit value in 0.1 W steps.
- **Grid-tie string inverters and microinverters** have no map in MPG.

---

## 11. Multi-Inverter Setups

**RS485 daisy chain:**

- Each inverter requires a unique Modbus slave ID (set per inverter in inverter communication settings or SolarmanPV app)
- Daisy-chain RS485 ports in series: adapter → inverter 1 → inverter 2 → ...
- Add 120Ω termination at the last device in the chain
- One adapter or Waveshare bridge serves all inverters

**Parallel installation (same site, synchronized inverters):**

- Parallel inverters do not automatically share a Modbus address
- Each parallel inverter requires its own dedicated RS485 cable or unique slave ID
- Configure separate device entries in MPG for each inverter

---

## 12. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| No response at all | Wrong port (see Section 5), wrong protocol for the model family (see Section 3), or A/B swapped | Identify the RS485 port, then pick the protocol that matches the hardware family |
| Response on some registers, 0 on others | Read-only registers via 8899 interface | Switch to RS485 direct for full register read/write access |
| Timeout or garbled data | A/B wires reversed | Swap the A and B wires (RJ45 pins 1 and 2) at the adapter end |
| Works with dongle unplugged, fails when plugged in | Shared port conflict | Remove dongle when using RS485 direct; or use a splitter cable |
| Idle voltage reads ~0V between A/B | 120Ω termination pulling voltage down | Normal behavior with termination load; 4–5V is idle without load |
| Solarman 8899 connection refused | Dongle not on LAN or IP changed | Check router DHCP; ping dongle IP; verify dongle is online |
| Waveshare bridge not working | Raw serial bridge mode selected | Reconfigure Waveshare to Modbus TCP gateway mode via VirCom |
| Wrong slave ID across multi-inverter chain | ID conflict | Assign unique Modbus slave ID to each inverter individually |
