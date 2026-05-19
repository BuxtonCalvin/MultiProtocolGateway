# Bridge module for JSON output transport that writes data to a file or stdout, with optional pretty printing and metadata inclusion.
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import TextIO

from defs.common import TransportSettings, strtobool

from .transport_base import transport_base


class json_out(transport_base):
    ''' JSON output transport that writes data to a file or stdout '''
    output_file: str = "stdout"
    pretty_print: bool = True
    append_mode: bool = False
    include_timestamp: bool = True
    include_device_info: bool = True
    use_utc_timestamp: bool = True

    file_handle: TextIO | None = None

    def __init__(self, settings: TransportSettings):
        self.output_file = settings.get("output_file", fallback=self.output_file)
        self.pretty_print = strtobool(settings.get("pretty_print", fallback=self.pretty_print))
        self.append_mode = strtobool(settings.get("append_mode", fallback=self.append_mode))
        self.include_timestamp = strtobool(settings.get("include_timestamp", fallback=self.include_timestamp))
        self.use_utc_timestamp = strtobool(settings.get("use_utc_timestamp", fallback=self.use_utc_timestamp))
        self.include_device_info = strtobool(settings.get("include_device_info", fallback=self.include_device_info))

        super().__init__(settings)

    def connect(self):
        """Initialize the output file handle"""
        self._log.info("json_out connect")

        if self.output_file.lower() == "stdout":
            self.file_handle = sys.stdout
        else:
            try:
                # Get the root path (3 levels up from this file)
                project_root = Path(__file__).resolve().parent.parent.parent.parent

                # Force path to look relative by stripping leading slashes/drives
                clean_setting: str = self.output_file.lstrip("\\/")
                file_path: Path = project_root / clean_setting

                # Create folders and file if missing
                file_path.parent.mkdir(parents=True, exist_ok=True)
                if not file_path.exists():
                    file_path.touch()

                mode = "a" if self.append_mode else "w"
                self.file_handle = open(
                    file_path, mode, encoding="utf-8"
                )
                self.connected = True
            except Exception as e:
                self._log.error(f"Failed to open output file {self.output_file}: {e}")
                self.connected = False
                return

        self.connected = True

    def write_data(self, data: dict[str, int | float | str ], from_transport: transport_base) -> None:
        """Write data as JSON to the output file"""
        if not self.connected:
            return

        self._log.info(f"write data from [{from_transport.transport_name}] to json_out transport")
        self._log.info(data)

        # Prepare the JSON output structure
        output_data = {}

        # Add device information if enabled
        if self.include_device_info:
            output_data["device"] = {
                "identifier": from_transport.device_identifier,
                "name": from_transport.device_name,
                "manufacturer": from_transport.device_manufacturer,
                "model": from_transport.device_model,
                "serial_number": from_transport.device_serial_number,
                "transport": from_transport.transport_name
            }

        # Add timestamp if enabled
        if self.include_timestamp:
            if self.use_utc_timestamp:
                output_data["timestamp"] = str(time.time())
            else:
                output_data["timestamp"] = str(datetime.now().astimezone())

        # Add the actual data
        output_data["data"] = data

        # Convert to JSON
        if self.pretty_print:
            json_string: str = json.dumps(output_data, indent=2, ensure_ascii=False)
        else:
            json_string = json.dumps(output_data, ensure_ascii=False)

        # Write to file
        try:
            if self.output_file.lower() != "stdout":
                # For files, add a newline and flush
                if self.file_handle is not None:
                    self.file_handle.write(json_string + "\n")
                    self.file_handle.flush()
            else:
                # For stdout, just print
                print(json_string)
        except Exception as e:
            self._log.error(f"Failed to write to output: {e}")
            self.connected = False

    def init_bridge(self, from_transport: transport_base):
        """Initialize bridge - not needed for JSON output"""
        pass

    def __del__(self) -> None:
        """Cleanup file handle on destruction"""
        if self.file_handle and self.output_file.lower() != "stdout":
            try:
                self.file_handle.close()
            except Exception as e:
                self._log.error(f"Failed to cleanup file handle to output: {e}")
                pass
