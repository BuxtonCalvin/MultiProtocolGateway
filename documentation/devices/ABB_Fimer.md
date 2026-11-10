# ABB / FIMER Solar Inverters - MQTT Integration Guide

> **Supported Models:** ABB/FIMER TRIO-5.8/8.5, TRIO-20.0/27.6, TRIO-50.0/60.0 (TL and TM), legacy ABB/Power-One SunSpec inverters (PVI, UNO, TRIO, Central, Ultra), FIMER REACT2, VSN300/VSN700 logger cards
> **Protocol:** Modbus RTU over RS485 (native TRIO / legacy SunSpec) · Modbus TCP (REACT2, VSN300/VSN700)
> **Interface:** Inverter RS485 port, or Ethernet/Wi-Fi logger card
> **Status:** Seven protocols available in MPG, most built directly from vendor documents; write controls are mapped on some but should be treated with care
> **ABB energy meters** (B21/B23/B24) are covered in [ABB.md](ABB.md).

---

## Table of Contents

1. [Overview](#1-overview)
2. [Supported Models & Protocol Versions](#2-supported-models--protocol-versions)
3. [Choosing a Protocol](#3-choosing-a-protocol)
4. [Hardware Requirements](#4-hardware-requirements)
5. [Inverter Setup](#5-inverter-setup)
6. [MPG Configuration](#6-mpg-configuration)
7. [Register Map Notes](#7-register-map-notes)
8. [Troubleshooting](#8-troubleshooting)
9. [Source Documents](#9-source-documents)

---

## 1. Overview

ABB sold its solar inverter business to FIMER in 2020, and the Power-One (Aurora) designs it inherited are still widely installed. Several incompatible Modbus interfaces exist across the range, so MPG ships a separate protocol for each:

- **Native RS485 maps** for the TRIO families, documented by ABB as "Modbus RTU Register Map" PDFs. Measurements are input registers (function 04); the holding map carries a heartbeat, grid measures and the power-management controls.
- **SunSpec maps** for legacy inverters with native SunSpec Modbus, for the REACT2 storage inverter and for inverters behind a VSN300/VSN700 logger card.

The two SunSpec protocols that read a model chain (`abb_fimer_react2_sunspec` and `abb_fimer_vsn300_sunspec`) use MPG's [dynamic register discovery](../architecture/dynamic_register_discovery.md). Rows are anchored on the SunSpec models the device actually reports, and fall back to defaults captured from real hardware, so a changed firmware layout is followed rather than read at wrong addresses.

---

## 2. Supported Models & Protocol Versions

| Device | Protocol Version | Transport | Default Serial / Port |
| --- | --- | --- | --- |
| TRIO-5.8 / 8.5-TL-OUTD | `abb_fimer_trio_5_8_8_5` | `modbus_rtu` | 9600 8N1 |
| TRIO-20.0 / 27.6-TL-OUTD | `abb_fimer_trio_20_27` | `modbus_rtu` | 19200 8N1 (EU), 9600 (US/JP) |
| TRIO-50.0 / 60.0-TL-OUTD | `abb_fimer_trio_50_60` | `modbus_rtu` | 19200 8N1 |
| TRIO-TM-50.0-400 / TM-60.0-480 | `abb_fimer_trio_tm_50_60` | `modbus_rtu` | 19200 8N1 (adds DC inputs 2 and 3) |
| Legacy ABB/Power-One with native SunSpec (PVI 10/12K, TRIO, Central, Ultra) | `abb_fimer_sunspec` | `modbus_rtu` | 9600; slave id is the inverter address |
| FIMER REACT2 | `abb_fimer_react2_sunspec` | `modbus_tcp` | Port 502, SunSpec base 40000, unit 1 |
| Any inverter behind VSN300 / VSN700 | `abb_fimer_vsn300_sunspec` | `modbus_tcp` | Port 502, SunSpec base 0, unit 2 |

Row counts: TRIO-5.8/8.5 has 52 input and 32 holding rows; TRIO-20/27.6 has 78 input and 35 holding; TRIO-50/60 has 40 input and 38 holding; TRIO-TM has 46 input and 5 holding; `abb_fimer_sunspec` has 84 rows; REACT2 has 236 rows; VSN300 has 60 rows.

---

## 3. Choosing a Protocol

| If you connect to... | Use |
| --- | --- |
| The inverter's own RS485 port, TRIO-5.8/8.5 | `abb_fimer_trio_5_8_8_5` |
| The inverter's own RS485 port, TRIO-20.0/27.6 | `abb_fimer_trio_20_27` |
| The inverter's own RS485 port, TRIO-50/60 TL | `abb_fimer_trio_50_60` |
| The inverter's own RS485 port, TRIO-TM-50/60 | `abb_fimer_trio_tm_50_60` |
| A native-SunSpec legacy inverter (marker `SunS` at registers 0-1) | `abb_fimer_sunspec` |
| A REACT2 reached natively (SunSpec base 40000) | `abb_fimer_react2_sunspec` |
| A VSN300/VSN700 logger card (SunSpec base 0), including one fitted to a REACT2 bundle | `abb_fimer_vsn300_sunspec` |

Do **not** mix these up. A logger serves SunSpec models 1, 103, 160 and then 120/121/123, so it only matches the legacy native map up to register 171, and registers beyond that return unrelated data. A TL inverter answers the TM-only DC inputs 2 and 3 with exception 2, which is why the TL and TM maps are separate.

---

## 4. Hardware Requirements

### RS485 (TRIO and legacy SunSpec)

- USB to RS485 adapter (isolated recommended) or RS485-to-Ethernet bridge
- Twisted-pair cable to the inverter's RS485 terminals or RJ45 port
- Only the RS485 port set to Modbus is reachable; see below

### Ethernet / Wi-Fi (REACT2, VSN300/VSN700)

No extra hardware is needed. The logger card serves Modbus TCP on port 502 once enabled.

---

## 5. Inverter Setup

### TRIO-50/60 and TRIO-TM

- Both RS485 ports default to the **Aurora** protocol. Change one to **Modbus RTU Slave** with ABB's *Aurora Manager LITE* software (installer-level access is required).
- Protocol, slave ID, baud and parity are all set there. Defaults: 19200 baud, 8 data bits, 1 stop bit, no parity, minimum timeout 100 ms.
- **Warning:** writing 0 to holding registers 1005 or 1006 switches that RS485 port away from Modbus immediately. The gateway can then no longer reach the inverter until you restore it with Aurora Manager Lite.
- Minimum inverter firmware is 1711C (EU), 1711D (US) or 1708B (JP).

### TRIO-20.0/27.6 and TRIO-5.8/8.5

- On the TRIO-20.0/27.6, a third-party installation guide (Elum Energy) describes setting the address under the **Address** menu, then setting the **PMU RS485** protocol to Modbus RTU and the baud rate. The TRIO-5.8/8.5 menus are likely similar but this was not confirmed; consult the inverter manual.
- ABB's register documents give 8 data bits, 1 stop bit, no parity and 2400-115200 baud, with a default of 19200 (EU 20/27.6), 9600 (US/JP 20/27.6) or 9600 (5.8/8.5).

### VSN300 / VSN700

- Enable the Modbus TCP server in the logger's web interface. The card translates SunSpec requests to the proprietary Aurora protocol and answers from cached data.
- The inverter unit ID depends on the setup: on VSN300 it is commonly **2**; some setups use 254 or 247. If one ID gets no response, try the others.

### REACT2

- Modbus TCP on port 502 with SunSpec base 40000 and unit 1 by default (register 40068 holds the device address). Minimum inverter firmware 2022C is noted in the REACT2 SunSpec conformance statement.

---

## 6. MPG Configuration

Follow the MultiProtocolGateway Modbus RTU to MQTT configuration guide:
<https://github.com/BuxtonCalvin/MultiProtocolGateway/wiki/Configuration-Examples#modbus-rtu-to-mqtt>

### TRIO over RS485

``` ini
[transport.trio_50]
transport = modbus_rtu
protocol_version = abb_fimer_trio_50_60
port = /dev/ttyUSB0
baudrate = 19200
address = 2
read_interval = 15
bridge = transport.mqtt
device_manufacturer = ABB
device_model = TRIO-50.0-TL-OUTD
```

### Inverter behind a VSN300

``` ini
[transport.vsn300_inverter]
transport = modbus_tcp
protocol_version = abb_fimer_vsn300_sunspec
host = 192.168.1.80
port = 502
address = 2
read_interval = 15
bridge = transport.mqtt
```

### REACT2

``` ini
[transport.react2]
transport = modbus_tcp
protocol_version = abb_fimer_react2_sunspec
host = 192.168.1.81
port = 502
address = 1
read_interval = 15
bridge = transport.mqtt
```

Set the TRIO slave ID (`address`) to the value programmed in Aurora Manager Lite or the inverter menu.

---

## 7. Register Map Notes

- **Addressing:** ABB's TRIO tables list 1-based Modbus data addresses and state that the on-wire address is one less. Every CSV register is the table address minus 1, and each row's note shows the original table address.
- **Float word order (all TRIO protocols):** ABB defines its floats ("SF32") as word-swapped, low register first, so every `FLOAT32` row in the TRIO CSVs carries `Register_Endian` `little_endian-CDAB` (checked: 10 + 48 + 10 + 16 + 9 + 20 + 4 + 22 float rows across the eight TRIO files, none without it). ABB's example: 20000 W is returned as registers `0x4000 0x469C`. The TRIO-5.8/8.5 and 20/27.6 documents only say "IEEE 32 Float", so for those two families the same word order is applied as a family-wide convention.
- **32-bit integer counters:** the `UINT` energy counters (TRIO-5.8/8.5, 50/60 and TM) are read high register first with no adjustment, as ABB's documents state. This differs from the floats. If an energy counter looks wrong (for example huge or tiny values), try adding `little_endian-CDAB` to those `UINT` rows.
- **Energy counters:** ABB's documents are inconsistent between float and U32. TRIO-5.8/8.5 uses U32 (high register first, assumed), and TRIO-20/27.6 follows the document as written (float). If energy values are absurd, change the six rows to `UINT` with no adjustment.
- **Reads must start and end on mapped registers.** The inverter answers a read that begins or ends on a reserved register, or that splits a 32-bit value or string, with exception 2. Reads that merely span reserved registers are fine.
- **SunSpec scaling:** REACT2 and VSN300 apply the `Context` adjustment, which reads the scale-factor register beside each value, so a changed scale factor is followed automatically. The legacy native map uses ABB's fixed factors (A_SF=-2, V_SF=-2, W_SF=0, Hz_SF=-2, WH_SF=0, Tmp_SF=-1).
- **Writable registers:** the TRIO holding maps mark ABB's documented controls `RW` with ABB's range for validation (power management block 1 at holding 189-226, block 2 at 500-515). Set points revert at inverter power-off. Configure registers 500-505 before sending a block-2 set point. Whether anything is written is decided by the transport's `write_enabled` setting and the per-register toggle in the web UI.
- **VSN300 writes are unproven.** The integration author found that the card stores the limit and enable flag but answers every write of the enable flag with exception 7 and the inverter keeps producing at full power.
- **Not covered:** writable controls and set points in the TRIO input maps, grid-support models 126-145 on REACT2 (several hundred curve and trip-limit points), vendor model 65232, string-combiner model 403 and TRIO auxiliary-board serial and part numbers.

---

## 8. Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| No response on TRIO RS485 | Port still on the Aurora protocol | Switch the port to Modbus RTU Slave with Aurora Manager Lite |
| Exception 2 on reads | A read starts/ends on a reserved register | Use the shipped map, which starts and ends on mapped registers; lower `batch_size` if you added rows |
| Float values read `1e-30` or `1e+30` | Word order not applied | Check the float rows still carry `little_endian-CDAB` |
| Energy counters absurd | Float vs U32 mismatch, or word order | See the counters notes above; try `little_endian-CDAB` on `UINT` rows |
| VSN300 answers only one unit ID | Logger translates for the connected inverter | Use unit ID 2 (or 254/247) as shown in the logger |
| SunSpec data only valid up to register 171 | Wrong protocol for a logger | Use `abb_fimer_vsn300_sunspec` rather than `abb_fimer_sunspec` |
| Connection lost after a write | Wrote 0 to register 1005 or 1006 | Restore Modbus with Aurora Manager Lite |

---

## 9. Source Documents

- `protocols/abb_fimer/` (JSON and CSV files; header comments in each CSV record the source and caveats)
- ABB/FIMER "Modbus RTU Register Map" PDFs: TRIO-5.8/8.5 (GB00, 2015), TRIO-20.0/27.6 (GT05, 2016), TRIO-50.0/60.0 and TRIO-TM (Gt03.0, 2019)
- ABB/Power-One "SunSpec Inverter Modbus Map" workbook (2013)
- FIMER REACT2 SunSpec PICS (Rev 003)
- FIMER VSN300 user manual (Modbus TCP gateway and SunSpec adapter)
- FIMER Aurora Manager LITE product manual: <https://www.fimer.com/sites/default/files/FIMER_AURORA-Manager-LITE-ProductManual_Rev_A_EN_0.pdf>
- Home Assistant integration for ABB/Power-One/FIMER inverters: <https://github.com/alexdelprete/ha-abb-powerone-pvi-sunspec>
- Eniris SmartgridOne ABB/FIMER page: <https://docs.eniris.be/en/Controller/Devices/PV-hybrid-and-battery-inverters/ABB-FIMER>
- [SunSpec dynamic register discovery](../architecture/dynamic_register_discovery.md)
