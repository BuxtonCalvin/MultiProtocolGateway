# Afore Inverters - MQTT Integration Guide

> **Supported Models:** Afore AF-SL single-phase hybrid inverters (`afore_af_sl_hybrid`); Afore BNT string inverters, two-MPPT layout (`afore_bnt_t4`)
> **Protocol:** Modbus RTU over RS485
> **Interface:** Inverter RS485 port
> **Status:** Protocols available in MPG. Both maps follow public community definitions and have **not** been confirmed on hardware

---

## Table of Contents

1. [Overview](#1-overview)
2. [Supported Models & Protocol Versions](#2-supported-models--protocol-versions)
3. [Hardware Requirements](#3-hardware-requirements)
4. [Serial Settings & Addressing](#4-serial-settings--addressing)
5. [What the Maps Publish](#5-what-the-maps-publish)
6. [Word Order and Scaling](#6-word-order-and-scaling)
7. [Startup (Static) Registers](#7-startup-static-registers)
8. [MPG Configuration](#8-mpg-configuration)
9. [Troubleshooting](#9-troubleshooting)
10. [Verification Status](#10-verification-status)
11. [Not Mapped](#11-not-mapped)
12. [Source Documents](#12-source-documents)

---

## 1. Overview

Afore does not publish one Modbus map for the whole brand. Its hybrid inverters use the **T6** register layout (input registers in the 500, 1000, 2000 and 2500 ranges), while its older BNT string inverters use the **T4** layout (input registers 0-26, holding registers 0-15). The two layouts are unrelated, so MPG ships them as separate protocols.

Every row is read-only.

---

## 2. Supported Models & Protocol Versions

| Model | Protocol Version | Notes |
| --- | --- | --- |
| AF-SL / AF-SL-1 single-phase hybrids (for example AF3.6K-SL, AF5K-SL) | `afore_af_sl_hybrid` | Input registers only. Phase R only |
| BNT string inverters with two MPPT inputs (BNTxxxKTL-2mppt) | `afore_bnt_t4` | Input and holding registers |
| BNT string inverters with three MPPT inputs | none | The T4 PDF documents this layout, but PV3 adds two registers at 11-12 and moves every later input register up by two. Not mapped |

If you are not sure which family your unit belongs to, try `afore_af_sl_hybrid` first and check that `grid_voltage` is plausible. A BNT string inverter answers input registers 0-26 and rejects the 500+ range.

---

## 3. Hardware Requirements

- USB to RS485 adapter with A/B terminals (an isolated adapter is recommended for permanent installs), or a transparent RS485 to Ethernet bridge set to Modbus TCP <=> Modbus RTU
- Cable to the inverter's RS485 terminals
- Linux/Raspberry Pi host with a free USB port

On the hybrids the inverter's RS485 port must be switched to the **Modbus** protocol in the inverter menu before it answers (evcc documents this for the AF-SL).

---

## 4. Serial Settings & Addressing

| Setting | Value |
| --- | --- |
| Baud | 9600 |
| Data / parity / stop | 8 data bits, no parity, 1 stop bit |
| Modbus RTU address | 1 by default; 1-32 (T4 holding register 10) |
| Function codes | 04 for input registers, 03 for holding registers (T4 only) |
| Minimum query period | 1 s (T4 PDF) |

---

## 5. What the Maps Publish

### `afore_af_sl_hybrid` (input registers)

| Group | Registers | Notes |
| --- | --- | --- |
| Grid | `507`, `510`, `513`, `522`, `529`, `535` | Voltage, current, frequency, total output power, phase R power, total grid power |
| PV | `553`-`560` | Total PV power, PV1 and PV2 voltage, current and power |
| Daily energy | `1002`-`1007` | Grid export and import, load, PV |
| Total energy | `1014`-`1027`, `2011`-`2014` | Inverter, grid export and import, load, PV, battery charge and discharge |
| Battery | `2000`-`2010` | Status, temperature, SOC, voltage, current, power, daily charge and discharge |
| Inverter | `2500`, `2514`-`2515` | Running status, inner and radiator temperature |

Battery and running status names come from the code tables in `afore_af_sl_hybrid.json`.

### `afore_bnt_t4`

| Group | Registers | Notes |
| --- | --- | --- |
| Input: state | `0` | Inverter state flags |
| Input: grid | `1`-`6`, `11` | Three voltages, three currents, frequency |
| Input: PV | `7`-`10` | PV1 and PV2 voltage and current |
| Input: temperature | `12`-`13` | Module and housing temperature |
| Input: energy | `14`-`21` | Day energy, output power, day run time, total energy |
| Input: faults | `22`-`26` | Fault flags E01-E05, named in `afore_bnt_t4.json` |
| Holding: identity | `0`-`6` | DSP and HMI version, serial number, grid regulation |
| Holding: clock | `7`-`9` | Year, month, day, hour, minute, second |
| Holding: settings | `10`-`15` | Communication address, language, grid connection voltage and frequency limits |

---

## 6. Word Order and Scaling

- Values are big-endian. 32-bit values (`UINT` / `INT`) use two consecutive registers, high word first (ABCD), the MPG default, so no `Register_Endian` change is needed. The `afore_bnt_t4` day energy, output power, run time and total energy follow this, as do the hybrid power and energy counters.
- Several community definitions read only the **low word** of a 32-bit value. MPG reads both words, so the values agree until the counter passes 65,535 raw units.
- T4 energy is in Wh on the wire and is published as kWh.
- **T4 currents use 0.01 A.** The PDF lists 0.1 A, but the working Solarman definition for the BNT units uses 0.01 A, so MPG follows the working definition.
- T4 documents the three grid voltages as line voltages (Uab, Ubc, Uca); the Solarman definition publishes them as L1, L2 and L3 voltages. MPG uses the L1-L3 names.
- **Hybrid battery power is positive while discharging**, the opposite of the Deye and EG4 maps. The hybrid battery current and grid current signs are not confirmed, so check them against the inverter display.
- Temperatures are published without an offset. Neither source describes negative values.

---

## 7. Startup (Static) Registers

The `afore_bnt_t4` holding map reads DSP version, HMI version, serial number, grid regulation, communication address, language and the four grid connection limits **once per connection**. They change rarely, so they are not polled. Only the clock (holding registers 7-9) is polled.

The serial number is read as hexadecimal digits across holding registers 2-5. The PDF shows two characters per byte, which is not an ASCII layout, so compare the published value with the label on the inverter.

The hybrid map has no startup rows. No public definition documents a serial number, firmware or rated-power register for the T6 layout.

---

## 8. MPG Configuration

Follow the MultiProtocolGateway configuration guide:
<https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki/Configuration-Examples>

``` ini
[transport.afore]
transport = modbus_rtu
protocol_version = afore_af_sl_hybrid
port = /dev/ttyUSB0
baudrate = 9600
address = 1
read_interval = 15
bridge = transport.mqtt
device_manufacturer = Afore
device_model = AF5K-SL
```

For a BNT string inverter use `protocol_version = afore_bnt_t4` and your model name.

Both protocol files ask for a `batch_size` of 40 registers per read.

---

## 9. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| No response at all | RS485 port not switched to Modbus, A/B wires reversed, or wrong address | Set the port to Modbus in the inverter menu, swap A and B, try address 1 |
| Hybrid map times out but a BNT unit is connected | Wrong protocol family | Use `afore_bnt_t4` |
| Serial number is not readable | HEX layout is inferred | Compare with the inverter label and report the raw registers |
| Clock reads the wrong date | The PDF's read and write tables disagree on byte order | Compare with the inverter display and report the raw registers |
| Battery power sign looks inverted | Afore reports positive while discharging | Compare with the inverter display and apply an adjustment in the protocol editor |
| Energy counters jump | A definition reading only the low word wraps at 65,535 raw units | MPG reads both words |

---

## 10. Verification Status

The register addresses were cross-checked between the T4 PDF, the BNTxxxKTL-2mppt Solarman definition, the AF-SL Solarman definition and the evcc `afore-hybrid` template. **Neither map has been run against an Afore inverter by the maintainer.** Rows marked `INFERRED` in the hybrid CSV notes (the 32-bit pairing of totals that a community definition reads as one low word) and the T4 serial number and clock byte order need confirming. If you run this on a unit, please report which model and firmware you used.

---

## 11. Not Mapped

- The T6 holding registers (settings), which the public definitions do not document
- The T6 serial number, firmware and rated-power registers
- T4 writable settings (FC06): safety, time, address, language and grid limits
- T4 three-MPPT inverters (different register layout)
- Per-phase S and T values on the hybrid (single-phase models use phase R)

---

## 12. Source Documents

- Afore, *AFORE T4* Modbus communication protocol (PDF)
- Afore BNTxxxKTL-2mppt definition: <https://github.com/StephanJoubert/home_assistant_solarman>
- Afore AF-SL Solarman definition: <https://github.com/ozzzi/afore-power-flow-card>
- evcc, [Afore Hybrid Inverter](https://docs.evcc.io/en/meters/afore-hybrid-inverter) template
- `protocols/afore/afore_af_sl_hybrid.json`
- `protocols/afore/afore_af_sl_hybrid.input_registry_map.csv`
- `protocols/afore/afore_bnt_t4.json`
- `protocols/afore/afore_bnt_t4.input_registry_map.csv`
- `protocols/afore/afore_bnt_t4.holding_registry_map.csv`
