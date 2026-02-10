
<img width="1192" height="997" alt="Exemplo" src="https://github.com/user-attachments/assets/b330e253-9fab-408b-ae67-a86dba85c925" />








# Universal Video Downloader (PySide6)

Desktop video downloader for:
- TikTok
- YouTube
- Instagram
- Twitter/X

It uses `yt-dlp` under the hood and provides a simple PySide6 GUI.

## Features

- Queue multiple downloads and run them sequentially
- Quality presets:
  - `Best (Video + Audio)`
  - `1080p (MP4)`
  - `720p (MP4)`
  - `Audio Only (MP3)`
- Per-item queue status (`Queued`, `Downloading`, `Done`, `Failed`, `Canceled`)
- Progress + activity log
- Optional `cookies.txt` for authenticated/private links

## Setup

### Option 1: uv (recommended)

1. Create a virtual environment:

```powershell
uv venv
```

2. Install dependencies from `pyproject.toml`:

```powershell
uv sync
```

3. Run:

```powershell
uv run main.py
```

You can also run the app entry point:

```powershell
uv run video-downloader
```

### Option 2: pip

1. Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Install dependencies:

```powershell
pip install -r requirements.txt
```

3. Run:

```powershell
python main.py
```

## Usage

1. Paste a video URL.
2. Pick a quality preset.
3. Click `Add To Queue`.
4. Repeat for more links.
5. Click `Start Queue`.

## Notes

- Some private/restricted videos require authentication cookies to download.
- The app has an optional `cookies.txt` field for authenticated downloads.
- `Audio Only (MP3)` requires FFmpeg available in your system `PATH`.
- Site behavior changes often; if a site breaks, update `yt-dlp`:

```powershell
pip install -U yt-dlp
```

- Use this tool only for content you are allowed to download.
