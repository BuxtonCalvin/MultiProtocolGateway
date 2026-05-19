# Register Map Source References

This document lists every primary source used to build the CSV register map files
for all eight manufacturers. Sources are grouped by manufacturer and ordered by
authority (official manufacturer documentation first, then open-source and
community-verified sources).

---

## Victron Energy

### Modbus TCP (CCGX / Cerbo GX / Venus OS)

| Source | URL | Notes |
| --- | --- | --- |
| CCGX Modbus TCP Register List (authoritative live version) | <https://github.com/victronenergy/dbus_modbustcp/blob/master/CCGX-Modbus-TCP-register-list.xlsx> | Official Victron GitHub; master branch; always current |
| CCGX Modbus TCP Register List v2.60 (Scribd mirror) | <https://www.scribd.com/document/617872743/CCGX-Modbus-TCP-register-list-2-60> | Historical reference; unit ID mapping confirmed here |
| CCGX Modbus TCP Register List v3.00 (Scribd mirror) | <https://www.scribd.com/document/671009116/CCGX-Modbus-TCP-register-list-3-00> | 128-page register reference including all device services |
| Victron Modbus TCP FAQ | <https://www.victronenergy.com/live/ccgx:modbustcp_faq> | Official FAQ; unit ID table; enabling Modbus TCP |

### VE.Direct Serial Text Protocol

| Source | URL | Notes |
| --- | --- | --- |
| VE.Direct Protocol v3.34 (official PDF) | <https://www.victronenergy.com/upload/documents/VE.Direct-Protocol-3.34.pdf> | Primary protocol specification; all text-mode fields; checksum format; field tables for MPPT, BMV, Phoenix |
| VE.Direct Protocol FAQ | <https://www.victronenergy.com/live/vedirect_protocol:faq?do=export_pdf> | Connector pinout; baud rate; DTR/RTS power requirements; HEX mode notes |

### VE.Bus / MK3-USB

| Source | URL | Notes |
| --- | --- | --- |
| Victron D-Bus object path reference (Venus OS wiki) | <https://github.com/victronenergy/venus/wiki/dbus> | Authoritative D-Bus path ↔ register mapping; basis for all VE.Bus logical field names |
| kellerza/sunsynk GitHub (cross-reference for sign conventions) | <https://github.com/kellerza/sunsynk> | Used for VE.Bus sign convention cross-reference only |

---

## SMA Solar Technology

### SMA Modbus (Sunny Boy / Sunny Tripower / Sunny Island)

| Source | URL | Notes |
| --- | --- | --- |
| SMA Modbus Technical Information (SMA-Modbus-TI-en-10) | <https://files.sma.de/downloads/SMA-Modbus-TI-en-10.pdf> | Official SMA Modbus profile; unit ID default 3; register address scheme; data block format |
| SMA SunSpec Modbus Interface Technical Information | <https://kesko-onninen-pim-resources-production.s3-eu-west-1.amazonaws.com/pimdocuments/SunSpec_Modbus-TI-en-15.pdf> | SunSpec profile for SMA devices; unit ID 126 for SunSpec; register 43090 Grid Guard |
| SMA Data Manager Modbus Interface Technical Information | <https://files.sma.de/downloads/EDMx-Modbus-TI-en-16.pdf> | Data Manager M / SHM 2.0 aggregated Modbus registers; site-level power and energy |
| SMA Modbus Protocol Interface (product page) | <https://www.sma.de/en/products/product-features-interfaces/modbus-protocol-interface> | Official FAQ; max 4 TCP connections; NaN behavior; unit ID error code 11 explanation |

### SMA Sunny Island RS485

| Source | URL | Notes |
| --- | --- | --- |
| Sunny Island Modbus User Manual (SI-Modbus-BA-en-11) | <https://devicelib.my-gekko.com/download_docu.php?id_pk=212&ext=pdf&fname=Sunny+Island+Registerlist.pdf> | SI4.4M/SI6.0H/SI8.0H Modbus register assignment tables; power setpoint protocol; FIXn scaling format |
| Sunny Island Operating Manual SI4.4M/6.0H/8.0H (SI44M-80H-12-BE-en-13) | <https://files.sma.de/downloads/SI44M-80H-12-BE-en-13.pdf> | RS485 interface commissioning; Modbus function configuration section pp.113-114 |

### SMA Speedwire / Energy Meter

| Source | URL | Notes |
| --- | --- | --- |
| SMA WebBox Modbus Technical Description (WEBBOX-MODBUS-TB-en-19) | <https://files.sma.de/downloads/WEBBOX-MODBUS-TB-US-en-19.pdf> | Speedwire multicast address 239.12.255.254:9522; OBIS channel coding; unit ID architecture |

---

## Fronius

### SunSpec Modbus (Datamanager / GEN24)

| Source | URL | Notes |
| --- | --- | --- |
| Fronius Inverter Register Map Float v1.0 with Symo Hybrid Model 124 (Scribd) | <https://www.scribd.com/document/895381823/fronius-Inverter-Register-Map-Float-v1-0-with-SYMOHYBRID-MODEL-124> | Primary float model register map; models 111/112/113; IC124 storage control; Fronius-specific registers 212-217; site aggregate 500-510 |
| Fronius Datamanager Integer Register Map (forum-fronius.pl mirror) | <https://www.forum-fronius.pl/wp-content/uploads/asgarosforum/2812/Inverter_register_map.pdf> | Integer+SF model 101/102/103; nameplate IC120; basic settings IC121 |
| Fronius GEN24 Modbus TCP and RTU Operating Instructions (official HTML manual) | <https://manuals.fronius.com/html/4204102649/en-US.html> | Official GEN24 Modbus manual; scale factor auto-scaling warning; unit IDs; meter addressing; function codes |
| Fronius Datamanager 2.0 Modbus Quick Start Guide (official PDF) | <https://www.fronius.com/~/downloads/Solar%20Energy/User%20Information/SE_UI_Quick_Start_Guide_Modbus_RTU_DE_EN.pdf> | RTU commissioning; enabling Modbus on Datamanager 2.0 |

---

## SolarEdge

### SunSpec Modbus (All Inverters)

| Source | URL | Notes |
| --- | --- | --- |
| SolarEdge SunSpec Implementation Technical Note (knowledge-center, latest) | <https://knowledge-center.solaredge.com/sites/kc/files/sunspec-implementation-technical-note.pdf> | Primary reference; June 2025 version 3.2; register map tables 40001-40131; RS485 115200 bps default; SetApp configuration steps |
| SolarEdge SunSpec Technical Note v2.5 (Jan 2023, knowledge-center) | <https://knowledge-center.solaredge.com/sites/kc/files/2023-01/sunspec-implementation-technical-note.pdf> | Version 2.5; Synergy multi-phase address offsets (+50/+70); acc32 NaN value 0x00000000 note |
| SolarEdge SunSpec Technical Note (Loxone community mirror) | <https://api.library.loxone.com/downloader/file/2072/sunspec-implementation-technical-note.pdf> | Version 2.x; response time data; explicit vs non-explicit register addressing examples |
| SolarEdge SunSpec Technical Note (OpenEnergyMonitor community mirror) | <https://community.openenergymonitor.org/uploads/short-url/mxH1GpGasGWEbFg4zOL03mu2xxu.pdf> | Meter model (201/203) register tables; M_EVENT flags; energy quadrant definitions |
| StorEdge Monitoring and Control Application Note (knowledge-center) | <https://knowledge-center.solaredge.com/sites/kc/files/storedge_monitoring_and_control_using_non_solarege_gateways.pdf> | StorEdge battery registers 62836+; RS485 Expansion Kit; monitoring and control configuration |

---

## Deye / Sunsynk

### Modbus RTU RS485

| Source | URL | Notes |
| --- | --- | --- |
| kellerza/sunsynk GitHub — single_phase.py (primary authoritative source) | <https://github.com/kellerza/sunsynk/blob/main/src/sunsynk/definitions/single_phase.py> | Community-maintained open-source Home Assistant integration; most authoritative register address list; all decimal addresses with names and scaling verified against physical hardware |
| kellerza/sunsynk GitHub Issue #59 — New Modbus Protocol Document | <https://github.com/kellerza/sunsynk/issues/59> | Changelog of register address updates vs official PDF versions; register 62/181/274-279 corrections |
| Deye SUN Inverter Modbus Manual (Scribd) | <https://es.scribd.com/document/849692043/Deye-SUN-Inverter-Modbus-Manual> | Official Deye three-phase Modbus RTU protocol document; physical interface; frame format; error handling |
| Deye Modbus Registers PDF (Scribd community compilation) | <https://www.scribd.com/document/790942448/deye-modbus-registers-pdf> | Community register address compilation; cross-referenced against kellerza/sunsynk |
| DIY Solar Forum — Modbus comms with Deye inverter | <https://diysolarforum.com/threads/modbus-comms-with-deye-inverter.46197/> | Community validation of register addresses; sign convention confirmation |

---

## Sol-Ark

### Modbus RTU RS485

| Source | URL | Notes |
| --- | --- | --- |
| kellerza/sunsynk GitHub (shared register map) | <https://github.com/kellerza/sunsynk/blob/main/src/sunsynk/definitions/single_phase.py> | Sol-Ark uses identical Deye/Sunsynk register map; same source applies |
| Sol-Ark Battery Integration Guide SK140-0026-004 (official PDF) | <https://www.sol-ark.com/wp-content/uploads/2024/06/SK140-0026-004-EN-Manual.pdf> | Official Sol-Ark battery communications guide; RS485 pin configurations for 8K/12K/15K; CAN vs RS485 port selection; BMS integration |
| DIY Solar Forum — Turn on/off Deye/Sunsynk via Modbus | <https://diysolarforum.com/threads/turn-on-off-deye-sunsynk-hybrid-inverter-via-modbus-protocol.62239/> | Register 43 and 80 write confirmation; three-phase vs single-phase differences |

---

## Enphase

### SunSpec Modbus TCP (IQ Gateway)

| Source | URL | Notes |
| --- | --- | --- |
| Enphase AC-coupling with Victron Battery Inverters using Modbus TCP/IP Technical Brief | <https://enphase.com/en-gb/download/ac-coupling-victron-battery-inverters-using-modbus-tcpip-tech-brief> | Official Enphase document confirming SunSpec 700-range DIM write control; IQ Gateway Modbus TCP port 502; enabling steps via Installer App |
| Enphase IEEE 1547-2018 Compliance Updates (official) | <https://enphase.com/installers/resources/ieee-1547-2018> | Confirms SunSpec Modbus TCP support; IQ Gateway firmware 7.3.460+; DIM 700 range for interoperability |
| Node-RED flow — Accessing SunSpec with Modbus TCP (Enphase tested) | <https://flows.nodered.org/flow/a2cf93c63522556846b5558a32677515> | Community-verified Enphase IQ Gateway SunSpec Modbus TCP implementation; models 701/703/704 confirmed; production meter model 201/203 |
| SunSpec Alliance Modbus Specification (referenced by Enphase) | <https://sunspec.org/specifications/> | SunSpec model 101/102/103 inverter block; model 201/203 meter block; model 704 DER management; base register 40001 |

---

## SOK Battery

### PACE BMS Modbus RS485

| Source | URL | Notes |
| --- | --- | --- |
| PACE BMS Modbus Protocol for RS485 V1.3 (2017-06-27) — akkudoktor.net mirror | <https://akkudoktor.net/uploads/short-url/cFOQIt1XVL4EQGy5419TFv64dBE.pdf> | Primary PACE BMS P16S100A protocol specification; all register addresses; scaling factors; alarm bitmask definitions |
| PACE BMS Modbus Protocol V1.3 — syssi/esphome-pace-bms GitHub | <https://github.com/syssi/esphome-pace-bms/blob/main/docs/PACE-BMS-Modbus-Protocol-for-RS485-V1.3-20170627.pdf> | Identical document; second verified source; ESPHome implementation confirms register addresses against hardware |
| PACE BMS 16S Lithium Home Storage Specification — akkudoktor.net | <https://akkudoktor.net/uploads/short-url/8IIu2OquK38Jt9mmixM5whqkdt1.pdf> | 16S pack specification; dual RS485 interface; DIP switch address range 2-15; 9600 bps default |
| SOK SK48V100 Manual — SOK Battery NZ | <https://sokbattery.co.nz/wp-content/uploads/2023/08/SOK48v100-Manual.pdf> | Product-specific manual confirming PACE BMS P16S100A internal BMS; RJ45 RS485 pinout (pins 4/5/6); daisy-chain wiring |
