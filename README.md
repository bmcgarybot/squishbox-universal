# 🗜️ SquishBox

**Point at a folder, click squish, watch files shrink.** A dead-simple batch video transcoder with a web UI — like Handbrake, but for whole folders.

## What It Does

- Scans folders for video files (mp4, mkv, avi, mov, ts, wmv, flv, webm)
- Shows you everything: codec, resolution, file size, duration
- One click to batch-encode everything to HEVC/H.265 in MKV
- **Multi-worker:** Run 1–8 simultaneous encode jobs
- **Multi-folder:** Queue files from multiple folders at once
- Uses Intel QSV / AMD AMF / NVIDIA NVENC if available, falls back to CPU
- Tracks progress, speed, ETA, and space saved per file
- Built-in folder browser (no pasting paths)
- Dark-themed web dashboard on `localhost:5555`

## Install (Windows)

### 1. Clone the repo

```powershell
cd $HOME
git clone https://github.com/bmcgarybot/squishbox.git
cd squishbox
```

### 2. Install Python dependency

```
pip install flask
```

That's the only dependency.

### 3. Install FFmpeg

FFmpeg and ffprobe must be in your PATH.

**Option A — winget (easiest):**
```powershell
winget install Gyan.FFmpeg
```

**Option B — Manual:**
1. Download from https://www.gyan.dev/ffmpeg/builds/ (get the "essentials" build)
2. Extract somewhere (e.g., `C:\ffmpeg`)
3. Add `C:\ffmpeg\bin` to your system PATH
4. Verify: `ffmpeg -version`

### 4. Create desktop shortcut (optional)

```powershell
cd $HOME\squishbox
powershell -ExecutionPolicy Bypass -File install-shortcut.ps1
```

This creates a **SquishBox** shortcut on your desktop.

### 5. Run

Double-click the desktop shortcut, or:

```
cd $HOME\squishbox
python app.py
```

Open **http://localhost:5555** in your browser.

## Updating

When there's an update, run these two commands:

```powershell
cd $HOME\squishbox
git pull
```

Then click **🔄 Restart** in SquishBox's top-right corner (or close the command window and reopen).

That's it. Your settings and history are preserved.

## Reset / Fresh Start

If something goes wrong and you want to start clean:

```powershell
# Delete saved state (keeps the app, just resets progress/settings)
cd $HOME\squishbox
del .state.json
del .squishbox_history.json
```

Then restart SquishBox. Everything will be back to defaults.

**Full reinstall:**
```powershell
cd $HOME
rmdir /s /q squishbox
git clone https://github.com/bmcgarybot/squishbox.git
cd squishbox
pip install flask
python app.py
```

## Usage

1. Click **📂 Browse** → navigate to your folder → **✅ Scan This Folder**
2. Review the file list — already-HEVC files are marked "skipped"
3. Adjust **Workers** with [−] [+] buttons (more workers = more files encoding at once)
4. Click **➕ Add Folder** to add more folders to the same queue
5. Click **🗜️ Squish All** to encode everything
6. Watch progress bars, encoding speed, and space saved in real-time

## Multi-Worker Encoding

SquishBox can run multiple encode jobs in parallel:

- **Workers: [−] 1 [+]** — adjust how many files encode simultaneously
- Intel QSV supports ~3–4 simultaneous sessions
- CPU encoding benefits from 2–3 workers depending on core count
- Each worker shows its own progress bar

## Multi-Folder Queue

Encode files from multiple locations in one session:

1. Scan your first folder normally
2. Click **➕ Add Folder** → browse to another folder
3. Both folders' files appear in one unified queue
4. Workers pull from either folder automatically

Great for encoding across local drives AND network shares at the same time.

## Settings

Click ⚙️ in the top-right to configure:

| Setting | Default | Options |
|---------|---------|---------|
| Quality | CRF 23 | 18 (Visually Lossless) → 28 (Small File) |
| Max Resolution | 1080p | 4K, 1080p, 720p |
| Container | MKV | MKV, MP4 |
| Hardware Encoding | Auto-detect | Auto / Force QSV / Force CPU |
| Delete Originals | OFF | Toggle on to remove source files after encode |
| Output Naming | `_squished` suffix | Suffix or Replace original |

### Defaults Explained

- **HEVC (H.265)** — Modern codec, ~50% smaller than H.264 at same quality
- **MKV container** — Supports all subtitle and audio formats
- **CRF 23** — Good balance of quality and file size
- **Audio: copy** — Original audio preserved, no re-encoding
- **Subtitles: copy** — All subtitle tracks preserved
- **1080p max** — 4K content is downscaled; smaller files are left alone

## Intel QSV Setup

If you have an Intel CPU with integrated graphics, SquishBox uses hardware encoding for **much** faster transcoding.

**Requirements:**
1. Intel GPU drivers installed (default on Windows 11)
2. FFmpeg built with QSV support (default Windows builds include it)

SquishBox auto-detects QSV on startup. Check the badge in the top-right:
- **QSV: ✓ Available** — Hardware encoding is active
- **QSV: ✗ Not found** — Using CPU (libx265) instead

## Custom Port

Default is **5555**. To run on a different port:

```
python app.py --port 5556
```

Useful for running multiple instances on different ports.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Dashboard shows old version after `git pull` | Click **🔄 Restart** in SquishBox, or close + reopen the command window |
| `git pull` — "not a git repository" | You're in the wrong folder. Run `cd $HOME\squishbox` first |
| QSV not detected | Update Intel GPU drivers, then check `ffmpeg -encoders \| findstr qsv` |
| Files not showing after scan | Make sure folder has video files (mp4, mkv, avi, mov, ts, etc.) |
| Encode fails immediately | Check that `ffmpeg -version` works in your terminal |
| Port 5555 already in use | Use `python app.py --port 5556` |

## Tech Stack

- Python 3.10+ with Flask
- FFmpeg/ffprobe for all encoding
- Single `app.py` + single `templates/index.html`
- No database, no Docker, no npm, no build step

## License

MIT — do whatever you want with it.
