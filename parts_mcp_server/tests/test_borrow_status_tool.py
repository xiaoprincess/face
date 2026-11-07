import asyncio
import unittest
from unittest.mock import patch
from fastmcp import Client
import parts_mcp


class BorrowStatusToolTests(unittest.TestCase):
    def call(self, request_id):
        async def run():
            async with Client(parts_mcp.mcp) as client:
                result = await client.call_tool("get_part_borrow_status", {"request_id": request_id})
                return result.structured_content or result.data
        return asyncio.run(run())

    def test_status_uses_only_get(self):
        with patch.object(parts_mcp, "_http_json", return_value=(200, {"success": True, "guidance_status": "timeout"})) as http:
            data = self.call("existing-request")
            self.assertEqual(data["guidance_status"], "timeout")
            self.assertEqual(http.call_args.args[0], "GET")
            self.assertTrue(http.call_args.args[1].endswith("/parts/api/device/borrow/existing-request"))

    def test_invalid_id_never_calls_website(self):
        with patch.object(parts_mcp, "_http_json") as http:
            self.assertFalse(self.call("../other")["success"])
            http.assert_not_called()
