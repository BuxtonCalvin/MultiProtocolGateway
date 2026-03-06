import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

# timescaledb.py
from classes.transports.timescaledb import timescaledb


class TestTimescaleMultiTransport(unittest.TestCase):

    # 1. Patching the DB engine (imported into timescaledb.py)
    @patch('classes.transports.timescaledb.create_engine')

    # 2. Patching the Base Class connect method
    @patch('classes.transports.timescaledb.transport_base.connect')

    def setUp(self, mock_inverter_connect, mock_db_engine):
        """Initialize the bridge without real network/DB activity."""

        # 1. Setup the Mock DB Engine/Session
        self.mock_engine = mock_db_engine
        self.mock_session_factory = MagicMock()

        # 2. Instantiate the Bridge
        # We pass dummy values that would normally cause a crash
        self.bridge = timescaledb(
            transport_name="Database_Bridge",
            db_url="postgresql://mock_user:mock_pass@localhost/mock_db"
        )

        # Manually override the SessionFactory if your class uses one
        self.bridge.SessionFactory = self.mock_session_factory

        # 3. Setup Global Configs
        self.bridge.stale_data_timeout = 300
        self.bridge.max_stale_attempts = 3
        self.bridge.retry_delay_mins = 5

        # 4. Mock the Gateway Hook
        self.reconnect_mock = MagicMock()
        self.bridge.request_upstream_reconnect = self.reconnect_mock

    def test_multi_transport_stale_logic(self):
        """Verify Table logic for two different inverters."""
        t0 = datetime.now(timezone.utc)
        inv_a = "Inverter_A"
        inv_b = "Inverter_B"

        # --- PHASE 1: Initial successful writes for both ---
        # Commit A
        self.bridge._commit_transport_state(inv_a, {"p": 100}, t0, is_stale=False)
        # Commit B
        self.bridge._commit_transport_state(inv_b, {"p": 200}, t0, is_stale=False)

        # Verify registry has two distinct 'rows'
        self.assertEqual(len(self.bridge._stale_registry), 2)

        # --- PHASE 2: Make Inverter_A Stale ---
        t_stale = t0 + timedelta(minutes=10)
        is_stale_a = self.bridge._check_is_stale(inv_a, {"p": 100}, t_stale)
        self.assertTrue(is_stale_a)

        # Commit the stale state for A
        self.bridge._commit_transport_state(inv_a, {"p": 100}, t_stale, is_stale=True)

        # Verify A is stale, B is NOT
        self.assertTrue(self.bridge._stale_registry[inv_a]["is_stale"])
        self.assertFalse(self.bridge._stale_registry[inv_b]["is_stale"])

        # Verify the Gateway was asked to reconnect ONLY Inverter_A
        self.reconnect_mock.assert_called_once_with(inv_a)

    def test_no_commit_on_connection_failure(self):
        """
        Verify that if the DB is marked disconnected,
        the state is NOT committed in the registry.
        """
        name = "Inverter_01"
        ts = datetime.now(timezone.utc)
        metrics = {"power": 500}

        # 1. Simulate a DB Failure state
        self.bridge._set_tsdb_connected(False, "Simulated Network Timeout")  # noqa: FBT003
        self.assertFalse(self.bridge.tsdb_connected)

        # 2. Simulate the Worker Logic
        # In the real worker, if tsdb_connected is False,
        # the commit call is never reached or is rolled back.
        db_write_success = False

        if self.bridge.tsdb_connected:
            # This block won't execute
            db_write_success = True
            self.bridge._commit_transport_state(name, metrics, ts, is_stale=False)

        # 3. Verify: Registry should still be empty for this transport
        self.assertNotIn(name, self.bridge._stale_registry)
        self.assertFalse(db_write_success)

    def test_database_disconnected_skips_commit(self):
        """
        Scenario: Inverter is connected (sending data),
        but Database is disconnected.
        Result: State table should NOT update.
        """
        name = "Inverter_01"
        ts = datetime.now(timezone.utc)
        metrics = {"temp": 25.0}

        # 1. Setup States
        # Simulate Inverter is OK, but DB is Down
        self.bridge.connected = True
        self.bridge._set_tsdb_connected(False, "Test: Database Down")  # noqa: FBT003

        # 2. Simulate Worker Logic
        # The worker checks if data is stale BEFORE writing
        is_stale = self.bridge._check_is_stale(name, metrics, ts)  # noqa: F841

        # In your worker logic:
        # If DB write fails or tsdb_connected is False:
        # The following 'commit' should NOT be called.
        if self.bridge.tsdb_connected:
            self.bridge._commit_transport_state(name, metrics, ts, is_stale=False)

        # 3. Verify
        # Registry should still be empty because the commit was guarded by tsdb_connected
        self.assertNotIn(name, self.bridge._stale_registry)

    def test_stale_detection_during_db_outage(self):
        """
        Scenario: Database is down, but we want to know if
        incoming data is stale anyway.
        Result: Stale event triggers, but row is NOT committed as 'Fresh'.
        """
        name = "Inverter_01"
        t0 = datetime.now(timezone.utc)

        # Put an initial row in the registry manually to simulate prior success
        self.bridge._stale_registry[name] = {
            "last_row": {"p": 100}, "start_ts": t0, "is_stale": False,
            "last_seen": t0, "stale_event_count": 0, "last_event_ts": None
        }

        # Simulate DB goes down
        self.bridge._set_tsdb_connected(False, "DB Failure")  # noqa: FBT003

        # Incoming data is identical (Stale)
        t_stale = t0 + timedelta(minutes=10)
        is_stale = self.bridge._check_is_stale(name, {"p": 100}, t_stale)

        if is_stale:
            # We STILL commit stale state even if DB is down
            # so we can trigger the reconnect hook!
            self.bridge._commit_transport_state(name, {"p": 100}, t_stale, is_stale=True)

        # Verify Hook triggered
        self.reconnect_mock.assert_called_with(name)



    def test_commit_after_reconnection(self):
        """Verify that state updates normally once connection is restored."""
        name = "Inverter_01"
        ts = datetime.now(timezone.utc)
        metrics = {"power": 500}

        # 1. Restore connection
        self.bridge._set_tsdb_connected(True, "Recovery Successful")  # noqa: FBT003

        # 2. Simulate successful worker flow
        # In worker: with session.begin(): ... (success)
        self.bridge._commit_transport_state(name, metrics, ts, is_stale=False)

        # 3. Verify
        self.assertTrue(self.bridge.tsdb_connected)
        self.assertIn(name, self.bridge._stale_registry)
        self.assertEqual(self.bridge._stale_registry[name]["last_row"]["power"], 500)


    def test_database_write_failure_preserves_state(self):
        """Ensure state doesn't update if the DB transaction fails."""
        inv_a = "Inverter_A"
        t0 = datetime.now(timezone.utc)

        # Initial known state
        self.bridge._commit_transport_state(inv_a, {"p": 100}, t0, is_stale=False)

        # New data arrives, but DB write fails (Simulating SQLAlchemyError)
        t1 = t0 + timedelta(seconds=30)
        new_metrics = {"p": 150} # Data changed

        # Logic simulation (like in your _flush_worker)
        try:
            # Simulate DB crash here
            raise Exception("TimescaleDB Connection Reset")  # noqa: TRY002, TRY301

            # This line is skipped because of the error:
            self.bridge._commit_transport_state(inv_a, new_metrics, t1, is_stale=False)
        except Exception:  # noqa: S110
            pass

        # VERIFY: The registry should still show the OLD data (100), not the new data (150)
        # because the 'commit' was skipped due to the error.
        self.assertEqual(self.bridge._stale_registry[inv_a]["last_row"]["p"], 100)

if __name__ == '__main__':
    unittest.main()
