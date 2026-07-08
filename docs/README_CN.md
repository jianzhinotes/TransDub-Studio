# ✨ TransDub Studio

<div align="center">

**贴近剪映 / ElevenLabs 体验的 AI 视频翻译配音工作台**

[English](../README.md) · [原项目 pyVideoTrans](https://github.com/jianchang512/pyvideotrans) · [许可证 GPL-3.0](../LICENSE)

作者：**jianzhinotes** · [jianzhi.notes@gmail.com](mailto:jianzhi.notes@gmail.com) · [GitHub ⭐](https://github.com/jianzhinotes/TransDub-Studio)

</div>

## TransDub Studio 是什么？

**TransDub Studio** 是基于 [pyVideoTrans](https://github.com/jianchang512/pyvideotrans) 深度重构的版本，围绕剪映/ElevenLabs 式的现代工作流重新设计了整套界面——但**跑在你自己的电脑上**。

`语音识别 → 字幕翻译 → AI 配音 / 声音克隆 → 音视频合成`

## 🚀 为什么选 TransDub Studio —— 对比剪映 / ElevenLabs

剪映和 ElevenLabs 那样精致的编辑体验，但**本地、私密、免费**——你的视频和语音不必离开本机。

| | **TransDub Studio** | 剪映 CapCut | ElevenLabs |
|---|:---:|:---:|:---:|
| **本地 / 离线运行** | ✅ 核心链路可 100% 本地 | ☁️ 仅云端 | ☁️ 仅云端 |
| **费用** | ✅ **免费**本地方案，或自带 API | 会员 + 配音时长限制 | 按字符计费 |
| **时长 / 水印限制** | 无 | 有 | 配额限制 |
| **数据隐私** | ✅ 不出本机 | 上传云端 | 上传云端 |
| **音色克隆** | ✅ F5-TTS 本地克隆 | 有限 | ✅ 云端 |
| **渠道自由** | ✅ 79 个识别/翻译/配音渠道自选 | 固定 | 固定 |
| **逐句配音编辑** | ✅ | 有限 | ✅ |
| **时间轴校对** | ✅ | ✅ | ✅ |
| **开源可定制** | ✅ GPL-3.0 | ❌ | ❌ |

**核心优势**

- **🔒 本地私密。** 识别（faster-whisper）、翻译（本地 LLM / 离线模型）、音色克隆（F5-TTS）全都能离线跑，除非**你自己**选云端 API，否则什么都不上传。剪映和 ElevenLabs 始终把你的素材传到它们服务器。
- **💰 免费、无限制。** 全免费方案——faster-whisper + Google/本地LLM 翻译 + Edge-TTS——零成本，**无订阅、无水印、无时长/配额限制**。ElevenLabs 按字符/分钟计费；剪映配音要会员且有时长限制。
- **🎛 引擎任你选。** 识别/翻译/配音共 79 个渠道：免费本地、DeepSeek、OpenAI、Gemini、DeepL、ElevenLabs、Azure…… 随意组合，不被单一厂商绑定。
- **✂️ 两家之长合一。** 剪映式分步校对（先校原文，再校译文，再校配音）**加上** ElevenLabs 式逐句编辑（改原文/译文、换音色、重配、拖时间轴）——都在一个内嵌工作区里，不弹窗。
- **♻️ 工程可反复重开。** 每个完成的任务都存成本地工程，随时重开再编辑再导出，且**只重跑对齐+合成**、不重跑整条流水线（不重复消耗 API 费用）。
- **🖥 原生应用体验。** 重新打包的 macOS `.app`、API 密钥本地加密存储、设置自动记忆，另有经典「高级模式」支持批量处理与全部参数。

## ✨ 全新 Flow 界面（默认）

启动即进入三步流程：

1. **首页** —— 拖入视频（或点击选择），最近任务带状态徽标，一键打开历史结果。
2. **配置页** —— 一页搞定：源/目标语言、三张渠道卡（识别 / 翻译 / 配音，实时显示"已配置/需配置密钥"状态点，点「配置」直接填 API Key）、模型与音色选择、三个开关（字幕、自动对齐、保留背景音）。上次的选择自动记忆。
3. **进度页** —— 每个任务一张六阶段步进卡（准备→识别→翻译→配音→对齐→合成）。配音完成后自动进入 **配音工作台（Dubbing Studio）**：逐句卡片（原文+译文并排）、可拖拽时间轴（拖块改位置、拉边改时长）、单句换音色重配、原声/配音 A/B 即时试听——最终合成前就能精修一切。

经典完整界面（批量处理、全部渠道、高级参数）仍在 **工具 → 高级模式** 中。

## 📦 安装

### 最简单 —— 下载安装器

到 [**Releases**](https://github.com/jianzhinotes/TransDub-Studio/releases) 页面下载对应安装器:

- **Windows:** `TransDub-Studio-Setup-<版本>.exe` → 双击,按向导走。
- **macOS:** `TransDub-Studio-<版本>.dmg` → 打开,拖进 Applications,首次**右键 → 打开**(未签名版)。

安装器很小;首次启动时下载运行环境 + 模型(几个 GB),之后完全本地运行。构建**未签名**,所以 Windows SmartScreen 会提示"更多信息 → 仍要运行",macOS 首次需要右键打开。

> 更喜欢命令行,或你的版本还没发布安装器?用下面的一行命令。

`uv` 会自动管理 Python 3.10 运行环境,无需手动装别的。首次启动会按需下载本地识别模型(faster-whisper),之后核心流程完全本地运行。依赖和模型合计几个 GB,首次安装请给好点的网络和一点耐心。

### macOS — 一键安装

打开**终端**，粘贴这一行：

```bash
curl -fsSL https://raw.githubusercontent.com/jianzhinotes/TransDub-Studio/main/install.sh | bash
```

之后每次启动：

```bash
cd ~/TransDub-Studio && uv run python sp.py
```

### Windows — 一键安装

先装 [Git for Windows](https://git-scm.com/download/win)，然后在 **PowerShell** 里粘贴：

```powershell
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/jianzhinotes/TransDub-Studio/main/install.ps1 | iex"
```

之后每次启动：

```powershell
cd $HOME\TransDub-Studio; uv run python sp.py
```

> Windows 上依赖会自动装 **CUDA（GPU）版 PyTorch**：有 NVIDIA 显卡 + 较新驱动就自动 GPU 加速，没有则回退 CPU（慢一些但能跑）。macOS 用的是 CPU/Metal 版。

### 手动安装（任意平台）

```bash
git clone https://github.com/jianzhinotes/TransDub-Studio.git
cd TransDub-Studio
uv sync              # 首次，自动装 Python 3.10 + 依赖
uv run python sp.py
```

**环境要求：** macOS（Apple 芯片或 Intel）或 Windows 10/11，预留 5–8 GB 给依赖和模型。

在此基础上，TransDub Studio 针对本地部署、DeepSeek 翻译质量、F5-TTS 原声克隆稳定性、macOS 应用体验和最终配音可靠性做了改进。

本仓库 **不是 pyVideoTrans 官方项目**，而是修改版，并继续遵循 **GPL-3.0** 协议发布。

## 相比原版 pyVideoTrans 的主要改进

### 1. macOS 应用体验优化

- 应用名称改为 **TransDub Studio**。
- 增加更符合 macOS 风格的应用图标。
- 增加 macOS `.app` 启动壳。
- 双击启动时不再弹出多个黑色终端窗口。
- 增加单实例启动逻辑，避免重复打开多个程序。
- F5-TTS 本地服务改为后台静默启动。
- 使用 `Application Support` 中的运行目录，减少 macOS 权限问题。

### 2. DeepSeek 翻译质量改进

- 调整 DeepSeek 字幕翻译提示词，让翻译更重视整段上下文。
- 减少把字幕切成很多小段后逐段翻译导致的上下文丢失。
- 强化术语一致性、句意连贯性和中文表达自然度。
- 减少目标中文字幕中不必要残留英文。
- 翻译缓存加入提示词影响，避免修改提示词后继续复用旧翻译缓存。

### 3. F5-TTS 原声克隆稳定性改进

- 改进参考音频选择逻辑，避免把参考音频里的英文人名或短句泄漏进中文配音。
- 增加生成音频中的异常英文检测。
- 发现生成结果夹杂与字幕无关的英文时自动重试。
- 降低部分本地推理参数，减少 Apple Silicon 机器长时间运行时的压力。
- 增加本地 F5-TTS 推理后的内存清理，降低长任务不稳定概率。
- 如果配音片段生成失败，会中断任务，而不是继续产出有问题的成品。

### 4. 配音与成品可靠性改进

- 改进 TTS 片段失败时的处理。
- 降低最终视频里混入原英文或异常英文的概率。
- 提升本地声音克隆视频翻译流程的稳定性。
- 保留原版可人工校对识别、翻译、配音结果的工作流，同时增加生成音频检查。

### 5. 项目身份与版权声明整理

- 使用新的下游项目名称：**TransDub Studio**。
- 保留对原项目 pyVideoTrans 和原作者的明确署名。
- 新增 [NOTICE](../NOTICE) 和 [MODIFICATIONS.md](../MODIFICATIONS.md)，说明本仓库是修改版，以及主要修改内容。
- 继续使用 GPL-3.0 协议，与原项目保持一致。

## 适合谁使用？

TransDub Studio 主要适合希望在 macOS 本地运行 AI 视频翻译和配音流程的用户，尤其是：

- 使用 DeepSeek 或 OpenAI-compatible API 做字幕翻译。
- 使用本地 F5-TTS 做声音克隆。
- 把英文视频翻译并配成中文。
- 想要双击启动的 macOS 应用体验，而不是纯命令行运行。

## 当前状态

这是一个个人下游定制版本，不是 pyVideoTrans 官方发行渠道。

大型本地模型不建议提交到仓库中，应在本地按需下载或单独部署。

## 源码部署

需要：

- Python 3.10
- FFmpeg
- `uv`

克隆仓库：

```bash
git clone https://github.com/jianzhinotes/TransDub-Studio.git
cd TransDub-Studio
```

安装依赖：

```bash
uv sync
```

启动：

```bash
uv run sp.py
```

## 支持的工作流

TransDub Studio 继承 pyVideoTrans 的主要能力，包括：

- 语音识别 / 字幕生成。
- 使用本地或在线渠道进行字幕翻译。
- AI 配音和声音克隆。
- 音频、视频、字幕合并。
- 识别、翻译、配音阶段的人工校对。
- CLI 批处理。

原版 pyVideoTrans 的通用功能与配置文档可参考：

- [pyVideoTrans 仓库](https://github.com/jianchang512/pyvideotrans)
- [pyVideoTrans 文档](https://pyvideotrans.com)

## 许可证与署名

TransDub Studio 基于 [pyVideoTrans](https://github.com/jianchang512/pyvideotrans) 修改，原项目由 [jianchang512](https://github.com/jianchang512) 创建。

原项目使用 **GPL-3.0** 协议，本修改版也继续使用 **GPL-3.0** 协议发布。

本仓库与 pyVideoTrans 官方项目无从属或背书关系。详情见：

- [LICENSE](../LICENSE)
- [NOTICE](../NOTICE)
- [MODIFICATIONS.md](../MODIFICATIONS.md)

## 致谢

本项目依赖 pyVideoTrans 以及多个开源项目，包括：

- [pyVideoTrans](https://github.com/jianchang512/pyvideotrans)
- [FFmpeg](https://github.com/FFmpeg/FFmpeg)
- [PySide6](https://pypi.org/project/PySide6/)
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- [openai-whisper](https://github.com/openai/whisper)
- [edge-tts](https://github.com/rany2/edge-tts)
- [F5-TTS](https://github.com/SWivid/F5-TTS)
- [CosyVoice](https://github.com/FunAudioLLM/CosyVoice)

