# Modifications in TransDub Studio

TransDub Studio is a customized build based on [pyVideoTrans](https://github.com/jianchang512/pyvideotrans).

Notable downstream changes include:

- macOS application wrapper and local deployment improvements.
- Custom macOS-style application icon.
- Single-instance launch behavior and no-terminal background startup.
- DeepSeek subtitle translation tuning for better whole-context translation.
- F5-TTS local voice-cloning workflow fixes, including safer reference-audio selection and English-leak detection.
- Dubbing reliability improvements to avoid silently producing broken output when synthesis fails.

This project remains licensed under GPL-3.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
