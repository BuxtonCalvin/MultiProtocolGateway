# Kostal — MQTT Integration Guide

> **Supported Models:** Kostal Pico inverters (PIKO series per Eniris)  
> **Protocol:** Kostal Pico Modbus RTU · SunSpec on PIKO-CI family  
> **Interface:** COM1 RS485 (RJ45) · COM2 Modbus RTU meter port  
> **Status:** Stub protocol — use Kostal download-center PDF for your exact model

---

## Overview

Eniris supports Kostal Pico inverters via Modbus TCP and Modbus RTU ([Kostal device page](https://docs.eniris.io/en/Controller/Devices/PV-hybrid-and-battery-inverters/Kostal)).

Eniris wiring guidance:

- Assign a **unique Modbus address** to each inverter on the bus (default often **1**).
- Use lower addresses first for faster discovery on SmartgridOne controllers.
- COM1 = RS485 data logger / external master; COM2 = external energy meter (Modbus RTU).

Kostal PIKO-CI and related models publish SunSpec-style holding registers at 19200 baud, even parity on some SKUs — confirm in your interface description PDF.

## Supported Protocols

| Model Family | Protocol Version | Notes |
| --- | --- | --- |
| Kostal Pico (Eniris) | `kostal_kostal_pico_inverters` | Stub — map pending |

## MPG Configuration

```ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 19200
unit_id = 1
protocol_version = kostal_kostal_pico_inverters
```

Try `baud = 9600` if 19200 fails on older Pico units.

## Sources

- Eniris Kostal page: <https://docs.eniris.io/en/Controller/Devices/PV-hybrid-and-battery-inverters/Kostal>
- Kostal PIKO-CI Modbus interface PDF (SunSpec): <https://www.kostal-solar-electric.com/fileadmin/downloadcenter/kse/BA/Protokollbeschreibung/BA_PIKO-CI_KOSTAL-Interface-description-Modbus-Sunspec.pdf>
- `protocols/kostal/kostal_kostal_pico_inverters.json`
