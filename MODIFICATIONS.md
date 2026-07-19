# Modifications in TransDub Studio

TransDub Studio is a customized build based on [pyVideoTrans](https://github.com/jianchang512/pyvideotrans).

Notable downstream changes include:

- macOS application wrapper and local deployment improvements.
- Custom macOS-style application icon.
- Single-instance launch behavior and no-terminal background startup.
- DeepSeek subtitle translation tuning for better whole-context translation.
- F5-TTS local voice-cloning workflow fixes, including safer reference-audio selection and English-leak detection.
- Dubbing reliability improvements to avoid silently producing broken output when synthesis fails.
- Versioned `.tdproj` v2 state with stable dubbing-unit IDs, candidate history, quality reports, and legacy-project migration.
- An experimental local-first joint dubbing planner that evaluates semantic grouping, Chinese wording, duration, synthesis, and quality feedback in one loop, with an optional whole-turn DeepSeek candidate backend, strict local validation, response caching, and deterministic offline fallback.
- A non-blocking, default-on Dubbing Studio smart-planning preview that uses DeepSeek when configured, falls back locally, exposes per-segment timing diagnostics, and never automatically replaces subtitles or audio.
- Deferred whole-track waveform and dubbed-preview construction for long videos, keeping the editor responsive while retaining immediate timeline and per-segment controls.
- Explicit per-segment A/B synthesis and listening for saved plans, reusing the current TTS backend without retranslation or changing the production audio selection.
- A one-click smart-dubbing default flow: after choosing a video and target language, recognition, translation, joint semantic/timing orchestration, TTS, quality checks, alignment, and rendering run without routine proofreading pauses. Engine controls remain available under Advanced settings, and persisted smart queues provide interruption-safe resume.
- Long-video F5-TTS hardening: source-timeline reference extraction, Apple Silicon low-memory lifecycle management, high-risk preflight samples, batch plus per-clip language gates, service recovery, cache-preserving retries, and ASR-verified Chinese resume anchors matched by speaker.

This project remains licensed under GPL-3.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
