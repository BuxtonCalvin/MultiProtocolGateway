# Description: classes/messaging/message_handler.py
# File: message_handler.py
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

# classes/messaging/message_handler.py
"""
Application-wide messaging handler.

Mirrors the pattern of Protocol_Gateway._setup_logging — a class-level
initializer reads the [messages] section of config.cfg and wires up every
enabled messaging service.  Any transport (or any other component) then
calls the module-level helper:

    from classes.messaging.message_handler import send_message
    send_message("Solar inverter fault detected", title="MPG Alert")

Config example (config.cfg):
-------------------------------
[messages]
enabled          = true

# -- Pushover --
pushover_enabled = true
pushover_user_key  = uXXXXXXXXXXXXXXXXXXXXXXXX
pushover_api_token = aXXXXXXXXXXXXXXXXXXXXXXXX

# -- Telegram --
telegram_enabled   = true
telegram_bot_token = 123456:AAXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
telegram_chat_ids  = 987654321, @my_channel

# Shared defaults (override per-service if desired)
default_title    = MPG Notification
-------------------------------

Service driver contract
-----------------------
Each driver module in the ``messaging/`` package must expose:
  - A ``Client`` class constructed with the service-specific credentials.
  - A ``Message`` (or equivalent) dataclass accepted by ``Client.send()``.
  - ``Client.send(message_obj)`` must be synchronous (blocking is fine; it
    will always be called from a worker thread, never from an async context).

Adding a new service
--------------------
1.  Create ``classes/messaging/<service>_client.py`` following the pattern
    of pushover.py / telegram_client.py.
2.  Add a block in ``MessageHandler._init_services()`` below.
3.  Document the required config keys in the docstring above.
"""
from __future__ import annotations

import logging
from configparser import ConfigParser
from typing import Any

from classes.messaging.pushover import Client as PushoverClient
from classes.messaging.pushover import Message as PushoverMessage
from classes.messaging.telegram_client import TelegramClient, TelegramMessage

_log: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level singleton access
# ---------------------------------------------------------------------------

_handler: "MessageHandler | None" = None


def send_message(
    message: str,
    title: str = "",
    priority: int = 0,
    services: list[str] | str | None = None,
    **kwargs: Any,
) -> None:
    """
    Send *message* via messaging services.

    This is the single entry point used by transport_base.send_message() and
    any other component that wants to emit a notification.

    Parameters
    ----------
    message:
        The notification body (required).
    title:
        Short heading.  Falls back to ``default_title`` from config.
    priority:
        Pushover-style priority (-2 … 2).  Other services interpret it as
        best they can (e.g. Telegram maps > 0 to a normal sound notification).
    services:
        Which services to use.  Accepts:
          - ``None`` (default) — send via **all** active services.
          - A single service name string, e.g. ``"pushover"``.
          - A list of service name strings, e.g. ``["pushover", "telegram"]``.
        Service names are case-insensitive and must match those registered in
        ``MessageHandler._active_services`` (e.g. "pushover", "telegram").
    **kwargs:
        Passed through to each service driver for future extensibility.
    """
    if _handler is None:
        _log.debug("MessageHandler not initialized — message dropped: %s", message)
        return
    _handler.dispatch(message=message, title=title, priority=priority, services=services, **kwargs)


# ---------------------------------------------------------------------------
# MessageHandler
# ---------------------------------------------------------------------------

class MessageHandler:
    """
    Reads the [messages] config section and initializes one client per enabled
    service.  Call ``Protocol_Gateway._setup_messaging(cfg)`` once at startup;
    after that use the module-level ``send_message()`` helper everywhere.
    """

    _initialized: bool = False

    # ------------------------------------------------------------------
    # Class-level initializer (mirrors _setup_logging)
    # ------------------------------------------------------------------

    @classmethod
    def setup(cls, cfg: ConfigParser) -> None:
        """
        Initialise the messaging subsystem from *cfg*.

        Safe to call multiple times — subsequent calls are no-ops unless
        ``force=True`` is passed.  This matches the _logging_initialized guard
        in Protocol_Gateway.
        """
        global _handler
        if cls._initialized:
            return

        if not cfg.has_section("messages"):
            _log.debug("[messages] section not found — messaging disabled.")
            cls._initialized = True
            return

        enabled: bool = cfg.getboolean("messages", "enabled", fallback=False) if hasattr(cfg, "getboolean") else (
            cfg.get("messages", "enabled", fallback="false").lower() in ("true", "yes", "1", "on")
        )
        if not enabled:
            _log.info("Messaging disabled via [messages] enabled = false")
            cls._initialized = True
            return

        default_title: str = cfg.get("messages", "default_title", fallback="MPG Notification")
        instance = cls(cfg, default_title)
        _handler = instance
        cls._initialized = True
        _log.info(
            "Messaging initialized — active services: %s",
            ", ".join(instance._active_services) or "none",
        )

    # ------------------------------------------------------------------
    # Instance
    # ------------------------------------------------------------------

    def __init__(self, cfg: ConfigParser, default_title: str) -> None:
        self._default_title: str = default_title
        self._clients: list[dict[str, Any]] = []  # list of {"name": str, "client": obj, "Message": type}
        self._active_services: list[str] = []
        self._init_services(cfg)

    # ------------------------------------------------------------------
    # Service wiring — add new services here
    # ------------------------------------------------------------------

    def _init_services(self, cfg: ConfigParser) -> None:
        """Wire up each messaging service that is enabled in config."""

        # ---- Pushover ------------------------------------------------
        self._init_pushover(cfg)

        # ---- Telegram ------------------------------------------------
        self._init_telegram(cfg)

        # ---- Future services: add _init_<service>(cfg) calls here ----

    def _init_pushover(self, cfg: ConfigParser) -> None:
        """Initialise the Pushover client if pushover_enabled = true."""
        enabled: bool = _cfg_bool(cfg, "messages", "pushover_enabled", False)  # noqa: FBT003
        if not enabled:
            return

        user_key: str  = cfg.get("messages", "pushover_user_key",  fallback="").strip()
        api_token: str = cfg.get("messages", "pushover_api_token", fallback="").strip()

        if not user_key or not api_token:
            _log.warning(
                "Pushover enabled but pushover_user_key / pushover_api_token "
                "are missing from [messages] — Pushover disabled."
            )
            return

        try:
            client: PushoverClient = PushoverClient(user_key=user_key, api_token=api_token)
            self._clients.append(
                {"name": "pushover", "client": client, "Message": PushoverMessage}
            )
            self._active_services.append("pushover")
            _log.debug("Pushover client ready.")
        except Exception as exc:
            _log.error("Failed to initialise Pushover client: %s", exc)

    def _init_telegram(self, cfg: ConfigParser) -> None:
        """Initialise the Telegram client if telegram_enabled = true."""
        enabled: bool = _cfg_bool(cfg, "messages", "telegram_enabled", False)  # noqa: FBT003
        if not enabled:
            return

        bot_token: str  = cfg.get("messages", "telegram_bot_token", fallback="").strip()
        chat_ids_raw: str = cfg.get("messages", "telegram_chat_ids",  fallback="").strip()

        if not bot_token or not chat_ids_raw:
            _log.warning(
                "Telegram enabled but telegram_bot_token / telegram_chat_ids "
                "are missing from [messages] — Telegram disabled."
            )
            return

        try:

            client = TelegramClient(bot_token=bot_token, chat_ids=chat_ids_raw)
            self._clients.append(
                {"name": "telegram", "client": client, "Message": TelegramMessage}
            )
            self._active_services.append("telegram")
            _log.debug("Telegram client ready.")
        except Exception as exc:
            _log.error("Failed to initialise Telegram client: %s", exc)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def dispatch(
        self,
        message: str,
        title: str = "",
        priority: int = 0,
        services: list[str] | str | None = None,
        **kwargs: Any,
    ) -> None:
        """
        Fan the notification out to the requested services.

        Parameters
        ----------
        services:
            ``None``  → all active services.
            ``str``   → single service by name (case-insensitive).
            ``list``  → explicit subset of services (case-insensitive).
        """
        effective_title: str = title or self._default_title

        # Normalize the services filter to a set of lowercase names, or None for "all".
        target: set[str] | None
        if services is None:
            target = None
        elif isinstance(services, str):
            target = {services.strip().lower()}
        else:
            target = {s.strip().lower() for s in services}

        for entry in self._clients:
            service_name: str = entry["name"]

            if target is not None and service_name.lower() not in target:
                _log.debug("Skipping service '%s' (not in requested set).", service_name)
                continue

            client = entry["client"]
            MessageCls = entry["Message"]

            try:
                msg_obj = self._build_message(
                    service_name, MessageCls, message, effective_title, priority, kwargs
                )
                if msg_obj is None:
                    continue
                client.send(msg_obj)
                _log.debug("Message dispatched via %s.", service_name)
            except Exception as exc:
                _log.error("Error dispatching via %s: %s", service_name, exc)

    # ------------------------------------------------------------------
    # Message factory — keeps per-service constructor differences here
    # ------------------------------------------------------------------

    @staticmethod
    def _build_message(
        service: str,
        MessageCls: type,
        text: str,
        title: str,
        priority: int,
        extra: dict[str, Any],
    ) -> Any:
        """
        Construct a service-specific message object.

        Each ``if service == ...`` block maps the generic parameters to the
        constructor signature of that service's ``Message`` class.
        """
        if service == "pushover":
            # Clamp priority to the Pushover range [-2, 2]
            p: int = max(-2, min(2, priority))
            return MessageCls(message=text, title=title, priority=p)  # type: ignore[call-arg]

        if service == "telegram":
            # Telegram has no numeric priority; map > 0 → sound on, ≤ 0 → silent
            silent: bool = priority < 0
            return MessageCls(  # type: ignore[call-arg]
                text=text,
                title=title,
                disable_notification=silent,
            )

        # Generic fallback — try a positional 'message' or 'text' kwarg
        try:
            return MessageCls(message=text, title=title)
        except TypeError:
            try:
                return MessageCls(text=text, title=title)
            except TypeError:
                _log.warning("Cannot construct message for unknown service '%s'.", service)
                return None


# ---------------------------------------------------------------------------
# Config helper — tolerates both ConfigParser and CustomConfigParser
# ---------------------------------------------------------------------------

def _cfg_bool(cfg: ConfigParser, section: str, option: str, fallback: bool) -> bool:
    try:
        raw: str = cfg.get(section, option, fallback=str(fallback))
        return raw.strip().lower() in ("true", "yes", "1", "on", "enabled", "enable")
    except Exception:
        return fallback
