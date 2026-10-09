# Description: log_redaction.py
# File: log_redaction.py
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

"""log_redaction.py

Keeps secrets out of the log file.

Third-party libraries log things MPG never chose to log: ``httpx`` prints every request URL at INFO,
and a Telegram request URL *contains the bot token* (``https://api.telegram.org/bot<token>/getMe``);
at DEBUG, HTTP and database clients can print headers, connection strings and settings dictionaries.
A log is the file people attach to bug reports, so it must not hold credentials.

``SecretRedactionFilter`` is attached to the *handlers* (not to individual loggers, which would miss
records from child loggers) so it sees every record that is written, whichever library produced it.
Exception tracebacks are kept in full -- frames, file names, line numbers, source lines and the exception
type and message -- and only a credential found inside one is replaced.  Source lines are code, not data,
so inside a traceback frame only the unmistakable secret shapes are applied (a token, a ``Bearer`` value,
URL credentials, a registered secret), never the ``password=...`` / ``'token': ...`` patterns, which would
rewrite the code being shown (``password=password`` is not a leak).  It redacts in two complementary ways:

* **Patterns** for well-known shapes: Telegram bot tokens, ``Bearer``/``Basic`` credentials,
  ``user:password@host`` in URLs, ``password=...`` / ``token=...`` pairs, and ``'password': '...'``
  entries in dumped dictionaries or JSON.
* **Known secrets**: every ``password``/``token``/``secret``/``api_key``/``user_key`` value in
  ``config.cfg`` is registered at startup (``register_config_secrets``) and replaced wherever it
  appears, in any format.  This is what catches a token in a place no pattern anticipated.

Redaction only changes what is written; it never changes what is sent.  Files written before this
filter existed are not rewritten, so old logs may still contain secrets.
"""
from __future__ import annotations

import logging
import re
import threading
from configparser import ConfigParser

REDACTED: str = "<redacted>"

_SECRET_KEY: str = r"(?:password|passwd|pwd|secret|api[_-]?key|apikey|access[_-]?token|auth[_-]?token|bot[_-]?token|user[_-]?key|token)"  # noqa: S105 - a regex of option names, not a password

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Telegram Bot API URL: .../bot<digits>:<token>/method (also /file/bot...)
    (re.compile(r"(?i)\b(bot)\d{6,12}:[A-Za-z0-9_-]{30,}"), r"\1<redacted>"),
    # A bare Telegram-token-shaped string anywhere else
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}"), REDACTED),
    # Authorization headers: Bearer <token>, Basic <base64>
    (re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 <redacted>"),
    # Credentials embedded in a URL: scheme://user:password@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^@\s/]+)@"), r"\1\2:<redacted>@"),
    # key=value in query strings, form bodies and keyword arguments
    (re.compile(rf"(?i)\b({_SECRET_KEY})=([^&\s'\",;)\]}}]+)"), r"\1=<redacted>"),
    # 'key': 'value' / "key": "value" in dumped dicts and JSON
    (re.compile(rf"""(?i)((["'])(?:{_SECRET_KEY}|authorization)\2\s*:\s*)(["'])[^"']*\3"""), r"\1\3<redacted>\3"),
)

_MIN_SECRET_LENGTH: int = 6  # shorter values would mangle ordinary words; the key=value patterns still cover them
_secret_lock: threading.Lock = threading.Lock()
_secrets: set[str] = set()
_secrets_re: re.Pattern[str] | None = None


_NOT_SECRETS: frozenset[str] = frozenset({"true", "false", "none", "null", "yes", "no", "on", "off"})


def register_secret(value: str | None) -> None:
    """Replace ``value`` with ``<redacted>`` wherever it appears in a log record from now on."""
    global _secrets_re
    if not value or len(value.strip()) < _MIN_SECRET_LENGTH:
        return
    # A number or a flag is a setting, not a credential, and registering one would blank it out of every log line.
    if value.strip().isdigit() or value.strip().lower() in _NOT_SECRETS:
        return
    with _secret_lock:
        _secrets.add(value.strip())
        # Longest first, so a secret that contains another is replaced as a whole.
        _secrets_re = re.compile("|".join(re.escape(s) for s in sorted(_secrets, key=len, reverse=True)))


def register_config_secrets(cfg: ConfigParser) -> int:
    """
    Register every secret-looking value in ``config.cfg`` (any option whose name contains
    password, passwd, pwd, secret, token, api_key or user_key). Returns how many were registered.
    """
    # Whole words only: "telegram_bot_token" and "pushover_user_key" match, "max_tokens_free" does not.
    name_re: re.Pattern[str] = re.compile(rf"(?i)(?<![a-z0-9]){_SECRET_KEY}(?![a-z0-9])")
    count: int = 0
    for section in cfg.sections():
        for option in cfg.options(section):
            if name_re.search(option):
                value: str = cfg.get(section, option, raw=True, fallback="")
                before: int = len(_secrets)
                register_secret(value)
                count += len(_secrets) - before
    return count


def clear_secrets() -> None:
    """Forget every registered secret (used by tests)."""
    global _secrets_re
    with _secret_lock:
        _secrets.clear()
        _secrets_re = None


# The first four patterns are unmistakable secret shapes and are safe anywhere; the rest key off a NAME
# (password=, 'token':) and so would also match ordinary code such as ``login(password=password)``.
_SHAPE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = _PATTERNS[:4]
_FRAME_RE: re.Pattern[str] = re.compile(r'^\s*File ".*", line \d+')
_CODE_INDENT: str = "    "


def _redact_line(line: str, code: bool = False) -> str:
    for pattern, replacement in _SHAPE_PATTERNS if code else _PATTERNS:
        line = pattern.sub(replacement, line)
    known: re.Pattern[str] | None = _secrets_re
    if known is not None:
        line = known.sub(REDACTED, line)
    return line


def redact(text: str) -> str:
    """
    Return ``text`` with credentials replaced by ``<redacted>``.

    In multi-line text the source and caret lines that follow a traceback's ``File "...", line N`` header
    are code, so only the unmistakable secret shapes are applied to them (see the module docstring).
    """
    if "\n" not in text:
        return _redact_line(text)
    out: list[str] = []
    in_frame: bool = False
    for line in text.split("\n"):
        if _FRAME_RE.match(line):
            in_frame = True
            out.append(_redact_line(line))
        elif in_frame and line.startswith(_CODE_INDENT):
            out.append(_redact_line(line, code=True))
        else:
            in_frame = False
            out.append(_redact_line(line))
    return "\n".join(out)


class SecretRedactionFilter(logging.Filter):
    """Handler filter that redacts credentials from the message, exception text and stack of every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message: str = record.getMessage()
        except Exception:  # noqa: BLE001 - a malformed log call must not become a logging failure
            message = str(record.msg)
        redacted: str = redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        # Tracebacks are formatted later by the handler's formatter, which reuses exc_text if it is set.
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def install_redaction(logger: logging.Logger | None = None) -> int:
    """
    Attach SecretRedactionFilter to every handler of ``logger`` (default: the root logger) that does not
    already have one. Returns how many handlers were newly covered.
    """
    target: logging.Logger = logger if logger is not None else logging.getLogger()
    added: int = 0
    for handler in target.handlers:
        if not any(isinstance(f, SecretRedactionFilter) for f in handler.filters):
            handler.addFilter(SecretRedactionFilter())
            added += 1
    return added
