# KSTAR — MQTT Integration Guide

> **Supported Models:** BluePulse hybrid, BluE-S (1st gen), BlueSpark (2nd gen), KSG grid-tied  
> **Protocol:** Modbus RTU over RS485 · Modbus TCP on supported models  
> **Interface:** RS485 per Eniris device pages  
> **Status:** Stub protocols — register maps pending

---

## Overview

[Eniris KSTAR documentation](https://docs.eniris.io/en/Controller/Devices/PV-hybrid-and-battery-inverters/KSTAR) lists four RS485-capable product lines:

| Series | Eniris device type | MPG protocol_version |
| --- | --- | --- |
| BluePulse | BluePulse Hybrid Inverter | `kstar_bluepulse_hybrid_inverter` |
| BluE-S (1st gen) | BluE-S Single-Phase | `kstar_blue_series` |
| BlueSpark (2nd gen) | BlueSpark Single-Phase | `kstar_bluespark_series` |
| KSG | KSG Inverter | `kstar_ksg_series` |

## MPG Configuration (stub)

```ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 1
protocol_version = kstar_blue_series
```

## Register map status

Insufficient public register detail was captured in this pass. See [Pending_Inverter_Register_Sources.md](Pending_Inverter_Register_Sources.md).

## Sources

- Eniris KSTAR device pages under PV/Hybrid Inverters
- `protocols/kstar/` stub JSON files
