# MultiProtocolGateway — Architecture Diagrams

Mermaid diagrams for the Multi Protocol Gateway (MPG) data bridge. MPG reads solar/energy hardware over Modbus, CAN, and serial protocols, decodes register maps, and forwards telemetry to MQTT, TimescaleDB, InfluxDB, and JSON outputs. Configuration is managed through a FastAPI web UI on port 1717.

---

## 1. Flowchart — System Data & Configuration Flow

```mermaid
flowchart TD
    subgraph Hardware["Physical Layer"]
        INV["Solar Inverter / BMS / Meter"]
    end

    subgraph Config["Configuration Layer"]
        CFG["config.cfg<br/>(INI transport sections)"]
        PROTO["protocols/<br/>JSON + CSV register maps"]
        MASK["variable_mask / screen files"]
        STAGE[(SQLite Staging DB<br/>mpg_staging.db)]
        UI["FastAPI Web UI<br/>:1717"]
    end

    subgraph Gateway["Protocol_Gateway"]
        START([Start gateway])
        LOAD["Load config.cfg<br/>Instantiate transports"]
        WIRE["Wire scrapers → bridges<br/>via bridge= setting"]
        MODE{{"read_mode?"}}
        SEQ["Sequential<br/>(shared RS-485 bus)"]
        CONC["Concurrent<br/>(parallel connections)"]
        INTER["Interleaved<br/>(round-robin blocks)"]
        GROUP["ScrapeGroup<br/>one read per physical device"]
        SCHED["Scheduler loop<br/>read_interval cadence"]
    end

    subgraph Scrapers["Scraper Transports (Input)"]
        MB["modbus_rtu / tcp / tls / udp"]
        CAN["canbus"]
        SER["serial_pylon / serial_frame"]
        PACE["modbus_pace / EG4 variants"]
    end

    subgraph Decode["Protocol Decoding"]
        PS["protocol_settings"]
        REG["registry_map_entry CSV rows"]
        PROC["process_registry()<br/>decode bytes → typed values"]
        FILT["Variable filter<br/>mask / screen / failure tracker"]
    end

    subgraph Bridges["Bridge Transports (Output)"]
        MQTT["mqtt"]
        TS["timescaledb"]
        IN1["influxdb_out"]
        IN3["influxdb3_out"]
        JSON["json_out"]
    end

    subgraph External["External Systems"]
        BROKER["MQTT Broker"]
        HA["Home Assistant"]
        TSDB[(TimescaleDB / PostgreSQL)]
        INFLUX[(InfluxDB)]
        JFILE["JSON file"]
        GRAF["Grafana / Node-RED"]
        ALERT["Pushover / Telegram"]
    end

    %% Config flow
    CFG --> LOAD
    PROTO --> PS
    MASK --> FILT
    UI <-->|"stage edits"| STAGE
    STAGE -->|"commit"| CFG
    STAGE -->|"commit"| MASK
    UI -->|"preview diff"| STAGE

    %% Startup
    START --> LOAD --> WIRE --> MODE
    MODE --> SEQ & CONC & INTER
    SEQ & CONC & INTER --> SCHED

    %% Data flow
    INV -->|"Modbus / CAN / Serial"| MB & CAN & SER & PACE
    MB & CAN & SER & PACE --> GROUP
    GROUP --> PS
    REG --> PS
    PS --> PROC --> FILT
    FILT --> SCHED
    SCHED --> MQTT & TS & IN1 & IN3 & JSON

    MQTT --> BROKER --> HA
    TS --> TSDB --> GRAF
    IN1 & IN3 --> INFLUX --> GRAF
    JSON --> JFILE

    BROKER -.->|"MQTT writeback"| MB

    SCHED -.->|"connection loss / recovery"| ALERT
```

---

## 2. Sequence Diagram — Scrape Cycle & Bridge Output

```mermaid
sequenceDiagram
    autonumber
    participant Gateway as Protocol_Gateway.run()
    participant Group as ScrapeGroup
    participant Scraper as modbus_* / canbus / serial_*
    participant Fail as RegisterFailureTracker
    participant Proto as protocol_settings
    participant Filter as mask / screen filter
    participant Bridge as mqtt / timescaledb / influxdb_*
    participant Dest as External destination

    Gateway->>Gateway: Check read_interval elapsed
    Gateway->>Group: members_due(now)
    Group->>Scraper: read_group_data_iter(members)<br/>or read_data_iter()
    Scraper->>Scraper: connect() if disconnected
    Scraper->>Fail: skip disabled register ranges
    Scraper->>Scraper: read_modbus_registers()<br/>(batched, retried, bus-locked)
    Scraper->>Proto: process_registry(raw bytes)
    Proto->>Proto: Apply data_type, scale, enum _desc
    Proto-->>Scraper: decoded metrics dict
    Scraper-->>Group: TransportCycleResult + full_data

    loop "For each due member"
        Group->>Filter: _filter_for_member(full_data, member)
        Filter-->>Group: member_data
        Group->>Bridge: write_data(member_data, member)
        
        alt "MQTT bridge"
            Bridge->>Dest: publish per-topic / JSON blob
            Bridge->>Dest: HA auto-discovery (optional)
        else "TimescaleDB bridge"
            Bridge->>Dest: INSERT device_metrics_narrow / wide table
        else "InfluxDB bridge"
            Bridge->>Dest: batch line protocol write
        else "JSON bridge"
            Bridge->>Dest: append to output file
        end
        
        Group->>Group: mark_forwarded(member, now)
    end
```

### Sequence Diagram — Web UI Configuration Commit

```mermaid
sequenceDiagram
    autonumber
    participant Admin as Administrator
    participant UI as Web UI (HTMX)
    participant API as FastAPI Routers
    participant DB as SQLite Staging DB
    participant Scan as Scanner
    participant Diff as diff_engine
    participant Writer as config_writer
    participant Disk as config.cfg + CSVs + masks
    participant GW as Protocol_Gateway

    Admin->>UI: Open http://localhost:1717
    UI->>API: GET /api/devices/nav
    API->>DB: Query settings + connection status
    DB-->>UI: Dashboard (scrapers & bridges)

    Admin->>UI: Edit device setting / register toggle
    UI->>API: PATCH /api/devices/{name}/settings/{id}
    API->>DB: Update value_staged, mark is_dirty
    API->>DB: Update AppState dirty flags

    Admin->>UI: Click "Preview Changes"
    UI->>API: GET /api/commit/diff
    API->>Diff: build_diff()
    Diff->>DB: Compare staged vs disk
    Diff-->>UI: SettingDiff + ProtocolDiff preview

    Admin->>UI: Click "Commit All Changes"
    UI->>API: POST /api/commit
    API->>Writer: commit_all()
    Writer->>Disk: Write config.cfg
    Writer->>Disk: Write mask/screen/writable CSV overrides
    Writer->>DB: Sync value_disk, clear dirty flags
    Writer->>DB: Archive ConfigBackup

    Scan->>Disk: File watcher detects change
    Scan->>DB: Rescan and refresh staging state
    GW->>Disk: Reload config on next cycle
```

---

## 3. Class Diagram — Core Classes & Relationships

```mermaid
classDiagram
    direction TB

    class Protocol_Gateway {
        -transports: list~transport_base~
        -scrape_groups: dict
        -read_mode: str
        +run()
        +load_transports()
        +init_bridge()
        -_route_data_to_bridges()
    }

    class ScrapeGroup {
        +primary: transport_base
        +members: list~transport_base~
        +scrape_interval: float
        +members_due(now)
        +mark_forwarded()
    }

    class TransportState {
        +transport: transport_base
        +group: ScrapeGroup
        +completed_cleanly: bool
    }

    class transport_base {
        +protocolSettings: protocol_settings
        +transport_name: str
        +read_interval: float
        +bridges: list~transport_base~
        +connect()
        +read_data_iter()
        +write_data()
    }

    class modbus_base {
        +failure_tracker: RegisterFailureTracker
        +read_modbus_registers()
        +process_registry()
    }

    class RegisterFailureTracker {
        +record_failure()
        +is_disabled(range)
    }

    class modbus_rtu
    class modbus_tcp
    class modbus_tls
    class modbus_udp
    class modbus_pace
    class canbus
    class serial_pylon
    class serial_frame_transport
    class mqtt
    class timescaledb
    class influxdb_out
    class influxdb3_out
    class json_out

    class protocol_settings {
        +registry_maps: dict
        +process_registry()
        +load_protocol()
    }

    class registry_map_entry {
        +register_address: str
        +variable_name: str
        +data_type: str
        +write_mode: str
    }

    class Scanner {
        +scan_config()
        +sync_to_db()
    }

    class DiffResult {
        +settings: list~SettingDiff~
        +protocols: list~ProtocolDiff~
    }

    class MessageHandler {
        +send_message()
    }

    Protocol_Gateway "1" *-- "many" transport_base : manages
    Protocol_Gateway "1" *-- "many" ScrapeGroup : groups scrapers
    ScrapeGroup "1" o-- "many" transport_base : members

    transport_base <|-- modbus_base
    modbus_base <|-- modbus_rtu
    modbus_base <|-- modbus_tcp
    modbus_base <|-- modbus_tls
    modbus_base <|-- modbus_udp
    modbus_base <|-- modbus_pace
    transport_base <|-- canbus
    transport_base <|-- serial_pylon
    transport_base <|-- serial_frame_transport
    transport_base <|-- mqtt
    transport_base <|-- timescaledb
    transport_base <|-- influxdb_out
    transport_base <|-- influxdb3_out
    transport_base <|-- json_out

    modbus_base *-- RegisterFailureTracker
    transport_base *-- protocol_settings
    protocol_settings *-- registry_map_entry

    transport_base "scraper" --> "bridge" transport_base : bridge= config

    timescaledb *-- TimescaleDBConnectionManager
    timescaledb *-- BacklogManager
    timescaledb *-- RollupManager

    Scanner ..> Setting : syncs
    DiffResult ..> Setting : compares
    Protocol_Gateway ..> MessageHandler : alerts
```

---

## 4. Entity Relationship (ER) Diagram

### SQLite Staging Database (`mpg_staging.db`)

Used by the Web UI to stage configuration edits before committing to disk. Not used for telemetry storage.

```mermaid
erDiagram
    Setting {
        int id PK
        string section
        string key
        text value_disk
        text value_staged
        string transport_type
        boolean is_active
        boolean is_dirty
        boolean is_orphan
        datetime created_at
        datetime updated_at
    }

    ProtocolRegister {
        int id PK
        string protocol_group
        string protocol_name
        string registry_type
        string register_address
        string variable_name
        string write_mode_protocol
        boolean user_write_enabled
        boolean mask_enabled
        boolean screen_enabled
        boolean is_dirty
    }

    DeviceProtocolSelection {
        int id PK
        string device_name
        string protocol_name
        string registry_type
        string register_address
        boolean user_write_enabled
        boolean mask_enabled
        boolean screen_enabled
        boolean is_dirty
    }

    ConfigBackup {
        int id PK
        datetime created_at
        text filepath
        int file_size_bytes
        string trigger
        text notes
    }

    AppState {
        int id PK
        datetime last_scan_at
        datetime last_commit_at
        boolean has_dirty_settings
        boolean has_dirty_protocols
        boolean has_orphans
        int dirty_settings_count
        int dirty_protocols_count
        int orphan_count
        string scanner_status
    }

    SettingDescription {
        int id PK
        string key UK
        text transports
        text description
        text description_disk
        boolean is_dirty
    }

    ProtocolRegister ||--o{ DeviceProtocolSelection : "defines register for"
    Setting }o--|| AppState : "dirty flags tracked in"
    ProtocolRegister }o--|| AppState : "dirty flags tracked in"
```

### TimescaleDB Telemetry Schema (created by `timescaledb` bridge)

```mermaid
erDiagram
    ProtocolRegistry {
        int protocol_id PK
        text protocol_name UK
        text wide_table_name
        int metric_count
        text rollup_prefix
        boolean rollup_enabled
        boolean rollup_setup_complete
        datetime last_refresh_at
    }

    MetricCatalog {
        int catalog_id PK
        int protocol_id FK
        text metric_name
        text clean_column_name
        text data_type
        float unit_mod
        text notes
    }

    DeviceInfo {
        int device_info_id PK
        int protocol_id FK
        text device_identifier
        text device_serial_number
        text device_name
        text device_manufacturer
        text device_model
        text transport UK
    }

    DeviceMetricsNarrow {
        datetime m_time PK, FK
        int device_info_id PK, FK
        text metric_name PK
        float metric_value
        text metric_ascii
    }

    DeviceMetricsWide {
        datetime m_time PK, FK
        int device_info_id PK, FK
        float dynamic_metric_columns
    }

    ProtocolRegistry ||--o{ MetricCatalog : "has metrics"
    ProtocolRegistry ||--o{ DeviceInfo : "has devices"
    DeviceInfo ||--o{ DeviceMetricsNarrow : "time-series rows"
    DeviceInfo ||--o{ DeviceMetricsWide : "wide rows (metric_count &lt;= 200)"
    ProtocolRegistry ||--o{ DeviceMetricsWide : "creates per-protocol table"
    Application ||--o{ DeviceMetricsWide : "adds dynamic metric columns"
```

---

## 5. User Journey — MPG Administrator

```mermaid
journey
    title MultiProtocolGateway Administrator Journey
    section Install & Start
      Install Python deps or Docker image: 4: Administrator
      Connect hardware (RS-485 / Ethernet / CAN): 3: Administrator
      Start protocol_gateway.py or mpg.py: 5: Administrator
    section Initial Setup
      Open Web UI at localhost:1717: 5: Administrator
      View Dashboard scrapers and bridges: 4: Administrator
      Create Device via wizard: 4: Administrator
      Configure host port serial protocol_version: 3: Administrator
      Link scraper to bridge transport: 4: Administrator
    section Protocol Tuning
      Open Protocol Editor for register map: 3: Administrator
      Toggle mask screen write-enabled per register: 3: Administrator
      Run Live Analysis against hardware: 3: Administrator
      Review diff preview before commit: 5: Administrator
      Commit All Changes to config.cfg: 5: Administrator
    section Runtime Monitoring
      Gateway polls hardware on read_interval: 5: System
      Telemetry appears on MQTT or TSDB: 5: Administrator
      View live log stream in Web UI: 4: Administrator
      Check Grafana Home Assistant Node-RED dashboards: 5: Administrator
      Receive Pushover Telegram alert on disconnect: 4: Administrator
    section Maintenance
      Roll back config from backup: 4: Administrator
      Adjust read_mode for shared bus: 3: Administrator
      Manage TimescaleDB wide table columns: 3: Administrator
      Update protocol maps for new firmware: 3: Administrator
```

---

## Related Documentation

- [README](../../README.md) — project overview and quick start
- [Transports](../usage/transports.md) — scraper and bridge configuration
- [Protocols](../usage/protocols.md) — register map editing
- [TimescaleDB Bridge](../bridges/TimeScaleDB/timescaledb.md) — telemetry schema and queries
- [MQTT Bridge](../bridges/MQTT/MQTT_bridge.md) — publish topics and writeback
