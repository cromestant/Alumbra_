"""Dev-only capture store: every ask's inputs and outcome, on disk.

Live failures could not be audited after the fact — media was never
persisted. For each /ask and /followup this keeps the uploaded photo,
clip and audio, the request, and then either the response (with the
full, uncollapsed scene) or the error, in a dated folder:

    backend/captures/2026-10-28/20261028-105512-123456-ask/

Only the newest CAPTURE_KEEP asks are kept; older ones are deleted as
new ones arrive — except a folder an unexpired follow-up cache entry
still holds (below), which the count cap never evicts. The folder is gitignored: these are personal photos.

On by default through the hackathon week (CAPTURE_DEFAULT_UNTIL), off
after it; always off with ALUMBRA_ENV=production. CAPTURE_ENABLED=1/0
overrides the date default (never production). Writing a capture never
fails or slows an ask beyond the disk write: errors are logged and
dropped.

The follow-up cache persists here too (one store, not a second pile of
photos): each cached ask writes cache-<ask_id>.json into its own capture
folder — everything a follow-up needs except the media bytes, plus
media_dir, the folder holding the photo/clip. A fresh ask's media_dir is
its own folder; a follow-up's is its root ask's, so each photo and clip
exists on disk exactly once. When no unexpired cache entry holds a
folder any more, its media is deleted: an expired conversation leaves
no photos behind. Request/response/trace JSON stays for the capture
window. Production follow-up persistence (the store is off there, so
the cache is memory-only) is a separate future decision.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import metric

CAPTURE_DIR = Path(os.environ.get(
    "CAPTURE_DIR",
    Path(__file__).resolve().parent.parent / "captures"))
CAPTURE_KEEP = 50
CAPTURE_DEFAULT_UNTIL = date(2026, 11, 1)  # last day of hackathon week

# An hour: a conversation, not an archive — long enough to pause and
# come back, counted from each ask's creation (a follow-up mints a new
# ask id with a fresh TTL). ASK_TTL_S overrides without a code change.
ASK_TTL_DEFAULT_S = 3600

MEDIA_STEMS = ("photo", "clip", "audio")
# Ask ids are uuid hex; anything else never reaches a glob pattern.
_ASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_lock = threading.Lock()


def enabled() -> bool:
    if os.environ.get("ALUMBRA_ENV", "").lower() == "production":
        return False
    flag = os.environ.get("CAPTURE_ENABLED", "").strip().lower()
    if flag in ("1", "true", "on", "yes"):
        return True
    if flag in ("0", "false", "off", "no"):
        return False
    return date.today() <= CAPTURE_DEFAULT_UNTIL


def ask_ttl_s() -> float:
    try:
        return float(os.environ.get("ASK_TTL_S", ASK_TTL_DEFAULT_S))
    except ValueError:
        return float(ASK_TTL_DEFAULT_S)


def _ext(data: bytes, default: str) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[4:8] == b"ftyp":
        return ".mp4"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return ".wav"
    if data[:4] == b"\x1aE\xdf\xa3":
        return ".webm"
    if data[:4] == b"OggS":
        return ".ogg"
    return default


def _write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False,
                               default=str))


def _prune(fresh: Path) -> None:
    """Newest CAPTURE_KEEP, oldest first out — never a folder a live
    cache entry holds (same rule as sweep_asks), nor the one just made."""
    folders = sorted(
        (d for day in CAPTURE_DIR.iterdir() if day.is_dir()
         for d in day.iterdir() if d.is_dir()),
        key=lambda d: d.name,
    )
    held, _ = _holders(ask_ttl_s(), time.time())
    keep = {d.resolve() for d, live in held.items() if live}
    keep.add(fresh.resolve())
    excess = len(folders) - CAPTURE_KEEP
    for d in folders:
        if excess <= 0:
            break
        if d.resolve() not in keep:
            shutil.rmtree(d, ignore_errors=True)
            excess -= 1
    for day in CAPTURE_DIR.iterdir():
        if day.is_dir() and not any(day.iterdir()):
            day.rmdir()


def begin(
    endpoint: str,
    question: str,
    image: bytes | None = None,
    media: bytes | None = None,
    audio: bytes | None = None,
    parent_ask_id: str | None = None,
    camera: dict[str, float] | None = None,
) -> Path | None:
    """Store the request; returns the capture folder (None when off or
    the write failed)."""
    if not enabled():
        return None
    try:
        now = datetime.now()
        folder = (CAPTURE_DIR / now.strftime("%Y-%m-%d")
                  / f"{now.strftime('%Y%m%d-%H%M%S-%f')}-{endpoint}")
        with _lock:
            folder.mkdir(parents=True, exist_ok=False)
            for name, data, default in (("photo", image, ".jpg"),
                                        ("clip", media, ".bin"),
                                        ("audio", audio, ".bin")):
                if data:
                    (folder / (name + _ext(data, default))).write_bytes(data)
            req = {
                "ts": now.isoformat(),
                "endpoint": endpoint,
                "question_text": question,
                "parent_ask_id": parent_ask_id,
                "bytes": {"photo": len(image or b""),
                          "clip": len(media or b""),
                          "audio": len(audio or b"")},
            }
            if camera is not None:
                req["camera"] = camera
            _write_json(folder / "request.json", req)
            _prune(folder)
        return folder
    except Exception as e:  # never fail the ask over a capture
        print(json.dumps({"capture_error": repr(e)}), flush=True)
        return None


def finish(folder: Path | None, result: dict[str, Any],
           trace: dict[str, Any]) -> None:
    """The response as shipped, plus the full scene behind it."""
    if folder is None:
        return
    try:
        _write_json(folder / "response.json", result)
        _write_json(folder / "trace.json", trace)
    except Exception as e:
        print(json.dumps({"capture_error": repr(e)}), flush=True)


def fail(folder: Path | None, status: int, detail: Any) -> None:
    """The failure, so it can be audited after the fact."""
    if folder is None:
        return
    try:
        _write_json(folder / "error.json", {"status": status,
                                            "detail": detail})
    except Exception as e:
        print(json.dumps({"capture_error": repr(e)}), flush=True)


# ----------------------------------------------------------------------------
# Follow-up cache on disk
# ----------------------------------------------------------------------------

def _inside(path: Path) -> bool:
    try:
        path.resolve().relative_to(CAPTURE_DIR.resolve())
        return True
    except ValueError:
        return False


def persist_ask(folder: Path | None, ask_id: str, entry: dict[str, Any],
                media_dir: Path | None) -> bool:
    """Write a cache entry (no media bytes) into its capture folder.
    False — memory-only — when there is nowhere to write or the write
    failed (logged; never fails the ask)."""
    if folder is None or media_dir is None or not _ASK_ID_RE.match(ask_id):
        return False
    try:
        rec = dict(entry, ask_id=ask_id, media_dir=str(
            media_dir.resolve().relative_to(CAPTURE_DIR.resolve())))
        tmp = folder / f".cache-{ask_id}.tmp"
        _write_json(tmp, rec)
        os.replace(tmp, folder / f"cache-{ask_id}.json")
        return True
    except Exception as e:
        print(json.dumps({"capture_error": repr(e)}), flush=True)
        return False


def load_ask(ask_id: str) -> tuple[dict[str, Any], Path] | None:
    """(cache entry with image/media bytes, media_dir), or None when the
    ask is unknown or its files are missing/corrupt — an expiry."""
    if not _ASK_ID_RE.match(ask_id or "") or not CAPTURE_DIR.exists():
        return None
    try:
        hit = next(CAPTURE_DIR.glob(f"*/*/cache-{ask_id}.json"), None)
        if hit is None:
            return None
        rec = json.loads(hit.read_text())
        media_dir = CAPTURE_DIR / rec.pop("media_dir")
        rec.pop("ask_id", None)
        if not _inside(media_dir):
            return None
        photo = next(media_dir.glob("photo.*"), None)
        if photo is None:
            return None
        clip = next(media_dir.glob("clip.*"), None)
        rec["image"] = metric.upright_photo(photo.read_bytes())[0]
        rec["media"] = clip.read_bytes() if clip else None
        rec["created"] = float(rec["created"])
        return rec, media_dir
    except Exception as e:
        print(json.dumps({"capture_error": repr(e)}), flush=True)
        return None


def _holders(ttl_s: float, now: float
             ) -> tuple[dict[Path, bool], list[Path]]:
    """({folder: held by an unexpired entry}, expired cache files). An
    entry holds its own folder and its media_dir; corrupt entries count
    as expired."""
    held: dict[Path, bool] = {}
    expired: list[Path] = []
    for f in CAPTURE_DIR.glob("*/*/cache-*.json"):
        dirs, live = {f.parent}, False
        try:
            rec = json.loads(f.read_text())
            live = now - float(rec["created"]) <= ttl_s
            md = CAPTURE_DIR / str(rec["media_dir"])
            if _inside(md):
                dirs.add(md)
        except Exception:
            pass
        for d in dirs:
            held[d] = held.get(d, False) or live
        if not live:
            expired.append(f)
    return held, expired


def sweep_asks(ttl_s: float, now: float) -> None:
    """Expire cache entries on disk: delete expired cache files, and the
    media of every folder no unexpired entry holds."""
    if not CAPTURE_DIR.exists():
        return
    with _lock:
        held, expired = _holders(ttl_s, now)
        for d, live in held.items():
            if live or not d.exists():
                continue
            for m in d.iterdir():
                if m.name.split(".")[0] in MEDIA_STEMS:
                    m.unlink(missing_ok=True)
        for f in expired:
            f.unlink(missing_ok=True)
