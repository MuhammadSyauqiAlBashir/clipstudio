"""FFmpeg/ffprobe helpers (blocking: call them through asyncio.to_thread from the worker)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import numpy as np

from . import config


class MediaError(Exception):
    pass


def run(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        tail = "\n".join(p.stderr.strip().splitlines()[-6:])
        raise MediaError(f"{Path(cmd[0]).name} failed ({p.returncode}): {tail}")
    return p


def ffmpeg(*args: str, timeout: float | None = None) -> subprocess.CompletedProcess:
    return run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-threads", config.FFMPEG_THREADS,
                *args], timeout=timeout)


def probe(path: Path | str) -> dict:
    p = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], timeout=120)
    data = json.loads(p.stdout or "{}")
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    fps = 30.0
    if v and v.get("avg_frame_rate", "0/0") != "0/0":
        n, d = v["avg_frame_rate"].split("/")
        fps = float(n) / float(d) if float(d) else 30.0
    dur = float(data.get("format", {}).get("duration") or (v or {}).get("duration") or 0)
    return {"duration": dur, "width": int((v or {}).get("width") or 0), "height": int((v or {}).get("height") or 0),
            "fps": fps, "has_video": v is not None, "has_audio": a is not None}


def extract_audio(src: Path, out: Path, max_seconds: float | None = None):
    """16 kHz mono MP3 (~48 kbps): small enough for Groq's upload limit per 10-minute chunk."""
    args = ["-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "48k"]
    if max_seconds:
        args += ["-t", f"{max_seconds:.2f}"]
    ffmpeg(*args, str(out), timeout=3 * 3600)


def cut_audio(src: Path, start: float, end: float, out: Path):
    ffmpeg("-ss", f"{start:.2f}", "-t", f"{max(0.5, end - start):.2f}", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
           "-c:a", "libmp3lame", "-b:a", "48k", str(out), timeout=300)


def loudness(audio: Path, step: float = 0.5) -> list[float]:
    """Momentary loudness (LUFS) every `step` seconds, from FFmpeg's EBU R128 scanner (one value per 0.1 s)."""
    p = subprocess.Popen(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-threads", "1", "-i", str(audio),
                          "-af", "ebur128=metadata=1,ametadata=mode=print:key=lavfi.r128.M:file=/dev/stdout",
                          "-f", "null", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    out: list[float] = []
    next_t, t = 0.0, 0.0
    assert p.stdout is not None
    for line in p.stdout:
        if line.startswith("frame:"):
            m = re.search(r"pts_time:([\d.]+)", line)
            t = float(m.group(1)) if m else t
        elif line.startswith("lavfi.r128.M=") and t + 1e-6 >= next_t:
            v = line.split("=", 1)[1].strip()
            try:
                out.append(max(-70.0, float(v)))
            except ValueError:
                out.append(-70.0)
            next_t += step
    p.wait()
    return out


def frames(src: Path, start: float, end: float, fps: float = 4, width: int = 640):
    """Yield (t, BGR frame) samples between start and end."""
    info = probe(src)
    if not info["width"]:
        return
    h = int(round(info["height"] * width / info["width"] / 2) * 2)
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-threads", config.FFMPEG_THREADS,
           "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}", "-i", str(src),
           "-vf", f"fps={fps},scale={width}:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    size = width * h * 3
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as p:
        i = 0
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            yield start + i / fps, np.frombuffer(buf, np.uint8).reshape(h, width, 3)
            i += 1


def thumbnail(video: Path, out: Path, at: float = 1.0):
    ffmpeg("-ss", f"{at:.2f}", "-i", str(video), "-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "4", str(out),
           timeout=120)
