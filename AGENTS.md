# AGENTS.md

## Project

Single-file PySide6 desktop GUI (`main.py`, ~1710 lines) that wraps yt-dlp for video downloading. No tests, no CI, no lint/typecheck tooling.

## Run

```powershell
# Recommended (uv)
uv venv && uv sync
uv run main.py

# Alternative (pip)
python -m venv .venv && .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

Entry point script: `video-downloader` → `main:main` (defined in `pyproject.toml`).

## Build

PyInstaller spec: `VideoDownloader.spec`. Builds a single-file `.exe` (no console). Run with `pyinstaller VideoDownloader.spec` from the project root.

## Dependencies

- `PySide6>=6.7.0` — Qt GUI framework
- `yt-dlp>=2025.1.0` — video extraction/download engine
- Python >=3.10 required
- `ffmpeg` needed only for audio (MP3) extraction — must be in system PATH

## Architecture

Everything lives in `main.py`. Key classes:

- `MainWindow` — main GUI, queue management, drag-and-drop, clipboard integration, system tray
- `DownloadWorker(QObject)` — runs yt-dlp in a QThread with progress hooks; rate-limited to 10 Hz
- `FormatAnalyzerWorker(QObject)` — probes a URL for available formats without downloading
- `SpeedChartWidget(QWidget)` — custom painted real-time speed chart (30-sample rolling window)

Quality presets defined in `QUALITY_PRESETS` dict (line ~50). Format analysis populates the combo box dynamically.

## Conventions

- `uv.lock` is gitignored — do not commit it
- `cookies.txt` is gitignored
- All styles (light + dark theme) are inline Qt stylesheets in `apply_styles()` — no external CSS
- Resource loading uses `sys._MEIPASS` fallback for PyInstaller bundles (`get_resource_path`)
- Windows-specific: sets AppUserModelID via ctypes for taskbar icon grouping
