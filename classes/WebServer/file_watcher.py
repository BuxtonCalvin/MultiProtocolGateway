# Description: file_watcher.py — Monitors config.cfg and the protocols directory for on-disk changes and triggers a re-scan automatically.
# File: file_watcher.py
#
# Copyright 2026 Kevin Burke
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://apache.org
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
file_watcher.py — Monitors config.cfg and the protocols directory for
on-disk changes and triggers a re-scan automatically.

Uses the `watchdog` library.  If watchdog is not installed the watcher
degrades gracefully to a no-op so the server still starts.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

_log: logging.Logger = logging.getLogger(__name__)

# Mute the watchdog buffer and observer debug logs
logging.getLogger("watchdog.observers.inotify_buffer").setLevel(logging.WARNING)
logging.getLogger("watchdog").setLevel(logging.WARNING)


class _MPGEventHandler:
    """Handles file system events from watchdog.

    Dispatches two independent, independently-debounced triggers:
      - changes to ``config_path`` itself      -> ``on_config_changed()``
      - changes anywhere under ``protocols_dir`` -> ``scanner.run()``
    These are deliberately kept separate rather than firing both on any
    change: editing config.cfg (transport/read_mode settings) needs the live
    gateway rebuilt, while editing a protocol CSV/JSON needs the webUI's
    reference-data DB re-scanned — the two are unrelated and don't need to
    trigger each other.
    """

    def __init__(
        self,
        scanner: Any,
        config_path: Path,
        on_config_changed: Callable[[], None] | None = None,
        debounce_seconds: float = 2.0,
        config_debounce_seconds: float = 1.0,
    ) -> None:
        self._scanner: Any = scanner
        self._config_path: Path = config_path
        self._on_config_changed: Callable[[], None] | None = on_config_changed
        self._debounce: float = debounce_seconds
        self._config_debounce: float = config_debounce_seconds
        self._pending: threading.Timer | None = None
        self._pending_config: threading.Timer | None = None
        self._lock: threading.Lock = threading.Lock()

    def _schedule_scan(self, path: Path) -> None:
        """Debounce: wait briefly before triggering scan in case of burst saves."""
        with self._lock:
            if self._pending is not None:
                self._pending.cancel()
            self._pending = threading.Timer(self._debounce, self._do_scan, args=(path,))
            self._pending.daemon = True
            self._pending.name = "MPGFileScanDebounce"
            self._pending.start()

    def _do_scan(self, path: Path) -> None:
        _log.info(f"File change detected: {path} — triggering re-scan")
        try:
            self._scanner.run()
        except Exception as exc:
            msg: str = f"Re-scan after file change failed: {exc}"
            _log.error(msg)

    def _schedule_config_reload(self, path: Path) -> None:
        """Debounce config.cfg changes separately from protocols_dir scans —
        a save in an editor is often several rapid writes (temp file +
        rename, etc.); a shorter window than the scan debounce since a
        config reload is comparatively cheap to (potentially) redo once
        more if a second edit lands just after the first debounce fires."""
        with self._lock:
            if self._pending_config is not None:
                self._pending_config.cancel()
            self._pending_config = threading.Timer(
                self._config_debounce, self._do_config_reload, args=(path,)
            )
            self._pending_config.daemon = True
            self._pending_config.name = "MPGConfigReloadDebounce"
            self._pending_config.start()

    def _do_config_reload(self, path: Path) -> None:
        _log.info(f"Config file change detected: {path} — triggering gateway reload")
        if self._on_config_changed is None:
            return
        try:
            self._on_config_changed()
        except Exception as exc:
            msg: str = f"Gateway reload after config change failed: {exc}"
            _log.error(msg)

    # watchdog interface

    def on_any_event(self, event: FileSystemEvent) -> None:
        # Filter for only the event types you care about
        if event.event_type in ("modified", "created", "deleted"):
            if not getattr(event, "is_directory", False):
                changed_path: Path = Path(os.fsdecode(event.src_path))
                try:
                    is_config_file: bool = changed_path.resolve() == self._config_path.resolve()
                except OSError:
                    # resolve() can raise on some platforms for a path mid-delete;
                    # fall back to a plain (unresolved) comparison rather than
                    # dropping the event entirely.
                    is_config_file = changed_path == self._config_path

                if is_config_file:
                    self._schedule_config_reload(changed_path)
                else:
                    self._schedule_scan(changed_path)


class FileWatcher:
    """
    Wraps watchdog observers for config.cfg and protocols/.
    Starts in a daemon thread; stops cleanly on server shutdown.
    """

    def __init__(
        self,
        scanner: Any,
        config_path: Path,
        protocols_dir: Path,
        on_config_changed: Callable[[], None] | None = None,
    ) -> None:
        self._scanner: Any = scanner
        self._config_path: Path = config_path
        self._protocols_dir: Path = protocols_dir
        self._on_config_changed: Callable[[], None] | None = on_config_changed
        self._observer: Any | None = None
        self._available = False

        try:

            self._available = True
        except ImportError:
            _log.warning("watchdog not installed — file watching disabled. Install with: pip install watchdog")

    def start(self) -> None:
        if not self._available:
            return

        handler_obj = _MPGEventHandler(self._scanner, self._config_path, self._on_config_changed)

        # Shim watchdog's ABC onto our handler
        class _WatchdogShim(FileSystemEventHandler):
            def __init__(self, inner: _MPGEventHandler) -> None:
                super().__init__()
                self._inner: _MPGEventHandler = inner
            def on_any_event(self, event: FileSystemEvent) -> None:
                self._inner.on_any_event(event)

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
