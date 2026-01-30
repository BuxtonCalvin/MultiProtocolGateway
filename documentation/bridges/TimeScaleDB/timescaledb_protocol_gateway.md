# TimescaleDB Module for Python Protocol Gateway

## Overview

The TimescaleDB module is a **transform / sink transport** for the Python Protocol Gateway.  
Its primary responsibility is to:

- Receive telemetry data from an upstream scraper transport (e.g. Modbus TCP inverter)
- Persist time-series data into a **TimescaleDB (PostgreSQL)** backend
- Detect **stale data conditions**
- Trigger **automatic upstream reconnects** when data stops flowing
- Enable downstream visualization and analytics via **Grafana**

The module does **not** scrape data itself. Instead, it acts as a consumer of bridged data streams and focuses on persistence, monitoring, and reliability.

---

## Architecture Overview

```
[ Inverter / Device ]
          |
          v
[ Modbus / TCP Transport ]
          |
          v
[ Protocol Gateway ]
          |
          v
[ TimescaleDB Transport ]
          |
          v
[ TimescaleDB (Postgres) ]
          |
          v
[ Grafana ]
```

---

## What the TimescaleDB Module Does

### Core Responsibilities

- Converts incoming measurements into normalized rows
- Writes data into hypertables optimized for time-series workloads
- Maintains metadata for devices and variables
- Detects stale data conditions
- Requests upstream reconnects when stale data persists
- Backlog data during database outages and replay on recovery
- Provide Grafana-ready metrics for visualization

### Stale Data Handling

The module tracks:
- Time since last successful write
- Number of reconnect attempts
- Retry backoff interval

When data becomes stale:
1. A reconnect is requested from the Protocol Gateway
2. The upstream scraper transport is reset and a reconnection is tried
3. Scraping resumes automatically if the device is reachable


## 3. Data Flow Architecture

Inverter / Device
↓
Transport (modbus_tcp, serial, mqtt, etc.)
↓
Protocol Gateway
↓
TimescaleDB Module
↓
TimescaleDB (Hypertables)
↓
Grafana Dashboards
---
---

## 4. Database Schemas

### 4.1 Narrow Table (Recommended)

One row per metric per timestamp.

| Column | Description |
|------|-------------|
| m_time | Timestamp |
| device_info_id | Device identifier |
| metric | Metric name |
| value | Metric value |

**Benefits**
- Ideal for Grafana
- Flexible schema
- Efficient aggregation

---

### 4.2 Wide Table (Optional)

One row per timestamp with multiple metric columns.

| Column | Description |
|------|-------------|
| m_time | Timestamp |
| device_info_id | Device identifier |
| inverter_power | Example metric |
| grid_voltage | Example metric |

**Benefits**
- Faster inserts
- Easier CSV/SQL exports

## 6. Example SQL Queries

### 6.1 Power Over Time

```sql
SELECT
  time_bucket('1 minute', m_time) AS t,
  avg(value) AS power
FROM metrics_narrow
WHERE metric = 'inverter_power'
GROUP BY t
ORDER BY t;

6.2 Daily Energy Estimate
SELECT
  date_trunc('day', m_time) AS day,
  sum(value) * 1/60 AS kwh
FROM metrics_narrow
WHERE metric = 'inverter_power'
GROUP BY day;

6.3 Device Health (Last Seen)
SELECT
  device_info_id,
  max(m_time) AS last_seen
FROM metrics_narrow
GROUP BY device_info_id;

## Docker Compose Installation

### Images Used

- **TimescaleDB HA:** `timescaledb-ha:pg18`
- **Protocol Gateway:** `buxtoncalvin/pythonprotocolgateway:test`
- **Grafana:** `grafana/grafana:latest`

---

## Example docker-compose.yml

```yaml
version: "3.9"

services:
  timescaledb:
    image: timescale/timescaledb-ha:pg18
    environment:
      POSTGRES_PASSWORD: example
      POSTGRES_USER: postgres
      POSTGRES_DB: telemetry
    ports:
      - "5432:5432"
    volumes:
      - tsdb_data:/var/lib/postgresql/data

  protocol-gateway:
    image: buxtoncalvin/pythonprotocolgateway:test
    depends_on:
      - timescaledb
    volumes:
      - ./config.cfg:/app/config.cfg
    command: ["python", "protocol_gateway.py", "--config", "config.cfg"]

  grafana:
    image: grafana/grafana:latest
    ports:
      - "3000:3000"
    depends_on:
      - timescaledb

volumes:
  tsdb_data:
```
8. Grafana Setup

Open Grafana: http://localhost:3000

Login: admin / admin

Add data source:

Type: PostgreSQL

Host: timescaledb:5432

Database: metrics

User: postgres

Password: postgres

SSL: disabled

Enable TimescaleDB option

Create panels using SQL queries
---

## Configuration File

```ini
[logging]
level = INFO
rotation = weekly
backup_count = 4
console = true
```
9. TimescaleDB Module Configuration (config.cfg)
9.1 Example Configuration
[timescaledb]
enabled = true

host = timescaledb
port = 5432
database = metrics
username = postgres
password = postgres

# Schema options
wide_table = true
narrow_table = true

# Write behavior
batch_size = 100
flush_interval_seconds = 1

# Stale detection
stale_after_seconds = 300
max_stale_attempts = 3
retry_delay_minutes = 5

# Backlog
enable_persistent_storage = true
backlog_path = /data/backlog

# Notifications
enable_pushover = false
pushover_user_key =
pushover_api_token =
---



10. Configuration Options Explained
Option	Description
wide_table	Enable wide table writes
narrow_table	Enable narrow table writes
batch_size	Rows per DB transaction
flush_interval_seconds	Queue drain frequency
stale_after_seconds	Stale threshold
max_stale_attempts	Max reconnect attempts
retry_delay_minutes	Delay between retries
enable_persistent_storage	Disk backlog when DB down

11. Operational Behavior

Normal: continuous inserts, low latency

DB outage: data queued/backlogged

Transport failure: stale detection → reconnect

Recovery: backlog replay, counters reset

12. When to Use This Module

Use the TimescaleDB module when you need:

Reliable time-series persistence

Grafana-ready analytics

Automatic scraper recovery

Long-term historical analysis

Edge gateway resiliency

13. Summary

The TimescaleDB module provides a production-grade ingestion and monitoring layer that integrates cleanly with the Python Protocol Gateway. It is designed to be predictable, observable, and resilient — pairing naturally with inverter telemetry, industrial sensors, and edge data collection workloads.
## Summary

The TimescaleDB module provides:

- Reliable time-series persistence
- Automatic stale data detection
- Self-healing reconnect behavior
- First-class support for Grafana visualization
