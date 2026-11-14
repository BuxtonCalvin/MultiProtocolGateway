# Sunmart Hybrid Inverter - MQTT Integration Guide

> **Supported Models:** Sunmart off-grid / hybrid inverters (for example the VMII, MKSII, Max, MPS, PS and VII series) built on the SRNE platform
> **Protocol:** Modbus RTU over RS485 (`sunmart`)
> **Interface:** RS485 (RJ45) port on the inverter
> **Status:** Protocol available in MPG. Small curated map; several scalings are unverified
> Sunmart inverters use the **SRNE register layout** (0x0100 battery SOC, 0xE204 output priority). The `sunmart` map is byte-for-byte identical to the `powmr` map, so everything in [PowMr.md](PowMr.md) applies. For the full SRNE register set, see [SRNE.md](SRNE.md).

---

## Table of Contents

1. [Overview](#1-overview)
2. [Supported Models & Protocol Versions](#2-supported-models--protocol-versions)
3. [Hardware Requirements](#3-hardware-requirements)
4. [Port Identification & Wiring](#4-port-identification--wiring)
5. [Serial Settings](#5-serial-settings)
6. [What the Map Publishes](#6-what-the-map-publishes)
7. [Writable Registers](#7-writable-registers)
8. [Scaling Caveats](#8-scaling-caveats)
9. [MPG Configuration](#9-mpg-configuration)
10. [Troubleshooting](#10-troubleshooting)
11. [Source Documents](#11-source-documents)

---

## 1. Overview

Sunmart sells SRNE-platform inverters (Pytes lists Sunmart models as compatible over RJ45 RS485). The `sunmart` protocol is a 49-row subset of the SRNE map chosen for Home Assistant use (the register notes mention select and text-sensor entities). It reads telemetry and a few settings, and can change three of them. Everything is a holding register (function code 03).

The protocol JSON, like `powmr.json`, sets no `transport`, `baud` or `batch_size`, so set the transport, baud rate and address in your own configuration.

---

## 2. Supported Models & Protocol Versions

| Model | Protocol Version | Notes |
| --- | --- | --- |
| Sunmart single-phase off-grid / hybrid | `sunmart` | Rows for L1 and PV1 / PV2 |
| Sunmart models with L2 / L3 outputs | `sunmart` | L2 / L3 rows are included |
| Other SRNE-platform brands | `srne_v1.7`, `srne_2021_v1.96`, `srne_v3.9` | See [SRNE.md](SRNE.md); PowMr: [PowMr.md](PowMr.md) |

---

## 3. Hardware Requirements

- USB to RS485 adapter (an isolated adapter is recommended) or an RS485 to Ethernet bridge
- A cable or RJ45 breakout for the inverter's communication port
- Linux/Raspberry Pi host

---

## 4. Port Identification & Wiring

The Sunmart documentation reviewed does not give a pinout. SRNE-platform inverters differ by model, and the PowMr community reports disagree:

- Some SRNE-platform models have a true RS485 port on an RJ45 jack with A and B on different pins (pins 5/6 on one reported model, 7/8 on another).
- Some RJ45 ports carry **RS232**, not RS485. An RS485 adapter will not work on those.
- For comparison, the SRNE ABP family documented by SolarAssistant uses pin 2 for ground, pin 7 for A and pin 8 for B.

**Check your model's manual or the label next to the port before wiring**, and try swapping A and B if there is no response.

---

## 5. Serial Settings

| Setting | Value |
| --- | --- |
| Baud | 9600 (SRNE default) |
| Data / parity / stop | 8N1 is the SRNE default; one forum user reports parity E on their unit |
| Slave address | 1 on most units; check the inverter display or app |
| Function code | 03 (read) and 06 (write) |

`modbus_rtu` takes `baudrate`, `parity` and `stopbits` from your transport section (defaults 9600, `N`, 1).

---

## 6. What the Map Publishes

| Group | Registers | Notes |
| --- | --- | --- |
| Battery | `0x0100`-`0x0102` | SOC (%), voltage (0.1 V), current (0.1 A) |
| PV | `0x0107`-`0x0109`, `0x010F`-`0x0111` | PV1 and PV2 voltage, current and power |
| Faults | `0x0204`-`0x0205` | Two active fault codes. The 40 code names are in `sunmart.json` |
| Grid and inverter output | `0x0213`-`0x0218`, `0x021B`, `0x021F` | Grid voltage, current, frequency; inverter output voltage, current, frequency, active power, load ratio (L1) |
| Temperatures | `0x0220`-`0x0222` | DC/DC, DC/AC and transformer (0.1 degC) |
| Three-phase extras | `0x022A`-`0x0239` | L2 and L3 grid voltage and current, inverter voltage, current, active power and load ratio |
| Settings (read) | `0xE001`, `0xE006`-`0xE009`, `0xE205`, `0xE20A` | PV maximum charging current, charge limit, equalization, boost / bulk and float voltage, grid and total maximum charging current |
| Daily counters | `0xF02D`-`0xF031` | Battery Ah charged / discharged today, energy generated and consumed today (0.1 kWh), days in operation |

Several rows are read at a reduced rate (the notes mention `skip_updates`), and the register addresses are quoted in hex in each row's note.

---

## 7. Writable Registers

| Register | Variable | Values |
| --- | --- | --- |
| `0xE01D` (57373) | `charge_cutoff_soc` | 50-100 %, a maximum-SOC cut-off |
| `0xE204` (57860) | `output_source_priority` | 0 = Solar-Utility-Battery (SUB), 1 = Utility-Solar-Battery (USB), 2 = Solar-Battery-Utility (SBU) |
| `0xE210` (57872) | `buzzer_alarm` | 0 = off, 1 = on |

Writes only happen when write-back is enabled for the transport. Changing the output priority changes where your load is powered from, so test with a non-critical load first. The two select-style writes are described in the CSV as "optimistic": MPG assumes the write succeeded rather than reading it back.

---

## 8. Scaling Caveats

- **Charge voltages** (`0xE006`-`0xE009`) are published at 0.1 V per count with a 9.0-15.5 V range. In the SRNE protocol these voltages are quoted for a **12 V** system, so on a 24 V or 48 V battery the inverter's actual voltage is the published value multiplied by the system voltage / 12 (x2 and x4). SRNE's protocol document (V1.92, section 4) states that all battery setting voltages are expressed for a 12 V battery and converted for other system voltages. That multiplier is not applied by this protocol, so compare with the inverter display. The notes on `equalization_charge_voltage` and `boost_bulk_charge_voltage` (`0xE007`, `0xE008`) are copies of the `0xE006` note.
- **Battery current sign:** the CSV applies a -0.1 multiplier so that charging and discharging read in the opposite sense to the raw register. Confirm the direction on your unit.
- Registers without a scale in the note (`pv1_power`, `inverter_active_power_l1`, load ratios, Ah counters) are raw values.

---

## 9. MPG Configuration

Follow the MultiProtocolGateway configuration guide:
<https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki/Configuration-Examples>

``` ini
[transport.sunmart]
transport = modbus_rtu
protocol_version = sunmart
port = /dev/ttyUSB0
baudrate = 9600
address = 1
read_interval = 10
bridge = transport.mqtt
device_manufacturer = Sunmart
device_model = VMII
```

---

## 10. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| No response | RS232 port, wrong pins, A/B swapped, wrong address | Confirm the port type for your model; swap A and B; try address 1 |
| Garbled or no frames | Parity mismatch | Try `parity = E` |
| Charge voltage is 2 or 4 times lower than the display | 24 V / 48 V system (12 V reference) | Multiply by system voltage / 12, or add an adjustment in the protocol editor |
| Battery current sign is backwards | Sign convention | Add a value formula to invert it |
| L2 / L3 rows are zero | Single-phase inverter | Expected |

---

## 11. Source Documents

- SRNE, *Energy storage inverter MODBUS communication protocol* V1.92 (section 4, battery setting voltages) and the repository's `srne_v1.7` map; see also [SRNE.md](SRNE.md)
- Home Assistant community, [Integrating SRNE MPPT inverter with HA](https://community.home-assistant.io/t/integrating-srne-mppt-inverter-with-ha/490475)
- Pytes, [Compatibility list of Pytes ESS and inverters](https://cdn.shopify.com/s/files/1/0549/8073/9127/files/Compatibility_List_of_Pytes_ESS_and_Inverters.pdf) (Sunmart models over RS485 RJ45)
- [PowMr.md](PowMr.md) (identical map)
- SolarAssistant, [SRNE ABP RS485](https://solar-assistant.io/help/inverters/srne/ABP/rs485) (comparison pinout)
- `protocols/sunmart/sunmart.json`
- `protocols/sunmart/sunmart_holding_registry_map.csv`
