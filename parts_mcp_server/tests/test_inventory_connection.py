import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from inventory_store import _connect


class ConnectionLifetimeTests(unittest.TestCase):
    def test_connection_is_closed_after_success(self):
        connection = MagicMock()
        with patch.object(Path, "exists", return_value=True), patch("inventory_store.sqlite3.connect", return_value=connection):
            with _connect(Path("test-only.db")) as opened:
                self.assertIs(opened, connection)
        connection.close.assert_called_once()
        connection.__exit__.assert_called_once_with(None, None, None)

    def test_connection_is_closed_after_transaction_failure(self):
        connection = MagicMock()
        with patch.object(Path, "exists", return_value=True), patch("inventory_store.sqlite3.connect", return_value=connection):
            with self.assertRaises(ValueError):
                with _connect(Path("test-only.db")):
                    raise ValueError("rollback required")
        connection.close.assert_called_once()
        self.assertEqual(connection.__exit__.call_args.args[0], ValueError)
