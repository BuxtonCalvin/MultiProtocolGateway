"""
file_watcher.py — Monitors config.cfg and the protocols directory for
on-disk changes and triggers a re-scan automatically.

Uses the `watchdog` library.  If watchdog is not installed the watcher
degrades gracefully to a no-op so the server still starts.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

_log: logging.Logger = logging.getLogger(__name__)


class _MPGEventHandler:
    """Handles file system events from watchdog."""

    def __init__(self, scanner, debounce_seconds: float = 2.0) -> None:
        self._scanner = scanner
        self._debounce: float = debounce_seconds
        self._pending: threading.Timer | None = None
        self._lock = threading.Lock()

    def _schedule_scan(self, path: str) -> None:
        """Debounce: wait briefly before triggering scan in case of burst saves."""
        with self._lock:
            if self._pending:
                self._pending.cancel()
            self._pending = threading.Timer(self._debounce, self._do_scan, args=(path,))
            self._pending.daemon = True
            self._pending.name = "MPGFileScanDebounce"
            self._pending.start()

    def _do_scan(self, path: str) -> None:
        _log.info(f"File change detected: {path} — triggering re-scan")
        try:
            self._scanner.run()
        except Exception as exc:
            msg: str = f"Re-scan after file change failed: {exc}"
            _log.error(msg)

    # watchdog interface
    def on_modified(self, event) -> None:
        if not getattr(event, "is_directory", False):
            self._schedule_scan(event.src_path)

    def on_created(self, event) -> None:
        if not getattr(event, "is_directory", False):
            self._schedule_scan(event.src_path)

    def on_deleted(self, event) -> None:
        if not getattr(event, "is_directory", False):
            self._schedule_scan(event.src_path)


class FileWatcher:
    """
    Wraps watchdog observers for config.cfg and protocols/.
    Starts in a daemon thread; stops cleanly on server shutdown.
    """

    def __init__(self, scanner, config_path: Path, protocols_dir: Path) -> None:
        self._scanner = scanner
        self._config_path: Path = config_path
        self._protocols_dir: Path = protocols_dir
        self._observer = None
        self._available = False

        try:

            self._available = True
        except ImportError:
            _log.warning("watchdog not installed — file watching disabled. Install with: pip install watchdog")

    def start(self) -> None:
        if not self._available:
            return

        handler_obj = _MPGEventHandler(self._scanner)

        # Shim watchdog's ABC onto our handler
        class _WatchdogShim(FileSystemEventHandler):
            def __init__(self, inner) -> None:
                self._inner = inner
            def on_modified(self, event) -> None:
                self._inner.on_modified(event)
            def on_created(self, event) -> None:
                self._inner.on_created(event)
            def on_deleted(self, event) -> None:
                self._inner.on_deleted(event)

        shim = _WatchdogShim(handler_obj)
        self._observer = Observer()
        try:
            self._observer.name = "MPGFileWatcher"
        except Exception as exc:
            msg: str = f"Failed to set observer name: {exc}"
            _log.warning(msg)

        # Watch config file's parent directory, filter in handler
        self._observer.schedule(shim, str(self._config_path.parent), recursive=False)

        if self._protocols_dir.exists():
            self._observer.schedule(shim, str(self._protocols_dir), recursive=True)

        self._observer.daemon = True
        self._observer.start()
        try:
            for idx, emitter in enumerate(getattr(self._observer, "emitters", [])):
                emitter.name = f"MPGFileEmitter_{idx}"
        except Exception as exc:
            msg: str = f"Failed to set emitter names: {exc}"
            _log.warning(msg)
        _log.info(f"File watcher started: watching {self._config_path.parent} and {self._protocols_dir}")

    def stop(self) -> None:
        if self._observer and self._observer.is_alive():
            self._observer.stop()
            self._observer.join(timeout=5)
            _log.info("File watcher stopped.")
