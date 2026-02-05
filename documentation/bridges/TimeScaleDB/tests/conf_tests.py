from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from classes.transports.timescaledb import RollupManager


@pytest.fixture
def mock_session() -> MagicMock:
    """Provides a fake SQLAlchemy session for all tests."""
    return MagicMock(spec=Session)

@pytest.fixture
def rollup_manager() -> RollupManager:
    """Provides an instance of RollupManager with a mocked logger."""
    from classes.transports.timescaledb import RollupManager
    manager = RollupManager()
    manager._log = MagicMock()
    return manager
