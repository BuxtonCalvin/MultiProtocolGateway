# GoodWe — MQTT Integration Guide

> **Supported Models:** GoodWe energy storage / hybrid inverters (Eniris: Energy Storage family)  
> **Protocol:** Modbus RTU over RS485 · Modbus TCP on supported firmware  
> **Interface:** EMS / RS485 port (RJ45 adapter common)  
> **Status:** Partial register map — verify against your ARM/DSP firmware document

---

## Overview

GoodWe hybrid and energy-storage inverters expose telemetry via standard Modbus RTU. Eniris ([SmartgridOne GoodWe page](https://docs.eniris.io/en/Controller/Devices/PV-hybrid-and-battery-inverters/GoodWe)) documents RS485 wiring for the Energy Storage product line.

Factory defaults from GoodWe protocol documentation:

| Parameter | Default |
| --- | --- |
| Baud rate | 9600 |
| Unit ID | 247 (0xF7) |
| Function codes | 0x03 read, 0x06 single write, 0x10 multi write |

> Register addresses differ between ET/EH/BT/BH/HV families and firmware revisions. Use the PDF matching your inverter series before enabling writes.

## Supported Protocols

| Model Family | Protocol Version | Notes |
| --- | --- | --- |
| Energy storage / hybrid (Eniris RS485) | `goodwe_energy_storage` | Initial telemetry map from GoodWe 2024 hybrid protocol PDF |

## MPG Configuration

```ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 247
protocol_version = goodwe_energy_storage
```

## Wiring

- Connect RS485 A/B per GoodWe EMS port pinout (often RJ45 via USB–RS485 dongle).
- Inverter must be powered (DC or AC) before Modbus responds.
- Swap A/B at the adapter if you see consistent timeouts.

## Sources

- Eniris GoodWe device page: <https://docs.eniris.io/en/Controller/Devices/PV-hybrid-and-battery-inverters/GoodWe>
- GoodWe Modbus Hybrid protocol (April 2024): community PDF mirror linked from `Pending_Inverter_Register_Sources.md`
- `protocols/goodwe/goodwe_energy_storage.json`
- `protocols/goodwe/goodwe_energy_storage.holding_registry_map.csv`
