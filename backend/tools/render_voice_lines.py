"""Render the app's fixed spoken lines into bundled MP3 clips.

Reads the wording from Spoken.kt (the one place it lives), synthesizes
each line through the backend's TTS helper in the configured profile
(TTS_VOICE_PROFILE), and writes android/app/src/main/assets/voice/:
<line_id>.mp3 plus manifest.json ({profile, lines: {id: text}}). The
app plays a clip when the text it is asked to speak matches a manifest
line exactly; anything else (or a missing/corrupt clip) is phone TTS.

Re-run whenever a line's wording or the voice profile changes; the MP3s
are generated files, never hand-edited.

    ../.venv/bin/python tools/render_voice_lines.py [--dry-run]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "backend"))
from app import pipeline  # noqa: E402

SPOKEN = ROOT / "android/app/src/main/java/ai/alumbra/app/Spoken.kt"
OUT = ROOT / "android/app/src/main/assets/voice"

# Spoken by the backend's answer audio (answer-attached), so not bundled:
# one speaker per line. ERROR_PREFIX is never spoken on its own.
BACKEND_LINES = {"ESCALATE_LINE", "DIDNT_CATCH_QUESTION"}
NOT_SPOKEN = {"ERROR_PREFIX"}
# Spoken.errorDetail(m) for every message an ask error can carry.
ERROR_DETAIL_OF = ["SERVER_UNREACHABLE", "SERVER_ERROR", "BAD_RESPONSE",
                   "CANCELLED"]

_CONST = re.compile(r'^\s*const val (\w+)\s*=\s*$|^\s*const val (\w+)\s*=\s*(.+)$')
_LIT = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _unescape(s: str) -> str:
    return re.sub(r'\\(u[0-9a-fA-F]{4}|.)', lambda m: (
        chr(int(m.group(1)[1:], 16)) if m.group(1).startswith("u")
        else {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1))), s)


def spoken_constants(src: str) -> dict[str, str]:
    """const val NAME = "..." [+ "..." ...] — literal-only constants."""
    out: dict[str, str] = {}
    lines = src.splitlines()
    i = 0
    while i < len(lines):
        m = _CONST.match(lines[i])
        if not m:
            i += 1
            continue
        name = m.group(1) or m.group(2)
        expr = (m.group(3) or "").strip()
        while not expr or expr.endswith("+"):
            i += 1
            expr += " " + lines[i].strip()
        if re.fullmatch(r'(\s*"(?:[^"\\]|\\.)*"\s*\+?)+', expr):
            out[name] = "".join(_unescape(x) for x in _LIT.findall(expr))
        i += 1
    return out


def manifest() -> dict[str, str]:
    consts = spoken_constants(SPOKEN.read_text())
    lines = {name.lower(): text for name, text in consts.items()
             if name not in BACKEND_LINES | NOT_SPOKEN}
    for m in ERROR_DETAIL_OF:
        lines[f"error_{m.lower()}"] = consts["ERROR_PREFIX"] + consts[m]
    return lines


def main() -> int:
    lines = manifest()
    profile = pipeline.tts_profile()
    print(f"{len(lines)} lines, profile {profile}")
    if "--dry-run" in sys.argv:
        for k, v in lines.items():
            print(f"  {k}: {v}")
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    failed = []
    for line_id, text in lines.items():
        meta: dict = {}
        data = pipeline.synthesize_speech(text, cache=True, profile=profile,
                                          meta=meta)
        if data is None:
            failed.append(line_id)
            print(f"  FAIL {line_id}: {meta.get('error')}")
            continue
        (OUT / f"{line_id}.mp3").write_bytes(data)
        print(f"  {line_id}: {len(data)} B, cache_hit={meta['cache_hit']}")
    for stale in OUT.glob("*.mp3"):
        if stale.stem not in lines:
            stale.unlink()
    (OUT / "manifest.json").write_text(json.dumps(
        {"profile": profile,
         "lines": {k: v for k, v in lines.items() if k not in failed}},
        indent=1, ensure_ascii=False) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
