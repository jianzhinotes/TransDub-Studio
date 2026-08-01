#!/usr/bin/env python3
"""Repair run/performance journals whose owner process no longer exists."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from videotrans.dub.run_state import repair_stale_project_run


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--reason", default="旧版运行进程已退出，任务安全中断")
    args = parser.parse_args()
    repaired = repair_stale_project_run(args.project_dir.expanduser(), args.reason)
    print(f"repaired={repaired}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
