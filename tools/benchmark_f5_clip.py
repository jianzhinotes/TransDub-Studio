#!/usr/bin/env python3
"""Generate one local F5 clip and print latency/duration for diagnostics."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

from gradio_client import Client, handle_file
from pydub import AudioSegment


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:7860")
    parser.add_argument("--ref", type=Path, required=True)
    parser.add_argument("--ref-text", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--speed", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--nfe", type=int, default=32)
    args = parser.parse_args()

    started = time.monotonic()
    result = Client(args.url, httpx_kwargs={"timeout": 900}).predict(
        ref_audio_input=handle_file(str(args.ref)),
        ref_text_input=args.ref_text,
        gen_text_input=args.text,
        remove_silence=True,
        randomize_seed=False,
        seed_input=42,
        cross_fade_duration_slider=0.0,
        nfe_slider=args.nfe,
        speed_slider=args.speed,
        api_name="/basic_tts",
    )
    source = Path(result[0] if isinstance(result, (tuple, list)) else result)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, args.output)
    print(json.dumps({
        "elapsed_s": round(time.monotonic() - started, 3),
        "audio_duration_s": round(len(AudioSegment.from_file(args.output)) / 1000, 3),
        "speed": args.speed,
        "output": str(args.output),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
