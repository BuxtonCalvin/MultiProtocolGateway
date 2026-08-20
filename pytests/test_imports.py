# Description: Import smoke tests for Python modules under classes.
# File: test_imports.py
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

"""Import smoke tests for Python modules under classes."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
CLASSES_ROOT: Path = PROJECT_ROOT / "classes"


def module_names() -> list[str]:
    """Return importable module names for classes and subfolders."""
    names: list[str] = []
    for path in CLASSES_ROOT.rglob("*.py"):
        if path.name == "__init__.py":
            continue
        rel: Path = path.relative_to(PROJECT_ROOT).with_suffix("")
        names.append(".".join(rel.parts))
    return sorted(names)


@pytest.mark.parametrize("module_name", module_names())
def test_classes_modules_import_or_skip_missing_optional_dependencies(module_name: str) -> None:
    """Smoke test: classes modules should import unless an optional third-party dependency is absent."""
    if module_name == "classes.WebServer.migrations.env":
        pytest.skip("Alembic env.py requires Alembic's migration runtime context")
    try:
        importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        pytest.skip(f"optional dependency unavailable while importing {module_name}: {exc.name}")
