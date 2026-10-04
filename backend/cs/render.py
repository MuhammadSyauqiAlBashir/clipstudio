"""9:16 rendering with FFmpeg: crop/layout from the face plan + burned .ass captions.
Preview: 540x960 ultrafast (for review). Final: 1080x1920 (only after the owner approves)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import config, media

QUALITY = {
    "preview": {"w": 540, "h": 960, "preset": "ultrafast", "crf": "30", "abr": "96k", "fps": 30},
    "final": {"w": 1080, "h": 1920, "preset": "veryfast", "crf": "20", "abr": "160k", "fps": 60},
}


def sendcmd_lines(path: list[tuple[float, int]], step: float = 1 / 15) -> str:
    """Crop x commands, linearly interpolated between the 4 fps samples so the pan is smooth."""
    if not path:
        return ""
    lines = []
    t_end = path[-1][0]
    i = 0
    t = 0.0
    while t <= t_end + 1e-6:
        while i + 1 < len(path) and path[i + 1][0] <= t:
            i += 1
        t0, x0 = path[i]
        if i + 1 < len(path):
            t1, x1 = path[i + 1]
            # jumps (cuts) stay jumps; small moves glide
            x = x0 if abs(x1 - x0) > 120 else x0 + (x1 - x0) * max(0.0, min(1.0, (t - t0) / max(1e-6, t1 - t0)))
        else:
            x = x0
        lines.append(f"{t:.3f} crop x {int(x) - int(x) % 2};")
        t += step
    return "\n".join(lines) + "\n"


def video_filter(plan: dict, q: dict, work: Path) -> str:
    W, H = q["w"], q["h"]
    lay = plan["layout"]
    if lay == "track":
        cmd = work / "crop.cmd"
        cmd.write_text(sendcmd_lines(plan["path"]))
        x0 = plan["path"][0][1] if plan["path"] else 0
        v = (f"[0:v]sendcmd=f={cmd.name},crop=w={plan['crop_w']}:h={plan['src_h']}:x={x0}:y=0,"
             f"scale={W}:{H}:flags=bicubic,setsar=1[v0]")
    elif lay == "split":
        (ax, ay, aw, ah), (bx, by, bw, bh) = plan["boxes"]
        v = (f"[0:v]split[a][b];[a]crop={aw}:{ah}:{ax}:{ay},scale={W}:{H // 2},setsar=1[top];"
             f"[b]crop={bw}:{bh}:{bx}:{by},scale={W}:{H // 2},setsar=1[bot];[top][bot]vstack[v0]")
    elif lay == "vertical":
        v = f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[v0]"
    else:  # general: whole frame on a blurred background
        v = (f"[0:v]split[a][b];[a]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,crop={W // 4}:{H // 4},"
             f"boxblur=10:2,eq=brightness=-0.08,scale={W}:{H}[bg];[b]scale={W}:-2[fg];"
             f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v0]")
    fps = min(q["fps"], max(24, round(plan.get("fps") or 30)))
    return f"{v};[v0]fps={fps},subtitles=f=clip.ass:fontsdir={config.ASSETS}[v]"


def render(src: Path, start: float, end: float, plan: dict, ass_text: str, out: Path, quality: str, work: Path):
    """Blocking. `work` must be the clip's own dir (holds clip.ass / crop.cmd; FFmpeg runs there)."""
    q = QUALITY[quality]
    work.mkdir(parents=True, exist_ok=True)
    (work / "clip.ass").write_text(ass_text, encoding="utf-8")
    if plan["layout"] == "audio":
        raise media.MediaError("source has no video")
    graph = video_filter(plan, q, work)
    tmp = out.with_suffix(".part.mp4")
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-threads", config.FFMPEG_THREADS,
           "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(src),
           "-filter_complex", graph, "-map", "[v]", "-map", "0:a:0?",
           "-c:v", "libx264", "-preset", q["preset"], "-crf", q["crf"], "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", q["abr"], "-ar", "48000", "-movflags", "+faststart", str(tmp)]
    p = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=3600)
    if p.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise media.MediaError("render failed: " + "\n".join(p.stderr.strip().splitlines()[-6:]))
    tmp.replace(out)
