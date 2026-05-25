# Description: Telegram messaging client. Wraps python-telegram-bot in a fire-and-forget helper so it can be used from
# File: telegram_client.py
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

# Telegram messaging client.
# Wraps python-telegram-bot in a fire-and-forget helper so it can be used from
# synchronous or threaded contexts (matching the Pushover client interface).
#
# Install:  pip install "python-telegram-bot>=20.0"
#
# Only the bot token and a chat_id are required.  The chat_id can be a single
# user, a group, or a channel (prefix with @).  Multiple chat IDs can be
# supplied as a comma-separated string and every message will be fanned out to
# all of them.
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from telegram import Bot as _TelegramBot
from telegram.error import TelegramError

_log: logging.Logger = logging.getLogger(__name__)


class TelegramMessage:
    """
    A simple message that can be sent via the Telegram Bot API.

    Parameters
    ----------
    text:
        The message body (HTML or plain text depending on *parse_mode*).
    title:
        Optional bold heading prepended to the body (plain text only).
    parse_mode:
        "HTML" (default), "MarkdownV2", or None for plain text.
    disable_notification:
        Send silently — notification arrives without sound.
    """

    def __init__(
        self,
        text: str,
        title: str = "",
        parse_mode: str | None = "HTML",
        disable_notification: bool = False,
    ) -> None:
        if not text:
            raise ValueError("'text' cannot be empty!")

        self.title: str = str(title)[0:256]
        self.text: str = str(text)[0:4096]
        self.parse_mode: str | None = parse_mode
        self.disable_notification: bool = disable_notification
        self.response_data: Any = None

    @property
    def full_text(self) -> str:
        """Combines title + body the same way Pushover combines title + message."""
        if self.title:
            if self.parse_mode and self.parse_mode.upper() == "HTML":
                return f"<b>{self.title}</b>\n{self.text}"
            return f"{self.title}\n{self.text}"
        return self.text


class TelegramClient:
    """
    Fire-and-forget Telegram bot client.

    Works from any thread — internally runs the async PTB call on a dedicated
    event loop so the caller never has to worry about async/await.

    Parameters
    ----------
    bot_token:
        The BotFather token for your bot (required).
    chat_ids:
        One or more recipient chat IDs (user IDs, group IDs, or @channels).
        Accepts a list[str | int] or a single comma-separated string.
    """

    def __init__(self, bot_token: str, chat_ids: list[str | int] | str) -> None:
        if not bot_token:
            raise ValueError("'bot_token' cannot be empty!")

        self.bot_token: str = bot_token

        # Normalize chat_ids — accept "id1, id2" or [id1, id2]
        if isinstance(chat_ids, str):
            self.chat_ids: list[str | int] = [
                c.strip() for c in chat_ids.split(",") if c.strip()
            ]
        else:
            self.chat_ids = list(chat_ids)

        if not self.chat_ids:
            raise ValueError("At least one chat_id is required!")

        # Dedicated event loop running on a daemon thread so async PTB calls
        # don't interfere with the application's main loop or FastAPI's loop.
        self._loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="TelegramClientLoop"
        )
        self._thread.start()
        self._bot = _TelegramBot(token=self.bot_token)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send(self, message_obj: TelegramMessage) -> bool:
        """
        Send *message_obj* to all configured chat IDs.

        Blocks the calling thread only long enough to submit the coroutine;
        the actual network calls run on the dedicated loop thread.

        Returns True when all sends were submitted, False on error.
        """
        future = asyncio.run_coroutine_threadsafe(
            self._send_all(message_obj), self._loop
        )
        try:
            future.result(timeout=15)
            return True  # noqa: TRY300
        except Exception as exc:
            _log.error("Telegram send failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _send_all(self, message_obj: TelegramMessage) -> None:
        async with self._bot:
            for chat_id in self.chat_ids:
                try:
                    await self._bot.send_message(
                        chat_id=chat_id,
                        text=message_obj.full_text,
                        parse_mode=message_obj.parse_mode,
                        disable_notification=message_obj.disable_notification,
                    )
                    message_obj.response_data = {"status": "ok", "chat_id": chat_id}
                    _log.debug("Telegram message sent to chat_id=%s", chat_id)
                except TelegramError as exc:
                    _log.error(
                        "Telegram send error for chat_id=%s: %s", chat_id, exc
                    )
                    message_obj.response_data = {"status": "error", "detail": str(exc)}
