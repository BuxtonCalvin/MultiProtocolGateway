# InfluxDB Time-Shifted Data Import / Export Tool

This script provides a safe and flexible way to:

- Export time-series data from **InfluxDB**
- Modify or repair the data
- Re-insert the corrected data back into InfluxDB
- Import **EG4 inverter CSV exports** into an existing InfluxDB schema
- Shift timestamps to a new time range during insertion

It also includes **schema validation, field mapping, and type safety checks** to prevent accidental data corruption.

---

## Table of Contents

1. Overview
2. Key Features
3. Supported Workflows
4. Installation
5. Configuration
6. Usage
7. Schema Mapping Workflow (EG4 Imports)
8. Time Shifting Logic
9. Type Safety
10. Example Workflows
11. File Outputs
12. Safety Features
13. Troubleshooting

---

## Overview

The script converts data from two possible sources into a **uniform Python dictionary format** before inserting into InfluxDB.

Sources supported:

1. **InfluxDB Query Export**
2. **EG4 CSV Export**

Regardless of the source format, the script performs:

1. Field normalization
2. Optional field name mapping
3. Timestamp conversion
4. Timestamp shifting
5. Type validation
6. Batch insertion into InfluxDB

This allows historical data to be:

- Copied to another date
- Repaired
- Replayed into the database
- Imported from external sources

---

## Key Features

### Unified Data Processing

Both data sources are normalized into the same internal structure:

``` text
{
  measurement
  time
  fields
  tags
}
```

This ensures identical behavior regardless of source.

---

### Automatic Schema Validation

When importing **EG4 CSV data**, the script compares:

``` text
CSV column names
vs
InfluxDB field keys
```

If mismatches are detected, the script generates:

``` text
mapping_needed.csv
```

This allows the user to correct field mappings before insertion.

---

### Fuzzy Field Name Matching

The script attempts to automatically match CSV fields to InfluxDB fields using:

- normalization
- camelCase detection
- unit removal
- token matching
- similarity scoring

Example conversions:

``` text
pv1Voltage       -> pv1_voltage
AC Voltage (V)   -> ac_voltage
GridFrequencyHz  -> grid_frequency
TempC            -> temp_c
```

A confidence score determines whether a mapping suggestion is inserted automatically.

---

### Time Shifting

The script supports **time-range shifting** when inserting data.

Example use cases:

- Copy yesterday's inverter data to today
- Replay corrected data into the same time range
- Move historical data to a new date

Example configuration:

``` ini
SOURCE_START_TIME = 2026-03-09T06:00:00Z
TARGET_START_TIME = 2026-03-10T06:00:00Z
```

Every timestamp is shifted by:

``` code
time_delta = TARGET_START_TIME - SOURCE_START_TIME
```

---

### Field Type Safety

Before writing to InfluxDB the script verifies:

``` text
float fields receive floats
integer fields receive integers
boolean fields receive booleans
string fields receive strings
```

If a mismatch occurs:

``` text
[WARN] Type mismatch for field 'battery_voltage'
```

The value is skipped rather than corrupting the measurement.

---

## Supported Workflows

### Workflow 1 — Export → Edit → Reinsert Influx Data

``` text
InfluxDB
   ↓
CSV Export
   ↓
Manual Editing (Excel, etc.)
   ↓
Re-insert Corrected Data
```

Typical uses:

- Fix bad sensor values
- Replay corrected data
- Copy data to another date

---

### Workflow 2 — Import EG4 CSV Data

``` text
EG4 Web Interface
   ↓
CSV Export
   ↓
Schema Validation
   ↓
Field Mapping
   ↓
Insert into InfluxDB
```

---

## Installation

### Python Requirements

Python 3.10+

Install dependencies:

```bash
pip install pandas influxdb
```

---

## Configuration

All configuration occurs at the top of the script.

---

### Source Selection

``` ini
USE_INFLUX_OR_EG4_EXPORT = 'InfluxDB'
```

Options:

``` text
InfluxDB  -> export from database then reinsert
EG4       -> import EG4 CSV
```

---

### InfluxDB Connection

``` ini
HOST = 'xxx.xxx.xxx.xxx'
PORT = 8086
USER = 'User Name'
PASSWORD = 'password'
DATABASE = 'your db name
```

---

### Measurement

``` ini
MEASUREMENT = 'your measurement name'
```

The destination measurement in InfluxDB.

---

### Time Range

``` ini
SOURCE_START_TIME
SOURCE_END_TIME
TARGET_START_TIME
```

Example:

``` ini
SOURCE_START_TIME = '2026-03-09T06:09:15Z'
SOURCE_END_TIME   = '2026-03-09T16:53:15Z'
TARGET_START_TIME = '2026-03-10T06:09:15Z'
```

---

### Static Tags

Tags applied to every inserted point.

Example:

``` ini
STATIC_TAGS = {
  device_identifier
  device_name
  device_manufacturer
  device_model
  device_serial_number
  transport
}
```

These columns are automatically excluded from field processing.

---

### Timezone

Used only for EG4 imports.

``` ini
LOCAL_TIME_ZONE = 'America/Los_Angeles'
```

EG4 timestamps are converted:

``` ini
Local Time → UTC
```

before insertion.

---

## Usage

Run the script:

``` ini
python influx_import.py
```

Behavior depends on `USE_INFLUX_OR_EG4_EXPORT`.

---

### InfluxDB Export Workflow

#### Step 1

Set:

``` text
USE_INFLUX_OR_EG4_EXPORT = 'InfluxDB'
```

Run the script.

---

#### Step 2

The script exports data:

``` text
influx_data_export.csv
```

---

#### Step 3

Edit the CSV if desired.

Examples:

- fix incorrect sensor values
- remove bad rows
- modify fields

---

#### Step 4

Run the script again.

The script will:

1. Confirm deletion of existing points
2. Delete the source time range in the influxdb database
3. Insert corrected rows into the influxdb database

---

### EG4 Import Workflow

#### Step 1

Set:

``` text
USE_INFLUX_OR_EG4_EXPORT = 'EG4'
```

---

#### Step 2

Place the EG4 CSV export in the script directory. Make sure the name of the file is **EG4_data.csv**:

``` text
EG4_data.csv
```

---

#### Step 3

Run the script.

If schema differences exist it generates:

``` text
mapping_needed.csv
```

---

#### Step 4

Edit the mapping file.

Example:

``` csv
source,import_names,export_names,suggested_mapping,confidence_score
CSV0  ,fac         ,fac         ,fac              ,1.0
CSV0  ,feps        ,feps        ,feps             ,1.0
CSV0  ,peps        ,peps        ,peps             ,1.0
```

The easiest way to edit the csv mapping_needed.csv file is in MS Excel.  Open the file and:
  
  1. Review the auto mappings that are designated by an * in the suggested_mapping column. Cut any of the matching remaining names that match the import_names and paste the cut names into the export_names column.  Likewise, remove any names from the export_names column that do not match the import_names.

  2. After review, delete all **rows** where the "source" column equals "InfluxDB"

  3. Then delete the 1st, 4th and 5th **columns**.

---

#### Step 5

Save the corrected file as:

``` text
mapped_columns.csv
```

---

#### Step 6

Run the script again.

The data from teh EG4 export will now be inserted into InfluxDB.

---

## Time Shifting Logic

Each timestamp is adjusted using:

``` code
time_delta = TARGET_START_TIME - SOURCE_START_TIME
```

Every record becomes:

``` code
new_time = original_time + time_delta
```

This allows entire time ranges to be moved.

---

## Value Normalization

The script automatically converts values.

Examples:

``` text
0x1478  -> integer
25%     -> float
"123"   -> integer
"45.6"  -> float
""      -> None
```

---

## File Outputs

Generated files include:

``` text
influx_data_export.csv
mapping_needed.csv
mapped_columns.csv
```

---

## Safety Features

### User Confirmation

Before deleting data:

``` code
⚠ WARNING: This operation will permanently delete data.
Proceed with deletion? [y/n]
```

---

### Type Checking

Prevents field type conflicts.

---

### Mapping Validation

Prevents incorrect field insertion.

---

### Missing Column Warnings

``` code
[WARN] Unmapped CSV columns skipped:
```

---

## Troubleshooting

### Timestamp Column Missing

Error:

``` code
Timestamp column missing
```

Ensure CSV contains:

``` code
time
or
Time
```

---

### Mapping Not Found

If `mapped_columns.csv` is missing the script will regenerate:

``` text
mapping_needed.csv
```

---

### No Data Found

If export returns no rows:

``` code
[INFO] No data found in InfluxDB for the specified measurement.
```

Verify the time range.

---

## Summary

This tool allows safe manipulation of time-series data by providing:

- controlled export
- schema validation
- automatic field mapping
- timestamp shifting
- strict type checking
- safe reinsertion

It is particularly useful for:

- repairing bad historical data
- replaying corrected datasets
- importing EG4 inverter exports
- migrating time ranges in InfluxDB.

---
