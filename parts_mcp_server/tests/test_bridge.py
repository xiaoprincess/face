import json
import unittest
from unittest.mock import patch

import xiaozhi_bridge


class BridgeStatusTest(unittest.TestCase):
    def observe_result(self, result):
        status = xiaozhi_bridge._new_status()
        pending = {}
        with patch.object(xiaozhi_bridge, "_write_status"):
            xiaozhi_bridge._observe_message(
                json.dumps(
                    {
                        "id": 7,
                        "method": "tools/call",
                        "params": {"name": "request_part_borrow"},
                    }
                ),
                "xiaozhi",
                status,
                pending,
            )
            xiaozhi_bridge._observe_message(
                json.dumps({"id": 7, "result": result}),
                "local",
                status,
                pending,
            )
        return status["last_result"]

    def test_structured_business_failure_is_reported_as_error(self):
        self.assertEqual(
            self.observe_result(
                {"structuredContent": {"success": False, "message": "库存不足"}}
            ),
            "error",
        )

    def test_text_business_failure_is_reported_as_error(self):
        self.assertEqual(
            self.observe_result(
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({"success": False, "message": "识别失败"}),
                        }
                    ]
                }
            ),
            "error",
        )

    def test_successful_tool_result_is_reported_as_success(self):
        self.assertEqual(
            self.observe_result({"structuredContent": {"success": True}}),
            "success",
        )


if __name__ == "__main__":
    unittest.main()
