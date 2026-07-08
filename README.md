<div align="center">

# ✨ TransDub Studio

### Turn any video into another language — transcribe, translate, clone the voice, and dub — **100% on your own machine.**

A **free, local, open-source** alternative to CapCut dubbing &amp; ElevenLabs Dubbing Studio.

[![Latest release](https://img.shields.io/github/v/release/jianzhinotes/TransDub-Studio?color=2E7CF6&label=%E2%AC%87%EF%B8%8F%20download&sort=semver)](https://github.com/jianzhinotes/TransDub-Studio/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/jianzhinotes/TransDub-Studio/total?color=44b556&label=downloads)](https://github.com/jianzhinotes/TransDub-Studio/releases)
[![Stars](https://img.shields.io/github/stars/jianzhinotes/TransDub-Studio?style=flat&color=e0a94f)](https://github.com/jianzhinotes/TransDub-Studio/stargazers)
[![License: GPL-3.0](https://img.shields.io/badge/license-GPL--3.0-blue)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS-lightgrey)

**[⬇️ Download for Windows &amp; macOS](https://github.com/jianzhinotes/TransDub-Studio/releases/latest)** · [📖 中文说明](docs/README_CN.md) · [🚀 Why it's different](#why)

<br>

<!-- ============================================================
     👇 DEMO GOES HERE — this is the single highest-leverage asset.
     Record a 30–60s before/after clip (English video → Chinese dub
     with a cloned voice), export as a GIF, save it to
     docs/assets/demo.gif, then REPLACE the italic line below with:
         ![TransDub Studio demo](docs/assets/demo.gif)
     (A screen recording of the timeline dubbing studio works great too.)
     ============================================================ -->

🎬 *A 60-second demo lands here soon — meanwhile, [download it](https://github.com/jianzhinotes/TransDub-Studio/releases/latest) and try a clip in ~2 minutes.*

<br>

<sub>Author **jianzhinotes** · <jianzhi.notes@gmail.com> · built on [pyVideoTrans](https://github.com/jianchang512/pyvideotrans) · GPL-3.0</sub>

</div>

## What is TransDub Studio?

**TransDub Studio** is a customized build of [pyVideoTrans](https://github.com/jianchang512/pyvideotrans), rebuilt around a modern, CapCut/ElevenLabs-style workflow for AI video translation, subtitle translation, voice cloning, and dubbing — but running **on your own machine**.

`speech recognition → subtitle translation → AI dubbing / voice cloning → audio-video synthesis`

<a id="why"></a>

## 🚀 Why TransDub Studio — vs CapCut & ElevenLabs

The polished editing experience of CapCut and ElevenLabs, but **local, private, and free** — your video and voice never have to leave your computer.

| | **TransDub Studio** | CapCut (剪映) | ElevenLabs |
|---|:---:|:---:|:---:|
| **Runs locally / offline** | ✅ full pipeline can run 100% local | ☁️ cloud only | ☁️ cloud only |
| **Cost** | ✅ **free** local stack, or bring-your-own API | membership + dubbing limits | pay-per-character |
| **Length / watermark limits** | none | yes | quota-limited |
| **Data privacy** | ✅ stays on your machine | uploaded to cloud | uploaded to cloud |
| **Voice cloning** | ✅ F5-TTS, local | limited | ✅ cloud |
| **Channel choice** | ✅ 79 recognition/translation/TTS channels, mix & match | fixed | fixed |
| **Per-line dubbing edit** | ✅ | limited | ✅ |
| **Timeline proofreading** | ✅ | ✅ | ✅ |
| **Open source / customizable** | ✅ GPL-3.0 | ❌ | ❌ |

**Key advantages**

- **🔒 Local & private.** Recognition (faster-whisper), translation (local LLM / offline models), and voice cloning (F5-TTS) can all run offline. Nothing is uploaded unless *you* pick a cloud API. CapCut and ElevenLabs always send your media to their servers.
- **💰 Free, no limits.** A fully free stack — faster-whisper + Google/local-LLM translation + Edge-TTS — costs nothing, has **no subscription, no watermark, no length or quota caps**. ElevenLabs bills per character/minute; CapCut gates dubbing behind membership and time limits.
- **🎛 Your choice of engines.** 79 channels across recognition / translation / TTS. Free local, DeepSeek, OpenAI, Gemini, DeepL, ElevenLabs, Azure… mix them however you like — not locked to one vendor.
- **✂️ Best of both editors.** CapCut-style step-by-step proofreading (fix the transcript, then the translation, then the dubbing) **and** ElevenLabs-style per-line editing (edit source/translation, swap voices, re-dub, drag the timeline) — in one inline workspace, no popups.
- **♻️ Reopenable projects.** Every finished job is saved as a local project you can reopen anytime to re-edit and re-export — and it only re-runs alignment + merge, not the whole pipeline (no repeated API cost).
- **🖥 Native app experience.** Rebranded macOS `.app`, encrypted local API-key storage, remembered settings, and a classic Advanced Mode for batch processing and every parameter.

## ✨ The new Flow UI (default)

Launch the app and you land in a streamlined three-step flow:

1. **Home** — drag & drop a video (or click to browse), see recent tasks with status chips, one-click reopen of results.
2. **Configure** — everything on one page: source/target language, three channel cards (Speech Recognition / Translation / Dubbing) with live "configured / needs API key" status dots and inline API-key setup, model & voice pickers, and just three toggles (subtitles, auto-align, keep BGM). Your last choices are remembered.
3. **Progress** — a per-task six-stage stepper (prepare → recognize → translate → dub → align → merge). At the dubbing pause the **Dubbing Studio** opens automatically: per-line speaker cards (source + translation side by side), editable timeline (drag subtitle blocks, stretch edges), per-line re-dub with voice switching, and instant original/dubbed A/B preview — all before the final render.

The classic full-featured UI (batch processing, all 79 channels, advanced parameters) is still available via **Tools → Advanced Mode**.

### Run from source

## 📦 Installation

### Easiest — download an installer

Grab the latest installer from the [**Releases**](https://github.com/jianzhinotes/TransDub-Studio/releases) page:

- **Windows:** `TransDub-Studio-Setup-<version>.exe` → double-click, follow the wizard.
- **macOS:** `TransDub-Studio-<version>.dmg` → open, drag to Applications, then **right-click → Open** the first time (unsigned build).

The installer is small; on first launch it downloads the runtime + models (a few GB) and then runs fully local. Builds are **unsigned**, so Windows SmartScreen shows *More info → Run anyway* and macOS needs the right-click-Open once.

> Prefer the command line, or no installer published yet for your version? Use the one-liners below.

`uv` manages the Python 3.10 runtime for you, so there's nothing else to install by hand. First launch downloads the recognition model (faster-whisper) on demand; after that the core pipeline runs fully local. Dependencies + models take a few GB, so give the first setup a good connection and some patience.

### macOS — one command

Open **Terminal** and paste:

```bash
curl -fsSL https://raw.githubusercontent.com/jianzhinotes/TransDub-Studio/main/install.sh | bash
```

Then launch it anytime:

```bash
cd ~/TransDub-Studio && uv run python sp.py
```

### Windows — one command

Install [Git for Windows](https://git-scm.com/download/win) first, then in **PowerShell** paste:

```powershell
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/jianzhinotes/TransDub-Studio/main/install.ps1 | iex"
```

Then launch it anytime:

```powershell
cd $HOME\TransDub-Studio; uv run python sp.py
```

> On Windows, dependencies pull the **CUDA (GPU) build** of PyTorch. With an NVIDIA GPU + recent driver you get GPU acceleration automatically; without one it still runs on CPU (just slower). The macOS build uses the CPU/Metal PyTorch wheel.

### Manual (any platform)

```bash
git clone https://github.com/jianzhinotes/TransDub-Studio.git
cd TransDub-Studio
uv sync              # first time only, installs Python 3.10 + deps
uv run python sp.py
```

**Requirements:** macOS (Apple Silicon or Intel) or Windows 10/11, ~5–8 GB free for dependencies and models.

## Major improvements over the upstream project

### 0. Flow UI, Dubbing Studio & timeline preview

- New default CapCut/ElevenLabs-style flow: Home → single-page smart config → staged progress.
- ElevenLabs-style **Dubbing Studio** at the post-dubbing pause: speaker cards, editable timeline, per-line re-dub, A/B audio preview. No more countdown auto-skip.
- Read-only **Timeline Preview** tool (video + original/dubbed waveforms + subtitle blocks on one synced timeline) available from the Tools menu for any video + SRT.
- Fixed a long-standing bug where subtitle/text edits in the dubbing review step were silently discarded (queue_tts.json was never written back / reloaded).

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

