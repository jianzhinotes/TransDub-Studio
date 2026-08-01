## TransDub Studio v1.1.2 — Honest exports, clearer continuation

- A task is now marked as video-complete only after its actual render is verified on disk.
- Queue-end and 100% progress messages can no longer create a false “completed” video.
- Subtitle-only jobs are explicitly labelled as such, rather than pretending a video was created.
- Translation review now makes the right-bottom **Save & Continue Dubbing** action unambiguous.

## TransDub Studio v1.1.2 — 成品可核验、下一步更清楚

- 只有在实际成品视频落盘并通过核验后，任务才会显示视频完成。
- 队列结束和 100% 进度不再会造成“假完成”。
- 仅字幕任务会明确标识，不再误显示为视频成品。
- 翻译校对页将右下角操作明确为“保存并继续配音”。

## TransDub Studio v1.1.1 — More natural, speaker-stable Chinese dubbing

This maintenance release improves the full local Chinese-dubbing path for real long-form interviews.
It keeps ASR, translation/LLM and TTS as replaceable backends, while making their handoff more
consistent, recoverable and easier to verify.

### Highlights


- **Speaker identity contract.** Each reliable voice cluster keeps a verified reference bank;
  generated clips are checked against the intended speaker so a plausible but wrong voice does not
  silently pass.
- **Chinese-first F5-TTS route.** Stable Chinese conditioning anchors preserve a speaker's identity
  across lines. Source-language rhythm is now optional prosody guidance instead of a continual
  phonetic driver, which reduces English leakage in Chinese output.
- **Safer semantic handoff.** Clause-aware segmentation, normalized numbers/units and stronger
  translation alignment keep technical statements and short continuation phrases together before
  synthesis.
- **Focused repair, not reruns.** Preflight checks, atomic per-clip verification checkpoints and a
  repair queue preserve good work while isolating mixed-language, truncated or mismatched clips.
- **Dialogue-first finishing.** Background music/effects are ducked only during dialogue, with
  clipping-safe gain and a diagnostic mix report.
- **Better resource behavior.** Apple Silicon and local F5 service lifecycle controls reduce peak
  memory pressure and make long jobs resume more predictably after interruption.
- **Product demo.** The README now includes an inline 50-second, sound-on AI Chinese-dubbing demo,
  with a separate full-screen player.

## TransDub Studio v1.1.1 — 更自然、音色更稳定的中文配音

这一补丁版聚焦真实长访谈的本地中文配音链路。识别、翻译/LLM、TTS 仍是可替换后端，
但它们之间的衔接、恢复和验收更加稳定、可追溯。

### 主要更新


- **说话人身份契约。** 每个可靠声纹簇保留已核验的参考库；生成音频会与目标说话人交叉检查，避免“听着通顺、人物却错了”的片段混入成片。
- **中文优先 F5-TTS。** 用稳定的中文音色锚点保持同一人物跨句一致；原声只作为可选韵律参考，不再持续驱动中文音素，显著减少英文夹杂。
- **更稳的语义交接。** 句法续句合并、数字/单位标准化和翻译对齐校验，让技术表述与短尾句在合成前保持完整。
- **局部返工而非整片重跑。** 预飞、逐段原子质检断点与返工队列会保留已通过结果，只隔离中英混杂、截断或内容不符的片段。
- **对白优先混音。** 对话出现时才压低音乐/环境声，并加入削波保护和可诊断的混音报告。
- **更好的资源表现。** Apple Silicon 与本地 F5 服务的生命周期控制降低峰值内存压力，也让中断后的长任务更可靠地恢复。
- **产品演示。** README 现已内嵌 50 秒有声 AI 中文配音演示，同时保留全屏播放器。

---

## ⬇️ Download & install

| Your OS | File | How to install |
|---|---|---|
| **Windows 10/11** | `TransDub-Studio-Setup-*.exe` | Double-click → follow the wizard. If SmartScreen warns, click **More info → Run anyway**. |
| **macOS 11+** | `TransDub-Studio-*.dmg` | Open the dmg → drag **TransDub Studio** to Applications → **right-click the app → Open** the first time. |

> **First launch downloads a few GB** (PyTorch + models) and sets things up — this is normal and only happens once. A window/terminal stays open showing progress; leave it until it finishes, then the app opens by itself. After that it runs **fully local**.

**Why the security warnings?** These builds are **unsigned** (code-signing needs paid Apple/Microsoft certificates). The steps above are the standard way to run an unsigned app — nothing is being uploaded.

**If it won't start:**
- Make sure the first-run setup fully finished (the multi-GB download can take a while on slow networks).
- Windows: check `install-log.txt` inside the install folder (`%LOCALAPPDATA%\Programs\TransDub Studio`).
- Both: `ffmpeg` is fetched automatically (BtbN on Windows, Homebrew on macOS). If dubbing errors out, install ffmpeg and relaunch.

Prefer the command line? See the [one-line installers and source setup](https://github.com/jianzhinotes/TransDub-Studio#-installation) in the README.

---

## ⬇️ 下载与安装（中文）

| 系统 | 文件 | 安装方法 |
|---|---|---|
| **Windows 10/11** | `TransDub-Studio-Setup-*.exe` | 双击 → 按向导走。若 SmartScreen 拦截,点**更多信息 → 仍要运行**。 |
| **macOS 11+** | `TransDub-Studio-*.dmg` | 打开 dmg → 把 **TransDub Studio** 拖进 Applications → 首次**右键点应用 → 打开**。 |

> **首次启动会下载几个 GB**(PyTorch + 模型)并完成初始化——属正常,只发生一次。会有个窗口/终端显示进度,等它跑完应用会自动打开。之后**完全本地运行**。

**为什么有安全提示?** 这些构建**未签名**(签名需付费的 Apple/微软证书)。上面的步骤是运行未签名应用的标准做法,不会上传任何东西。

**起不来时:**
- 确认首次初始化真的跑完了(网慢时几个 GB 要等一会)。
- Windows:看安装目录里的 `install-log.txt`(`%LOCALAPPDATA%\Programs\TransDub Studio`)。
- ffmpeg 会自动下载(Windows 用 BtbN,macOS 用 Homebrew)。若配音报错,手动装 ffmpeg 后重开。

想用命令行?见 README 里的[一行安装脚本和源码安装](https://github.com/jianzhinotes/TransDub-Studio/blob/main/docs/README_CN.md#-安装)。
