# TransDub Studio

<div align="center">

**基于 pyVideoTrans 修改的 AI 视频翻译与配音工作室**

[English](../README.md) · [原项目 pyVideoTrans](https://github.com/jianchang512/pyvideotrans) · [许可证 GPL-3.0](../LICENSE)

</div>

## TransDub Studio 是什么？

**TransDub Studio** 是基于 [pyVideoTrans](https://github.com/jianchang512/pyvideotrans) 修改的下游定制版本，重点优化 macOS 本地视频翻译、字幕翻译、声音克隆和 AI 配音流程。

它保留了 pyVideoTrans 原有的核心流程：

`语音识别 → 字幕翻译 → AI 配音 / 声音克隆 → 音视频合成`

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

