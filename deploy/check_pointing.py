"""Explicit physical pointing check; stop the website service before running."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "unified_web_app"))
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "web/agent_yuntai"))
from hardware import hardware


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", type=int, default=5)
    args = parser.parse_args()
    try:
        result = hardware.point_to_cabinet_status(args.tag, timeout=12, delay=1, lost_timeout=6)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        print("STOP_COMMANDS_CONFIRMED", hardware.emergency_stop(), flush=True)
