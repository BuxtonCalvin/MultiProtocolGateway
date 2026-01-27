#!/usr/bin/env python3
"""
Main module for Growatt / Inverters ModBus RTU data to MQTT
"""


import importlib
import sys
import threading
import time

# Check if Python version is greater than 3.9
if sys.version_info < (3, 9):
    print("==================================================")
    print("WARNING: python version 3.9 or higher is recommended")
    print("Current version: " + sys.version)
    print("Please upgrade your python version to 3.9")
    print("==================================================")
    time.sleep(4)


import argparse
import logging
import logging.handlers
import sys
from configparser import ConfigParser, NoOptionError
from pathlib import Path

from classes.protocol_settings import protocol_settings, registry_map_entry
from classes.transports.transport_base import transport_base

__logo = """

██████╗ ██╗   ██╗████████╗██╗  ██╗ ██████╗ ███╗   ██╗
██╔══██╗╚██╗ ██╔╝╚══██╔══╝██║  ██║██╔═══██╗████╗  ██║
██████╔╝ ╚████╔╝    ██║   ███████║██║   ██║██╔██╗ ██║
██╔═══╝   ╚██╔╝     ██║   ██╔══██║██║   ██║██║╚██╗██║
██║        ██║      ██║   ██║  ██║╚██████╔╝██║ ╚████║
╚═╝        ╚═╝      ╚═╝   ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═══╝

██████╗ ██████╗  ██████╗ ████████╗ ██████╗  ██████╗ ██████╗ ██╗          ██████╗  █████╗ ████████╗███████╗██╗    ██╗ █████╗ ██╗   ██╗
██╔══██╗██╔══██╗██╔═══██╗╚══██╔══╝██╔═══██╗██╔════╝██╔═══██╗██║         ██╔════╝ ██╔══██╗╚══██╔══╝██╔════╝██║    ██║██╔══██╗╚██╗ ██╔╝
██████╔╝██████╔╝██║   ██║   ██║   ██║   ██║██║     ██║   ██║██║         ██║  ███╗███████║   ██║   █████╗  ██║ █╗ ██║███████║ ╚████╔╝
██╔═══╝ ██╔══██╗██║   ██║   ██║   ██║   ██║██║     ██║   ██║██║         ██║   ██║██╔══██║   ██║   ██╔══╝  ██║███╗██║██╔══██║  ╚██╔╝
██║     ██║  ██║╚██████╔╝   ██║   ╚██████╔╝╚██████╗╚██████╔╝███████╗    ╚██████╔╝██║  ██║   ██║   ███████╗╚███╔███╔╝██║  ██║   ██║
╚═╝     ╚═╝  ╚═╝ ╚═════╝    ╚═╝    ╚═════╝  ╚═════╝ ╚═════╝ ╚══════╝     ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚══════╝ ╚══╝╚══╝ ╚═╝  ╚═╝   ╚═╝

"""  # noqa: W291


class CustomConfigParser(ConfigParser):
    def get(self, section, option, *args, **kwargs):
        fallback = None

        if "fallback" in kwargs: #override kwargs fallback, for manually handling here
            fallback = kwargs["fallback"]
            kwargs["fallback"] = None

        if isinstance(option, list):
            for name in option:
                try:
                    value = super().get(section, name, *args, **kwargs)
                except NoOptionError:
                    value = None

                if value:
                    break
        else:
            try:
                value = super().get(section, option, *args, **kwargs)
            except NoOptionError:
                value = None

        if not value: #apply fallback
            value = fallback

        if value is None:
            if isinstance(option, list):
                raise NoOptionError(option[0], section)
            else:
                raise NoOptionError(option, section)

        if isinstance(value, int):
            return value

        if isinstance(value, float):
            return value

        if value is not None:
            # Strip leading/trailing whitespace and inline comments
            value = value.strip()
            # Remove inline comments (everything after #)
            if '#' in value:
                value = value.split('#')[0].strip()
        return value

    def getint(self, section, option, *args, **kwargs): #bypass fallback bug
        value = self.get(section, option, *args, **kwargs)
        return int(value) if value is not None else None

    def getfloat(self, section, option, *args, **kwargs): #bypass fallback bug
        value = self.get(section, option, *args, **kwargs)
        return float(value) if value is not None else None

    def getboolean(self, section, option, *args, **kwargs): #bypass fallback bug and handle case-insensitive boolean values
        value = self.get(section, option, *args, **kwargs)
        if value is None:
            return None

        # Convert to string and handle case-insensitive boolean values
        value_str = str(value).lower().strip()

        # Handle various boolean representations
        if value_str in ('true', 'yes', 'on', '1', 'enable', 'enabled'):
            return True
        elif value_str in ('false', 'no', 'off', '0', 'disable', 'disabled'):
            return False
        else:
            raise ValueError(f'Not a boolean: {value}')  # noqa: EM102


class Protocol_Gateway:
    """
    Main class, implementing the Growatt / Inverters to MQTT functionality
    """
    _logging_initialized = False

    @classmethod
    def _setup_logging(cls, cfg) -> None:
        """
        created to eliminate multi gig log file sizes in docker as logging.StreamHandler(sys.stdout) results in a
        json file that can quickly grow quiet large.

        Args:
            cfg : passed the config.cfg file for settings.
        """
        if cls._logging_initialized:
            return

        # Read logging config
        level_name: str = cfg.get("logging", "level", fallback="INFO").upper()
        level: int = getattr(logging, level_name, logging.INFO)

        log_dir = Path(cfg.get("logging", "log_dir", fallback="logs"))
        log_file: str = cfg.get("logging", "log_file", fallback="PPG.log")

        rotation: str = cfg.get("logging", "rotation", fallback="weekly").lower()
        backup_count: int = cfg.getint("logging", "backup_count", fallback=4)
        # fallback specific weekday rotation on a Monday
        when: str = cfg.get("logging", "when", fallback="W0")
        interval: int = cfg.getint("logging", "interval", fallback=1)
        max_bytes: int = cfg.getint("logging", "max_bytes", fallback=100 * 1024 * 1024)

        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / log_file

        # ---- Choose handler ----
        if rotation == "weekly":
            handler = logging.handlers.TimedRotatingFileHandler(
                filename=log_path,
                when=when,
                interval=interval,
                backupCount=backup_count,
                utc=True,
            )

        elif rotation == "daily":
            handler = logging.handlers.TimedRotatingFileHandler(
                filename=log_path,
                when="D",
                interval=1,
                backupCount=backup_count,
                utc=True,
            )

        elif rotation == "size":
            handler = logging.handlers.RotatingFileHandler(
                filename=log_path,
                maxBytes=max_bytes,
                backupCount=backup_count,
            )

        else:
            # Fallback: console only
            handler = logging.StreamHandler()

        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        )
        handler.setFormatter(formatter)

        # ---- Root logger wiring ----
        root = logging.getLogger()
        root.setLevel(level)
        root.handlers.clear()
        root.addHandler(handler)

        # Optional console logging
        if cfg.getboolean("logging", "console", fallback=False):
            console = logging.StreamHandler()
            console.setFormatter(formatter)
            root.addHandler(console)

        cls._logging_initialized = True

    __log = None
    # log level, available log levels are CRITICAL, FATAL, ERROR, WARNING, INFO, DEBUG, EXCEPTION
    __log_level = "DEBUG"

    __running : bool = False
    ''' controls main loop'''

    __transports : list[transport_base] = []
    ''' transport_base is for type hinting. this can be any transport'''

    config_file : str

    # Simple read completion tracking
    __read_completion_tracker : dict[str, bool] = {}
    ''' Track which transports have completed their current read cycle '''
    __read_tracker_lock : threading.Lock = None

    # Concurrency control
    __disable_concurrency : bool = False
    ''' When true, transports read sequentially instead of concurrently.
        Concurrent mode (false) is recommended for multiple devices with same address
        as it prevents timing interference between rapid sequential reads. '''

    # Transport timing control
    __transport_delay_offset : float = 0.5
    ''' Additional delay between different transports to prevent conflicts '''

    # Sequential transport delay
    __sequential_delay : float = 1.0
    ''' Delay between sequential transport reads to prevent device confusion '''

    def __init__(self, config_file : str):

        base_dir: Path = Path(__file__).resolve().parent

        default_cfg: Path = base_dir / "growatt2mqtt.cfg"
        alternate_cfg: Path = base_dir / config_file

        if alternate_cfg.is_file():
            self.config_file = alternate_cfg
        else:
            self.config_file = default_cfg

        #pymodbus_log = logging.getLogger('pymodbus')
        #pymodbus_log.setLevel(logging.DEBUG)
        #pymodbus_log.addHandler(handler)

        self.__settings = CustomConfigParser()
        self.__settings.read(self.config_file)

        self._setup_logging(self.__settings)
        self.__log: logging.Logger = logging.getLogger(__name__)

        ##[general]
        self.__log_level = self.__settings.get("general","log_level", fallback="INFO")

        # Read concurrency setting - default to sequential (disabled) for better stability
        self.__disable_concurrency = self.__settings.getboolean("general", "disable_concurrency", fallback=True)
        self.__log.info(f"Concurrency mode: {'Sequential' if self.__disable_concurrency else 'Concurrent'}")

        # Read sequential delay setting
        self.__sequential_delay = self.__settings.getfloat("general", "sequential_delay", fallback=1.0)
        if self.__disable_concurrency:
            self.__log.info(f"Sequential delay between transports: {self.__sequential_delay} seconds")

        log_level = getattr(logging, self.__log_level, logging.INFO)
        self.__log.setLevel(log_level)
        self.__log.info("Loading...")

        for section in self.__settings.sections():
            transport_cfg = self.__settings[section]
            transport_type      = transport_cfg.get("transport", fallback="")
            protocol_version    = transport_cfg.get("protocol_version", fallback="")

            # Process sections that either start with "transport" OR have a transport field
            if section.startswith("transport") or transport_type:
                if not transport_type and not protocol_version:
                    raise ValueError("Missing Transport / Protocol Version")

                if not transport_type and protocol_version: #get transport from protocol settings...  todo need to make a quick function instead of this

                    protocolSettings : protocol_settings = protocol_settings(protocol_version)

                    if not transport_type and not protocolSettings.transport:
                        raise ValueError("Missing Transport")

                    if not transport_type:
                        transport_type = protocolSettings.transport


                # Import the module
                module = importlib.import_module("classes.transports."+transport_type)
                # Get the class from the module
                cls = getattr(module, transport_type)
                transport : transport_base = cls(transport_cfg)

                transport.on_message = self.on_message
                self.__transports.append(transport)

        #connect first
        for transport in self.__transports:
            self.__log.info("Connecting to "+str(transport.type)+":" +str(transport.transport_name)+"...")
            transport.connect()

        time.sleep(0.7)
        #apply links
        for to_transport in self.__transports:
            for from_transport in self.__transports:
                if to_transport.bridge == from_transport.transport_name:
                    to_transport.init_bridge(from_transport)
                    from_transport.init_bridge(to_transport)

        # Initialize read completion tracking
        self.__read_tracker_lock = threading.Lock()
        for transport in self.__transports:
            if transport.read_interval > 0:
                self.__read_completion_tracker[transport.transport_name] = False
        self._wire_reconnect_hooks()

    def on_message(self, transport: transport_base, entry: registry_map_entry, data: str) -> None:
        for to_transport in self.__transports:
            if to_transport is transport:
                continue

            if self._are_bridged(transport, to_transport):
                to_transport.write_data({entry.variable_name: data}, transport)
                break

    def _process_transport_read(self, transport):
        """Process a single transport read operation"""
        try:
            # Always ensure transport is connected before reading
            if not transport.connected:
                self.__log.info(f"Transport {transport.transport_name} not connected, connecting...")
                transport.connect()

            self.__log.debug(f"Starting read cycle for {transport.transport_name}")
            info = transport.read_data()

            if not info:
                self.__log.warning(f"Transport {transport.transport_name} completed read cycle with NO DATA - this may indicate a device issue")
                self._mark_read_complete(transport)
                return

            self.__log.debug(f"Transport {transport.transport_name} completed read cycle with {len(info)} fields")

            # Write to output transports immediately (as before)
            if transport.bridge:
                for to_transport in self.__transports:
                    if to_transport.transport_name == transport.bridge:
                        to_transport.write_data(info, transport)
                        break

            self._mark_read_complete(transport)

        except Exception as err:
            self.__log.error(f"Error processing transport {transport.transport_name}: {err}")
            # traceback.print_exc()
            self._mark_read_complete(transport)

    def _mark_read_complete(self, transport):
        """Mark a transport as having completed its read cycle"""
        with self.__read_tracker_lock:
            self.__read_completion_tracker[transport.transport_name] = True
            self.__log.debug(f"Marked {transport.transport_name} read cycle as complete")

    def _reset_read_completion_tracker(self):
        """Reset the read completion tracker for the next cycle"""
        with self.__read_tracker_lock:
            for transport_name in self.__read_completion_tracker:
                self.__read_completion_tracker[transport_name] = False

    def _get_read_completion_status(self):
        """Get the current read completion status for debugging"""
        with self.__read_tracker_lock:
            return self.__read_completion_tracker.copy()

    def _are_bridged(self, a: transport_base, b: transport_base) -> bool:
        return (
            a.transport_name == b.bridge
            or b.transport_name == a.bridge
        )

    def reconnect_upstream_bridge(self, bridge_name: str) -> None:

        bridge = next(
            (t for t in self.__transports if t.transport_name == bridge_name),
            None
        )

        if not bridge:
            self.__log.warning(
                f"Reconnect requested for unknown transport '{bridge_name}'"
            )
            return

        for producer_transport in self.__transports:
            if producer_transport is bridge:
                continue

            if self._are_bridged(producer_transport, bridge):
                self.__log.warning(
                    f"Stale data detected in '{bridge_name}', "
                    f"reconnecting upstream '{producer_transport.transport_name}'"
                )
                producer_transport.connected = False
                producer_transport.last_read_time = 0
                return

        self.__log.warning(
            f"No upstream transport found for '{bridge_name}'"
        )

    # init the variable request_upstream_reconnect in the __init__ bridge.  If it goes true, reconnect routine triggers.
    def _wire_reconnect_hooks(self) -> None:
        for transport in self.__transports:
            if hasattr(transport, "request_upstream_reconnect"):
                transport.request_upstream_reconnect = (
                    lambda name=transport.transport_name:
                        self.reconnect_upstream_bridge(name)
                )

    def run(self):
        """
        run method, starts ModBus connection and bridge connection
        """

        self.__running = True

        if False:
            self.enable_write()

        while self.__running:
            try:
                now = time.time()
                ready_transports = []

                # Find all transports that are ready to read
                for transport in self.__transports:
                    if transport.read_interval > 0 and now - transport.last_read_time > transport.read_interval:
                        transport.last_read_time = now
                        ready_transports.append(transport)

                # Reset read completion tracker for this cycle
                if ready_transports:
                    self._reset_read_completion_tracker()
                    self.__log.debug(f"Starting read cycle for {len(ready_transports)} transports: {[t.transport_name for t in ready_transports]}")

                # Process transports based on concurrency setting
                if self.__disable_concurrency:
                    # Sequential processing - process transports one by one
                    for i, transport in enumerate(ready_transports):
                        self.__log.debug(f"Processing {transport.transport_name} sequentially ({i+1}/{len(ready_transports)})")

                        # Process current transport
                        self._process_transport_read(transport)

                        # Add delay between transports to prevent device confusion
                        if i < len(ready_transports) - 1:  # Don't delay after the last transport
                            self.__log.debug(f"Waiting {self.__sequential_delay} seconds before next transport...")
                            time.sleep(self.__sequential_delay)

                    # Log completion status for sequential mode
                    completion_status = self._get_read_completion_status()
                    completed = [name for name, status in completion_status.items() if status]
                    self.__log.debug(f"Sequential read cycle completed. Completed transports: {completed}")

                else:
                    # Concurrent processing - process transports in parallel
                    if len(ready_transports) > 1:
                        threads = []
                        for transport in ready_transports:
                            thread = threading.Thread(target=self._process_transport_read, args=(transport,))
                            thread.daemon = True
                            thread.start()
                            threads.append(thread)

                        # Wait for all threads to complete
                        for thread in threads:
                            thread.join()

                        # Log completion status
                        completion_status = self._get_read_completion_status()
                        completed = [name for name, status in completion_status.items() if status]
                        self.__log.debug(f"Concurrent read cycle completed. Completed transports: {completed}")

                    elif len(ready_transports) == 1:
                        # Single transport - process directly
                        self._process_transport_read(ready_transports[0])

            except Exception as err:
                #traceback.print_exc()
                self.__log.exception("Unhandled exception in main loop")
                self.__log.error(err)

            time.sleep(0.07) #change this in future. probably reduce to allow faster reads.


def main(args=None):
    """
    main method
    """

    # Create ArgumentParser object
    parser = argparse.ArgumentParser(description="Python Protocol Gateway")

    # Add arguments
    parser.add_argument("--config", "-c", type=str, help="Specify Config File")

    # Add a positional argument with default
    parser.add_argument("positional_config", type=str, help="Specify Config File", nargs="?", default="config.cfg")

    # Parse arguments
    args = parser.parse_args()

    # If '--config' is provided, use it; otherwise, fall back to the positional or default.
    args.config = args.config if args.config else args.positional_config

    print(__logo)

    ppg = Protocol_Gateway(args.config)
    ppg.run()


if __name__ == "__main__":
    main()
