"""
Uniform Formatting: Whether the data comes from a SQL-like Influx query or a CSV row exported from the EG4 interface, it ends up as a Python dictionary.
The fields Filter: The line "field in STATIC_TAGS" ensures that if your CSV happens to have a column with a name such as device_name,
it doesn't get double-written as a field (since we are already defining it as a tag).

Time Shifting: Both sources get the same time_delta treatment, ensuring the "hourly range on a different day" logic is preserved.
The reason for this is that the time shifting logic is applied consistently to all data points, regardless of their source.
This way, you can maintain the integrity of your time series data while still allowing for the necessary adjustments to fit your
desired time range in InfluxDB.  So for example, if your need to copy a previous day's data to the current day, you can calculate the
time delta based on the original start time (copied data range) and the target start time (inserted data range).

Or if you have a mistake in your underlying Influxdb data, you can export that data to CSV, fix the metrics using Excel or similar,
and then re-import the corrected data back into InfluxDB with the same time shifting logic applied.

"""
import math
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, cast

import pandas as pd
from influxdb import InfluxDBClient  # type: ignore
from influxdb.resultset import InfluxDBClientError, ResultSet  # type: ignore

# --- CONFIGURATION ---
#----------------------------------------------------------------------------------------------------------
# ***Must*** configure Constants

# Set to 'InfluxDB' to export date range data from InfluxDB to a CSV -- for reinsert, or 'EG4' to insert from EG4 CSV export to influxdb.
USE_INFLUX_OR_EG4_EXPORT: str = 'EG4'  # options: 'InfluxDB' or 'EG4'

# Influxdb connect params
HOST, PORT, USER, PASSWORD, DATABASE = '10.17.2.42', 8086, 'KevinB', 'SoleSole12', 'openLuup'

# InfluxDB measurement name to write to (adjust as needed) ie:
# if your InfluxDB schema has a measurement (Influxdb Folder) called "device_data" where you want to insert the data, set MEASUREMENT = 'device_data'.
MEASUREMENT = 'device_data'

# Timezone of the source EG4 data (adjust as needed). InfluxDB expects UTC for inserts, so the script conditionally handles data
# extracted from the influxdb database by defaulting to UTC.  EG4 data is local time, so we apply the local timezone for the conversion to UTC.
LOCAL_TIME_ZONE = 'America/Los_Angeles'

# The time range in the source data that you want to import into InfluxDB.  This is used to calculate the time delta for shifting the timestamps.
# year-month-day T hour:minute:second   (source data is in local time, but will be converted to UTC for the InfluxDB query engine)
SOURCE_START_TIME = '2026-06-01T09:00:00'  # Original start time in the source data (adjust as needed) local time
SOURCE_END_TIME =   '2026-06-01T14:00:00'  # Original end time in the source data (adjust as needed) always greater or equal to start time local time
TARGET_START_TIME = '2026-06-01T09:00:00'  # Desired start time for data to be inserted into InfluxDB (adjust as needed)

if pd.to_datetime(SOURCE_END_TIME) < pd.to_datetime(SOURCE_START_TIME):
    raise ValueError("SOURCE_END_TIME must be >= SOURCE_START_TIME")

# Set to True to automatically convert integers to floats if InfluxDB encounters a type conflict.
# Set to False to strictly throw an exception and reject type mismatches.
ALLOW_FLOAT_COERCION = True

# Static Tags to be added to every point.  Adjust values as needed, but keep the keys consistent with your InfluxDB schema.
# If your CSV contains any of these columns, they will be ignored as fields and only the static tag value will be used.
STATIC_TAGS: dict[str, str] = {
    "device_identifier": '4066670074',
    "device_name": 'EG4_4066670074',
    "device_manufacturer": 'EG4',
    "device_model": '18KPV',
    "device_serial_number": '4066670074',
    "transport": 'transport.modbus_tcp'
}
#-----------------------------------------------------------------------------------------------------------------
# ***Optional*** configure Constants

# Base directory that contains the script for relative paths (adjust as needed)
BASE_PATH: Path = Path(__file__).parent

# Data Input file specifying mapped column definition data (if it exists).  If this file does not exist, the script will perform schema
# validation and create a mapping_needed.csv file for you to edit.  Using that mapping_needed.csv, delete the last "," per row to shift the suggested mapping
# that you want to keep, into the "export_names" column. An "*" represents an auto-inserted suggestion. Then delete all the rows at the bottom of the CSV with
# "InfluxDB" as the "source". Then delete the 1st and 4th columns leaving only the export_names and import_names columns. Save as "mapped_columns.csv", and
# restart the script. This allows you to resolve any anomalies in the schema between the CSV and InfluxDB fields.
IMPORT_MAP: Path = BASE_PATH / 'mapped_columns.csv'

# Path to CSV file that contains the EG4 interface export data.
EXPORT_FILE_FROM_EG4: Path = BASE_PATH / '2026-06-01.csv'

# Output file for data exported from InfluxDB with the same time shifting logic applied.  This can be used to copy data from one measurement
# to another within InfluxDB using the same mapping and time shifting logic.
EXPORT_FILE_FROM_INFLUX: Path = BASE_PATH / 'influx_data_export.csv'

EXPORT_FILE_FROM_INFLUX_EDIT: Path = BASE_PATH / 'influx_data_export_edit.csv'

# Output file for schema anomalies. Hand edit this file and save as mapped_columns.csv to resolve anomalies.
MAPPING_NEEDED_FILE: Path = BASE_PATH / 'mapping_needed.csv'

CONFIDENCE_THRESHOLD = 0.85  # Minimum confidence score for auto - inserting a mapping (adjust as needed)

# These are the fields that will be ignored when comparing CSV columns to InfluxDB fields during schema validation,
# and also ignored as fields when writing to InfluxDB (since they are either tags or reserved keywords).
IGNORE_TAGS: set[str] = set(STATIC_TAGS.keys()) | {"time", "Time", "measurement"}

# --- mapping dictionary ---
field_mapper: dict[str, str] = {}

# ----------END CONFIG-----------------------------------------------------------------------------------------------------

InfluxClient = InfluxDBClient(host=HOST, port=PORT, username=USER, password=PASSWORD, database=DATABASE)
influx_fields: dict[str, str] = {}


def normalize_metric_name(name: str) -> str:
    """
    Normalize metric names for fuzzy comparison.

    Handles:
        pv1Voltage        -> pv1_voltage
        AC Voltage (V)    -> ac_voltage
        BatStatus0_BMS    -> batstatus0_bms
        GridFrequencyHz   -> grid_frequency_hz
        TempC             -> temp_c
    """

    name = name.strip()

    # remove parentheses units
    name = re.sub(r"\(.*?\)", "", name)

    # convert camelCase → snake_case
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)

    # separate number/letter boundaries
    name = re.sub(r"([a-zA-Z])(\d)", r"\1_\2", name)
    name = re.sub(r"(\d)([a-zA-Z])", r"\1_\2", name)

    # normalize separators
    name = re.sub(r"[ \-]+", "_", name)

    # collapse duplicate underscores
    name = re.sub(r"_+", "_", name)

    # normalize known unit suffixes
    name = re.sub(r"(voltage|volt)$", "voltage", name, flags=re.I)
    name = re.sub(r"(current|amp|amps|ampere)$", "current", name, flags=re.I)
    name = re.sub(r"(frequency|hz)$", "frequency", name, flags=re.I)
    name = re.sub(r"(percent|pct|%)$", "percent", name, flags=re.I)

    return name.lower().strip("_")

def guess_mapping(csv_field: str, influx_candidates: set[str]) -> tuple[str, float]:
    """
    Finds the best Influx field name match and returns (name, confidence_score).
    Returns ("", 0.0) if no match meets the threshold.
    """
    normalized_csv: str = normalize_metric_name(csv_field)

    # Map normalized names back to original Influx names
    normalized_map = {
        normalize_metric_name(name): name
        for name in influx_candidates
    }

    best_match = ""
    best_score = 0.0
    cutoff = 0.55

    # Initialize matcher once with the target string
    matcher: SequenceMatcher[str] = SequenceMatcher(None, b=normalized_csv)

    for normalized_candidate in normalized_map.keys():
        matcher.set_seq1(normalized_candidate)

        # quick_ratio() is an efficient way to skip obvious non-matches
        if matcher.quick_ratio() >= cutoff:
            score = matcher.ratio()
            # bonus for shared tokens
            tokens_csv: set[str] = set(normalized_csv.split("_"))
            tokens_cand: set[str] = set(normalized_candidate.split("_"))
            score += 0.1 * len(tokens_csv & tokens_cand)

            if score >= cutoff and score > best_score:
                best_score = score
                best_match = normalized_map[normalized_candidate]

    return best_match, round(best_score, 3)

# Compare CSV columns to InfluxDB fields and identify anomalies such as missing fields or potential mapping issues.
# If any anomalies are found, a mapping_needed.csv file will be created with suggested mappings for user review and correction
# before proceeding with the data import.
def perform_schema_validation(csv_path: Path) -> None:
    global influx_fields

    print(f"--- Schema Validation: {csv_path} columns vs InfluxDB columns---")

    # 1. Get Fields from CSV
    df_sample: pd.DataFrame = pd.read_csv(csv_path, nrows=0)
    csv_cols: set[str] = set(df_sample.columns)

    # 2. Get Fields from InfluxDB
    results: ResultSet = cast(ResultSet, InfluxClient.query(f'SHOW FIELD KEYS FROM "{MEASUREMENT}"'))  # type: ignore
    influx_fields = { point["fieldKey"] : point["fieldType"] for point in results.get_points() if "fieldKey" in point and "fieldType" in point}  # type: ignore

    csv_fields: set[str] = csv_cols - IGNORE_TAGS

    anomalies: list[dict[str, str | float]] = []

    csv_missing: list[str] = []
    influx_missing: list[str] = []

    # CSV fields missing from Influx
    for f in csv_fields:
        if f not in influx_fields and f not in field_mapper:
            csv_missing.append(f)

    # Influx fields missing from CSV
    for f in influx_fields:
        if f not in csv_fields and f not in field_mapper.values():
            influx_missing.append(f)

    # Equal names
    for f in csv_fields:
        if f in influx_fields and f not in field_mapper:
            anomalies.append({
                "source": "CSV0",
                "import_names": f,
                "export_names": f,
                "suggested_mapping": f,
                "confidence_score": 1.0
            })

    # CSV anomalies with guesses
    for f in csv_missing:

        guess, score = guess_mapping(f, set(influx_missing))
        display_name: str = "*" if score >= CONFIDENCE_THRESHOLD else guess
        new_name: str = guess if score >= CONFIDENCE_THRESHOLD else ""

        anomalies.append({
            "source": "CSV1",
            "import_names": f,
            "export_names": new_name,
            "suggested_mapping": display_name,
            "confidence_score": score
        })

    # Influx anomalies
    for f in influx_missing:
        anomalies.append({
            "source": "InfluxDB",
            "import_names": "",
            "export_names": f,
            "suggested_mapping": "",
            "confidence_score": 0.0
        })


    if anomalies:
        anomalies.sort(key=lambda x: (str(x["source"]).lower(), str(x["import_names"] or "").lower()))
        anomaly_df = pd.DataFrame(anomalies)

        # export anomalies to CSV for user review and mapping
        anomaly_df.to_csv(MAPPING_NEEDED_FILE, index=False)
        print("\n[!] ***ANOMALIES found****")
        print(anomaly_df)
        print(f"\n[ACTION] A {MAPPING_NEEDED_FILE} has been created.")
        print(f"Please fill in the 'export_names' column, delete the influxdb rows, delete the first and 4th columns, and save as {IMPORT_MAP}. Then restart.")
        sys.exit() # Pause execution
    else:
        print("--- Schema Match Successful: No anomalies found between CSV and InfluxDB fields. ---")

def load_field_mapper(map_file: Path) -> dict[str, str]:
    """
    Load field_mapper from CSV if it exists.

    Dict format:
        import_names: export_names
    """
    if not map_file.exists():
        return {}

    df: pd.DataFrame = pd.read_csv(map_file)

    # Remove incomplete mappings using dropna on the relevant columns.  If any of these are missing, the mapping is not usable.
    df = df.dropna(subset=["import_names", "export_names"], how="any")

    mapper: dict[str, str] = dict(zip(df["import_names"], df["export_names"]))

    print(f"[INFO] Loaded {len(mapper)} field mappings from {map_file}")
    return mapper


def normalize_value(myVal: Any) -> int | float | str | None:
    """
    Normalize CSV values into types safe for InfluxDB.

    Handles:
    - hex strings (0x1478 → int)
    - percentages (25% → float)
    - numeric strings (123 → int, 45.6 → float)
    - empty values → None
    - everything else returned unchanged
    """

    if myVal is None:
        return None

    # Pandas may give NaN
    try:

        if isinstance(myVal, float) and math.isnan(myVal):
            return None
    except Exception:  # noqa: S110
        pass

    if isinstance(myVal, str):

        str_val: str = myVal.strip()

        if not str_val:
            return None

        # hex value
        if str_val.startswith(("0x", "0X")):
            try:
                return int(str_val, 16)
            except ValueError:
                return str_val

        # percentage
        if str_val.endswith("%"):
            try:
                return float(str_val[:-1])
            except ValueError:
                return str_val

        # integer
        if str_val.isdigit():
            try:
                return int(str_val)
            except ValueError:
                pass

        # float
        try:
            return float(str_val)
        except ValueError:
            return str_val

    return cast("int | float | str | None", myVal)

def coerce_to_influx_type(field_name: str, value: Any, influx_types: dict[str, str]) -> Any:
    """
    Attempt to coerce a value to the expected InfluxDB field type.

    Handles common EG4 export inconsistencies such as:
    - floats that represent percentages but should be integers
    - numeric codes that represent strings
    """

    expected: str | None = influx_types.get(field_name)

    if expected is None:
        return value

    try:

        if expected == "string":
            return str(value)

        if expected == "float":
            return float(value)

        if expected == "integer":

            # SOC style values 0.24 → 24
            if isinstance(value, float) and 0 <= value < 1:
                print(f"[INFO] Converting fractional value {value} → integer percentage for '{field_name}'")
                return int(round(value * 100))
            return int(value)

        if expected == "boolean":

            if isinstance(value, bool):
                return value

            if isinstance(value, (int, float)):
                return bool(value)

            if isinstance(value, str):
                v = value.lower().strip()

                if v in ("true", "1", "yes", "on"):
                    return True

                if v in ("false", "0", "no", "off"):
                    return False

    except (ValueError, TypeError):
        pass

    return value

def check_field_type(field_name: str, value: Any, influx_field_names: dict[str, str]) -> bool:
    """
    Prevent writing a value whose type conflicts with the
    existing InfluxDB field type.
    """

    if field_name not in influx_field_names:
        return True

    expected = influx_field_names[field_name]

    # Change: Python integers are acceptable, but they MUST be cast to floats
    if expected == "float" and not isinstance(value, (int, float)):
        return False

    if expected == "integer" and not isinstance(value, int):
        return False

    if expected == "boolean" and not isinstance(value, bool):
        return False

    if expected == "string" and not isinstance(value, str):
        return False

    return True

def load_influx_field_types() -> None:
    global influx_fields

    results: ResultSet = cast(
        ResultSet,
        InfluxClient.query(f'SHOW FIELD KEYS FROM "{MEASUREMENT}"')  # type: ignore
    )

    influx_fields = {
        p["fieldKey"]: p["fieldType"]
        for p in results.get_points()  # type: ignore
        if "fieldKey" in p and "fieldType" in p
    }
def confirm_action(message: str) -> bool:
    """
    Prompt user to confirm a destructive action.

    Returns True if user confirms, False otherwise.
    """
    while True:
        response: str = input(f"{message} [y/n]: ").strip().lower()

        if response in ("y", "yes"):
            return True
        if response in ("n", "no"):
            return False

        print("Please enter 'y' or 'n'.")

# function to delete points from InfluxDB in the specified time range for the given measurement.
# This can be used to clear out any existing data in the target time range before re-importing corrected data.
def delete_points_in_time_range() -> None:
    """
    Deletes points from InfluxDB in the specified time range for the given measurement.
    Converts local configuration times to UTC before executing the query.
    """
    # Convert local user strings to true UTC for the InfluxDB query engine
    start_utc = pd.to_datetime(SOURCE_START_TIME).tz_localize(LOCAL_TIME_ZONE).tz_convert("UTC").strftime('%Y-%m-%dT%H:%M:%SZ')
    end_utc = pd.to_datetime(SOURCE_END_TIME).tz_localize(LOCAL_TIME_ZONE).tz_convert("UTC").strftime('%Y-%m-%dT%H:%M:%SZ')

    query = f'DELETE FROM "{MEASUREMENT}" WHERE time >= \'{start_utc}\' AND time < \'{end_utc}\''  # noqa: S608

    print("\n⚠ WARNING: This operation will permanently delete data.")
    print(f"Measurement : {MEASUREMENT}")
    print(f"Time range (Local) : {SOURCE_START_TIME} → {SOURCE_END_TIME}")
    print(f"Time range (UTC)   : {start_utc} → {end_utc}\n")

    if not confirm_action("Proceed with deletion?"):
        print("Deletion cancelled by user.")
        sys.exit()

    InfluxClient.query(query)  # type: ignore
    print(f"Deleted points from {MEASUREMENT} between {SOURCE_START_TIME} and {SOURCE_END_TIME} (Local time equivalent).")

def export_influx_data_to_csv() -> None:
    """
    Function to query data from InfluxDB, apply the same time shifting logic, and export to CSV.
    """
    # Convert local user strings to true UTC for the InfluxDB selection filter
    start_utc: str = pd.to_datetime(SOURCE_START_TIME).tz_localize(LOCAL_TIME_ZONE).tz_convert("UTC").strftime('%Y-%m-%dT%H:%M:%SZ')
    end_utc: str = pd.to_datetime(SOURCE_END_TIME).tz_localize(LOCAL_TIME_ZONE).tz_convert("UTC").strftime('%Y-%m-%dT%H:%M:%SZ')

    results: ResultSet = cast(
        ResultSet,
        InfluxClient.query(  # type: ignore
            f'SELECT * FROM "{MEASUREMENT}" '  # noqa: S608
            f'WHERE time >= \'{start_utc}\' '
            f'AND time < \'{end_utc}\''
        )
    )
    points: list[dict[str, Any]] = list(results.get_points())  # type: ignore

    if not points:
        print("[INFO] No data found in InfluxDB for the specified measurement time window.")
        return

    # Calculate time delta for shifting the timestamps using naive local times
    time_delta: pd.Timedelta = pd.to_datetime(TARGET_START_TIME) - pd.to_datetime(SOURCE_START_TIME)

    processed_rows: list[dict[str, Any]] = []

    for point in points:
        # InfluxDB returns UTC time. Convert it to user's Local Time first
        influx_time_utc: pd.Timestamp = cast(pd.Timestamp, pd.to_datetime(point['time'])).tz_convert("UTC")
        influx_time_local: pd.Timestamp = influx_time_utc.tz_convert(LOCAL_TIME_ZONE).tz_localize(None)

        # Shift local time by the user's delta configuration
        new_time: pd.Timestamp = influx_time_local + time_delta

        # Build a new row with mapped fields
        new_row: dict[str, Any] = {'time': new_time.isoformat()}

        for field, value in point.items():
            if field in IGNORE_TAGS:
                continue
            new_row[field] = value

        processed_rows.append(new_row)

    # Export to CSV
    output_df = pd.DataFrame(processed_rows)
    output_df.to_csv(EXPORT_FILE_FROM_INFLUX, index=False)
    print(f"Successfully exported {len(processed_rows)} rows to {EXPORT_FILE_FROM_INFLUX}")


def write_csv_to_influx(export_file_name: Path) -> None:

    df: pd.DataFrame = pd.read_csv(export_file_name)

    # Determine if we need to skip mapping
    is_influx_source_csv = (export_file_name == EXPORT_FILE_FROM_INFLUX)

    # Calculate time delta for shifting the timestamps
    time_delta: pd.Timedelta = pd.to_datetime(TARGET_START_TIME) - pd.to_datetime(SOURCE_START_TIME)
    new_points: list[dict[str, Any]] = []
    missing_columns: set[str] = set()

    for row in df.to_dict("records"):

        time_val = row.get("Time") or row.get("time")

        if time_val is None:
            raise ValueError("Timestamp column missing (expected 'Time' or 'time')")

        ts: pd.Timestamp = cast(pd.Timestamp, pd.to_datetime(time_val))

        if USE_INFLUX_OR_EG4_EXPORT == "EG4":
            ts = ts.tz_localize(LOCAL_TIME_ZONE, nonexistent="shift_forward").tz_convert("UTC")
        else:
            ts = ts.tz_localize(LOCAL_TIME_ZONE, nonexistent="shift_forward").tz_convert("UTC")

        new_time: pd.Timestamp = (ts + time_delta).tz_localize(None)

        # Build Fields and apply Mapping
        fields: dict[str, int | float | str | bool] = {}

        for col, raw_val in row.items():

            if col in IGNORE_TAGS:
                continue

            target_field_name: str | None
            if is_influx_source_csv:
                target_field_name = str(col)
            else:
                target_field_name = field_mapper.get(str(col))

            if target_field_name is None:
                missing_columns.add(str(col))
                continue

            val: int | float | str | None = raw_val
            try:
                val = normalize_value(raw_val)
                val = coerce_to_influx_type(target_field_name, val, influx_fields)
            except (ValueError, TypeError):
                pass

            if val is None:
                continue

            # === DYNAMIC LIVE SCHEMA COERCION ===
            db_expected_type = influx_fields.get(target_field_name)

            if db_expected_type == "float":
                try:
                    val = float(val)
                except (ValueError, TypeError):
                    continue

            elif db_expected_type == "integer":
                try:
                    val = int(val)
                except (ValueError, TypeError):
                    continue
            # ====================================

            if not check_field_type(target_field_name, val, influx_fields):
                print(f"[WARN] Type mismatch for field '{target_field_name}', skipping value: {val}")
                continue

            fields[target_field_name] = val

        if not fields:
            continue

        if pd.isna(new_time):
            continue

        new_points.append({
            'measurement': MEASUREMENT,
            'time': new_time.isoformat(),
            'fields': fields,
            'tags': STATIC_TAGS
        })

    if missing_columns:
        print("[WARN] Unmapped CSV columns skipped:")
        for c in sorted(missing_columns):
            print(f"  - {c}")

    if new_points:
        print(f"[INFO] Attempting to write {len(new_points)} points to InfluxDB...")
        while True:
            try:
                InfluxClient.write_points(new_points, batch_size=1000)  # type: ignore
                print(f"Successfully updated {len(new_points)} points.")
                break # Success! Exit the loop.

            except InfluxDBClientError as e:
                # Check if this is a field type conflict error
                if ALLOW_FLOAT_COERCION and e.code == 400 and "field type conflict" in e.content:  # type: ignore
                    import re
                    match: re.Match[str] | None = re.search(r'input field \\?"([^\\"]+)\\?"', e.content)  # type: ignore

                    if match:
                        offending_field: str | Any = match.group(1)
                        print(f"[FIX] Detected InfluxDB type conflict on field '{offending_field}'. Dynamically forcing to float and retrying...")

                        for point in new_points:
                            if 'fields' in point and offending_field in point['fields']:
                                val = point['fields'][offending_field]
                                if isinstance(val, (int, float)) and not isinstance(val, bool):
                                    point['fields'][offending_field] = float(val)

                        continue  # Retry writing the corrected batch

                # If ALLOW_FLOAT_COERCION is False, or it's a non-coercible 400 error, crash out safely
                print(f"[ERROR] Database write failed. Coercion status: {ALLOW_FLOAT_COERCION}")
                raise
    return

# --- start execution ---

if USE_INFLUX_OR_EG4_EXPORT == 'InfluxDB':  # pyright: ignore[reportUnnecessaryComparison] -- USE_INFLUX_OR_EG4_EXPORT is a hand-edited config toggle; both branches are reachable across different runs, just not within one static analysis pass
    print("[INFO] Using InfluxDB export for data export...")
    # we already know the schemas match since we are exporting from InfluxDB, so we can skip the validation and just export the data for review and correction.
    if not EXPORT_FILE_FROM_INFLUX.exists():
        print(f"[INFO] Export file {EXPORT_FILE_FROM_INFLUX} not found. Creating export.")
        export_influx_data_to_csv()
        print(f"[INFO] Exported data from InfluxDB to {EXPORT_FILE_FROM_INFLUX}. Please review, edit and save this file as needed.")
        sys.exit()
    else:
        #-- If the export file already exists, we assume the user has reviewed and edited it as needed, and we can proceed with the import.
        print(f"[INFO] Export file {EXPORT_FILE_FROM_INFLUX} found. Proceeding with import to InfluxDB.")
        # Delete existing points in the time range before re-importing corrected data to avoid duplicates or conflicts.
        delete_points_in_time_range()
        load_influx_field_types()
        write_csv_to_influx(EXPORT_FILE_FROM_INFLUX)
else:
    # Load mapping file if it exists
    field_mapper = load_field_mapper(IMPORT_MAP)

    # If mapping file does not exist → run validation
    if not IMPORT_MAP.exists():
        print("[INFO] Mapping file not found. Running schema validation.")

        print("[INFO] Using EG4 CSV export for schema validation...")
        perform_schema_validation(EXPORT_FILE_FROM_EG4)

    else:
        print(f"[INFO] Mapping file {IMPORT_MAP} found. Skipping validation.")
        # --- Data processing (Continues only if no anomalies) ---
        load_influx_field_types()
        write_csv_to_influx(EXPORT_FILE_FROM_EG4)

