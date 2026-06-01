# APsystems ECU - MQTT Integration Guide

> **Supported Models:** APsystems ECU-R / ECU-C gateways with SunSpec Modbus enabled
> **Protocol:** SunSpec Modbus TCP or RTU
> **Interface:** Ethernet or RS485
> **Status:** Initial MPG SunSpec protocol available

---

## Overview

APsystems ECU gateways can expose microinverter telemetry through SunSpec Modbus. MPG includes `apsystems_ecu_sunspec`, a SunSpec-oriented read-only map for common gateway and inverter telemetry.

## Supported Protocols

| Model Family | Protocol Version | Transport |
| --- | --- | --- |
| ECU-R / ECU-C SunSpec Modbus | `apsystems_ecu_sunspec` | Modbus TCP or RTU |

## MPG Configuration

``` ini
transport = modbus_tcp
host = 192.168.1.50
port = 502
unit_id = 1
protocol_version = apsystems_ecu_sunspec
```

## Notes

- Enable SunSpec Modbus in EMA Manager / ECU APP.
- APsystems documentation states RTU uses RS485 and TCP uses Ethernet.
- For RTU, use 8 data bits, 1 stop bit, no parity, and the same baud configured in the ECU.

## Sources

- APsystems SunSpec Modbus Rev 3.0: <https://global.apsystems.com/wp-content/uploads/2023/03/SunSpec-Modbus_Rev3.0_2023-03-03.pdf>
- `protocols/apsystems/apsystems_ecu_sunspec.json`
