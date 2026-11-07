"""Read-only stack checks; --hardware explicitly enables a physical MCP test.

Run on the Pi with parts_mcp_server/.venv/bin/python. No loan is created.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import urllib.request
import uuid
from contextlib import closing

from dotenv import dotenv_values
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent


def snapshot():
    with closing(sqlite3.connect(f"file:{ROOT / 'unified_web_app/instance/unified.db'}?mode=ro", uri=True)) as conn:
        rows = conn.execute("SELECT id, quantity FROM cabinet_components ORDER BY id").fetchall()
        return {"stock_hash": hashlib.sha256(json.dumps(rows).encode()).hexdigest(),
                "loan_count": conn.execute("SELECT COUNT(*) FROM part_applications").fetchone()[0]}


def payload(result):
    value = getattr(result, "structuredContent", None)
    if value is not None:
        return value
    for block in result.content:
        if getattr(block, "type", "") == "text":
            try:
                return json.loads(block.text)
            except ValueError:
                pass
    return {"is_error": result.isError}


async def check(args):
    before = snapshot()
    with urllib.request.urlopen("http://127.0.0.1:5050/health", timeout=10) as response:
        print("HTTP", response.status, response.read().decode(), flush=True)
    env = os.environ.copy()
    env.update({k: v for k, v in dotenv_values(ROOT / "parts_mcp_server/.env").items() if v is not None})
    env["PARTS_WEB_BASE_URL"] = "http://127.0.0.1:5050"
    params = StdioServerParameters(command=sys.executable,
        args=[str(ROOT / "parts_mcp_server/parts_mcp.py")], env=env,
        cwd=str(ROOT / "parts_mcp_server"))
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            listing = await session.list_tools()
            print("MCP_TOOLS", [tool.name for tool in listing.tools], flush=True)
            result = payload(await session.call_tool("get_inventory_overview", {}))
            print("INVENTORY", json.dumps(result, ensure_ascii=False), flush=True)
            if result.get("success") is not True:
                raise RuntimeError("MCP inventory check failed")
            if args.hardware:
                print("PHYSICAL_TEST_START", args.tag, flush=True)
                request_id = "deploy-check-" + uuid.uuid4().hex
                result = payload(await session.call_tool("test_face_and_point", {
                    "tag_id": args.tag, "timeout_seconds": 15,
                    "request_id": request_id,
                }))
                print("PHYSICAL_RESULT", json.dumps(result, ensure_ascii=False), flush=True)
                headers = {"X-Device-Token": env.get("PARTS_DEVICE_TOKEN", "")}
                for _ in range(30):
                    request = urllib.request.Request(
                        "http://127.0.0.1:5050/parts/api/device/tag-test/" + request_id,
                        headers=headers,
                    )
                    with urllib.request.urlopen(request, timeout=10) as response:
                        state = json.loads(response.read())
                    if state.get("worker_finished"):
                        print("WORKER_FINISHED", True, "stop_confirmed", state.get("stop_confirmed"), flush=True)
                        break
                    await asyncio.sleep(0.5)
                else:
                    raise RuntimeError("Physical worker has not exited; do not start another test")
    after = snapshot()
    print("DATA_UNCHANGED", before == after, flush=True)
    if before != after:
        raise RuntimeError("Inventory or loans changed during check; inspect concurrent activity")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hardware", action="store_true")
    parser.add_argument("--tag", type=int, default=5)
    asyncio.run(check(parser.parse_args()))
