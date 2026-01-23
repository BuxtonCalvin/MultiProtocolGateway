#!/usr/bin/env python3
"""
Main module for Growatt / Inverters ModBus RTU data to MQTT
"""


import importlib
import sys
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
from defs.common import strtobool

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

        return value.strip() if value is not None else value

    def getint(self, section, option, *args, **kwargs): #bypass fallback bug
        value = self.get(section, option, *args, **kwargs)
        return int(value) if value is not None else None

    def getfloat(self, section, option, *args, **kwargs): #bypass fallback bug
        value = self.get(section, option, *args, **kwargs)
        return float(value) if value is not None else None

    def getboolean(self, section, option, *args, **kwargs): #bypass fallback bug
        value = self.get(section, option, *args, **kwargs)
        return strtobool(value)


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


    def on_message(self, transport : transport_base, entry : registry_map_entry, data : str):
        ''' message received from a transport! '''
        for to_transport in self.__transports:
            if to_transport.transport_name != transport.transport_name:
                if to_transport.transport_name == transport.bridge or transport.transport_name == to_transport.bridge:
                    to_transport.write_data({entry.variable_name : data}, transport)
                    break

    def force_reconnect_by_name(self, name: str) -> None:
        """
        Search for a transport by name and flag it for reconnection.
        The 'run' loop will see 'connected=False' and call connect() on next tick.
        """
        for transport in self.__transports:
            # Checking against transport_name as defined in your transport_base
            if transport.transport_name == name:
                transport.connected = False
                # Reset the last_read_time to force an immediate read after connect
                transport.last_read_time = 0
                break

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
                for transport in self.__transports:
                    if transport.read_interval > 0 and now - transport.last_read_time  > transport.read_interval:
                        transport.last_read_time = now
                        #perform read
                        if not transport.connected:
                            transport.connect() #reconnect
                        else: #transport is connected

                            info = transport.read_data()

                            if not info:
                                continue

                            #todo. broadcast option
                            if transport.bridge:
                                for to_transport in self.__transports:
                                    if to_transport.transport_name == transport.bridge:
                                        to_transport.write_data(info, transport)
                                        break

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
