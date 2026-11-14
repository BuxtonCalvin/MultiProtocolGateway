# LuxPower Hybrid Inverter - MQTT Integration Guide

> **Supported Models:** LuxPowerTek LXP-LB 12K hybrid and the related LXP / ECO hybrid series (the EG4 18kPV, 12000XP and 6000XP are the same platform)
> **Protocol:** Modbus RTU over RS485 (`lxp_12k_hybrid`)
> **Interface:** RS485 monitoring port (RJ45)
> **Status:** Protocol available in MPG. Same register map as EG4
> LuxPowerTek is the manufacturer behind the EG4 hybrids. For EG4 wiring photos, terminal-block notes and the EG4 local API, see [EG4.md](EG4.md).

---

## Table of Contents

1. [Overview](#1-overview)
2. [Supported Models & Protocol Versions](#2-supported-models--protocol-versions)
3. [Hardware Requirements](#3-hardware-requirements)
4. [Port Identification & Wiring](#4-port-identification--wiring)
5. [Serial Settings](#5-serial-settings)
6. [What the Map Publishes](#6-what-the-map-publishes)
7. [Writable Registers](#7-writable-registers)
8. [MPG Configuration](#8-mpg-configuration)
9. [Troubleshooting](#9-troubleshooting)
10. [Source Documents](#10-source-documents)

---

## 1. Overview

LuxPower hybrid inverters publish a large Modbus RTU register map (the vendor document is *LuxPowerTek Modbus RTU Protocol*, first issued in 2017). The `lxp_12k_hybrid` protocol covers both register types:

- **Input registers** (374 rows): live telemetry
- **Holding registers** (398 rows): configuration and set points

Comparing the files in this repository, the LuxPower input map has exactly the same registers and variable names as `eg4_v58` and `eg4_18kpv`, and the holding map has the same register set as both (only the naming of the holding rows differs). If you own an EG4 inverter, use the EG4 protocols; use `lxp_12k_hybrid` for LuxPower-branded units.

---

## 2. Supported Models & Protocol Versions

| Model | Protocol Version | Baud Rate | Notes |
| --- | --- | --- | --- |
| LXP-LB-US 12K (split phase) | `lxp_12k_hybrid` | 19200 | Touch-screen model with RS485 / Wi-Fi / CAN |
| LXP-LB-EU 7K / 8K / 10K | `lxp_12k_hybrid` | 19200 | Needs the purpose-built RJ45 RS485 cable (section 4) |
| Other LXP / ECO hybrid models | `lxp_12k_hybrid` | 19200 | Same family; verify against live data |
| EG4 6000XP / 12000XP / 18kPV | `eg4_v58` | 19200 | See [EG4.md](EG4.md) |

---

## 3. Hardware Requirements

- USB to RS485 adapter (an isolated adapter is recommended; see the [Common Hardware Reference](Readme.md#common-hardware-reference))
- A cable with the correct RJ45 pinout for the inverter's RS485 port
- Linux/Raspberry Pi host

An RS485 to Ethernet bridge also works: set the serial side to 19200 8N1 and use `modbus_tcp` in MPG.

---

## 4. Port Identification & Wiring

The labelling of the monitoring port is not always intuitive. On the **LXP-LB-EU 7-10K**, the RS485 port is inside the inverter and needs a purpose-built USB RS485 cable with a specific RJ45 pinout; generic USB-RS485 RJ45 cables usually do not work. SolarAssistant documents the pinout for that model:

| RJ45 pin | Signal |
| --- | --- |
| 1 | RS485 B |
| 2 | RS485 A |
| 3-8 | Not used |

Other LuxPower models use different RJ45 pin-outs; for example the EG4 hybrids use pin 8 for A and pin 7 for B (see [EG4.md](EG4.md)). **Confirm the pinout for your exact model** before wiring; do not assume standard Ethernet cabling. If you get no response, swap A and B at the adapter.

---

## 5. Serial Settings

| Setting | Value |
| --- | --- |
| Baud | **19200** |
| Data / parity / stop | 8 data bits, no parity, 1 stop bit |
| Minimum polling period | 1 second |
| Largest read | 127 registers per request on current firmware |
| Address | Set in holding register 15 (Com AddR) |

> `modbus_rtu` takes its baud rate from the `baudrate` setting in your transport section (default 9600). The LuxPower protocol JSON has no `baud` entry, so **always set `baudrate = 19200`**.

---

## 6. What the Map Publishes

### Input registers

| Group | Registers | Notes |
| --- | --- | --- |
| State | `0` | Operating state (standby, fault, PV charge, on-grid, off-grid modes and combinations) |
| PV, battery | `1`-`11`, `38`-`39` | PV1-3 voltage and power, battery voltage, SOC and SOH (packed in register 5), charge / discharge power, bus voltages |
| Grid, inverter, EPS | `12`-`27`, `114`, `120`-`138` | Grid voltage and frequency, inverter and rectifier power, power factor, EPS voltage / frequency / power, power to and from grid, generator values, split-phase EPS values |
| Energy | `28`-`37` (daily), `40`-`59` (total), `124`-`126`, `133`-`139`, `171`-`173` | Daily 0.1 kWh, totals 1 kWh (32-bit): PV, inverter, rectifier, charge, discharge, EPS, to grid, to user, generator, load |
| Faults and warnings | `60`-`63` | 32 fault bits (E000-E031) and 32 warning bits (W000-W031) as individual variables; names in `lxp_12k_hybrid.json` |
| Temperatures | `64`-`67`, `108`-`112` | Inner, radiators, battery, plus BMS temperatures |
| BMS | `80`-`107` | Battery type and brand, charge / discharge limits, BMS status words, capacity, current, fault and warning codes, min / max cell voltage and temperature, cycle count |
| Identity | `115`-`119` | Serial number (10 characters from five registers) and its decoded fields |
| Parallel and phase | `113` | Master / slave, parallel number, single or three phase |
| AFCI | `140`-`152` | Arc-fault current, flags and arc counters per channel (where fitted) |
| Per-phase values | `180`-`193` | S and T phase power and current |
| Fault records | `400`-`411` | Two stored fault records |
| Battery pack blocks | `5002`-`5109` | Per-pack status blocks for packs reported to the inverter |
| Miscellaneous | `210`-`336` | `quick_charge_remaining` (210) plus undocumented `register_N` rows |

Many rows named `register_N` are unknown and are published only for analysis.

### Holding registers

Mostly configuration (see the next section) plus read-only identity registers (`2`-`6` serial-number fields, `7` firmware code, `19` device type).

---

## 7. Writable Registers

Holding rows are marked `W` (writable), `WD` (write-disabled: documented but not changeable from MPG) or `R`. Of the 398 rows, 305 are `W` and 36 are `WD`. Writable settings include charge and discharge power percentages and SOC limits (`64`-`67`, `74`-`75`, `82`-`83`), AC charge start and end SOC (`160`-`161`), output priority (`145`), line mode (`146`), battery capacity and voltage (`147`-`148`), equalization (`149`-`151`), float voltage (`144`), feed-in limit (`103`), grid peak-shaving (`206`-`208`) and the system charge SOC limit (`227`). Grid-protection thresholds (`25`-`58`), `114` and `226` are `WD`.

**Writing to an inverter can change grid-protection or battery behavior.** Writes only happen if write-back is enabled for the transport; see [Managing Write-Back](../usage/protocols.md#managing-write-back-via-the-web-ui). Change settings from the inverter or LuxPower app if you are not sure.

---

## 8. MPG Configuration

Follow the MultiProtocolGateway configuration guide:
<https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki/Configuration-Examples>

``` ini
[transport.luxpower]
transport = modbus_rtu
protocol_version = lxp_12k_hybrid
port = /dev/ttyUSB0
baudrate = 19200
address = 1
read_interval = 15
bridge = transport.mqtt
device_manufacturer = LuxPower
device_model = LXP-LB-12K
```

The protocol file asks for 40 registers per read.

---

## 9. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| No response | Wrong port, wrong cable pinout, A/B swapped, wrong baud | Check the port and cable for your model, swap A/B, set `baudrate = 19200` |
| Values look wrong for 32-bit energies | Wrong word order or a different map | Confirm the units against the inverter display |
| Wrong battery values | `batcomtype` / `battypeandbrand` differ from your pack | See the code tables in `lxp_12k_hybrid.json` |
| Unknown `register_N` variables | Undocumented registers | Block them with a variable screen file |

---

## 10. Source Documents

- LuxPowerTek / EG4electronics, *Modbus RTU Protocol* (V58, 2023-10-28)
- SolarAssistant, [LXP-LB-EU 7-10k RS485](https://solar-assistant.io/help/inverters/luxpower/LXP-LB/rs485) (RJ45 pinout)
- LuxPower, LXP-LB-US 12K specification
- [EG4.md](EG4.md)
- `protocols/luxpower/lxp_12k_hybrid.json`
- `protocols/luxpower/lxp_12k_hybrid.input_registry_map.csv`
- `protocols/luxpower/lxp_12k_hybrid.holding_registry_map.csv`
