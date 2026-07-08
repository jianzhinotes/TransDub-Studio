# Packaging — graphical installers

TransDub Studio ships as **small graphical installers** (a Windows `.exe` and a
macOS `.dmg`, each only a few MB). They bundle the tracked source and, on first
run, bootstrap the heavy runtime (`uv` + `uv sync`, PyTorch, models, ffmpeg).

> Why not one big offline app? The fully-installed app is 5–8 GB. GitHub Releases
> caps a single asset at 2 GB, so a self-contained bundle can't be published there.
> The installer approach keeps the download tiny; the multi-GB pull happens once on
> the user's machine (unavoidable for anything that fits in Releases).

## How a release is built

`.github/workflows/release.yml` runs on any `v*` tag:

```bash
git tag v1.0.1
git push origin v1.0.1
```

That triggers two jobs in parallel and attaches the artifacts to the GitHub
Release for the tag:

- **windows** (`windows-latest`) → Inno Setup → `TransDub-Studio-Setup-<ver>.exe`
- **macos** (`macos-latest`) → `hdiutil` → `TransDub-Studio-<ver>.dmg`

To dry-run without publishing, use the **workflow_dispatch** trigger (Actions tab
→ *Build installers* → *Run workflow*); artifacts are uploaded but not attached to
a release.

## Layout

```
packaging/
  windows/
    installer.iss     Inno Setup script (compiled by ISCC in CI)
    bootstrap.ps1     post-install: uv + ffmpeg + `uv sync`
    launch.vbs        console-less launcher (Start Menu / desktop shortcut target)
  macos/
    build_dmg.sh      assembles the .app and the .dmg
    launcher.sh       .app executable; launches directly once set up
    first_run_setup.sh  first-run bootstrap (runs in Terminal so progress is visible)
```

The payload is produced with `git archive HEAD`, so only tracked files ship —
`.venv/`, `models/`, `params.json`, `cfg.json`, `recent_tasks.json` and
`.secret_salt` are never included.

## Known rough edges (first-tag iteration expected)

- **ffmpeg.** Windows downloads a static build from BtbN; macOS uses Homebrew if
  present. If a platform can't fetch it, dubbing may error until ffmpeg is on PATH.
- **Signing.** Builds are **unsigned**. Windows shows a SmartScreen "More info →
  Run anyway"; macOS needs right-click → Open (or `xattr -dr com.apple.quarantine`)
  the first time. Proper signing/notarization needs paid developer accounts.
- **`uv sync` time.** First launch downloads several GB; the installer/Terminal
  window stays open with progress until it finishes.
