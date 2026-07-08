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
