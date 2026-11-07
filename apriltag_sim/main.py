"""Entry point for the AprilTag desktop simulation."""

from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
if str(PACKAGE_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGE_DIR))

from ui import run  # noqa: E402


def _self_test() -> int:
    """Build the whole UI once and close it, so packaging errors surface early."""

    import tkinter as tk

    from config import SimConfig
    from ui import SimApp

    root = tk.Tk()
    root.withdraw()
    SimApp(root, SimConfig.load())
    root.after(1200, root.destroy)
    root.mainloop()
    print("UI_SELF_TEST_OK")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in argv:
        return _self_test()
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
