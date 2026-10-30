"""Escalation cases: an ESCALATE ask, a volunteer's reply, its delivery.

Until now ESCALATE was only a behavior value: the app spoke the handoff
line and nothing existed on the other side. Here an escalated ask
becomes a case the volunteer console (/volunteer) lists and answers,
and the app polls until the reply arrives or the window closes.

    pending --reply--> answered
       |  \\--app stops waiting--> cancelled
       \\--window passes--> expired

Demo-grade, and deliberately so: cases live in memory (a restart loses
pending ones), and access is one shared VOLUNTEER_TOKEN from .env. The
token is a demo boundary, not a security model. The app's own poll and
cancel need no token: the case id (uuid hex, only ever sent to the app
that asked) is what it holds, and those answers carry no photo.

Off unless both ESCALATION_ENABLED is not 0 and VOLUNTEER_TOKEN is set —
without a token nobody can answer, so no case is made and the user is
not kept waiting. Off, every response is exactly as before and every
escalation endpoint 404s.

Privacy shape: a case carries only its own ask's photo (a reference,
served from the ask's store entry — never copied), question, scene
summary and draft. Nothing else from the user's history reaches the
console.

Lifecycle is written to the ask's capture folder as escalation.json
(store on) and logged as one line per transition, keyed by ask_id.
"""
from __future__ import annotations

import hmac
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WINDOW_DEFAULT_S = 120
POLL_S = 3
ANSWERED_KEEP_S = 1800   # answered/expired cases stay listed this long
NAME_MAX, REPLY_MAX = 60, 1000

_lock = threading.Lock()
_CASES: dict[str, dict[str, Any]] = {}


def _secret(name: str) -> str:
    from .pipeline import _load_secret  # env first, then .env
    return _load_secret(name)


def token() -> str:
    return _secret("VOLUNTEER_TOKEN").strip()


def enabled() -> bool:
    """Kill switch (ESCALATION_ENABLED=0) and the token both gate it."""
    flag = os.environ.get("ESCALATION_ENABLED", "").strip().lower()
    return flag not in ("0", "false", "no", "off") and bool(token())


def token_ok(given: str | None) -> bool:
    want = token()
    return bool(want) and hmac.compare_digest(
        (given or "").encode(), want.encode())


def window_s() -> int:
    try:
        w = int(os.environ.get("ESCALATION_WINDOW_S", WINDOW_DEFAULT_S))
    except ValueError:
        return WINDOW_DEFAULT_S
    return w if w > 0 else WINDOW_DEFAULT_S


def _iso(t: float | None) -> str | None:
    return (datetime.fromtimestamp(t, timezone.utc).isoformat()
            if t is not None else None)


def _log(case: dict[str, Any]) -> None:
    print(json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "ask_id": case["ask_id"],
        "escalation": {"id": case["id"], "outcome": case["status"],
                       "wait_ms": case["wait_ms"]},
    }), flush=True)


def _record(case: dict[str, Any]) -> None:
    folder = case.get("_folder")
    if folder is None:
        return
    try:
        Path(folder, "escalation.json").write_text(json.dumps({
            "id": case["id"], "ask_id": case["ask_id"],
            "status": case["status"],
            "created": _iso(case["created"]),
            "resolved": _iso(case["resolved"]),
            "volunteer": case["volunteer"], "reply": case["reply"],
            "wait_ms": case["wait_ms"],
        }, indent=1, ensure_ascii=False))
    except Exception as e:  # never fail an ask or a reply over the file
        print(json.dumps({"capture_error": repr(e)}), flush=True)


def _resolve(case: dict[str, Any], status: str, now: float) -> None:
    case["status"] = status
    case["resolved"] = now
    case["wait_ms"] = int((now - case["created"]) * 1000)
    _record(case)
    _log(case)


def _sweep_locked(now: float) -> None:
    for cid, c in list(_CASES.items()):
        end = c["created"] + c["window_s"]
        if c["status"] == "pending" and now > end:
            _resolve(c, "expired", end)  # wait_ms is the window, exactly
        if (c["status"] != "pending"
                and now - (c["resolved"] or now) > ANSWERED_KEEP_S):
            del _CASES[cid]


def scene_summary(scene: list[dict[str, Any]]) -> list[str]:
    """One line per name with its clock positions, in scene order
    ("trees at 9, 10, 11 o'clock"): what the AI saw, readable at a
    glance."""
    clocks: dict[str, list[Any]] = {}
    for e in scene:
        name = str(e.get("name", "")).strip()
        if not name:
            continue
        at = clocks.setdefault(name, [])
        c = e.get("clock")
        if c and c not in at:
            at.append(c)
    out = []
    for name, at in clocks.items():
        at.sort(key=lambda c: (c - 9) % 12)  # 9 o'clock first: left to right
        out.append(f"{name} at {', '.join(map(str, at))} o'clock"
                   if at else name)
    return out


def create(ask_id: str, question: str | None, scene: list[dict[str, Any]],
           draft: str | None) -> dict[str, Any] | None:
    """A pending case for an ESCALATE ask, or None when off. Returns the
    response's escalation field: what the app needs to wait."""
    if not enabled():
        return None
    now = time.time()
    case = {
        "id": uuid.uuid4().hex, "ask_id": ask_id, "created": now,
        "question": question or "", "scene": scene_summary(scene),
        "draft": draft or None, "status": "pending",
        "window_s": window_s(), "resolved": None, "volunteer": None,
        "reply": None, "wait_ms": None, "_folder": None,
    }
    with _lock:
        _sweep_locked(now)
        _CASES[case["id"]] = case
    # Opening is logged on the ask's own line (pipeline._compose).
    return {"id": case["id"], "poll_s": POLL_S,
            "window_s": case["window_s"]}


def attach_folder(case_id: str, folder: Path | None) -> None:
    """The ask's capture folder, once it exists (store on)."""
    if folder is None:
        return
    with _lock:
        case = _CASES.get(case_id)
        if case is None:
            return
        case["_folder"] = folder
        _record(case)


def _console(c: dict[str, Any], now: float) -> dict[str, Any]:
    end = c["resolved"] or now
    return {
        "id": c["id"], "status": c["status"],
        "created": _iso(c["created"]),
        "waiting_s": int(end - c["created"]),
        "window_s": c["window_s"],
        "question": c["question"], "scene": c["scene"],
        "draft": c["draft"], "volunteer": c["volunteer"],
        "reply": c["reply"], "wait_ms": c["wait_ms"],
    }


def list_cases() -> list[dict[str, Any]]:
    """Console view: pending first (oldest first), then resolved
    (newest first)."""
    now = time.time()
    with _lock:
        _sweep_locked(now)
        cases = list(_CASES.values())
        pending = sorted((c for c in cases if c["status"] == "pending"),
                         key=lambda c: c["created"])
        done = sorted((c for c in cases if c["status"] != "pending"),
                      key=lambda c: c["resolved"] or 0, reverse=True)
        return [_console(c, now) for c in pending + done]


def status(case_id: str) -> dict[str, Any] | None:
    """App poll: status, and when answered the volunteer and reply."""
    now = time.time()
    with _lock:
        _sweep_locked(now)
        c = _CASES.get(case_id)
        if c is None:
            return None
        out = {"id": c["id"], "status": c["status"],
               "window_s": c["window_s"],
               "remaining_s": max(0, int(c["created"] + c["window_s"] - now))
               if c["status"] == "pending" else 0}
        if c["status"] == "answered":
            out.update(volunteer=c["volunteer"], reply=c["reply"])
        return out


def ask_id_of(case_id: str) -> str | None:
    with _lock:
        c = _CASES.get(case_id)
        return c["ask_id"] if c else None


class CaseClosed(Exception):
    """A reply or cancel for a case that is no longer pending."""


def reply(case_id: str, volunteer: str, text: str) -> dict[str, Any] | None:
    """None when unknown; CaseClosed when expired/cancelled/answered."""
    volunteer = " ".join((volunteer or "").split())[:NAME_MAX]
    text = (text or "").strip()[:REPLY_MAX]
    if not volunteer or not text:
        raise ValueError("volunteer name and reply are both required")
    now = time.time()
    with _lock:
        _sweep_locked(now)
        c = _CASES.get(case_id)
        if c is None:
            return None
        if c["status"] != "pending":
            raise CaseClosed(c["status"])
        c["volunteer"], c["reply"] = volunteer, text
        _resolve(c, "answered", now)
        return _console(c, now)


def cancel(case_id: str) -> dict[str, Any] | None:
    """The app stopped waiting. Idempotent on an already-closed case."""
    now = time.time()
    with _lock:
        _sweep_locked(now)
        c = _CASES.get(case_id)
        if c is None:
            return None
        if c["status"] == "pending":
            _resolve(c, "cancelled", now)
        return {"id": c["id"], "status": c["status"]}
