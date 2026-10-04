"""Face framing for the 9:16 crop: OpenCV's YuNet face detector (MIT, ~230 KB, CPU-friendly; lighter than
MediaPipe) on 4 fps samples → layout (track / split / general / vertical) → smoothed crop path.
The plan is saved per clip so the final 1080x1920 render reuses it."""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from . import config, media

log = logging.getLogger("cs.faces")

MODEL = config.ASSETS / "face_detection_yunet_2023mar.onnx"
SAMPLE_FPS = 4
SAMPLE_W = 640
_detector = None


def detector(w: int, h: int):
    global _detector
    import cv2  # imported lazily: only the worker needs OpenCV

    if _detector is None:
        _detector = cv2.FaceDetectorYN.create(str(MODEL), "", (w, h), 0.7, 0.3, 20)
    _detector.setInputSize((w, h))
    return _detector


def detect(src: Path, start: float, end: float) -> list[tuple[float, list[tuple[float, float, float, float, float]]]]:
    """Per sample: (t, [(cx, cy, w, h, score)]) in 0..1 frame coordinates, largest face first."""
    out = []
    for t, frame in media.frames(src, start, end, SAMPLE_FPS, SAMPLE_W):
        h, w = frame.shape[:2]
        _, faces = detector(w, h).detect(frame)
        found = []
        if faces is not None:
            for f in faces:
                x, y, fw, fh, score = float(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[-1])
                if fw * fh < (w * h) * 0.002:  # ignore tiny faces (crowds, posters)
                    continue
                found.append(((x + fw / 2) / w, (y + fh / 2) / h, fw / w, fh / h, score))
        found.sort(key=lambda f: f[2] * f[3], reverse=True)
        out.append((t - start, found))
    return out


def choose_layout(samples, src_w: int, src_h: int) -> str:
    if src_h >= src_w:
        return "vertical"
    n = len(samples) or 1
    with_face = sum(1 for _, f in samples if f) / n
    if with_face < 0.4:
        return "general"
    crop_frac = (src_h * 9 / 16) / src_w
    two = 0
    for _, f in samples:
        if len(f) >= 2 and f[1][2] * f[1][3] >= 0.45 * f[0][2] * f[0][3] and abs(f[0][0] - f[1][0]) > crop_frac * 0.9:
            two += 1
    return "split" if two / n >= 0.5 else "track"


def track_path(samples, src_w: int, src_h: int) -> list[tuple[float, int]]:
    """Crop x (pixels) over time: follow the main face; smooth small moves, jump on cuts/speaker changes."""
    cw = src_h * 9 / 16
    target_prev = None
    raw: list[tuple[float, float | None]] = []
    for t, faces in samples:
        if not faces:
            raw.append((t, None))
            continue
        if target_prev is None:
            best = faces[0]
        else:  # prefer the face near the current one unless another is much bigger
            best = max(faces, key=lambda f: f[2] * f[3] * f[4] * (1.6 if abs(f[0] - target_prev) < 0.1 else 1.0))
        target_prev = best[0]
        raw.append((t, best[0]))
    # fill gaps by holding the last value (or the first known)
    first = next((v for _, v in raw if v is not None), 0.5)
    filled, last = [], first
    for t, v in raw:
        last = v if v is not None else last
        filled.append((t, last))
    # smoothing with a dead zone; big persistent changes = cut → jump
    path: list[tuple[float, float]] = []
    cur = filled[0][1] if filled else 0.5
    pending = 0
    for t, v in filled:
        if abs(v - cur) > 0.22:
            pending += 1
            if pending >= 2:
                cur = v
                pending = 0
        else:
            pending = 0
            if abs(v - cur) > 0.03:
                cur += (v - cur) * 0.25
        path.append((t, cur))
    out = []
    for t, cx in path:
        x = int(round(min(max(cx * src_w - cw / 2, 0), src_w - cw)))
        out.append((round(t, 3), x - x % 2))
    return out


def split_boxes(samples, src_w: int, src_h: int) -> list[list[int]]:
    """Two static crops (left speaker on top, right speaker below), each 9:8."""
    lefts, rights, ys = [], [], []
    for _, f in samples:
        if len(f) >= 2:
            a, b = sorted(f[:2], key=lambda x: x[0])
            lefts.append(a[0])
            rights.append(b[0])
            ys += [a[1], b[1]]
    ch = int(src_h * 0.72) // 2 * 2
    cw = int(ch * 9 / 8) // 2 * 2
    cy = float(np.median(ys)) if ys else 0.45
    y = int(min(max(cy * src_h - ch * 0.45, 0), src_h - ch)) // 2 * 2
    boxes = []
    for cx in (float(np.median(lefts)) if lefts else 0.3, float(np.median(rights)) if rights else 0.7):
        x = int(min(max(cx * src_w - cw / 2, 0), src_w - cw)) // 2 * 2
        boxes.append([x, y, cw, ch])
    return boxes


def plan(src: Path, start: float, end: float) -> dict:
    info = media.probe(src)
    w, h = info["width"], info["height"]
    if not info["has_video"] or not w:
        return {"layout": "audio", "src_w": 0, "src_h": 0, "fps": 30}
    samples = detect(src, start, end) if w > h else []
    layout = choose_layout(samples, w, h)
    p = {"layout": layout, "src_w": w, "src_h": h, "fps": info["fps"]}
    if layout == "track":
        p["path"] = track_path(samples, w, h)
        p["crop_w"] = int(h * 9 / 16) // 2 * 2
    elif layout == "split":
        p["boxes"] = split_boxes(samples, w, h)
    return p
