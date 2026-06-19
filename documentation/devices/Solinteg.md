# Solinteg — MQTT Integration Guide

> **Supported Models:** INTEG MHT-(4–50)K, MHS hybrids, connected grid meter  
> **Protocol:** Modbus RTU over RS485 · Modbus TCP  
> **Interface:** RS485 · built-in Ethernet  
> **Status:** Partial register map from public Modbus PDF

---

## Overview

Eniris documents Solinteg as using a **standardized protocol across all inverters** with both Modbus TCP and Modbus RTU ([Solinteg page](https://docs.eniris.io/en/Controller/Devices/PV-hybrid-and-battery-inverters/Solinteg)).

| Parameter | Default (Eniris) |
| --- | --- |
| Modbus RTU unit ID | 247 |
| Modbus TCP unit ID | 255 |

## Supported Protocols

| Model Family | Protocol Version | Notes |
| --- | --- | --- |
| INTEG MHT-(25–50)K | `solinteg_integ_mht_25_50_k` | Partial holding map |
| INTEG MHT-(4–20)K | `solinteg_integ_mht_25_50_k` | Same protocol family per Eniris |

## MPG Configuration

```ini
transport = modbus_rtu
port = /dev/ttyUSB0
baud = 9600
unit_id = 247
protocol_version = solinteg_integ_mht_25_50_k
```

## Sources

- Eniris Solinteg page: <https://docs.eniris.io/en/Controller/Devices/PV-hybrid-and-battery-inverters/Solinteg>
- Solinteg hybrid Modbus RTU protocol PDF: <https://eshop.helion.cz/user/related_files/solinteg_modbus_protocol_mht-25-50.pdf>
- `protocols/solinteg/solinteg_integ_mht_25_50_k.json`
- `protocols/solinteg/solinteg_integ_mht_25_50_k.holding_registry_map.csv`
