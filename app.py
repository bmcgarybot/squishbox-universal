"""
SquishBox — Point at a folder, click squish, watch files shrink.
A simple batch video transcoder with a web UI. FFmpeg under the hood.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

# Add local lib folder so pythonw can find Flask
_lib = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
if os.path.isdir(_lib) and _lib not in sys.path:
    sys.path.insert(0, _lib)

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)


# Catch all unhandled errors and return JSON instead of HTML
@app.errorhandler(404)
def handle_404(e):
    return jsonify({"error": "Not found"}), 404

@app.errorhandler(Exception)
def handle_exception(e):
    import traceback
    traceback.print_exc()
    return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".wmv", ".flv", ".webm"}

settings = {
    "quality": 23,           # CRF / global_quality value
    "max_resolution": 1080,  # 0 = no limit (4K), 1080, 720
    "container": "mkv",      # mkv or mp4
    "hw_mode": "auto",       # auto / videotoolbox / amf / qsv / nvenc / cpu
    "delete_originals": False,
    "output_mode": "replace", # suffix (_squished) or replace
    "min_file_size_mb": 10,  # skip files smaller than this (filters out NFO/info junk)
}

# Scanned files: {file_id: {path, filename, codec, resolution, width, height,
#                            size, duration, status, progress, speed_fps,
#                            eta, space_saved, error, queue_pos}}
scanned_files: OrderedDict[str, dict] = OrderedDict()
scan_folder: str = ""

# State file for persistence across restarts
_state_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".state.json")


def _save_state():
    """Persist scan state to disk."""
    try:
        state = {
            "scan_folder": scan_folder,
            "scanned_files": {k: {kk: vv for kk, vv in v.items()} for k, v in scanned_files.items()},
            "stats": stats,
        }
        with open(_state_file, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass


def _load_state():
    """Restore scan state from disk on startup."""
    global scan_folder, scanned_files, stats
    try:
        with open(_state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
        scan_folder = state.get("scan_folder", "")
        for k, v in state.get("scanned_files", {}).items():
            scanned_files[k] = v
            # Reset any in-progress states from previous run
            if v.get("status") in ("encoding", "queued"):
                scanned_files[k]["status"] = "pending"
                scanned_files[k]["progress"] = 0
                scanned_files[k]["queue_pos"] = 0
        stats.update(state.get("stats", {}))
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass


# Queue / encoding state
encode_queue: list[str] = []       # file_ids waiting
encoding_lock = threading.Lock()
cancel_event = threading.Event()

# Multi-worker pool
gpu_workers: int = 1               # GPU worker count
cpu_workers: int = 0               # CPU worker count
active_workers: dict[int, dict] = {}  # {worker_id: {"thread": Thread, "file_id": str|None, "type": "gpu"|"cpu"}}
_next_worker_id: int = 0
scan_folders: list[str] = []       # all scanned folder paths

# Stats
stats = {
    "total_space_saved": 0,
    "files_processed": 0,
}

# QSV/AMF/NVENC availability cache
_hw_encoder: str | None = None  # Will be set to the working GPU encoder


# ---------------------------------------------------------------------------
# Helpers — File Locking (multi-machine safe)
# ---------------------------------------------------------------------------

import socket as _socket

_LOCK_SUFFIX = ".squishbox.lock"
_LOCK_STALE_HOURS = 0.5  # Consider locks older than 30 minutes stale


def _lock_path(filepath: str) -> str:
    """Return the lock file path for a given video file."""
    return filepath + _LOCK_SUFFIX


def _acquire_lock(filepath: str) -> bool:
    """Try to create a lock file. Returns True if lock acquired, False if already locked by another machine."""
    lp = _lock_path(filepath)
    my_host = _socket.gethostname()
    # Check for existing lock
    if os.path.exists(lp):
        try:
            age_hours = (time.time() - os.path.getmtime(lp)) / 3600
            if age_hours >= _LOCK_STALE_HOURS:
                os.remove(lp)  # Stale lock — take over
            else:
                # Check if it's our own lock (from crash) — reclaim it
                with open(lp, "r") as f:
                    parts = f.read().strip().split("|")
                if parts and parts[0] == my_host:
                    os.remove(lp)  # Our own stale lock — reclaim
                else:
                    return False  # Another machine's valid lock — skip
        except OSError:
            return False
    # Create lock
    try:
        with open(lp, "w") as f:
            f.write(f"{my_host}|{os.getpid()}|{time.time()}\n")
        return True
    except OSError:
        return False


def _release_lock(filepath: str):
    """Remove the lock file for a given video file."""
    try:
        os.remove(_lock_path(filepath))
    except OSError:
        pass


def _is_locked(filepath: str) -> bool:
    """Check if a file is locked by another instance (not this machine)."""
    lp = _lock_path(filepath)
    if not os.path.exists(lp):
        return False
    try:
        age_hours = (time.time() - os.path.getmtime(lp)) / 3600
        if age_hours >= _LOCK_STALE_HOURS:
            os.remove(lp)  # Clean up stale lock
            return False
        # If WE created the lock (same hostname), it's ours — not locked
        with open(lp, "r") as f:
            parts = f.read().strip().split("|")
            if parts and parts[0] == _socket.gethostname():
                os.remove(lp)  # Clean up our own stale lock
                return False
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Helpers — FFprobe / FFmpeg
# ---------------------------------------------------------------------------

def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command, hiding the console window on Windows."""
    si = None
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return subprocess.run(cmd, startupinfo=si, **kwargs)


def detect_gpu_encoder() -> str | None:
    """Detect the best available GPU encoder by actually testing each one."""
    global _hw_encoder
    if _hw_encoder is not None:
        return _hw_encoder if _hw_encoder != "" else None

    # Order: Apple VideoToolbox → AMD AMF → Intel QSV → NVIDIA NVENC
    candidates = [
        ("hevc_videotoolbox", ["-q:v", "60"]),
        ("hevc_amf", ["-global_quality", "23"]),
        ("hevc_qsv", ["-global_quality", "23"]),
        ("hevc_nvenc", ["-rc", "constqp", "-qp", "23"]),
    ]

    for encoder, _ in candidates:
        try:
            # Quick test: encode 1 frame to verify the encoder works
            # Use nv12 pixel format — some AMD AMF drivers reject the default
            cmd = [
                "ffmpeg", "-y", "-f", "lavfi", "-i", "color=black:s=64x64:d=0.1",
                "-pix_fmt", "nv12",
                "-c:v", encoder, "-frames:v", "1",
                "-f", "null", "-",
            ]
            r = _run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                _hw_encoder = encoder
                return _hw_encoder
        except Exception:
            continue

    _hw_encoder = ""  # empty string = tested but nothing works
    return None


def probe_file(filepath: str) -> dict | None:
    """Return dict with codec, width, height, duration, size or None on error."""
    try:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(filepath),
        ]
        r = _run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)

        # Find video stream
        video = None
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                video = s
                break
        if not video:
            return None

        fmt = data.get("format", {})
        duration = float(fmt.get("duration", 0) or video.get("duration", 0) or 0)
        size = int(fmt.get("size", 0))
        codec = (video.get("codec_name") or "unknown").lower()
        width = int(video.get("width", 0))
        height = int(video.get("height", 0))

        return {
            "codec": codec,
            "width": width,
            "height": height,
            "duration": duration,
            "size": size,
        }
    except Exception:
        return None


def _choose_encoder() -> tuple[str, list[str]]:
    """Return (encoder_name, extra_ffmpeg_args) based on settings and availability."""
    mode = settings["hw_mode"]
    quality = settings["quality"]

    gpu_enc = detect_gpu_encoder()

    if mode == "cpu" or (mode == "auto" and not gpu_enc):
        return "libx265", ["-crf", str(quality), "-preset", "medium"]

    if mode == "auto" and gpu_enc:
        enc = gpu_enc
    elif mode == "videotoolbox":
        enc = "hevc_videotoolbox"
    elif mode == "qsv":
        enc = "hevc_qsv"
    elif mode == "amf":
        enc = "hevc_amf"
    elif mode == "nvenc":
        enc = "hevc_nvenc"
    else:
        enc = gpu_enc or "libx265"

    # Encoder-specific quality args
    if enc == "hevc_videotoolbox":
        # VideoToolbox uses -q:v (1-100, lower=better). Map CRF 18-28 → VT 40-75
        vt_quality = int(40 + (quality - 18) * 3.5)
        return enc, ["-q:v", str(vt_quality)]
    elif enc == "hevc_amf":
        return enc, ["-quality", "quality", "-rc", "cqp", "-qp_i", str(quality), "-qp_p", str(quality)]
    elif enc == "hevc_qsv":
        return enc, ["-global_quality", str(quality), "-preset", "medium"]
    elif enc == "hevc_nvenc":
        return enc, ["-rc", "constqp", "-qp", str(quality), "-preset", "p4"]
    else:
        return "libx265", ["-crf", str(quality), "-preset", "medium"]


# Containers that support HEVC
HEVC_CONTAINERS = {"mkv", "mp4", "mov", "ts", "webm", "m2ts"}


def _build_ffmpeg_cmd(src: str, dst: str, simple_mode: bool = False) -> list[str]:
    """Build the full ffmpeg command for encoding one file.
    
    If simple_mode=True, only maps video+audio (skips subtitles and extra streams)
    to avoid compatibility issues with legacy containers.
    """
    encoder, enc_args = _choose_encoder()
    max_h = settings["max_resolution"]

    # Determine output container from the destination file extension
    container = Path(dst).suffix.lstrip(".").lower()
    if container not in HEVC_CONTAINERS:
        container = "mkv"

    # Probe source for resolution
    info = probe_file(src)
    src_height = info["height"] if info else 0

    cmd = ["ffmpeg", "-y", "-i", str(src)]

    # Video filter: scale down if needed
    if max_h and src_height > max_h:
        cmd += ["-vf", f"scale=-2:{max_h}"]

    # Video codec
    cmd += ["-c:v", encoder] + enc_args

    # Audio: copy
    cmd += ["-c:a", "copy"]

    if simple_mode:
        # Simple mode: only video + audio, skip subtitles and data streams
        cmd += ["-map", "0:v:0", "-map", "0:a?"]
    else:
        # Full mode: map everything, preserve all metadata
        # Subtitles: copy all (MKV supports all subtitle formats; MP4 is limited)
        if container == "mkv":
            cmd += ["-c:s", "copy"]
        else:
            cmd += ["-c:s", "mov_text"]
        # Map video, audio, subtitles, and attachments (fonts, thumbnails, etc.)
        cmd += ["-map", "0:v:0", "-map", "0:a?", "-map", "0:s?", "-map", "0:t?"]

    # Always preserve metadata and chapters from source
    cmd += ["-map_metadata", "0", "-map_chapters", "0"]

    # Output
    cmd.append(str(dst))
    return cmd


def _output_path(src: str) -> str:
    """Compute output file path based on settings."""
    p = Path(src)
    if settings["output_mode"] == "replace":
        # If source container can't hold HEVC, switch to MKV
        src_ext = p.suffix.lstrip(".").lower()
        if src_ext in HEVC_CONTAINERS:
            out_ext = p.suffix  # keep original extension
        else:
            out_ext = ".mkv"   # upgrade to MKV
        # Use system temp dir (usually on C:) so we don't fill the source drive
        import tempfile
        temp_dir = tempfile.gettempdir()
        temp_name = p.stem + ".squishbox" + out_ext
        return os.path.join(temp_dir, temp_name)
    else:
        ext = f".{settings['container']}"
        stem = p.stem
        # Remove existing _squished suffix to avoid stacking
        stem = re.sub(r"_squished$", "", stem)
        return str(p.with_name(f"{stem}_squished{ext}"))


def _final_path(src: str, tmp_path: str) -> str:
    """If replace mode, return the final path (original name, appropriate ext)."""
    if settings["output_mode"] == "replace":
        p = Path(src)
        src_ext = p.suffix.lstrip(".").lower()
        if src_ext in HEVC_CONTAINERS:
            return src  # same filename
        else:
            # Can't use original extension (e.g. .avi), switch to .mkv
            return str(p.with_suffix(".mkv"))
    return tmp_path


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def scan_directory(folder: str, append: bool = False) -> dict:
    """Scan folder for video files and populate scanned_files.
    If append=True, adds to existing scan instead of clearing."""
    global scan_folder
    scan_folder = folder

    if not append:
        scanned_files.clear()
        scan_folders.clear()

    if folder not in scan_folders:
        scan_folders.append(folder)

    folder_path = Path(folder)
    if not folder_path.is_dir():
        return {"error": f"Not a valid directory: {folder}"}

    files_found = []
    min_bytes = settings["min_file_size_mb"] * 1024 * 1024  # convert MB to bytes
    try:
        all_files = sorted(folder_path.rglob("*"))
    except OSError:
        all_files = []
    for f in all_files:
        try:
            is_file = f.is_file()
        except OSError:
            continue
        if is_file and f.suffix.lower() in VIDEO_EXTENSIONS:
            # Skip tiny files (NFO, info stubs, samples)
            try:
                if f.stat().st_size < min_bytes:
                    continue
            except OSError:
                continue
            files_found.append(f)

    for f in files_found:
        info = probe_file(str(f))
        # Use path-based ID so rescans don't create duplicate entries
        fid = hashlib.md5(str(f).encode()).hexdigest()[:8]
        already_target = False
        if info:
            is_hevc = info["codec"] in ("hevc", "h265", "hev1")
            max_h = settings["max_resolution"]
            # In replace mode, container doesn't matter for skip logic
            if settings["output_mode"] == "replace":
                is_target_container = True
            else:
                is_target_container = f.suffix.lower() == f".{settings['container']}"
            # Don't skip if resolution exceeds max (user wants to downscale)
            needs_downscale = max_h and info["height"] > max_h
            already_target = is_hevc and is_target_container and not needs_downscale

        # Check if locked by another SquishBox instance
        is_locked = _is_locked(str(f))
        lock_host = ""
        if is_locked:
            file_status = "locked"
            # Read hostname from lock file
            try:
                with open(_lock_path(str(f)), "r") as lf:
                    parts = lf.read().strip().split("|")
                    lock_host = parts[0] if parts else ""
            except OSError:
                pass
            file_error = f"Locked by {lock_host}" if lock_host else "Locked by another SquishBox instance"
        elif already_target:
            file_status = "skipped"
            file_error = ""
        else:
            file_status = "pending"
            file_error = ""

        scanned_files[fid] = {
            "id": fid,
            "path": str(f),
            "filename": f.name,
            "codec": info["codec"] if info else "unknown",
            "resolution": f"{info['width']}x{info['height']}" if info else "?",
            "width": info["width"] if info else 0,
            "height": info["height"] if info else 0,
            "size": info["size"] if info else f.stat().st_size,
            "duration": info["duration"] if info else 0,
            "status": file_status,
            "progress": 0,
            "speed_fps": 0,
            "eta": "",
            "space_saved": 0,
            "new_size": 0,
            "error": file_error,
            "lock_host": lock_host,
            "queue_pos": 0,
        }

    _save_state()
    return {"count": len(scanned_files)}


# ---------------------------------------------------------------------------
# Encoding worker
# ---------------------------------------------------------------------------

def _parse_progress(line: str, duration: float) -> dict:
    """Parse an ffmpeg stderr line for progress info."""
    result = {}
    # frame= 1234 fps= 45.6 ...  time=00:01:23.45 ...
    fps_m = re.search(r"fps=\s*([\d.]+)", line)
    time_m = re.search(r"time=(\d+):(\d+):([\d.]+)", line)

    if fps_m:
        result["fps"] = float(fps_m.group(1))
    if time_m and duration > 0:
        h, m, s = int(time_m.group(1)), int(time_m.group(2)), float(time_m.group(3))
        elapsed_sec = h * 3600 + m * 60 + s
        pct = min(elapsed_sec / duration * 100, 99.9)
        result["progress"] = round(pct, 1)

        # ETA
        if result.get("fps", 0) > 0 and elapsed_sec > 0:
            remaining_sec = duration - elapsed_sec
            # frames remaining / fps (approximate)
            # Better: use time-based estimate
            rate = elapsed_sec / (time.time() - result.get("_start", time.time()) + 0.001)
            # simpler: remaining_sec directly as real-time estimate isn't reliable
            # Use ratio: if X real seconds produced Y video seconds
            pass
        result["elapsed_video_sec"] = elapsed_sec

    return result


def _run_ffmpeg(cmd, entry, duration, dst):
    """Run an FFmpeg command, tracking progress. Returns (success, error_lines)."""
    start_real = time.time()
    try:
        si = None
        if sys.platform == "win32":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        proc = subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            text=True,
            startupinfo=si,
            encoding="utf-8",
            errors="replace",
        )

        buf = ""
        last_lines = []
        while True:
            if cancel_event.is_set():
                proc.kill()
                entry["status"] = "cancelled"
                try:
                    os.remove(dst)
                except OSError:
                    pass
                return False, ["Cancelled"]

            ch = proc.stderr.read(1)
            if not ch:
                break
            if ch in ("\r", "\n"):
                if buf.strip():
                    last_lines.append(buf.strip())
                    if len(last_lines) > 20:
                        last_lines.pop(0)
                    info = _parse_progress(buf, duration)
                    if "progress" in info:
                        entry["progress"] = info["progress"]
                    if "fps" in info:
                        entry["speed_fps"] = round(info["fps"], 1)
                    if "elapsed_video_sec" in info and info["elapsed_video_sec"] > 0:
                        elapsed_real = time.time() - start_real
                        ratio = info["elapsed_video_sec"] / elapsed_real if elapsed_real > 0 else 1
                        remaining_video = duration - info["elapsed_video_sec"]
                        if ratio > 0:
                            eta_sec = remaining_video / ratio
                            if eta_sec < 60:
                                entry["eta"] = f"{int(eta_sec)}s"
                            elif eta_sec < 3600:
                                entry["eta"] = f"{int(eta_sec // 60)}m {int(eta_sec % 60)}s"
                            else:
                                entry["eta"] = f"{int(eta_sec // 3600)}h {int((eta_sec % 3600) // 60)}m"
                buf = ""
            else:
                buf += ch

        proc.wait()

        if proc.returncode != 0:
            try:
                os.remove(dst)
            except OSError:
                pass
            return False, last_lines

        return True, last_lines

    except Exception as e:
        try:
            os.remove(dst)
        except OSError:
            pass
        return False, [str(e)]


def _encode_one(file_id: str, worker_id: int, worker_type: str = "gpu"):
    """Encode a single file.
    worker_type: 'gpu' tries GPU first with CPU fallback, 'cpu' uses CPU only.
    """
    if worker_id in active_workers:
        active_workers[worker_id]["file_id"] = file_id
    entry = scanned_files.get(file_id)
    if not entry:
        return

    src = entry["path"]

    # Multi-machine lock: skip if another instance is encoding this file
    if not _acquire_lock(src):
        entry["status"] = "pending"  # Stay pending, not skipped — can retry
        entry["error"] = ""
        if worker_id in active_workers:
            active_workers[worker_id]["file_id"] = None
        return

    try:
        _encode_one_inner(entry, src, file_id, worker_id, worker_type)
    finally:
        _release_lock(src)


def _encode_one_inner(entry, src, file_id, worker_id, worker_type):
    """Inner encode logic, called with lock held."""
    # Pre-encode safety: re-probe file to catch duplicates already converted
    if os.path.isfile(src):
        recheck = probe_file(src)
        if recheck and recheck["codec"] in ("hevc", "h265", "hev1"):
            max_h = settings["max_resolution"]
            needs_downscale = max_h and recheck["height"] > max_h
            if not needs_downscale:
                entry["status"] = "skipped"
                entry["error"] = "Already HEVC (skipped duplicate)"
                if worker_id in active_workers:
                    active_workers[worker_id]["file_id"] = None
                return
    elif not os.path.isfile(src):
        entry["status"] = "error"
        entry["error"] = "File not found"
        if worker_id in active_workers:
            active_workers[worker_id]["file_id"] = None
        return

    dst = _output_path(src)
    entry["status"] = "encoding"
    entry["progress"] = 0

    duration = entry["duration"]
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_encode.log")

    attempts = []
    success = False
    err_lines = []

    # Build list of attempts based on worker type
    gpu_enc = detect_gpu_encoder()
    if worker_type == "gpu" and settings["hw_mode"] != "cpu" and gpu_enc:
        attempts.append(("GPU", False))
        attempts.append(("GPU", True))
    attempts.append(("CPU", False))
    attempts.append(("CPU", True))

    for attempt_label, simple_mode in attempts:
        if cancel_event.is_set():
            break

        entry["progress"] = 0
        entry["speed_fps"] = 0

        if attempt_label == "CPU":
            old_hw = settings["hw_mode"]
            settings["hw_mode"] = "cpu"
            cmd = _build_ffmpeg_cmd(src, dst, simple_mode=simple_mode)
            settings["hw_mode"] = old_hw
            mode_desc = f"CPU {'simple' if simple_mode else 'full'}"
        else:
            cmd = _build_ffmpeg_cmd(src, dst, simple_mode=simple_mode)
            mode_desc = f"GPU {'simple' if simple_mode else 'full'}"

        entry["eta"] = mode_desc

        success, err_lines = _run_ffmpeg(cmd, entry, duration, dst)

        if success:
            break

    # Write log
    try:
        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write(f"Source: {src}\n")
            lf.write(f"Destination: {dst}\n")
            lf.write(f"GPU encoder: {gpu_enc or 'none'}\n")
            lf.write(f"Last command: {' '.join(cmd)}\n")
            lf.write(f"Success: {success}\n\n")
            lf.write("--- FFmpeg output ---\n")
            for line in err_lines:
                lf.write(line + "\n")
    except OSError:
        pass

    if not success:
        # Don't overwrite "cancelled" status with "error"
        if entry.get("status") != "cancelled":
            entry["status"] = "error"
            error_lines = [l for l in err_lines if not l.startswith("frame=") and not l.startswith("size=")]
            err_detail = " | ".join(error_lines[-5:]) if error_lines else "unknown error"
            entry["error"] = err_detail[:300]
        return

    # Success
    entry["progress"] = 100

    # Handle replace mode
    final_dst = _final_path(src, dst)
    if settings["output_mode"] == "replace" and dst != final_dst:
        # Delete original first to free space on the target drive
        try:
            os.remove(src)
        except OSError:
            pass
        # Move temp to final (shutil.move handles cross-drive moves)
        try:
            shutil.move(dst, final_dst)
        except OSError:
            entry["error"] = "Encoded OK but failed to replace original"

    # Calculate space saved
    try:
        new_size = os.path.getsize(final_dst)
        saved = entry["size"] - new_size
        entry["space_saved"] = saved
        entry["new_size"] = new_size
        stats["total_space_saved"] += max(saved, 0)
    except OSError:
        new_size = 0
        pass

    stats["files_processed"] += 1

    # Mark done AFTER size is calculated (avoids race with frontend poll)
    entry["status"] = "done"

    # Save to job history
    _save_history_entry(entry, new_size)

    # Persist state to disk
    _save_state()

    # Delete original if setting enabled (suffix mode)
    if settings["delete_originals"] and settings["output_mode"] == "suffix":
        try:
            os.remove(src)
        except OSError:
            pass


def _worker(worker_id: int, worker_type: str = "gpu"):
    """Process the encode queue. Each worker pulls one file at a time."""
    while True:
        with encoding_lock:
            # Check if this worker type is over the target count
            type_target = gpu_workers if worker_type == "gpu" else cpu_workers
            type_count = sum(1 for w in active_workers.values() if w.get("type") == worker_type)
            if type_count > type_target:
                if worker_id in active_workers:
                    active_workers[worker_id]["file_id"] = None
                    del active_workers[worker_id]
                return

            if not encode_queue or cancel_event.is_set():
                if worker_id in active_workers:
                    active_workers[worker_id]["file_id"] = None
                cancel_event.clear()
                return
            file_id = encode_queue.pop(0)
            # Update queue positions
            for i, qid in enumerate(encode_queue):
                if qid in scanned_files:
                    scanned_files[qid]["queue_pos"] = i + 1

        _encode_one(file_id, worker_id, worker_type)

        if cancel_event.is_set():
            # Mark remaining as pending
            with encoding_lock:
                for qid in encode_queue:
                    if qid in scanned_files:
                        scanned_files[qid]["status"] = "pending"
                        scanned_files[qid]["queue_pos"] = 0
                encode_queue.clear()
                if worker_id in active_workers:
                    active_workers[worker_id]["file_id"] = None
                cancel_event.clear()
            return


def _start_workers():
    """Ensure the right number of GPU and CPU worker threads are running."""
    global _next_worker_id
    # Clean up dead workers
    dead = [wid for wid, w in active_workers.items() if not w["thread"].is_alive()]
    for wid in dead:
        del active_workers[wid]

    if not encode_queue:
        return

    cancel_event.clear()

    # Count current workers by type
    current_gpu = sum(1 for w in active_workers.values() if w.get("type") == "gpu")
    current_cpu = sum(1 for w in active_workers.values() if w.get("type") == "cpu")

    # Spin up GPU workers
    while current_gpu < gpu_workers and encode_queue:
        wid = _next_worker_id
        _next_worker_id += 1
        t = threading.Thread(target=_worker, args=(wid, "gpu"), daemon=True)
        active_workers[wid] = {"thread": t, "file_id": None, "type": "gpu"}
        t.start()
        current_gpu += 1

    # Spin up CPU workers
    while current_cpu < cpu_workers and encode_queue:
        wid = _next_worker_id
        _next_worker_id += 1
        t = threading.Thread(target=_worker, args=(wid, "cpu"), daemon=True)
        active_workers[wid] = {"thread": t, "file_id": None, "type": "cpu"}
        t.start()
        current_cpu += 1


# ---------------------------------------------------------------------------

def _active_file_ids() -> set:
    """Return set of file_ids currently being encoded by workers."""
    return {w["file_id"] for w in active_workers.values() if w["file_id"]}


# Format helpers
# ---------------------------------------------------------------------------

def fmt_size(b: int | float) -> str:
    if b < 0:
        return f"-{fmt_size(-b)}"
    if b < 1024:
        return f"{b} B"
    elif b < 1024**2:
        return f"{b / 1024:.1f} KB"
    elif b < 1024**3:
        return f"{b / 1024**2:.1f} MB"
    else:
        return f"{b / 1024**3:.2f} GB"


def fmt_duration(sec: float) -> str:
    if sec <= 0:
        return "?"
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# ---------------------------------------------------------------------------
# Job History
# ---------------------------------------------------------------------------

_history_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.json")


def _load_history() -> list:
    try:
        with open(_history_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_history_entry(entry: dict, new_size: int):
    history = _load_history()
    history.insert(0, {
        "filename": entry["filename"],
        "path": entry["path"],
        "codec": entry["codec"],
        "resolution": entry["resolution"],
        "original_size": entry["size"],
        "new_size": new_size,
        "space_saved": entry.get("space_saved", 0),
        "duration": entry.get("duration", 0),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    # Keep last 500 entries
    history = history[:500]
    try:
        with open(_history_file, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/native-browse", methods=["POST"])
def api_native_browse():
    """Open the OS native folder picker and return the selected path."""
    import subprocess as _nsp
    try:
        if sys.platform == "darwin":
            # macOS: use osascript to open Finder folder dialog
            script = 'tell application "Finder" to activate\n' \
                     'set folderPath to POSIX path of (choose folder with prompt "Select folder to scan")\n' \
                     'return folderPath'
            result = _nsp.run(["osascript", "-e", script],
                              capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                return jsonify({"path": None, "cancelled": True})
            path = result.stdout.strip()
            return jsonify({"path": path, "cancelled": False})
        elif sys.platform == "win32":
            # Windows: use PowerShell folder dialog (forced to foreground)
            ps_script = '''
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Application]::EnableVisualStyles()
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = "Select folder to scan"
$dialog.ShowNewFolderButton = $false
$form = New-Object System.Windows.Forms.Form
$form.TopMost = $true
$result = $dialog.ShowDialog($form)
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
    Write-Output $dialog.SelectedPath
} else {
    Write-Output "CANCELLED"
}'''
            result = _nsp.run(["powershell", "-Command", ps_script],
                              capture_output=True, text=True, timeout=120)
            path = result.stdout.strip()
            if path == "CANCELLED" or not path:
                return jsonify({"path": None, "cancelled": True})
            return jsonify({"path": path, "cancelled": False})
        else:
            # Linux: try zenity
            result = _nsp.run(["zenity", "--file-selection", "--directory",
                               "--title=Select folder to scan"],
                              capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                return jsonify({"path": None, "cancelled": True})
            return jsonify({"path": result.stdout.strip(), "cancelled": False})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/browse", methods=["POST"])
def api_browse():
    """List drives (if no path) or subdirectories of a given path."""
    data = request.json or {}
    target = data.get("path", "").strip()

    # macOS: auto-mount SMB shares when user types smb:// path
    if target.lower().startswith("smb://") and sys.platform == "darwin":
        parts = target.replace("smb://", "").strip("/").split("/")
        hostname = parts[0] if parts else ""

        if len(parts) < 2 or not parts[1]:
            # Just hostname — list available shares
            try:
                import subprocess as _sp4
                result = _sp4.run(["smbutil", "view", f"//{hostname}"],
                                  capture_output=True, text=True, timeout=10)
                shares = []
                in_shares = False
                for line in result.stdout.splitlines():
                    if "Share" in line and "Type" in line:
                        in_shares = True
                        continue
                    if in_shares and line.strip():
                        cols = line.split()
                        if cols and cols[-1] == "Disk":
                            share_name = " ".join(cols[:-1])
                            if not share_name.endswith("$"):  # Skip admin shares
                                shares.append({
                                    "name": f"📁 {share_name}",
                                    "path": f"smb://{hostname}/{share_name}",
                                    "type": "network",
                                })
                if not shares:
                    return jsonify({"error": f"No shares found on {hostname}. Check credentials."}), 400
                return jsonify({"items": shares, "current": f"smb://{hostname}", "parent": ""})
            except Exception as e:
                return jsonify({"error": f"Cannot list shares on {hostname}: {e}"}), 400

        # Has share name — mount it
        share_name = parts[1]
        try:
            import subprocess as _sp
            _sp.run(["open", target], timeout=10)
            import time as _t
            _t.sleep(3)
            # Find the mount point in /Volumes/
            for v in Path("/Volumes").iterdir():
                if v.name.lower() == share_name.lower() or v.name.lower().startswith(share_name.lower()):
                    target = str(v)
                    break
            else:
                target = f"/Volumes/{share_name}"
        except Exception as e:
            return jsonify({"error": f"Failed to mount: {e}"}), 400

    # macOS: convert smb:// to mount on other platforms too
    if target.lower().startswith("smb://") and sys.platform != "darwin":
        # Convert smb://host/share to \\host\share for Windows
        target = target.replace("smb://", "\\\\").replace("/", "\\")

    # No path → list drives on Windows, volumes on macOS, root on Linux
    if not target:
        if sys.platform == "win32":
            import string
            drives = []
            for letter in string.ascii_uppercase:
                dp = f"{letter}:\\"
                if os.path.isdir(dp):
                    drives.append({"name": f"{letter}:", "path": dp, "type": "drive"})
            # Also discover network shares via 'net use'
            try:
                result = _run(["net", "use"], capture_output=True, text=True, timeout=5)
                for line in result.stdout.splitlines():
                    parts = line.split()
                    # Lines like: OK  Z:  \\SERVER\Share  Microsoft Windows Network
                    # or:  OK  \\SERVER\Share  Microsoft Windows Network  (no drive letter)
                    for i, p in enumerate(parts):
                        if p.startswith("\\\\"):
                            unc = p
                            # Skip if already mapped to a drive letter (already in list)
                            has_letter = any(x.isalpha() and len(x) <= 2 and x.endswith(":") for x in parts[:i])
                            if not has_letter:
                                name = unc.split("\\")[-1] or unc
                                drives.append({"name": f"📡 {name}", "path": unc, "type": "network"})
                            break
            except Exception:
                pass
            return jsonify({"items": drives, "current": "", "parent": ""})
        elif sys.platform == "darwin":
            # macOS: show /Volumes/ entries (includes local disks + network shares)
            volumes = []
            vol_path = Path("/Volumes")
            if vol_path.is_dir():
                for entry in sorted(vol_path.iterdir(), key=lambda e: e.name.lower()):
                    if entry.is_dir():
                        # Detect if it's a network mount
                        is_network = False
                        try:
                            import subprocess as _sp2
                            mounts = _sp2.run(["mount"], capture_output=True, text=True, timeout=5).stdout
                            if f"on /Volumes/{entry.name}" in mounts and "smbfs" in mounts.split(f"on /Volumes/{entry.name}")[0].split("\n")[-1]:
                                is_network = True
                        except Exception:
                            pass
                        icon = "🖥" if is_network else "💾"
                        volumes.append({
                            "name": f"{icon} {entry.name}",
                            "path": str(entry),
                            "type": "network" if is_network else "drive",
                        })
            # Also add home directory for convenience
            home = Path.home()
            volumes.insert(0, {"name": f"🏠 {home.name}", "path": str(home), "type": "folder"})
            # Discover network computers via dns-sd / Bonjour
            try:
                import subprocess as _sp3
                result = _sp3.run(["bash", "-c", "timeout 2 dns-sd -B _smb._tcp local 2>/dev/null || true"],
                                  capture_output=True, text=True, timeout=5)
                seen_hosts = set()
                for line in result.stdout.splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 7 and parts[1] != "Timestamp":
                        host = parts[-1]
                        if host and host not in seen_hosts:
                            seen_hosts.add(host)
                            # Only show if not already mounted
                            if not any(v["name"].endswith(host) for v in volumes):
                                volumes.append({
                                    "name": f"🌐 {host}",
                                    "path": f"smb://{host}",
                                    "type": "network",
                                })
            except Exception:
                pass
            return jsonify({"items": volumes, "current": "", "parent": ""})
        else:
            target = "/"

    target_path = Path(target)
    if not target_path.is_dir():
        return jsonify({"error": f"Not a directory: {target}"}), 400

    items = []
    try:
        for entry in sorted(target_path.iterdir(), key=lambda e: e.name.lower()):
            try:
                if entry.is_dir() and not entry.name.startswith("."):
                    items.append({
                        "name": entry.name,
                        "path": str(entry),
                        "type": "folder",
                    })
            except (OSError, PermissionError):
                # Skip broken symlinks, network shortcuts, and inaccessible entries
                pass
    except PermissionError:
        return jsonify({"error": "Permission denied"}), 403
    except OSError:
        return jsonify({"error": f"Cannot read directory: {target}"}), 400

    parent = str(target_path.parent) if target_path.parent != target_path else ""

    return jsonify({
        "items": items,
        "current": str(target_path),
        "parent": parent,
    })


@app.route("/api/clear-locks", methods=["POST"])
def api_clear_locks():
    """Remove all .squishbox.lock files from scanned folders."""
    cleared = 0
    folders = scan_folders if scan_folders else ([scan_folder] if scan_folder else [])
    for folder in folders:
        fp = Path(folder)
        if fp.is_dir():
            for lock in fp.rglob("*" + _LOCK_SUFFIX):
                try:
                    lock.unlink()
                    cleared += 1
                except OSError:
                    pass
    # Also update scanned_files status for any locked files
    for fid, entry in list(scanned_files.items()):
        if entry.get("status") == "locked":
            entry["status"] = "pending"
            entry["error"] = ""
            entry["lock_host"] = ""
    return jsonify({"ok": True, "cleared": cleared})


@app.route("/api/scan", methods=["POST"])
def api_scan():
    data = request.json or {}
    folder = data.get("folder", "").strip()
    append = data.get("append", False)  # Add to existing scan
    if not folder:
        return jsonify({"error": "No folder specified"}), 400
    result = scan_directory(folder, append=append)
    if "error" in result:
        return jsonify(result), 400
    return jsonify({"ok": True, "count": result["count"], "folder": scan_folder, "folders": scan_folders})


@app.route("/api/files")
def api_files():
    files = []
    # Snapshot to avoid "OrderedDict mutated during iteration" when scan thread is running
    for fid, entry in list(scanned_files.items()):
        # Live re-check: if status is "locked", re-check if the lock is still there
        if entry.get("status") == "locked":
            if not _is_locked(entry["path"]):
                entry["status"] = "pending"
                entry["error"] = ""
                entry["lock_host"] = ""

        # Show relative path from scan root for nested files
        try:
            rel = str(Path(entry["path"]).relative_to(scan_folder))
        except ValueError:
            rel = entry["filename"]
        files.append({
            "id": entry["id"],
            "filename": rel,
            "codec": entry["codec"],
            "resolution": entry["resolution"],
            "size": entry["size"],
            "size_fmt": fmt_size(entry["size"]),
            "duration": entry["duration"],
            "duration_fmt": fmt_duration(entry["duration"]),
            "status": entry["status"],
            "progress": entry["progress"],
            "speed_fps": entry["speed_fps"],
            "eta": entry["eta"],
            "space_saved": entry["space_saved"],
            "space_saved_fmt": fmt_size(entry["space_saved"]) if entry["space_saved"] else "",
            "new_size": entry.get("new_size", 0),
            "new_size_fmt": fmt_size(entry["new_size"]) if entry.get("new_size") else "",
            "error": entry["error"],
            "lock_host": entry.get("lock_host", ""),
            "queue_pos": entry["queue_pos"],
        })
    return jsonify({
        "files": files,
        "folder": scan_folder,
        "stats": {
            "total_space_saved": stats["total_space_saved"],
            "total_space_saved_fmt": fmt_size(stats["total_space_saved"]),
            "files_processed": stats["files_processed"],
            "files_remaining": len(encode_queue) + len(_active_file_ids()),
            "is_encoding": len(_active_file_ids()) > 0,
            "workers_active": len(_active_file_ids()),
            "gpu_workers": gpu_workers,
            "cpu_workers": cpu_workers,
            "workers_max": gpu_workers + cpu_workers,
            "folders": scan_folders,
        },
    })


@app.route("/api/squish", methods=["POST"])
def api_squish():
    """Squish one file by ID."""
    data = request.json or {}
    file_id = data.get("id")
    if not file_id or file_id not in scanned_files:
        return jsonify({"error": "Invalid file ID"}), 400

    entry = scanned_files[file_id]
    if entry["status"] not in ("pending",):
        return jsonify({"error": f"File status is '{entry['status']}', can't squish"}), 400

    # Dedup: don't queue if same file path is already queued or encoding
    with encoding_lock:
        src_path = entry["path"]
        for qid in encode_queue:
            q_entry = scanned_files.get(qid)
            if q_entry and q_entry["path"] == src_path:
                return jsonify({"error": "File already in queue"}), 400
        for w in active_workers.values():
            if w["file_id"]:
                cur = scanned_files.get(w["file_id"])
                if cur and cur["path"] == src_path:
                    return jsonify({"error": "File is currently encoding"}), 400

        entry["status"] = "queued"
        entry["queue_pos"] = len(encode_queue) + 1
        encode_queue.append(file_id)

    _start_workers()
    return jsonify({"ok": True})


@app.route("/api/squish-all", methods=["POST"])
def api_squish_all():
    """Queue all pending files, skipping duplicate paths."""
    count = 0
    with encoding_lock:
        queued_paths = set()
        for qid in encode_queue:
            q_entry = scanned_files.get(qid)
            if q_entry:
                queued_paths.add(q_entry["path"])
        for w in active_workers.values():
            if w["file_id"]:
                cur = scanned_files.get(w["file_id"])
                if cur:
                    queued_paths.add(cur["path"])

        for fid, entry in scanned_files.items():
            if entry["status"] in ("pending", "locked") and entry["path"] not in queued_paths:
                entry["status"] = "queued"
                encode_queue.append(fid)
                entry["queue_pos"] = len(encode_queue)
                queued_paths.add(entry["path"])
                count += 1

    _start_workers()
    return jsonify({"ok": True, "queued": count})


@app.route("/api/squish-selected", methods=["POST"])
def api_squish_selected():
    """Queue selected files by ID list."""
    data = request.json or {}
    ids = data.get("ids", [])
    count = 0
    with encoding_lock:
        for fid in ids:
            entry = scanned_files.get(fid)
            if entry and entry["status"] in ("pending", "skipped"):
                entry["status"] = "queued"
                encode_queue.append(fid)
                entry["queue_pos"] = len(encode_queue)
                count += 1

    _start_workers()
    return jsonify({"ok": True, "queued": count})


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    """Cancel current encoding and clear the queue."""
    cancel_event.set()
    return jsonify({"ok": True})


@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    gpu_enc = detect_gpu_encoder()
    return jsonify({
        **settings,
        "gpu_encoder": gpu_enc or "none",
        "gpu_label": {
            "hevc_videotoolbox": "Apple VideoToolbox",
            "hevc_amf": "AMD AMF",
            "hevc_qsv": "Intel QSV",
            "hevc_nvenc": "NVIDIA NVENC",
        }.get(gpu_enc, "None detected"),
    })


@app.route("/api/settings", methods=["POST"])
def api_set_settings():
    data = request.json or {}
    if "quality" in data:
        settings["quality"] = max(18, min(28, int(data["quality"])))
    if "max_resolution" in data:
        val = int(data["max_resolution"])
        settings["max_resolution"] = val if val in (0, 720, 1080) else 1080
    if "container" in data:
        settings["container"] = data["container"] if data["container"] in ("mkv", "mp4") else "mkv"
    if "hw_mode" in data:
        settings["hw_mode"] = data["hw_mode"] if data["hw_mode"] in ("auto", "videotoolbox", "amf", "qsv", "nvenc", "cpu") else "auto"
    if "delete_originals" in data:
        settings["delete_originals"] = bool(data["delete_originals"])
    if "output_mode" in data:
        settings["output_mode"] = data["output_mode"] if data["output_mode"] in ("suffix", "replace") else "suffix"
    if "min_file_size_mb" in data:
        settings["min_file_size_mb"] = max(0, int(data["min_file_size_mb"]))
    return jsonify({"ok": True, "settings": settings})


@app.route("/api/rescan", methods=["POST"])
def api_rescan():
    """Re-scan the current folder."""
    if not scan_folder:
        return jsonify({"error": "No folder to rescan"}), 400
    result = scan_directory(scan_folder)
    if "error" in result:
        return jsonify(result), 400
    return jsonify({"ok": True, "count": result["count"]})


@app.route("/api/cancel-all", methods=["POST"])
def api_cancel_all():
    """Cancel current encoding and reset all queued/encoding files to pending."""
    cancel_event.set()
    time.sleep(0.5)
    with encoding_lock:
        for fid, entry in scanned_files.items():
            if entry["status"] in ("encoding", "queued", "cancelled"):
                entry["status"] = "pending"
                entry["progress"] = 0
                entry["speed_fps"] = 0
                entry["eta"] = ""
                entry["error"] = ""
                entry["queue_pos"] = 0
        encode_queue.clear()
        # Clean up workers
        for wid in list(active_workers.keys()):
            active_workers[wid]["file_id"] = None
    cancel_event.clear()
    _save_state()
    return jsonify({"ok": True})


@app.route("/api/workers", methods=["GET"])
def api_get_workers():
    """Get current worker status."""
    workers = []
    for wid, w in active_workers.items():
        entry = scanned_files.get(w["file_id"]) if w["file_id"] else None
        workers.append({
            "id": wid,
            "type": w.get("type", "gpu"),
            "file_id": w["file_id"],
            "filename": entry["filename"] if entry else None,
            "progress": entry["progress"] if entry else 0,
            "speed_fps": entry["speed_fps"] if entry else 0,
            "eta": entry["eta"] if entry else "",
            "alive": w["thread"].is_alive(),
        })
    return jsonify({
        "workers": workers,
        "gpu_workers": gpu_workers,
        "cpu_workers": cpu_workers,
        "total_workers": gpu_workers + cpu_workers,
        "active": len(_active_file_ids()),
    })


@app.route("/api/workers", methods=["POST"])
def api_set_workers():
    """Set worker counts. Supports gpu_delta, cpu_delta, or gpu_count/cpu_count."""
    global gpu_workers, cpu_workers
    data = request.json or {}
    if "gpu_count" in data:
        gpu_workers = max(0, min(4, int(data["gpu_count"])))
    if "cpu_count" in data:
        cpu_workers = max(0, min(8, int(data["cpu_count"])))
    if "gpu_delta" in data:
        gpu_workers = max(0, min(4, gpu_workers + int(data["gpu_delta"])))
    if "cpu_delta" in data:
        cpu_workers = max(0, min(8, cpu_workers + int(data["cpu_delta"])))
    # Ensure at least 1 total worker
    if gpu_workers + cpu_workers < 1:
        gpu_workers = 1
    # If increasing and there's work, spin up more workers
    if encode_queue:
        _start_workers()
    return jsonify({
        "ok": True,
        "gpu_workers": gpu_workers,
        "cpu_workers": cpu_workers,
        "total_workers": gpu_workers + cpu_workers,
        "active": len(_active_file_ids()),
    })


@app.route("/api/add-folder", methods=["POST"])
def api_add_folder():
    """Add another folder to the scan (append mode)."""
    data = request.json or {}
    folder = data.get("folder", "").strip()
    if not folder:
        return jsonify({"error": "No folder specified"}), 400
    result = scan_directory(folder, append=True)
    if "error" in result:
        return jsonify(result), 400
    return jsonify({"ok": True, "count": result["count"], "folders": scan_folders})


@app.route("/api/restart", methods=["POST"])
def api_restart():
    """Save state and restart the server."""
    _save_state()

    def _do_restart():
        time.sleep(1)
        if sys.platform == "win32":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            subprocess.Popen(
                [sys.executable] + sys.argv,
                startupinfo=si,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            subprocess.Popen([sys.executable] + sys.argv)
        os._exit(0)

    threading.Thread(target=_do_restart, daemon=True).start()
    return jsonify({"ok": True, "message": "Restarting..."})


@app.route("/api/log")
def api_log():
    """Return the last encode log for debugging."""
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_encode.log")
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            return f.read(), 200, {"Content-Type": "text/plain; charset=utf-8"}
    except FileNotFoundError:
        return "No encode log yet.", 200, {"Content-Type": "text/plain"}


@app.route("/api/history")
def api_history():
    """Return job history."""
    history = _load_history()
    # Add formatted sizes
    for h in history:
        h["original_size_fmt"] = fmt_size(h.get("original_size", 0))
        h["new_size_fmt"] = fmt_size(h.get("new_size", 0))
        h["space_saved_fmt"] = fmt_size(h.get("space_saved", 0))
        h["duration_fmt"] = fmt_duration(h.get("duration", 0))
        orig = h.get("original_size", 1)
        h["ratio"] = f"{((orig - h.get('new_size', orig)) / orig * 100):.0f}%" if orig > 0 else "0%"
    total_saved = sum(h.get("space_saved", 0) for h in history)
    return jsonify({
        "history": history,
        "total_saved": total_saved,
        "total_saved_fmt": fmt_size(total_saved),
        "total_files": len(history),
    })


@app.route("/api/clear-history", methods=["POST"])
def api_clear_history():
    """Wipe all encoding history."""
    try:
        if os.path.exists(_history_file):
            with open(_history_file, "w", encoding="utf-8") as f:
                json.dump([], f)
        # Also clear done/completed status from scanned files so the list looks fresh
        for fid, entry in list(scanned_files.items()):
            if entry.get("status") in ("done", "skipped", "error", "cancelled"):
                entry["status"] = "pending"
                entry["error"] = ""
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="SquishBox — batch video transcoder")
    parser.add_argument("--port", "-p", type=int, default=5555, help="Web UI port (default: 5555)")
    args = parser.parse_args()

    _load_state()
    gpu = detect_gpu_encoder()
    gpu_label = {"hevc_videotoolbox": "Apple VideoToolbox", "hevc_amf": "AMD AMF", "hevc_qsv": "Intel QSV", "hevc_nvenc": "NVIDIA NVENC"}.get(gpu, "None (CPU only)")
    print()
    print("  ╔═══════════════════════════════════════╗")
    print("  ║          🗜️  SquishBox v5.1            ║")
    print("  ║  Multi-worker + Multi-folder          ║")
    print("  ╚═══════════════════════════════════════╝")
    print()
    print(f"  → Open http://localhost:{args.port} in your browser")
    print(f"  → GPU encoder: {gpu_label} ({gpu or 'none'})")
    print()
    app.run(host="0.0.0.0", port=args.port, debug=False)
