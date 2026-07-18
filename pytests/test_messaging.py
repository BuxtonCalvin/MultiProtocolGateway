# Description: Unit tests for messaging clients and message dispatch.
# File: test_messaging.py
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

"""Unit tests for messaging clients and message dispatch."""

from __future__ import annotations

from configparser import ConfigParser
from unittest.mock import MagicMock, patch

import pytest

from classes.messaging import message_handler
from classes.messaging.message_handler import MessageHandler
from classes.messaging.pushover import Client, Glance, Message
from classes.messaging.telegram_client import TelegramMessage


def test_pushover_message_truncates_long_fields_and_rejects_none() -> None:
    """Edge cases: long Pushover fields are clamped and a None body raises ValueError."""
    msg = Message("x" * 5000, title="t" * 300, url="u" * 600, url_title="z" * 200)
    assert len(msg.message) == 4096
    assert len(msg.title) == 250
    assert len(msg.url) == 512
    assert len(msg.url_title) == 100
    with pytest.raises(ValueError, match="message"):
        Message(None)  # type: ignore[arg-type]


def test_pushover_glance_validates_count_and_percent() -> None:
    """Error handling: Glance validates numeric fields before API submission."""
    assert Glance(title="Solar", text="OK", count=3, percent=99).json["percent"] == 99
    with pytest.raises(TypeError, match="count"):
        Glance(count="3")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="percent"):
        Glance(percent=101)


@patch("classes.messaging.pushover.requests.post")
def test_pushover_client_send_posts_json_without_real_network(mock_post: MagicMock) -> None:
    """Mocks external API: Pushover send posts a JSON payload and stores response data."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"status": 1}
    mock_post.return_value = mock_response
    client = Client(user_key="user", api_token="token")  # noqa: S106
    msg = Message("battery ok", title="MPG")

    assert client.send(msg) is mock_response
    mock_post.assert_called_once()
    assert mock_post.call_args.kwargs["json"]["message"] == "battery ok"
    assert msg.response_data == {"status": 1}


def test_telegram_message_formats_html_title_and_rejects_empty_text() -> None:
    """Happy path and error handling: Telegram messages format titles and require non-empty text."""
    msg = TelegramMessage("Body", title="Alert")
    assert msg.full_text == "<b>Alert</b>\nBody"
    with pytest.raises(ValueError, match="text"):
        TelegramMessage("")


def test_message_handler_dispatch_filters_services_and_clamps_priority() -> None:
    """Happy path: dispatch targets requested services and clamps Pushover priority."""
    handler: MessageHandler = MessageHandler.__new__(MessageHandler)
    handler._default_title = "Default"
    pushover_client = MagicMock()
    telegram_client = MagicMock()
    handler._clients = [
        {"name": "pushover", "client": pushover_client, "Message": MagicMock(side_effect=lambda **kw: kw)},
        {"name": "telegram", "client": telegram_client, "Message": MagicMock(side_effect=lambda **kw: kw)},
    ]

    handler.dispatch("hello", priority=99, services="pushover")

    pushover_client.send.assert_called_once()
    telegram_client.send.assert_not_called()
    assert pushover_client.send.call_args.args[0]["priority"] == 2


def test_message_handler_setup_uses_config_and_mocks_clients() -> None:
    """Mocks external clients: setup reads config and creates only enabled, configured services."""
    cfg = ConfigParser()
    cfg["messages"] = {
        "enabled": "true",
        "default_title": "MPG",
        "pushover_enabled": "true",
        "pushover_user_key": "user",
        "pushover_api_token": "token",
        "telegram_enabled": "false",
    }
    MessageHandler._initialized = False
    message_handler._handler = None

    with patch("classes.messaging.message_handler.PushoverClient") as mock_client:
        MessageHandler.setup(cfg)

    mock_client.assert_called_once_with(user_key="user", api_token="token")  # noqa: S106
    assert message_handler._handler is not None
    assert message_handler._handler._active_services == ["pushover"]
