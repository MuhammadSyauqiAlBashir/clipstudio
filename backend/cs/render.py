"""9:16 rendering with FFmpeg: crop/layout from the face plan + burned .ass captions.
Preview: 540x960 ultrafast (for review). Final: 1080x1920 (only after the owner approves)."""

from __future__ import annotations

import math
import subprocess
from pathlib import Path

from . import config, media

QUALITY = {
    "preview": {"w": 540, "h": 960, "preset": "ultrafast", "crf": "30", "abr": "96k", "fps": 30},
    "final": {"w": 1080, "h": 1920, "preset": "veryfast", "crf": "20", "abr": "160k", "fps": 60},
}


def calm_path(path: list[tuple[float, int]], crop_w: int) -> list[list[tuple[float, float]]]:
    """The 4 fps face path → a calm camera, split into shots at cuts. Inside a shot the camera holds still while the
    face moves a little (dead zone), and moves are spread over ~1 s with a Gaussian, so a pan eases in and out
    instead of changing speed every 0.25 s or wobbling back and forth."""
    if not path:
        return []
    dead, jump = 0.10 * crop_w, 0.30 * crop_w
    shots: list[list[tuple[float, float]]] = [[(path[0][0], float(path[0][1]))]]
    for (_, xa), (t, xb) in zip(path, path[1:]):
        if abs(xb - xa) > jump:  # a cut or speaker change: new shot, the camera jumps
            shots.append([])
        shots[-1].append((t, float(xb)))
    out = []
    for shot in shots:
        ts = [t for t, _ in shot]
        held, cur = [], shot[0][1]
        for _, x in shot:
            if abs(x - cur) > dead:
                cur = x
            held.append(cur)
        dt = (ts[-1] - ts[0]) / (len(ts) - 1) if len(ts) > 1 else 0.25
        sigma = max(0.5, 0.35 / max(dt, 1e-3))  # ≈0.35 s
        r = int(3 * sigma)
        k = [math.exp(-(i * i) / (2 * sigma * sigma)) for i in range(-r, r + 1)]
        n = len(held)
        smooth = [sum(k[j + r] * held[min(max(i + j, 0), n - 1)] for j in range(-r, r + 1)) / sum(k) for i in range(n)]
        out.append(list(zip(ts, smooth)))
    return out


def sendcmd_lines(path: list[tuple[float, int]], crop_w: int = 608, fps: float = 30) -> str:
    """Crop x for every source frame (exactly one update per frame, so pans glide instead of stepping): the calm path,
    interpolated with Catmull-Rom curves (no speed jumps between the 4 fps samples)."""
    shots = calm_path(path, crop_w)
    if not shots:
        return ""
    lines = []
    t_end = path[-1][0]
    fps = min(max(float(fps or 30), 1.0), 120.0)
    si = 0
    for k in range(int(t_end * fps) + 1):
        t = k / fps
        while si + 1 < len(shots) and shots[si + 1][0][0] <= t:
            si += 1
        pts = shots[si]
        i = 0
        while i + 1 < len(pts) and pts[i + 1][0] <= t:
            i += 1
        if i + 1 >= len(pts) or t < pts[0][0]:
            x = pts[i][1]
        else:
            p0, p1, p2 = pts[max(i - 1, 0)][1], pts[i][1], pts[i + 1][1]
            p3 = pts[min(i + 2, len(pts) - 1)][1]
            u = (t - pts[i][0]) / max(1e-6, pts[i + 1][0] - pts[i][0])
            x = 0.5 * (2 * p1 + (p2 - p0) * u + (2 * p0 - 5 * p1 + 4 * p2 - p3) * u * u
                       + (3 * p1 - p0 - 3 * p2 + p3) * u ** 3)
            x = min(max(x, min(p1, p2)), max(p1, p2))  # never overshoot past the samples
        # sent half a frame early so it lands on frame k; 1-px steps (crop exact=1) keep slow glides smooth
        lines.append(f"{max(0.0, (k - 0.5) / fps):.4f} crop x {int(round(x))};")
    return "\n".join(lines) + "\n"


def video_filter(plan: dict, q: dict, work: Path) -> str:
    W, H = q["w"], q["h"]
    lay = plan["layout"]
    if lay == "track":
        cmd = work / "crop.cmd"
        cmd.write_text(sendcmd_lines(plan["path"], plan["crop_w"], plan.get("fps") or 30))
        x0 = plan["path"][0][1] if plan["path"] else 0
        v = (f"[0:v]sendcmd=f={cmd.name},crop=w={plan['crop_w']}:h={plan['src_h']}:x={x0}:y=0:exact=1,"
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
