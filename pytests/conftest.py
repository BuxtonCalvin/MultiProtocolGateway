# Description: Shared pytest fixtures for MultiProtocolGateway unit tests.
# File: conftest.py
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

"""Shared pytest fixtures for MultiProtocolGateway unit tests."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class DummySettings:
    """Small TransportSettings-compatible test double."""

    def __init__(self, name: str = "transport.test", **values: Any) -> None:
        self.name: str = name
        self.values: dict[str, Any] = values

    def _first_key(self, option: str | list[str]) -> str:
        if isinstance(option, list):
            for key in option:
                if key in self.values:
                    return key
            return option[0]
        return option

    def get(self, option: str | list[str], fallback: Any = None, **kwargs: Any) -> Any:
        return self.values.get(self._first_key(option), fallback)

    def getint(self, option: str | list[str], fallback: Any = None, **kwargs: Any) -> int:
        return int(self.get(option, fallback))

    def getfloat(self, option: str | list[str], fallback: Any = None, **kwargs: Any) -> float:
        return float(self.get(option, fallback))

    def getboolean(self, option: str | list[str], fallback: Any = None, **kwargs: Any) -> bool:
        raw = self.get(option, fallback)
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"1", "true", "yes", "on", "enabled", "enable"}

    def __contains__(self, key: object) -> bool:
        return key in self.values

    def __getitem__(self, key: str) -> Any:
        return self.values[key]


@pytest.fixture
def dummy_settings() -> type[DummySettings]:
    """Return the reusable TransportSettings test double class."""
    return DummySettings
