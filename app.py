"""
SquishBox — Point at a folder, click squish, watch files shrink.
A simple batch video transcoder with a web UI. FFmpeg under the hood.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".wmv", ".flv", ".webm"}

settings = {
    "quality": 23,           # CRF / global_quality value
    "max_resolution": 1080,  # 0 = no limit (4K), 1080, 720
    "container": "mkv",      # mkv or mp4
    "hw_mode": "auto",       # auto / qsv / nvenc / videotoolbox / vaapi / cpu
    "delete_originals": False,
    "output_mode": "suffix", # suffix (_squished) or replace
}

# Scanned files: {file_id: {path, filename, codec, resolution, width, height,
#                            size, duration, status, progress, speed_fps,
#                            eta, space_saved, error, queue_pos}}
scanned_files: OrderedDict[str, dict] = OrderedDict()
scan_folder: str = ""

# Queue / encoding state
encode_queue: list[str] = []       # file_ids waiting
current_encode_id: str | None = None
encoding_lock = threading.Lock()
encode_thread: threading.Thread | None = None
cancel_event = threading.Event()

# Stats
stats = {
    "total_space_saved": 0,
    "files_processed": 0,
}

# QSV availability cache
_hw_encoders: dict | None = None


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


def detect_hw_encoders() -> dict:
    """Detect all available HEVC hardware encoders.

    Returns dict like:
        {"qsv": True, "nvenc": False, "videotoolbox": True, "vaapi": False}
    """
    global _hw_encoders
    if _hw_encoders is not None:
        return _hw_encoders

    encoder_map = {
        "qsv": "hevc_qsv",           # Intel Quick Sync
        "nvenc": "hevc_nvenc",         # Nvidia NVENC
        "videotoolbox": "hevc_videotoolbox",  # macOS
        "vaapi": "hevc_vaapi",         # Linux AMD/Intel
    }

    _hw_encoders = {k: False for k in encoder_map}
    try:
        r = _run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10,
        )
        for key, enc_name in encoder_map.items():
            if enc_name in r.stdout:
                _hw_encoders[key] = True
    except Exception:
        pass
    return _hw_encoders


def _best_hw_encoder() -> str | None:
    """Return the best available HW encoder name, or None for CPU.

    Priority: nvenc > qsv > videotoolbox > vaapi
    (nvenc is fastest, qsv is most common, vtb for Mac, vaapi for Linux)
    """
    hw = detect_hw_encoders()
    priority = ["nvenc", "qsv", "videotoolbox", "vaapi"]
    for p in priority:
        if hw.get(p):
            return p
    return None


def _choose_encoder() -> tuple[str, list[str]]:
    """Return (encoder_name, extra_ffmpeg_args) based on settings and availability."""
    mode = settings["hw_mode"]
    quality = settings["quality"]
    hw = detect_hw_encoders()

    # Map hw_mode to encoder name + args
    encoder_configs = {
        "qsv": ("hevc_qsv", ["-global_quality", str(quality), "-preset", "medium"]),
        "nvenc": ("hevc_nvenc", ["-cq", str(quality), "-preset", "p4", "-tune", "hq"]),
        "videotoolbox": ("hevc_videotoolbox", ["-q:v", str(max(30, quality * 1.3))]),
        "vaapi": ("hevc_vaapi", ["-qp", str(quality)]),
        "cpu": ("libx265", ["-crf", str(quality), "-preset", "medium"]),
    }

    if mode == "auto":
        best = _best_hw_encoder()
        if best:
            return encoder_configs[best]
        return encoder_configs["cpu"]

    if mode in encoder_configs and mode != "cpu":
        if hw.get(mode, False):
            return encoder_configs[mode]
        # Requested HW not available, fall back to CPU
        return encoder_configs["cpu"]

    return encoder_configs["cpu"]


def _build_ffmpeg_cmd(src: str, dst: str) -> list[str]:
    """Build the full ffmpeg command for encoding one file."""
    encoder, enc_args = _choose_encoder()
    max_h = settings["max_resolution"]
    container = settings["container"]

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

    # Subtitles: copy all (MKV supports all subtitle formats; MP4 is limited)
    if container == "mkv":
        cmd += ["-c:s", "copy"]
    else:
        # MP4 only supports mov_text; try to copy, ffmpeg will drop incompatible
        cmd += ["-c:s", "mov_text"]

    # Map all streams
    cmd += ["-map", "0"]

    # Output
    cmd.append(str(dst))
    return cmd


def _output_path(src: str) -> str:
    """Compute output file path based on settings."""
    p = Path(src)
    ext = f".{settings['container']}"
    if settings["output_mode"] == "replace":
        # Write to temp, then replace after success
        return str(p.with_suffix(ext + ".tmp"))
    else:
        stem = p.stem
        # Remove existing _squished suffix to avoid stacking
        stem = re.sub(r"_squished$", "", stem)
        return str(p.with_name(f"{stem}_squished{ext}"))


def _final_path(src: str, tmp_path: str) -> str:
    """If replace mode, return the final path (original name, new ext)."""
    if settings["output_mode"] == "replace":
        p = Path(src)
        ext = f".{settings['container']}"
        return str(p.with_suffix(ext))
    return tmp_path


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def scan_directory(folder: str) -> dict:
    """Scan folder for video files and populate scanned_files."""
    global scan_folder, scanned_files
    scan_folder = folder
    scanned_files.clear()

    folder_path = Path(folder)
    if not folder_path.is_dir():
        return {"error": f"Not a valid directory: {folder}"}

    files_found = []
    for f in sorted(folder_path.iterdir()):
        if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS:
            files_found.append(f)

    for f in files_found:
        info = probe_file(str(f))
        fid = uuid.uuid4().hex[:8]
        already_target = False
        if info:
            is_hevc = info["codec"] in ("hevc", "h265", "hev1")
            is_target_container = f.suffix.lower() == f".{settings['container']}"
            already_target = is_hevc and is_target_container

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
            "status": "skipped" if already_target else "pending",
            "progress": 0,
            "speed_fps": 0,
            "eta": "",
            "space_saved": 0,
            "error": "",
            "queue_pos": 0,
        }

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


def _encode_one(file_id: str):
    """Encode a single file. Runs in the worker thread."""
    global current_encode_id
    current_encode_id = file_id
    entry = scanned_files.get(file_id)
    if not entry:
        return

    src = entry["path"]
    dst = _output_path(src)
    entry["status"] = "encoding"
    entry["progress"] = 0

    cmd = _build_ffmpeg_cmd(src, dst)
    duration = entry["duration"]
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
        while True:
            if cancel_event.is_set():
                proc.kill()
                entry["status"] = "cancelled"
                # Clean up partial output
                try:
                    os.remove(dst)
                except OSError:
                    pass
                return

            ch = proc.stderr.read(1)
            if not ch:
                break
            if ch in ("\r", "\n"):
                if buf.strip():
                    info = _parse_progress(buf, duration)
                    if "progress" in info:
                        entry["progress"] = info["progress"]
                    if "fps" in info:
                        entry["speed_fps"] = round(info["fps"], 1)
                    # Compute ETA
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
            entry["status"] = "error"
            entry["error"] = f"FFmpeg exited with code {proc.returncode}"
            try:
                os.remove(dst)
            except OSError:
                pass
            return

        # Success
        entry["progress"] = 100
        entry["status"] = "done"

        # Handle replace mode
        final_dst = _final_path(src, dst)
        if settings["output_mode"] == "replace" and dst != final_dst:
            # Remove original, rename temp to final
            try:
                os.remove(src)
            except OSError:
                pass
            try:
                os.rename(dst, final_dst)
            except OSError:
                entry["error"] = "Encoded OK but failed to replace original"

        # Calculate space saved
        try:
            new_size = os.path.getsize(final_dst)
            saved = entry["size"] - new_size
            entry["space_saved"] = saved
            stats["total_space_saved"] += max(saved, 0)
        except OSError:
            pass

        stats["files_processed"] += 1

        # Delete original if setting enabled (suffix mode)
        if settings["delete_originals"] and settings["output_mode"] == "suffix":
            try:
                os.remove(src)
            except OSError:
                pass

    except Exception as e:
        entry["status"] = "error"
        entry["error"] = str(e)
        try:
            os.remove(dst)
        except OSError:
            pass


def _worker():
    """Process the encode queue one at a time."""
    global current_encode_id, encode_thread
    while True:
        with encoding_lock:
            if not encode_queue or cancel_event.is_set():
                current_encode_id = None
                encode_thread = None
                cancel_event.clear()
                return
            file_id = encode_queue.pop(0)
            # Update queue positions
            for i, qid in enumerate(encode_queue):
                if qid in scanned_files:
                    scanned_files[qid]["queue_pos"] = i + 1

        _encode_one(file_id)

        if cancel_event.is_set():
            # Mark remaining as pending
            with encoding_lock:
                for qid in encode_queue:
                    if qid in scanned_files:
                        scanned_files[qid]["status"] = "pending"
                        scanned_files[qid]["queue_pos"] = 0
                encode_queue.clear()
                current_encode_id = None
                encode_thread = None
                cancel_event.clear()
            return


def _start_worker():
    """Start the worker thread if not already running."""
    global encode_thread
    if encode_thread is None or not encode_thread.is_alive():
        cancel_event.clear()
        encode_thread = threading.Thread(target=_worker, daemon=True)
        encode_thread.start()


# ---------------------------------------------------------------------------
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
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/scan", methods=["POST"])
def api_scan():
    data = request.json or {}
    folder = data.get("folder", "").strip()
    if not folder:
        return jsonify({"error": "No folder specified"}), 400
    result = scan_directory(folder)
    if "error" in result:
        return jsonify(result), 400
    return jsonify({"ok": True, "count": result["count"], "folder": scan_folder})


@app.route("/api/files")
def api_files():
    files = []
    for fid, entry in scanned_files.items():
        files.append({
            "id": entry["id"],
            "filename": entry["filename"],
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
            "error": entry["error"],
            "queue_pos": entry["queue_pos"],
        })
    return jsonify({
        "files": files,
        "folder": scan_folder,
        "stats": {
            "total_space_saved": stats["total_space_saved"],
            "total_space_saved_fmt": fmt_size(stats["total_space_saved"]),
            "files_processed": stats["files_processed"],
            "files_remaining": len(encode_queue) + (1 if current_encode_id else 0),
            "is_encoding": current_encode_id is not None,
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

    with encoding_lock:
        entry["status"] = "queued"
        entry["queue_pos"] = len(encode_queue) + 1
        encode_queue.append(file_id)

    _start_worker()
    return jsonify({"ok": True})


@app.route("/api/squish-all", methods=["POST"])
def api_squish_all():
    """Queue all pending files."""
    count = 0
    with encoding_lock:
        for fid, entry in scanned_files.items():
            if entry["status"] == "pending":
                entry["status"] = "queued"
                encode_queue.append(fid)
                entry["queue_pos"] = len(encode_queue)
                count += 1

    _start_worker()
    return jsonify({"ok": True, "queued": count})


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    """Cancel current encoding and clear the queue."""
    cancel_event.set()
    return jsonify({"ok": True})


@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    return jsonify({**settings, "hw_encoders": detect_hw_encoders(), "active_encoder": _choose_encoder()[0]})


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
        settings["hw_mode"] = data["hw_mode"] if data["hw_mode"] in ("auto", "qsv", "nvenc", "videotoolbox", "vaapi", "cpu") else "auto"
    if "delete_originals" in data:
        settings["delete_originals"] = bool(data["delete_originals"])
    if "output_mode" in data:
        settings["output_mode"] = data["output_mode"] if data["output_mode"] in ("suffix", "replace") else "suffix"
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print()
    print("  ╔═══════════════════════════════════════╗")
    print("  ║          🗜️  SquishBox v1.0            ║")
    print("  ║  Point at a folder. Click squish.     ║")
    print("  ╚═══════════════════════════════════════╝")
    print()
    print(f"  → Open http://localhost:5555 in your browser")
    hw = detect_hw_encoders()
    available = [k for k, v in hw.items() if v]
    encoder_name, _ = _choose_encoder()
    print(f"  → Hardware encoders: {', '.join(available) if available else 'none (CPU only)'}")
    print(f"  → Active encoder: {encoder_name}")
    print()
    app.run(host="0.0.0.0", port=5555, debug=False)
