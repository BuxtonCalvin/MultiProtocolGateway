# Kostal PIKO IQ / PLENTICORE - MQTT Integration Guide

> **Supported Models:** PIKO IQ, PLENTICORE plus (G1/G2), PLENTICORE G3, PLENTICORE BI and PLENTICORE MP G3 (hybrid and battery-ready inverters)
> **Protocol:** Modbus TCP, SunSpec (`kostal_sunspec`)
> **Interface:** Ethernet (RJ45), TCP port **1502**, unit ID **71**
> **Status:** Protocol available in MPG. The map follows Kostal's SunSpec interface description but has **not** been run against an inverter

---

## Table of Contents

1. [Overview](#1-overview)
2. [Supported Models & Protocol Versions](#2-supported-models--protocol-versions)
3. [Enabling Modbus on the Inverter](#3-enabling-modbus-on-the-inverter)
4. [Byte Order](#4-byte-order)
5. [What the Map Publishes](#5-what-the-map-publishes)
6. [Dynamic Register Layout](#6-dynamic-register-layout)
7. [MPG Configuration](#7-mpg-configuration)
8. [Troubleshooting](#8-troubleshooting)
9. [Verification Status](#9-verification-status)
10. [Not Mapped](#10-not-mapped)
11. [Source Documents](#11-source-documents)

---

## 1. Overview

KOSTAL inverters expose two Modbus TCP register tables on the same connection: a proprietary table (addresses 0 to about 1600) and a SunSpec chain starting at address 40000. The `kostal_sunspec` protocol reads the **SunSpec chain**: the common model, the three-phase inverter model, nameplate, immediate controls, multiple-MPPT, the wye three-phase meter model and the battery base model.

The Modbus interface is Ethernet only. There is no RS485 port to wire. Kostal recommends polling no faster than once per second.

---

## 2. Supported Models & Protocol Versions

| Model | Protocol Version | Notes |
| --- | --- | --- |
| PIKO IQ, PLENTICORE plus G1 | `kostal_sunspec` | Interface description firmware: UI 01.30.12092 |
| PLENTICORE plus G2 | `kostal_sunspec` | SW 02.15 or newer. Meter model 203 needs SW 02.12 or newer |
| PLENTICORE G3, BI, MP G3 | `kostal_sunspec` | SW 3.07 or newer for the latest interface revision. Meter model 203 needs SW 3.04.01 (G3) / 3.05.00 (MP G3) |
| Older PIKO / Pico RS485 units | `kostal_pico_inverters` | Stub protocol: no register map yet |

The Kostal manual (revision 2.9, 17 July 2026) lists these implemented SunSpec models: 1, 103, 113, 120, 123, 160, 203, 802 and the end model (65535).

---

## 3. Enabling Modbus on the Inverter

In the inverter web interface open **Settings > Modbus/SunSpec (TCP)** and switch on **Activate Modbus**. The page shows the port (1502) and the unit ID (71). The unit ID can be changed; if you change it, use the same value for `address` in MPG.

Give the inverter a DHCP reservation or a static IP so the integration survives a router restart.

---

## 4. Byte Order

The same settings page selects the byte order. KOSTAL's default is **little-endian (CDAB) "Standard Modbus"**, which suits the proprietary table. SunSpec data is **big-endian (ABCD)**, and MPG's SunSpec map assumes big-endian.

**Set the byte order to big-endian (ABCD) SunSpec** on the inverter. Symptoms of the wrong setting are plausible-looking but wrong 32-bit values (energy counters and event bitmasks) while 16-bit values such as power and voltage look correct. Note that other software reading the same inverter (for example a KOSTAL partner application) may expect the default, so change it with care.

---

## 5. What the Map Publishes

| Group | Source model | Notes |
| --- | --- | --- |
| Identity | Common (1) | Manufacturer, model, options, version, serial number, device address |
| AC and DC values | Inverter (103) | Phase currents, voltages, power, frequency, apparent and reactive power, power factor, energy, DC values, temperatures, operating state, event bitmasks. Values are integers with scale factors; MPG applies the scale factors |
| Nameplate | 120 | Rated power and limits |
| Controls | 123 | Connect, active-power limit, power-factor and reactive-power set points. Writable rows only take effect if write-back is enabled and Kostal accepts the write |
| MPPT | Multiple MPPT (160) | Global events plus one block per DC input (ID, current, voltage, power, operating state, module events). PLENTICORE reports three inputs |
| Grid meter | Meter (203) | Per-phase and total power, voltage, current, frequency, energy. Present only when a Kostal energy meter is connected and the firmware is recent enough |
| Battery | Battery base (802) | State of charge and health, charge status, state, type, voltage, current, power, capacity, events. Present only on hybrid inverters with a battery |

Enumerations (operating state, charge status, battery state and type, event bit names) come from the code tables in `kostal_sunspec.json`.

---

## 6. Dynamic Register Layout

Which blocks exist, and where, depends on the inverter. `kostal_sunspec` uses MPG's [dynamic register discovery](../architecture/dynamic_register_discovery.md): on every connect MPG walks the SunSpec model chain and builds the map from what the inverter reports, so a PV-only inverter gets no battery rows and an inverter with no meter gets no meter rows.

Before the first successful connect (and in the web UI) the map uses the layout in Kostal's start-address table. Kostal quotes 1-based addresses; MPG uses the 0-based address on the wire:

| Model | Kostal start address | Wire address (MPG anchor) |
| --- | --- | --- |
| 1 Common | 40003 | 40002 |
| 103 Three-phase inverter | 40071 | 40070 (`inverter_1_base`) |
| 113 Three-phase inverter, float | 40123 | 40122 |
| 120 Nameplate | 40185 | 40184 (`model_120_1_base`) |
| 123 Immediate controls | 40213 | 40212 (`model_123_1_base`) |
| 160 Multiple MPPT | 40239 | 40238 (`model_160_1_base`) |
| 203 Meter | 40309 | 40308 (`meter_1_base`) |
| 802 Battery base | 40416 | 40415 (`model_802_1_base`) |
| End model | 40480 | 40479 |

The 1-based to 0-based conversion matches evcc, which reads the Kostal inverter model at 40070.

---

## 7. MPG Configuration

Follow the MultiProtocolGateway configuration guide:
<https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki/Configuration-Examples>

``` ini
[transport.kostal]
transport = modbus_tcp
protocol_version = kostal_sunspec
host = 192.168.1.60
port = 1502
address = 71
read_interval = 10
bridge = transport.mqtt
device_manufacturer = KOSTAL
device_model = PLENTICORE plus
```

The port is **1502, not 502**, and the unit ID is **71, not 1**. The inverter accepts reads of up to 125 registers; the protocol file asks for 40 per read.

---

## 8. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| Connection refused | Modbus not activated, or port 502 used | Activate Modbus on the inverter and use port 1502 |
| Connects but values are zero or exceptions | Wrong unit ID | Use 71 (or the ID shown on the settings page) |
| 32-bit values and event flags look wrong | Byte order is little-endian | Set big-endian (ABCD) SunSpec on the inverter |
| No battery or meter variables | The inverter does not report models 802 or 203 | Expected on PV-only inverters, on firmware older than the versions in section 2, or with no meter connected |
| Variables disappear after a reconnect | Discovery removed blocks the inverter no longer reports | Check the log for "register map rebuilt from discovery" |
| Random timeouts | Polling too fast | Keep `read_interval` at 5 seconds or more; Kostal recommends more than 1 second |

---

## 9. Verification Status

The rows use the standard SunSpec layouts of the implemented models and have not been checked against a Kostal inverter. In particular:

- The model 160 row notes quote a fixed length of 48 (two inputs); the manual's start-address table implies 68 (three inputs). Discovery uses the length the inverter reports.
- Event bit names come from the SunSpec model definitions. Kostal's manual does not document vendor-specific event bits (`alarm_2` to `alarm_4` are published as raw words).

Please report which model and firmware you used if you try it.

---

## 10. Not Mapped

- Kostal's **proprietary register table** (addresses 0 to about 1600): operating data in float format, the external battery management and battery limitation registers, Multi Device Control plant registers, and IP configuration. These are not SunSpec and are not in this protocol.
- Float inverter model 113 (the float counterpart of model 103).
- Writes to the Kostal-specific set-point registers.

---

## 11. Source Documents

- KOSTAL Solar Electric, *PIKO IQ / PLENTICORE Interface description MODBUS (TCP) & SunSpec with control information*, revision 2.9: <https://www.kostal-solar-electric.com/fileadmin/downloadcenter/kse/BA/Protokollbeschreibung/BA_KOSTAL_Interface_MODBUS-TCP_SunSpec_with_Control.pdf>
- KOSTAL PLENTICORE G3 operating instructions, Modbus/SunSpec (TCP) settings: <https://documents.kostal.com/PLENTICORE-G3/BA/en-GB/473015051.html>
- Eniris Kostal page (older PIKO / Pico RS485 units): <https://docs.eniris.io/en/Controller/Devices/PV-hybrid-and-battery-inverters/Kostal>
- [Dynamic register discovery](../architecture/dynamic_register_discovery.md)
- `protocols/kostal/kostal_sunspec.json`
- `protocols/kostal/kostal_sunspec.holding_registry_map.csv`
