"""Dev-only capture store: every ask's inputs and outcome, on disk.

Live failures could not be audited after the fact — media was never
persisted. For each /ask and /followup this keeps the uploaded photo,
clip and audio, the request, and then either the response (with the
full, uncollapsed scene) or the error, in a dated folder:

    backend/captures/2026-10-28/20261028-105512-123456-ask/

Only the newest CAPTURE_KEEP asks are kept; older ones are deleted as
new ones arrive. The folder is gitignored: these are personal photos.

On by default through the hackathon week (CAPTURE_DEFAULT_UNTIL), off
after it; always off with ALUMBRA_ENV=production. CAPTURE_ENABLED=1/0
overrides the date default (never production). Writing a capture never
fails or slows an ask beyond the disk write: errors are logged and
dropped.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

CAPTURE_DIR = Path(os.environ.get(
    "CAPTURE_DIR",
    Path(__file__).resolve().parent.parent / "captures"))
CAPTURE_KEEP = 50
CAPTURE_DEFAULT_UNTIL = date(2026, 11, 1)  # last day of hackathon week

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


def _prune() -> None:
    folders = sorted(
        (d for day in CAPTURE_DIR.iterdir() if day.is_dir()
         for d in day.iterdir() if d.is_dir()),
        key=lambda d: d.name,
    )
    for d in folders[:-CAPTURE_KEEP] if len(folders) > CAPTURE_KEEP else []:
        shutil.rmtree(d, ignore_errors=True)
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
            _write_json(folder / "request.json", {
                "ts": now.isoformat(),
                "endpoint": endpoint,
                "question_text": question,
                "parent_ask_id": parent_ask_id,
                "bytes": {"photo": len(image or b""),
                          "clip": len(media or b""),
                          "audio": len(audio or b"")},
            })
            _prune()
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
