"""Tests for classes.log_redaction: secrets must not reach a log file, and ordinary lines must pass untouched."""

from __future__ import annotations

import configparser
import logging
import logging.handlers
import queue
from collections.abc import Iterator
from pathlib import Path

import pytest

from classes import log_redaction as lr

# Deliberately fake values with the real shapes (a Telegram token is "<8-10 digits>:<35 characters>").
FAKE_TOKEN = "1234567890:AAFakeFakeFakeFakeFakeFakeFake12345"  # noqa: S105


@pytest.fixture(autouse=True)
def _no_registered_secrets() -> Iterator[None]:
    lr.clear_secrets()
    yield
    lr.clear_secrets()


@pytest.mark.parametrize(
    "line",
    [
        f'HTTP Request: POST https://api.telegram.org/bot{FAKE_TOKEN}/getMe "HTTP/1.1 200 OK"',
        f"Set Bot API URL: https://api.telegram.org/bot{FAKE_TOKEN}/$methodName",
        f"https://api.telegram.org/file/bot{FAKE_TOKEN}/photos/a.jpg",
        f"a bare token {FAKE_TOKEN} in the middle of text",
        "Authorization: Bearer abcdef1234567890ABCDEF.xyz",
        "headers={'Authorization': 'Bearer abcdef1234567890', 'Accept': 'x'}",
        "connect postgresql://postgres:s3cretPW@10.0.0.5:5431/solar",
        "POST body token=abc123def456&user=u1&message=hello",
        "settings {'password': 'hunter22222', 'host': 'h'}",
        '{"api_key": "k-12345678", "x": 1}',
        "mqtt connect password=pa55word! host=h",
    ],
)
def test_known_shapes_are_redacted(line: str) -> None:
    out: str = lr.redact(line)
    assert "<redacted>" in out
    for secret in (FAKE_TOKEN, FAKE_TOKEN.split(":")[1], "abcdef1234567890", "s3cretPW", "abc123def456", "hunter22222", "k-12345678", "pa55word!"):
        assert secret not in out


def test_telegram_url_keeps_its_shape_for_diagnosis() -> None:
    out: str = lr.redact(f"POST https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage")
    assert out == "POST https://api.telegram.org/bot<redacted>/sendMessage"


@pytest.mark.parametrize(
    "line",
    [
        "Reconnect attempt 3 — waiting 7.93s...",
        "Wrote 2 points to InfluxDB (serial numbers: ,)",
        "token_count=5 tokens=none passwords_checked=0",
        "decoded PDU function_code(3 sub -1) -> ReadHoldingRegistersResponse(registers=[12340, 13878], status=1)",
        "Processing: 0x0 0x1 0x0 0x0 0x0 0x7 0x1 0x3 0x4 0x30 0x34 0x36 0x36",
        "2026-09-08 19:57:10,159 [WARNING] classes.transports.transport_base: [DISCONNECTED] transport.mqtt connection lost.",
        "Set Bot API URL: https://api.telegram.org/bot",
    ],
)
def test_ordinary_log_lines_are_untouched(line: str) -> None:
    assert lr.redact(line) == line


def test_registered_secret_is_replaced_in_any_format_and_short_values_are_ignored() -> None:
    lr.register_secret("Zx9-plain-secret")
    lr.register_secret("abc")  # too short to register safely
    assert lr.redact("value is Zx9-plain-secret!") == "value is <redacted>!"
    assert lr.redact("abc stays") == "abc stays"
    lr.register_secret("Zx9-plain-secret-and-more")
    assert lr.redact("Zx9-plain-secret-and-more") == "<redacted>"  # the longer secret is replaced as a whole


def test_config_secrets_are_registered_by_option_name() -> None:
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_string(
        "[messages]\ntelegram_bot_token = 99999999:NotARealTokenNotARealTokenNotAReal1\npushover_user_key = uUserKeyValue123456\n"
        "[mqtt]\npassword = my-broker-password\nhost = 10.0.0.1\nmax_tokens_free = 100000\n"
    )
    assert lr.register_config_secrets(cfg) == 3
    out: str = lr.redact("odd place: my-broker-password / uUserKeyValue123456 / host 10.0.0.1")
    assert out == "odd place: <redacted> / <redacted> / host 10.0.0.1"


# --------------------------------------------------------------------------- the filter on real handlers
def _logger_with_handler(name: str, stream_handler: logging.Handler) -> logging.Logger:
    logger: logging.Logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.addHandler(stream_handler)
    return logger


def test_filter_redacts_the_final_output_including_lazy_args_and_tracebacks(tmp_path: Path) -> None:
    path: Path = tmp_path / "t.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger: logging.Logger = _logger_with_handler("redaction.test.file", handler)
    assert lr.install_redaction(logger) == 1
    assert lr.install_redaction(logger) == 0  # idempotent

    logger.info("request to %s failed", f"https://api.telegram.org/bot{FAKE_TOKEN}/getMe")
    failure = f"cannot reach https://api.telegram.org/bot{FAKE_TOKEN}/getMe"
    try:
        raise RuntimeError(failure)  # noqa: TRY301
    except RuntimeError:
        logger.exception("send failed")
    logger.info("plain line stays readable: %d items", 3)
    handler.close()

    text: str = path.read_text(encoding="utf-8")
    assert FAKE_TOKEN not in text and FAKE_TOKEN.split(":")[1] not in text
    assert "request to https://api.telegram.org/bot<redacted>/getMe failed" in text
    assert "RuntimeError: cannot reach https://api.telegram.org/bot<redacted>/getMe" in text
    assert "plain line stays readable: 3 items" in text


def test_filter_covers_records_that_pass_through_a_queue_listener(tmp_path: Path) -> None:
    """The web UI sends records through a QueueHandler/QueueListener into the root handlers (see WebServer.main)."""
    path: Path = tmp_path / "q.log"
    sink = logging.FileHandler(path, encoding="utf-8")
    sink.setFormatter(logging.Formatter("%(message)s"))
    sink.addFilter(lr.SecretRedactionFilter())
    q: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
    listener = logging.handlers.QueueListener(q, sink)
    logger: logging.Logger = _logger_with_handler("redaction.test.queue", logging.handlers.QueueHandler(q))
    listener.start()
    try:
        logger.info("POST https://api.telegram.org/bot%s/getMe", FAKE_TOKEN)
        failure = f"token=abc123def456 {FAKE_TOKEN}"
        try:
            raise ValueError(failure)  # noqa: TRY301
        except ValueError:
            logger.exception("boom")
    finally:
        listener.stop()
        sink.close()
    text: str = path.read_text(encoding="utf-8")
    assert FAKE_TOKEN not in text and "abc123def456" not in text and "<redacted>" in text


def test_malformed_log_calls_do_not_break_logging(tmp_path: Path) -> None:
    handler = logging.FileHandler(tmp_path / "m.log", encoding="utf-8")
    logger: logging.Logger = _logger_with_handler("redaction.test.bad", handler)
    lr.install_redaction(logger)
    logger.info("too few args: %s %s", "only-one")  # logging prints its own error to stderr; it must not raise
    handler.close()
