# Description: based on https://github.com/nythepegasus/pushover-client
# File: pushover.py
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

# based on https://github.com/nythepegasus/pushover-client
import mimetypes
from time import time
from typing import Any, Literal, cast

import requests

# Define valid types using Literal for the static type checker
PriorityType = Literal[-2, -1, 0, 1, 2]
SoundType = Literal[
    "pushover", "bike", "bugle", "cashregister", "classical", "cosmic",
    "falling", "gamelan", "incoming", "intermission", "magic", "mechanic",
    "pianobar", "siren", "spacealarm", "tugboat", "alien", "climb",
    "persistent", "echo", "updown", "vibrate", "none"
]

# 2. Keep your lists for runtime validation or tracking if needed
SOUNDS: list[str] = [
    "pushover", "bike", "bugle", "cashregister", "classical", "cosmic",
    "falling", "gamelan", "incoming", "intermission", "magic", "mechanic",
    "pianobar", "siren", "spacealarm", "tugboat", "alien", "climb",
    "persistent", "echo", "updown", "vibrate", "none"
]

PRIORITIES: list[int] = [-2, -1, 0, 1, 2]

ATTACHMENT_TYPES: list[str] = [
    "image/jpeg",
    "image/png"
]


class Message:
    """
    A normal Message that can be sent through the Pushover API. To be displayed as a normal push notification.
    """

    def __init__(self, message: str, title: str = "", attachment: str = "", device: str = "", url: str = "",
                 url_title: str = "", priority: PriorityType = 0, sound: SoundType = "pushover", timestamp: float = 0.0,
                 retry: int = 30, expire: int = 10800) -> None:
        if message is None:
            raise ValueError("'message' cannot be None!")

        self._mime_test = None
        if attachment != "":
            # Use 'with' statement so the file is immediately closed after checking
            try:
                with open(attachment):
                    pass
                self._mime_test: str | None = mimetypes.guess_type(attachment)[0]
                if self._mime_test not in ATTACHMENT_TYPES:
                    raise ValueError("Attachment file must be valid jpeg/png image!")
            except FileNotFoundError:
                raise FileNotFoundError("Must input a valid file!")

        self.message = str(message)[0:4096]
        self.title = str(title)[0:250]
        self._attachment = str(attachment)
        self.device = str(device)
        self.url = str(url)[0:512]
        self.url_title = str(url_title)[0:100]
        self.priority = int(priority)
        self.sound = str(sound)
        # Avoid mutable/dynamic defaults directly in the signature; evaluate time at runtime if 0.0 passed
        self.timestamp = float(timestamp) if timestamp != 0.0 else time()
        self.retry = int(retry)
        self.expire = int(expire)
        self.response_data = None
        self._api_callback = "messages.json"

    @property
    def attachment(self) -> dict[str, tuple[str, Any, str | None]] | str:
        if self._attachment != "":
            # Note: This opens a file stream that should be closed by the sender (e.g., requests)
            return {"attachment": (self._attachment, open(self._attachment, "rb"), self._mime_test)}
        return ""

    @property
    def json(self) -> dict[str, Any]:
        return {k: v for k, v in (
            ("message", self.message), ("title", self.title), ("device", self.device), ("url", self.url),
            ("url_title", self.url_title), ("priority", self.priority), ("sound", self.sound),
            ("timestamp", self.timestamp), ("retry", self.retry), ("expire", self.expire)) if v != ""}


class Glance:
    """
    A Glance message is a message that is sent to a smart watch or similar device that can display a small amount of information.
    """

    def __init__(self, title: str | None = None, text: str | None = None, subtext: str | None = None, count: int = 0, percent: int = 0) -> None:
        self.title: str = ""
        self.text: str = ""
        self.subtext: str = ""
        if isinstance(text, str):
            self.text = text[0:100]
        if isinstance(title, str):
            self.title = title[0:100]
        if isinstance(subtext, str):
            self.subtext = subtext[0:100]
        if not isinstance(count, int):
            raise TypeError("'count' must be an integer!")
        if not isinstance(percent, int) or not (0 <= percent <= 100):
            raise ValueError("'percent' must be an integer between 0 and 100!")
        self.count: int = count
        self.percent: int = percent
        self._api_callback: str = "glances.json"
        self.response_data: Any = None

    @property
    def json(self) -> dict[str, Any]:
        return {k: v for k, v in (
            ("title", self.title), ("text", self.text), ("subtext", self.subtext), ("count", self.count),
            ("percent", self.percent)) if v is not None or v != 0}


class Client:
    """
    The client for using the Pushover API.
    """

    def __init__(self, user_key: str, api_token: str):
        self.user_key: str = user_key
        self.api_token: str = api_token
        self._api_url: str = "https://api.pushover.net/1/"
        self.last_message: Message | Glance | None = None
        self._api_verify: str = "users/validate.json"
        self._api_limits: str = "apps/limits.json"
        self._api_receipts: str = "receipts/{}.json"

    def verify_user(self) -> requests.Response:
        # Added a 10-second timeout to prevent the application from hanging
        r: requests.Response = requests.post(
            self._api_url + self._api_verify,
            json={"token": self.api_token, "user": self.user_key},
            timeout=10.0
        )

        response_json = r.json()
        if isinstance(response_json, dict) and response_json.get("status") == 1:
            print("User is verified!")
        else:
            print("User is not verified!")

        return r

    def get_limits(self) -> requests.Response:
        # Added a 5-second timeout for a lighter GET request
        return requests.get(
            self._api_url + self._api_limits,
            params={"token": self.api_token},
            timeout=5.0
        )

    def send(self, message_obj: Message | Glance) -> requests.Response | None:
        """
        Send a Message or Glance to the Pushover API.
        Returns the requests.Response, or None if an error occurred before the request.
        """
        import logging as _logging
        self.last_message = message_obj
        url = self._api_url + message_obj._api_callback

        payload: dict[str, Any] = {
            "token": self.api_token,
            "user":  self.user_key,
            **message_obj.json,
        }

        # Only Message carries an attachment; Glance never does.
        # The attachment property returns a dict[str, tuple[filename, fileobj, mimetype]]
        # or the empty string "" when there is no attachment.
        # requests._Files is a complex union that doesn't cleanly accept our 3-tuple;
        # cast to Any so the type checker is satisfied while runtime behavior is correct.
        raw_attachment = getattr(message_obj, "attachment", "")
        has_attachment: bool = isinstance(raw_attachment, dict)
        files = cast(Any, raw_attachment) if has_attachment else None

        try:
            if has_attachment:
                # Multipart form — credentials + fields as data, file as the files arg.
                r = requests.post(url, data=payload, files=files, timeout=10.0)
            else:
                r = requests.post(url, json=payload, timeout=10.0)

            message_obj.response_data = r.json()
            return r  # noqa: TRY300

        except requests.RequestException as exc:
            _logging.getLogger(__name__).error("Pushover send failed: %s", exc)  # noqa: TRY400
            return None

