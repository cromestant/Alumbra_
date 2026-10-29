"""Metric distance per scene instance, from capture geometry.

The qualitative labels (close / a few steps away / far) are a per-noun
box-size heuristic and contradicted themselves on duplicate segments
(capture 20261028-155046: one box, "close" as a sandal and "far" as a
slipper). With the app's capture metadata — device pitch and the focal
length in upload pixels — distance comes from geometry instead:

  ground plane (floor-standing objects, the primary path): the ray to
  the box's bottom edge meets the floor. Depression of that ray below
  horizontal is phi = pitch_deg + atan((y2 - H/2) / focal_px), and
  distance = cam_height_m / tan(phi).

  size prior (door / person / car only): distance = real height *
  focal_px / box height in px.

Conventions (as landed): pitch_deg is the angle the camera's optical
axis points DOWN from horizontal — level = 0, straight at the floor =
90, up at the sky negative. The principal point is assumed at the image
center, and the image is assumed upright (no roll).

Distance is advisory language: it changes how far things are said to
be and the order they are spoken, never whether something is a hazard.
Without metadata (old app) or with METRIC_DISTANCE_ENABLED=0 nothing
here runs and answers are exactly as before.
"""
from __future__ import annotations

import io
import math
import os
from typing import Any

from PIL import Image, ImageOps

CAM_HEIGHT_DEFAULT_M = 1.52  # phone as Charles holds it (~5 ft, 2026-10-29)
MIN_DEPRESSION_DEG = 5.0     # below this, tan() explodes: no metric
RANGE_M = (0.3, 12.0)
EDGE_PX = 3                  # a box edge this close to the frame is clipped
AGREE_FRAC = 0.4             # both estimators within 40% -> agreement
PHOTO_LONG_PX = 1280         # tap photos are cut to the clip frames' size

# Typical real heights, canonical categories only. Deliberately short: a
# wrong precise number is worse than a label, so no jars, books, bottles
# or furniture (variance too high).
PRIOR_HEIGHT_M = {"door": 2.03, "person": 1.70, "car": 1.50}


def enabled() -> bool:
    """Kill switch: METRIC_DISTANCE_ENABLED=0 off."""
    return os.environ.get("METRIC_DISTANCE_ENABLED", "").strip().lower() \
        not in ("0", "false", "no", "off")


def cam_height_m() -> float:
    try:
        h = float(os.environ.get("CAM_HEIGHT_M", CAM_HEIGHT_DEFAULT_M))
    except ValueError:
        return CAM_HEIGHT_DEFAULT_M
    return h if math.isfinite(h) and h > 0 else CAM_HEIGHT_DEFAULT_M


def parse_camera(pitch_deg: Any, focal_px: Any) -> dict[str, float] | None:
    """The upload's capture metadata, or None (both fields are needed,
    finite, in range; anything else is an old app or a bad reading)."""
    try:
        pitch, focal = float(pitch_deg), float(focal_px)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(pitch) and math.isfinite(focal)):
        return None
    if not (-90.0 <= pitch <= 90.0) or focal <= 0:
        return None
    return {"pitch_deg": pitch, "focal_px": focal}


def upright_photo(data: bytes) -> tuple[bytes, float]:
    """(upright JPEG, pixel scale applied). A tap photo arrives at full
    sensor size, stored sideways with an EXIF rotate tag (Pixel 9a:
    4000x3000, orientation 6) that nothing downstream applies, so boxes,
    clocks and the floor geometry all read it on its side. Apply the tag
    and cap the long side at PHOTO_LONG_PX (the clip frames' size). An
    upright image already within the cap (a clip frame, an old upload)
    comes back as the same bytes, scale 1. Deterministic, so a follow-up
    reloading the stored original gets the same pixels."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            orient = im.getexif().get(274, 1)
            if orient in (None, 1) and max(im.size) <= PHOTO_LONG_PX:
                return data, 1.0
            up = ImageOps.exif_transpose(im)
            scale = min(1.0, PHOTO_LONG_PX / max(up.size))
            if scale < 1.0:
                up = up.resize((max(1, round(up.size[0] * scale)),
                                max(1, round(up.size[1] * scale))),
                               Image.LANCZOS)
            buf = io.BytesIO()
            up.convert("RGB").save(buf, "JPEG", quality=90)
            return buf.getvalue(), scale
    except Exception:
        return data, 1.0


def scale_camera(camera: dict[str, float] | None,
                 scale: float) -> dict[str, float] | None:
    """focal_px follows the pixels; rotation leaves it unchanged."""
    if camera is None or scale == 1.0:
        return camera
    return dict(camera, focal_px=round(camera["focal_px"] * scale, 1))


def ground_distance(box: Any, height: int, pitch_deg: float,
                    focal_px: float, cam_h: float) -> float | None:
    """Floor-plane distance to the box's base, or None when a gate fails:
    base clipped by the frame bottom, depression < 5 deg, or a result
    outside [0.3, 12] m."""
    y2 = float(box[3])
    if y2 >= height - EDGE_PX:
        return None
    alpha = math.degrees(math.atan((y2 - height / 2) / focal_px))
    phi = pitch_deg + alpha
    if phi < MIN_DEPRESSION_DEG:
        return None
    d = cam_h / math.tan(math.radians(phi))
    return d if RANGE_M[0] <= d <= RANGE_M[1] else None


def prior_distance(head: str, box: Any, height: int,
                   focal_px: float) -> float | None:
    """Size-prior distance for a canonical category, or None (not in the
    table, clipped at the frame's top or bottom, out of range)."""
    real_h = PRIOR_HEIGHT_M.get(head)
    if real_h is None:
        return None
    y1, y2 = float(box[1]), float(box[3])
    if y1 <= EDGE_PX or y2 >= height - EDGE_PX or y2 <= y1:
        return None
    d = real_h * focal_px / (y2 - y1)
    return d if RANGE_M[0] <= d <= RANGE_M[1] else None


def reconcile(ground: float | None, prior: float | None,
              floor_standing: bool) -> tuple[float | None, str | None, bool]:
    """(distance_m, distance_src, disagreed). Within 40% -> the ground
    value ("both"); beyond -> ground for floor-standing instances, else
    no metric (label fallback). One present -> that one."""
    if ground is not None and prior is not None:
        if abs(ground - prior) <= AGREE_FRAC * ground:
            return ground, "both", False
        if floor_standing:
            return ground, "ground", True
        return None, None, True
    if ground is not None:
        return ground, "ground", False
    if prior is not None:
        return prior, "prior", False
    return None, None, False


def spoken(d: float) -> str:
    """Coarse words for a distance — never more precise than the method:
    < 0.4 m arm's reach, < 0.7 half a meter, to 3 m the nearest half
    meter, beyond the nearest meter."""
    if d < 0.4:
        return "within arm's reach"
    if d < 0.7:
        return "about half a meter away"
    if d <= 3.0:
        r = math.floor(d * 2 + 0.5) / 2
    else:
        r = float(math.floor(d + 0.5))
    whole, half = int(r), (r - int(r)) >= 0.5
    if whole == 0:
        return "about half a meter away"
    if half:
        return f"about {whole} and a half meters away"
    return f"about {whole} meter{'s' if whole != 1 else ''} away"
