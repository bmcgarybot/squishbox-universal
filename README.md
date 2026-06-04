# 🗜️ SquishBox Universal

**Point at a folder. Click squish. Watch files shrink.**

A dead-simple batch video transcoder with a web UI. Works on **any system** — Windows, Mac, or Linux. Auto-detects your GPU for hardware-accelerated encoding.

## Supported Hardware Encoders

| GPU | Encoder | Detected As |
|-----|---------|-------------|
| **Nvidia** | NVENC | `hevc_nvenc` |
| **Intel** | Quick Sync | `hevc_qsv` |
| **Apple Silicon/Mac** | VideoToolbox | `hevc_videotoolbox` |
| **AMD/Intel (Linux)** | VAAPI | `hevc_vaapi` |
| **No GPU** | CPU fallback | `libx265` |

SquishBox auto-detects what's available and picks the fastest option. You can also force a specific encoder in settings.

## Install

### 1. Install Python + Flask
```bash
pip install flask
```

### 2. Install FFmpeg

**Windows (winget):**
```powershell
winget install Gyan.FFmpeg
```

**Windows (manual):** Download from [gyan.dev/ffmpeg](https://www.gyan.dev/ffmpeg/builds/), extract, add to PATH.

**Mac:**
```bash
brew install ffmpeg
```

**Linux:**
```bash
sudo apt install ffmpeg    # Debian/Ubuntu
sudo dnf install ffmpeg    # Fedora
```

### 3. Run SquishBox
```bash
python app.py
```

Open **http://localhost:5555** in your browser.

**Windows shortcuts:** Double-click `start.bat` or run `start.ps1`.

## How to Use

1. Paste a folder path into the input box → click **Scan**
2. See all video files with codec, resolution, size
3. Click **Squish All** or squish individual files
4. Watch progress bars, space saved counter, ETA
5. Done — your files are now HEVC/MKV and much smaller

## Settings

- **Quality:** CRF 18 (visually lossless) → 28 (small file). Default: 23
- **Max Resolution:** 4K / 1080p / 720p. Default: 1080p
- **Container:** MKV or MP4. Default: MKV
- **Hardware:** Auto / Nvidia NVENC / Intel QSV / macOS VideoToolbox / Linux VAAPI / CPU
- **Delete Originals:** Off by default
- **Output:** `_squished` suffix or replace original

## What It Does

- Converts video to **HEVC (H.265)** — same quality, ~50-70% smaller files
- Copies audio and subtitles without re-encoding
- Downscales 4K → 1080p (configurable)
- Skips files already in target format
- Shows space saved per file and total

## Requirements

- Python 3.10+
- Flask
- FFmpeg + ffprobe in PATH
- A web browser

## License

Proprietary. Copyright (c) 2026 BMC Luminary Ventures LLC. All rights reserved.
