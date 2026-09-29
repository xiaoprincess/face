import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import simulate_xiaozhi_borrow


class RemoteSimulationTest(unittest.TestCase):
    def test_remote_command_uses_mcp_virtual_environment(self):
        args = SimpleNamespace(
            remote_host=None,
            remote_user=None,
            remote_path=None,
            remote_hostkey=None,
            remote_timeout=300,
            keyword="锂电池",
            quantity=1,
            execute=True,
        )
        completed = SimpleNamespace(returncode=0)
        with patch.dict(os.environ, {"PARTS_PLINK": sys.executable}, clear=False):
            with patch.object(
                simulate_xiaozhi_borrow.subprocess,
                "run",
                return_value=completed,
            ) as run:
                result = simulate_xiaozhi_borrow._run_on_raspberry_pi(args)

        self.assertEqual(result, 0)
        command = run.call_args.args[0]
        remote_command = command[-1]
        self.assertIn("cd /home/xiao/face/parts_mcp_server", remote_command)
        self.assertIn(
            "/home/xiao/face/parts_mcp_server/.venv/bin/python",
            remote_command,
        )
        self.assertIn("--execute", remote_command)
        self.assertEqual(run.call_args.kwargs["timeout"], 300)


if __name__ == "__main__":
    unittest.main()
