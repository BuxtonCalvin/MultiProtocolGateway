# A-Tronix AX Hybrid Inverter - MQTT Integration Guide

> **Supported Models:** a-TroniX Hybridpower AX2 Gen3 (Solinteg / Wattsonic Gen3 register layout); a-TroniX AX 1ph, AX 3ph and AX Plus hybrids are reported to be related units (see section 2)
> **Protocol:** Modbus RTU over RS485 · Modbus TCP (`a_tronix`)
> **Interface:** EMS / monitoring RS485 port
> **Status:** Protocol available in MPG. The map follows the Solinteg vendor document; it has **not** been confirmed on an a-TroniX unit

---

## Table of Contents

1. [Overview](#1-overview)
2. [Supported Models & Protocol Versions](#2-supported-models--protocol-versions)
3. [Hardware Requirements](#3-hardware-requirements)
4. [Serial Settings & Addressing](#4-serial-settings--addressing)
5. [What the Map Publishes](#5-what-the-map-publishes)
6. [Word Order and Scaling](#6-word-order-and-scaling)
7. [MPG Configuration](#7-mpg-configuration)
8. [Troubleshooting](#8-troubleshooting)
9. [Verification Status](#9-verification-status)
10. [Not Mapped](#10-not-mapped)
11. [Source Documents](#11-source-documents)

---

## 1. Overview

a-TroniX is the inverter brand of AKKU SYS Akkumulator- und Batterietechnik Nord GmbH (Halstenbek, Germany). a-TroniX publishes no register document of its own. The community (evcc, TapHome, Homey) reports that the **Hybridpower AX2 Gen3** uses the same register layout as Solinteg and Wattsonic Gen3 hybrid inverters, so the `a_tronix` protocol is a transcription of the Solinteg *Hybrid Inverter MODBUS RTU Protocol* v00.02 (2022-12-06).

The map reads inverter, grid-meter, PV, backup (EPS), battery and BMS telemetry plus the energy counters. Every row is read-only: the vendor marks several settings as read/write, but MPG publishes them as `R`.

---

## 2. Supported Models & Protocol Versions

| Model | Protocol Version | Notes |
| --- | --- | --- |
| Hybridpower AX2 Gen3 | `a_tronix` | evcc uses its Solinteg Gen3 template for this model |
| AX / AX Plus (rebadged FoxESS H3 Smart) | `foxess_h1_lan` | evcc uses its FoxESS H3 Smart template with unit ID 247. See [FoxESS.md](FoxESS.md) |
| Other Solinteg / Wattsonic Gen3 hybrids | `a_tronix` | Same vendor layout; see also [Solinteg.md](Solinteg.md) |

If you are not sure which family your unit belongs to, try `a_tronix` first and check that `inverter_sn` returns a readable serial number. If it does not, try `foxess_h1_lan`.

Older a-TroniX Gen2 (WTS) units use a different 1xxxx-5xxxx map that is **not** covered here.

---

## 3. Hardware Requirements

### Option A - USB to RS485 Adapter

- USB to RS485 adapter with A/B terminals (an isolated adapter is recommended for permanent installs)
- Cable to the inverter's RS485 terminals
- Linux/Raspberry Pi host with a free USB port

### Option B - RS485 to Ethernet / Wi-Fi Bridge

For a remote inverter, use a transparent RS485 bridge (for example a Waveshare module). Set the bridge's data transfer mode to **Modbus TCP <=> Modbus RTU** (a Wattsonic Gen3 owner reported that the wrong transfer mode gave "no response" errors until this was corrected), keep the serial side at 9600 8N1, then point MPG at the bridge with `modbus_tcp`.

### Port identification

The a-TroniX AX 1ph manual shows terminals labelled `485A` and `485B` next to the CT1 / CT2 inputs for the measuring device. The 3-phase manual lists two RS485 interfaces. The manuals reviewed do not say which interface is the EMS / monitoring port for third-party software, so **confirm the port from the label or the installer manual before wiring**. The meter RS485 port is for the grid meter and is not the port MPG should use.

---

## 4. Serial Settings & Addressing

| Setting | Value |
| --- | --- |
| Baud | 9600 (vendor default) |
| Data / parity / stop | 8 data bits, no parity, 1 stop bit |
| Modbus RTU address | 1-247. The vendor gives no default; Wattsonic units commonly ship as **247** |
| Modbus TCP | Port 502, unit ID **255** (evcc's AX2 Gen3 template uses 255 for serial, RS485-over-TCP and TCP connections alike) |
| Function code | 03 only |
| Address base | 0-based on-wire addresses (vendor section 2.3.1). A 1-based tool such as QModMaster needs +1 |

Set MPG `address` to the unit ID you use. If 247 gets no reply, try the address shown on the inverter display, then 1.

---

## 5. What the Map Publishes

| Group | Registers | Notes |
| --- | --- | --- |
| Identity and status | `10000`-`10120`, `18000` | Serial number (16 characters), equipment info, firmware, safety code, running status, fault flags |
| Grid and meter | `10994`-`11016` | Meter phase and total power, grid voltage / current / frequency, inverter AC power |
| PV | `11018`-`11064` | Daily and total generation, PV input power, PV1 / PV2 voltage, current and power, temperatures |
| Backup (EPS) and inverter phases | `30200`-`30248` | Voltage, current, frequency and power for backup phases A/B/C, per-phase inverter power |
| Battery | `30254`-`30258`, `32000`-`32006`, `33000`-`33018` | Voltage, current, mode, power, SOC, SOH, BMS status, cell temperature and voltage extremes, BMS error / warning bitmasks |
| Energy counters | `31000`-`31008` (daily), `31102`-`31118` (total) | Grid injection / purchase, backup output, battery charge / discharge, PV generation, load |
| Settings (read-only) | `25000`-`25109`, `50000`-`50009`, `52500`-`52505`, `53006`-`53048` | Safety code, injection limit, working mode, EPS switch, battery brand and SOC limits, six charge / discharge schedule periods |

Mode, status and brand names (running status, battery mode, BMS status, safety codes, working modes, battery configuration) come from the code tables in `a_tronix.json`.

---

## 6. Word Order and Scaling

- Values are big-endian. 32-bit values (`UINT` / `INT`) use two consecutive registers, high word first (ABCD), the MPG default, so no `Register_Endian` change is needed.
- Scaling follows the vendor "accuracy" column: 10 is 0.1, 100 is 0.01, 1000 is 0.001. **Power registers whose vendor accuracy is 1000 (kW) are published as raw watts.**
- Signed registers (`INT`, `SHORT`) are used for power and battery current. The vendor table gives no sign convention for battery current or battery power, so check the direction against the inverter display before relying on it.
- `battery_soc` and `battery_soh` use the unit `0.01%` (raw 0-10000).
- Schedule start and stop times pack the hour in the high byte and the minute in the low byte.

---

## 7. MPG Configuration

Follow the MultiProtocolGateway configuration guide:
<https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki/Configuration-Examples>

### Modbus RTU over RS485

``` ini
[transport.a_tronix]
transport = modbus_rtu
protocol_version = a_tronix
port = /dev/ttyUSB0
baudrate = 9600
address = 247
read_interval = 15
bridge = transport.mqtt
device_manufacturer = a-TroniX
device_model = Hybridpower AX2 Gen3
```

### Modbus TCP (RS485 bridge or inverter Ethernet)

``` ini
[transport.a_tronix]
transport = modbus_tcp
protocol_version = a_tronix
host = 192.168.1.50
port = 502
address = 255
read_interval = 15
bridge = transport.mqtt
device_manufacturer = a-TroniX
device_model = Hybridpower AX2 Gen3
```

The protocol file asks for a `batch_size` of 20 registers per read.

---

## 8. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| No response at all | A/B wires reversed, wrong port, or wrong unit ID | Swap A and B at the adapter, confirm the EMS port, try 247, 255 (TCP) and 1 |
| No response through a bridge | Bridge transfer mode is not Modbus TCP <=> Modbus RTU | Change the bridge's data transfer mode |
| Serial number is garbage | Unit is not a Gen3 layout | Try `foxess_h1_lan`, or run the Live Analysis tool |
| `loading_energy_today` looks wrong | Reported inaccurate on some units | Cross-check as PV + grid - battery |
| Battery power sign looks inverted | Vendor gives no sign convention | Compare with the inverter display and apply an adjustment in the protocol editor |

---

## 9. Verification Status

The register addresses, types and scales were checked against the Solinteg vendor PDF and the Wattsonic Gen3 transcription at smarthome.exposed. **The a-TroniX clone relationship itself has not been confirmed by a hardware test of an AX unit.** If you run this on a unit, please report which model and firmware you used.

---

## 10. Not Mapped

- `53500`-`53523`, the "BMS data for EMS" block, which is written *by* an energy management system and is not inverter telemetry
- `28000`-`28015`, grid-protection limits
- `50200`-`50211`, EMS power scheduling
- The model-name table (`10008`: high byte is the inverter type, low byte is the model), which is published as a raw value

---

## 11. Source Documents

- Solinteg, *Hybrid Inverter MODBUS RTU Protocol* v00.02 (vendor PDF, 2022-12-06)
- Wattsonic Gen3 transcription: <https://smarthome.exposed>
- evcc, [A-Tronix Hybridpower AX2 Gen3](https://docs.evcc.io/en/meters/a-tronix-hybridpower-ax2-gen3) and [A-Tronix AX Plus Hybrid Inverter](https://docs.evcc.io/en/meters/a-tronix-ax-plus-hybrid-inverter) templates
- AKKU SYS, *a-TroniX inverter AX series 1ph, installation and operating instructions* (atx009de0823)
- `protocols/a_tronix/a_tronix.json`
- `protocols/a_tronix/a_tronix.holding_registry_map.csv`
