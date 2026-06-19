# Sofar — MQTT Integration Guide

> **Supported Models:** Sofar G3 hybrids, HYD 3000–6000-ES, EV11k-AC-02 (charger)  
> **Protocol:** Modbus RTU over RS485  
> **Interface:** RS485 COM / logger port (model-specific)  
> **Status:** Stub protocols only — register maps pending vendor PDF

---

## Overview

Eniris documents RS485 support for Sofar G3 and HYD 3000–6000-ES hybrid storage inverters, plus the Sofar EV11k-AC-02 EV charger.

| Eniris device | MPG protocol_version | Category |
| --- | --- | --- |
| Sofar G3 hybrids | `sofar_g3` | PV/Hybrid inverter |
| Sofar HYD 3000–6000-ES | `sofar_hyd3000` | PV/Hybrid inverter |
| Sofar EV11k-AC-02 | `sofar_sofar_ev11k_ac_02` | EV charger |

## MPG Configuration (stub)

```ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 1
protocol_version = sofar_g3
```

Adjust `unit_id` and baud per your inverter menu — Sofar documentation varies by generation.

## Register map status

Public Sofar Modbus PDFs exist but are often mirrored, gated, or split by ARM firmware. See [Pending_Inverter_Register_Sources.md](Pending_Inverter_Register_Sources.md).

## Sources

- Eniris Sofar pages: <https://docs.eniris.io/en/Controller/Devices/PV-hybrid-and-battery-inverters/Sofar>
- `protocols/sofar/sofar_g3.json`
- `protocols/sofar/sofar_hyd3000.json`
