# TransDub Studio

<div align="center">

**A customized AI video translation and dubbing studio based on pyVideoTrans**

[中文说明](docs/README_CN.md) · [Upstream pyVideoTrans](https://github.com/jianchang512/pyvideotrans) · [License: GPL-3.0](LICENSE)

</div>

## What is TransDub Studio?

**TransDub Studio** is a downstream customized build of [pyVideoTrans](https://github.com/jianchang512/pyvideotrans), focused on a smoother macOS local workflow for AI video translation, subtitle translation, voice cloning, and dubbing.

It keeps the original pyVideoTrans pipeline:

`speech recognition → subtitle translation → AI dubbing / voice cloning → audio-video synthesis`

Then it adds practical improvements around local deployment, DeepSeek translation quality, F5-TTS voice cloning stability, macOS app behavior, and dubbing output reliability.

This is **not** the official pyVideoTrans project. It is a modified version distributed under the same **GPL-3.0** license.

## Major improvements over the upstream project

### 1. macOS app experience

- Renamed and packaged as **TransDub Studio**.
- Added a custom macOS-style app icon.
- Added a macOS `.app` wrapper with background startup.
- Avoids opening extra black Terminal windows when launching the app.
- Adds single-instance behavior so double-clicking does not start multiple copies.
- Keeps the F5-TTS local service running quietly in the background.
- Uses a local Application Support runtime path to reduce macOS permission problems.

### 2. DeepSeek translation improvements

- Tunes the DeepSeek subtitle translation prompt for full-context subtitle translation.
- Sends SRT content with broader context instead of translating many small isolated chunks.
- Encourages consistent terminology, better sentence continuity, and more natural Chinese output.
- Reduces accidental untranslated English where the target output should be Chinese.
- Includes prompt-aware translation caching so prompt changes do not keep reusing stale translations.

### 3. F5-TTS voice cloning reliability

- Adds safer reference-audio selection to avoid leaking names or English phrases from reference clips into generated Chinese dubbing.
- Adds detection for unexpected English words in generated Chinese audio.
- Adds retry behavior when F5-TTS output appears to contain subtitle-unrelated English.
- Reduces heavy local inference settings for better behavior on Apple Silicon machines.
- Adds memory cleanup around local F5-TTS inference to reduce long-run instability.
- Fails fast when dubbing generation fails, instead of silently producing a broken final video.

### 4. Dubbing and audio output quality

- Improves handling around failed TTS segments.
- Reduces the chance of mixed original English appearing in a final dubbed result.
- Improves voice-cloning workflow stability for local video translation experiments.
- Keeps manual subtitle/proofreading workflow from pyVideoTrans while adding extra checks around generated audio.

### 5. Project identity and licensing cleanup

- Adds a clear downstream identity: **TransDub Studio**.
- Keeps attribution to the original pyVideoTrans author and project.
- Adds [NOTICE](NOTICE) and [MODIFICATIONS.md](MODIFICATIONS.md) to clearly document that this is a modified build.
- Keeps the project under GPL-3.0, consistent with upstream pyVideoTrans.

## Who is this for?

TransDub Studio is mainly for users who want to run an AI video translation and dubbing workflow locally on macOS, especially when using:

- DeepSeek-compatible APIs for subtitle translation.
- Local F5-TTS voice cloning.
- Chinese dubbing output from English source videos.
- A double-clickable macOS app experience instead of command-line-only usage.

## Current status

This repository is a personal downstream build. It is useful as a working customized version, but it is not an official release channel of pyVideoTrans.

Large local models are **not** intended to be committed into this repository. They should be downloaded or deployed separately when needed.

## Source deployment

Requirements:

- Python 3.10
- FFmpeg
- `uv`

Clone:

```bash
git clone https://github.com/jianzhinotes/TransDub-Studio.git
cd TransDub-Studio
```

Install dependencies:

```bash
uv sync
```

Launch:

```bash
uv run sp.py
```

## Supported workflow

TransDub Studio inherits the broad pyVideoTrans feature set, including:

- Speech recognition / subtitle generation.
- Subtitle translation through local or online translation channels.
- AI dubbing and voice cloning.
- Audio/video/subtitle merging.
- Manual proofreading during recognition, translation, and dubbing.
- CLI usage for batch processing.

See upstream pyVideoTrans documentation for the general feature set and configuration details:

- [pyVideoTrans repository](https://github.com/jianchang512/pyvideotrans)
- [pyVideoTrans documentation](https://pyvideotrans.com)

## License and attribution

TransDub Studio is based on [pyVideoTrans](https://github.com/jianchang512/pyvideotrans), created by [jianchang512](https://github.com/jianchang512).

The original project is licensed under **GPL-3.0**. This modified version is also distributed under **GPL-3.0**.

This repository is not affiliated with or endorsed by the official pyVideoTrans project. For details, see:

- [LICENSE](LICENSE)
- [NOTICE](NOTICE)
- [MODIFICATIONS.md](MODIFICATIONS.md)

## Acknowledgements

This project relies on the work of pyVideoTrans and many open-source projects, including:

- [pyVideoTrans](https://github.com/jianchang512/pyvideotrans)
- [FFmpeg](https://github.com/FFmpeg/FFmpeg)
- [PySide6](https://pypi.org/project/PySide6/)
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- [openai-whisper](https://github.com/openai/whisper)
- [edge-tts](https://github.com/rany2/edge-tts)
- [F5-TTS](https://github.com/SWivid/F5-TTS)
- [CosyVoice](https://github.com/FunAudioLLM/CosyVoice)

