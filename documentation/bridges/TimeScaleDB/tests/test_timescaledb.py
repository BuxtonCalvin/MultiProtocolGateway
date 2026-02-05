from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from classes.transports.timescaledb import RollupManager


@pytest.fixture
def mock_session() -> MagicMock:
    return MagicMock(spec=Session)

@pytest.fixture
def rollup_manager() -> RollupManager:

    obj = RollupManager()
    obj._log = MagicMock()
    return obj

def test_rollup_needs_rebuild_success(rollup_manager: RollupManager, mock_session: MagicMock) -> None:
    # Setup: Define the 3 values the DB should return in order
    # 1. view_definition, 2. current_interval_str, 3. is_match (bool)
    mock_session.scalar.side_effect = [
        "SELECT time_bucket('01:00:00'::interval, ...)", 
        "01:00:00", 
        True
    ]

    # Execute
    result: bool = rollup_manager.rollup_needs_rebuild(mock_session, "my_view", "hourly")

    # Assert
    assert result is False  # match found, so no rebuild needed  # noqa: S101
    assert mock_session.scalar.call_count == 3  # noqa: S101

def test_rollup_needs_rebuild_missing_view(rollup_manager, mock_session):
    # Setup: First call to DB returns None (view doesn't exist)
    mock_session.scalar.return_value = None

    result = rollup_manager.rollup_needs_rebuild(mock_session, "ghost_view", "hourly")

    assert result is True  # View missing, rebuild required  # noqa: S101

def test_timescaledb_calls_rollup_manager(mocker):
    from classes.transports.timescaledb import timescaledb

    # Mock the RollupManager instance inside the timescaledb class
    mock_manager = mocker.patch('timescaledb.RollupManager')
    mock_manager.return_value.rollup_needs_rebuild.return_value = False

    db = timescaledb()
    # Assume some method in timescaledb calls rollup_needs_rebuild
    db.check_all_rollups() 

    assert mock_manager.return_value.rollup_needs_rebuild.called  # noqa: S101
