#!/usr/bin/env python3
"""Preserve generated clips from a failed process-specific cache directory."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from videotrans.task.trans_create import TransCreate


def recover(project_dir: Path, cache_dir: Path) -> int:
    project = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    queue_path = project_dir / "checkpoints" / "smart-plan" / "smart_queue.json"
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    available = []
    for item in queue:
        candidate = cache_dir / Path(item.get("filename") or "").name
        if candidate.is_file() and candidate.stat().st_size > 0:
            available.append({**item, "filename": str(candidate)})

    task = TransCreate.__new__(TransCreate)
    cfg = project.get("cfg") or {}
    task.cfg = SimpleNamespace(
        target_dir=str(project_dir.parent),
        noextname=project_dir.name.removesuffix(".tdproj"),
        target_language_code=(project.get("target_language_code")
                              or cfg.get("target_language_code") or "zh-cn"),
        tts_type=int(cfg.get("tts_type") or 8),
        clear_cache=False,
    )
    task.queue_tts = available
    task.signal = lambda **_kwargs: None
    return task._save_dubbing_checkpoint()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("cache_dir", type=Path)
    args = parser.parse_args()
    saved = recover(args.project_dir.expanduser(), args.cache_dir.expanduser())
    print(f"saved={saved}")
    return 0 if saved else 1


if __name__ == "__main__":
    raise SystemExit(main())
