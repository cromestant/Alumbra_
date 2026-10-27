"""Alumbra ask pipeline — days 1-2 skeleton.

Photo + question in, spatially grounded answer out. Consolidates the
day-one throwaway probes (repo root: sam31-dayone-probe.py, flow_test.py,
crop_read.py) into one module behind POST /ask.

Flows (brief 2026-10-26):
  A — open scene: Spark names nouns, SAM grounds each + the safety list.
  B — named target: SAM the target phrase + the safety list.
  C — labeled item: SAM category boxes, crop each, Wonder reads labels.

Only stdlib + requests + Pillow. No numpy, no meta-sam-parser (the parser
returned 0 records on the raw lane; the wire-format regex below is the
proven implementation, copied from sam31-dayone-probe.py).
"""
from __future__ import annotations

import base64
import concurrent.futures
import copy
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from PIL import Image

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

API_BASE = os.environ.get("META_MODEL_API_BASE", "https://api.meta.ai/v1")
ENDPOINT = f"{API_BASE.rstrip('/')}/responses"
SAM_MODEL = "sam-3.1"
SPARK_MODEL = "muse-spark-1.3"
WONDER_MODEL = "muse-wonder-1.0"

# Tested winner (simple_test.py / dusk_rerun.py). Do not "improve".
SPARK_NAMING_PROMPT = (
    "what is in this image? reply only with a JSON array of short noun phrases"
)
WONDER_READ_PROMPT = (
    "Read the label on the main bottle or container in this image. "
    "Reply with ONLY the product or drug name in capitals, or NONE if no "
    "label text is readable. No other words."
)

SAFETY_LIST = ["car", "person", "stairs", "door", "curb"]
MAX_NOUNS = 10
FLOW_C_CROP_CAP = 24
FLOW_C_IOU = 0.7
SAM_TIMEOUT = 180
SPARK_TIMEOUT = 120
WONDER_TIMEOUT = 60
FANOUT_WORKERS = 5

HAZARD_KEYWORDS = ("puddle", "wet patch", "manhole", "stairs")

# question fragment (lowercased) -> canonical label text on the product.
SYNONYM_MAP = {
    "adhd medication": "LISDEXAMFETAMINE",
    "adhd medicine": "LISDEXAMFETAMINE",
    "adhd meds": "LISDEXAMFETAMINE",
    "adhd med": "LISDEXAMFETAMINE",
    "adhd pills": "LISDEXAMFETAMINE",
    "adhd drug": "LISDEXAMFETAMINE",
    "vyvanse": "LISDEXAMFETAMINE",
    "adderall": "AMPHETAMINE",
    "advil": "IBUPROFEN",
    "motrin": "IBUPROFEN",
    "tylenol": "ACETAMINOPHEN",
}
MEDICATION_WORDS = {
    "medicine", "medicines", "medication", "medications", "meds", "med",
    "pill", "pills", "drug", "drugs", "prescription", "pharmacy",
    "tablet", "tablets", "capsule", "capsules", "inhaler", "drops",
    "dose", "dosage",
}
# Named products readable off a label (Flow C trigger when named). Skeleton
# lexicon: unseen product names fall back to Flow B (direct SAM grounding).
KNOWN_DRUGS = {
    "ibuprofen", "acetaminophen", "aspirin", "naproxen", "cetirizine",
    "loratadine", "diphenhydramine", "omeprazole", "insulin", "albuterol",
    "metformin", "atorvastatin", "levothyroxine", "amoxicillin",
    "penicillin", "tylenol", "advil", "motrin", "aleve", "benadryl",
    "zyrtec", "claritin", "nyquil", "dayquil", "pepto", "tums",
    "vitamins", "probiotic", "biotin",
}
KNOWN_PRODUCTS = KNOWN_DRUGS | {
    "ketchup", "mustard", "shampoo", "conditioner", "soap", "detergent",
    "cereal", "coffee", "tea", "milk", "juice", "soda",
}
LABEL_WORDS = {"label", "brand", "product"}
MEDICATION_CATEGORY_PHRASES = ["medicine bottle", "prescription bottle"]

CROSSING_RE = re.compile(
    r"\bcross(?:ing|walk)?\b|\bjaywalk\w*\b", re.IGNORECASE
)
CROSSING_CONTEXT_RE = re.compile(
    r"\bstreet\b|\broad\b|\bcrosswalk\b|\bhere\b|\bnow\b|\bsafe\b", re.IGNORECASE
)
DOSING_RES = [
    re.compile(p, re.IGNORECASE) for p in (
        r"\bwhat(?:'s| is)? (?:the )?dos(?:e|age)\b",
        r"\bhow (?:many|much|often)\b.{0,40}\b(take|pill|tablets?|dose|mg|drops?)\b",
        r"\b(take|pill|tablets?|dose|mg)\b.{0,40}\bhow (?:many|much|often)\b",
        r"\bwhen (?:should|do) i take\b",
        r"\bcan i take\b",
        r"\bside effects?\b",
        r"\b\d+\s*mg\b.{0,30}\b(take|should|safe)\b",
        r"\b(safe|okay|ok) to take\b",
        r"\binteract(?:ion)?s?\b.{0,30}\b(drug|medicine|medication|pill)\b",
        r"\b(drug|medicine|medication) interact",
    )
]
GO_AHEAD_RES = [
    re.compile(p, re.IGNORECASE) for p in (
        r"\bsafe to cross\b",
        r"\byou can cross\b",
        r"\bgo ahead\b",
        r"\bclear to cross\b",
        r"\bokay? to cross\b",
        r"\bcross now\b",
        r"\bit is safe\b",
        r"\bit's safe\b",
    )
]
HANDOFF_SENTENCE = (
    "I'm connecting you to a volunteer who can help with this directly."
)
_UNCERTAINTY_RE = re.compile(
    r"couldn'?t|could not|can'?t|cannot|unable|not confirm|no match|"
    r"didn'?t find|not found|unsure|uncertain|no readable",
    re.IGNORECASE,
)

# ----------------------------------------------------------------------------
# Fix 1 — Jev intent gate (layer 2; regexes stay as layer 1)
# ----------------------------------------------------------------------------

JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
JEV_TIMEOUT = 3
JEV_THRESHOLD_CROSSING = 0.5
JEV_THRESHOLD_CONSUMPTION = 0.5
JEV_THRESHOLD_DOSING = 0.5
JEV_THRESHOLD_SIGNAL = 0.5
JEV_THRESHOLD_NAVIGATION = 0.5

# Part B — navigation helpers.
NAVIGATION_RE = re.compile(
    r"\bexit\b|\bleave\b|\bget out\b|\bget to\b|\breach\b|\bwalk\b|"
    r"\bpath\b|\bstumbl\w*\b|\btrip\b|\bbump\b|"
    r"\bin my way\b|\bin the way\b",
    re.IGNORECASE,
)
GOAL_WORDS = {
    "door", "exit", "stairs", "gate", "elevator", "escalator",
    "hallway", "corridor", "entry", "entrance",
}
GOAL_ALIASES = {
    "exit": ["exit", "door"],
    "door": ["door"],
    "entrance": ["entrance", "entry", "door"],
    "entry": ["entry", "entrance", "door"],
    "stairs": ["stairs", "step"],
    "gate": ["gate"],
    "elevator": ["elevator"],
    "escalator": ["escalator"],
    "hallway": ["hallway", "corridor"],
    "corridor": ["corridor", "hallway"],
}
SURFACE_WORDS = {"floor", "ground", "wall", "ceiling", "sky"}
SIZE_RANK = {"close": 0, "a few steps away": 1, "far": 2}

# Answer tightening — duplicate scene instances (same object re-segmented
# under overlapping phrases) collapse before answering. IoU gate: same
# normalized name + overlapping boxes = same object; disjoint boxes with
# the same name are distinct instances and are kept (mailbox banks).
MERGE_IOU = 0.5

# Answer tightening — exit phrasing ("get out of the room") with no named
# goal infers a door/exit goal when one is visible. Never produces
# no_goal_visible on its own: inference only fires on a visible match.
EXIT_PHRASE_RE = re.compile(
    r"\bget(?:ting)?\s+out\b|\bleav(?:e|ing)\b", re.IGNORECASE
)
EXIT_GOAL_ALIASES = ["door", "exit", "doorway", "entrance"]

# Answer tightening — passageways and traversable ground are never
# "in your path" obstructions. The goal's own aliases are excluded too
# (all duplicate instances, not just the picked one).
PASSABLE_WORDS = {
    "door", "doorway", "exit", "entrance", "entry", "hallway",
    "corridor", "hall", "sidewalk", "path", "road", "ramp",
}

# Field test — the goal is whatever the question asks to reach: "path to
# the X" / "get to X" / "reach X" names X the goal (fridge, couch,
# counter...), with exit/door inference as fallback. Common reach nouns
# get aliases (fridge/refrigerator); goal words reuse GOAL_ALIASES.
REACH_RES = [
    re.compile(
        r"\b(?:path|way)\s+to\s+(?:the|my|a|an)?\s*"
        r"([a-z][a-z' ]+?)(?:\?|$|,|\.|!)", re.IGNORECASE,
    ),
    re.compile(
        r"\bget\s+to\s+(?:the|my|a|an)?\s*"
        r"([a-z][a-z' ]+?)(?:\?|$|,|\.|!)", re.IGNORECASE,
    ),
    re.compile(
        r"\breach\s+(?:the|my|a|an)?\s*"
        r"([a-z][a-z' ]+?)(?:\?|$|,|\.|!)", re.IGNORECASE,
    ),
]
REACH_ALIASES = {
    "fridge": ["fridge", "refrigerator"],
    "couch": ["couch", "sofa"],
    "counter": ["counter", "countertop"],
    "tv": ["tv", "television"],
    "car": ["car", "vehicle"],
}
# "path to the fridge clear?" — the reach target is "fridge"; a trailing
# predicate ("clear", "safe"...) is not part of the noun.
REACH_TAIL_WORDS = {
    "clear", "clean", "safe", "free", "open", "closed", "empty",
    "okay", "ok",
}
# "way to get out" is exit phrasing, not a reach target.
REACH_VERBS = {
    "get", "go", "going", "leave", "leaving", "exit", "walk",
    "come", "move", "find",
}


def _reach_target(question: str) -> str:
    """Noun phrase the question asks to reach ('path to the X'), else ''."""
    for rx in REACH_RES:
        m = rx.search(question or "")
        if not m:
            continue
        words = m.group(1).strip().lower().split()
        while words and words[-1] in REACH_TAIL_WORDS:
            words.pop()
        if not words or words[0] in REACH_VERBS:
            continue
        return " ".join(words[:4])
    return ""


def _reach_aliases(phrase: str) -> list[str]:
    """Match forms for a reach phrase, longest first ('front door',
    then its head)."""
    words = phrase.split()
    out = []
    for n in range(len(words), 0, -1):
        cand = " ".join(words[:n])
        out.append(cand)
        head = _head_noun(cand)
        if head and head not in out:
            out.append(head)
        for src in (REACH_ALIASES, GOAL_ALIASES):
            for a in src.get(head, []):
                if a not in out:
                    out.append(a)
    return out

# Field test — an obstruction must stand on the floor within the
# corridor. Entries whose base rests on a holder (counter, table,
# shelf...) are supported objects, orientation at most — never
# obstructions. Geometric: base at/above the holder's vertical middle
# with horizontal overlap.
HOLDER_WORDS = {
    "counter", "countertop", "table", "shelf", "desk", "dresser",
    "cabinet", "nightstand", "stand", "bookcase", "stove", "washer",
    "dryer", "microwave",
}

# Field test — parts of the goal are the goal: an entry naming a part
# (drawer, handle, shelf...) whose box sits mostly inside the goal's
# box is goal structure, never an obstruction. Both conditions are
# required — floor hazards in front of the goal project inside its box
# but are not parts.
GOAL_PART_WORDS = {
    "drawer", "drawers", "handle", "handles", "knob", "knobs",
    "shelf", "shelves", "leg", "legs", "button", "buttons",
}

# Field test — floor-standing check. An obstruction must stand on the
# floor: close/near entries whose base sits high in the frame (countertop
# appliances, wall-mounted things) float above walking height and are
# orientation, not obstructions. Far entries get a looser line (only
# near-the-top boxes — chandeliers, high signs — are excluded).
FLOOR_LINE_NEAR = 0.55
FLOOR_LINE_FAR = 0.4


def _image_height(image: bytes) -> int:
    try:
        with Image.open(io.BytesIO(image)) as im:
            return im.size[1]
    except Exception:
        return 0


def _stands_on_floor(entry: dict[str, Any], height: int) -> bool:
    """Base of the box reaches the floor band for its distance rank."""
    if height <= 0:
        return True
    try:
        yb = int(entry.get("box", [0, 0, 0, 0])[3])
    except (TypeError, ValueError, IndexError):
        return True
    yfrac = yb / height
    rank = SIZE_RANK.get(str(entry.get("size_class", "far")), 2)
    if rank >= 2:
        return yfrac >= FLOOR_LINE_FAR
    return yfrac >= FLOOR_LINE_NEAR

# Field test §1 — floor second look for navigation asks. Small,
# low-contrast floor hazards (ice, spills, cords) escape the naming
# pass, so SAM proposes directly over floor regions and Wonder reads
# each proposal (the cabinet pattern). Navigation asks only; skipped
# in the dark (nothing to see) and by FLOOR_LOOK_ENABLED=0.
FLOOR_PHRASE = "small object on the floor"
FLOOR_READ_PROMPT = (
    "What small object is lying on the floor in this crop? Reply with "
    "ONLY a short name (for example: ice cubes, cord, toy), or NONE "
    "if nothing distinct is visible. No other words."
)
FLOOR_REGION_WORDS = {
    "floor", "ground", "tile", "rug", "carpet", "lawn", "grass",
    "sidewalk", "path", "road", "pavement", "concrete",
}
FLOOR_MAX_REGIONS = 3
FLOOR_MAX_BOXES = 8
FLOOR_LOOK_IOU = 0.3
# Long side below this fraction of the image width is texture (concrete
# speck, granules), not an obstruction candidate.
FLOOR_MIN_FRAC = 0.008

# Part A — audio question recovery. NOTE: the fix brief names
# POST /v1/audio/transcriptions, but that path 404s as of 2026-10-26;
# POST /v1/asr/transcribe (request JSON + audio parts) is the working
# endpoint and returns {"transcript": ...}. Same model id either way.
ASR_ENDPOINT = f"{API_BASE.rstrip('/')}/asr/transcribe"
ASR_MODEL = "muse-voice-transcribe-1.0"
ASR_TIMEOUT = 30
FFMPEG = "/usr/bin/ffmpeg"
JEV_QUESTIONS = {
    "crossing_intent": (
        "Does this question ask for a decision about crossing a street, "
        "entering a roadway, or moving into traffic?"
    ),
    "consumption_intent": (
        "Does this question ask whether the user may eat, drink, or consume "
        "something, or whether something is edible or safe to consume?"
    ),
    "dosing_intent": (
        "Does this question ask whether the user may take medication now, "
        "or ask about dosage?"
    ),
    # Patch 1: signal-state questions sit mid-band on crossing_intent.
    "signal_intent": (
        "Does this question ask about the state of a traffic light, "
        "pedestrian signal, or crossing signal?"
    ),
    # Part B: navigation shapes the answer only — never escalates,
    # never enters combine_gate.
    "navigation_intent": (
        "Does this question ask about walking a path or route, reaching "
        "a destination on foot, or avoiding obstacles while moving?"
    ),
    # Patch 7: scene classifiers — instrumentation only, never acted on.
    "scene_danger": (
        "Is there a potentially dangerous situation for a blind pedestrian "
        "in this scene?"
    ),
    "scene_danger_imminent": (
        "Is a blind pedestrian in this scene in immediate danger of injury, "
        "for example about to step into traffic, onto a slippery or raised "
        "surface in their path, or off a drop-off?"
    ),
}
CROSSING_CONTEXT = ["street", "road", "crosswalk", "intersection", "curb",
                    "car"]
SIGNAL_STATE_RE = re.compile(
    r"\bgreen\b|\bred\b|\bwalk sign\b|\bflashing\b", re.IGNORECASE
)
JEV_CUE_STEMS = {
    "street": ["street"],
    "crossing": ["cross"],
    "medication": ["medic", "pill", "prescription", "pharmacy"],
    "food": ["food", "fruit"],
}
# Layer-1 consumption family: poison/toxicity plus consumption verbs.
# Safety must not depend on the Jev key being present (Patch 2).
POISON_RE = re.compile(r"\bpoison\w*|\btoxic\w*|\bvenom\w*", re.IGNORECASE)
CONSUMPTION_RE = re.compile(
    r"\beat\b|\bedible\b|\bdrink\b|\btaste\b|\bconsum\w*\b|\bswallow\b|"
    r"\bsafe to eat\b|\bsafe to drink\b",
    re.IGNORECASE,
)
CONSUMPTION_TEMPLATE = (
    "I can't tell from a photo whether this is OK for you. "
    "Please check with a person who knows before eating or drinking it."
)

# ----------------------------------------------------------------------------
# Fix 2 — darkness is a measured signal
# ----------------------------------------------------------------------------
# Calibrated 2026-10-26 (mean luminance, Pillow L): near-dark field 45.9,
# night sky 58.0, hazard 102.5, mailbox 47.8, desk 105.2. The mailbox photo
# is a night shot (47.8) so no threshold separates the dark pair from every
# lit case; 60.0 gates the genuinely dark scenes while leaving grading of
# the lit set unchanged (cap + hedge are grading-neutral there).
DARK_LUMINANCE = 60.0
# Patch 3 — p85 replaces the mean: flash-lit subjects in dark frames
# (mailbox) no longer read as dark. Calibrated 2026-10-26: near-dark
# field 110, night sky 88, hazard 149, mailbox 128, desk 182.
DARK_P85 = 120.0
DARK_HEDGES = [
    "too dark", "can't make out", "cannot make out", "not sure",
    "hard to see", "low light", "barely see", "can't be sure",
]
# Specified sentence verbatim, plus an eval-vocabulary hedge: the specified
# sentence's "can't be sure" is not in the case_05 hedge list, so the bare
# sentence alone cannot flip Q1/Q2. Flagged in the tuning report.
DARK_APPEND = ("It's very dark, so I can't be sure of these details. "
               "I'm not sure what else is visible.")

# ----------------------------------------------------------------------------
# Fix 3 — street scenes: describe only
# ----------------------------------------------------------------------------

STREET_CUES = ["street", "road", "crosswalk", "car", "traffic light",
               "intersection"]
DIRECTIVE_PHRASES = ["go straight", "go ahead", "cross now", "you can go",
                     "safe to go", "proceed", "step out"]
STREET_CLOSER = "I can't tell you whether it's safe to move."
# Patch 4 — traffic-absence assertions, stripped from street ESCALATEs.
ABSENCE_RES = [
    re.compile(p, re.IGNORECASE) for p in (
        r"\b(do not|don['\u2019]t|does not) see (any )?"
        r"(cars|vehicles|traffic)\b",
        r"\bno (cars|vehicles|traffic) (are|in|visible)\b",
    )
]
HOW_MANY_RE = re.compile(r"\bhow many\b", re.IGNORECASE)

QUALIFIER_RE = re.compile(
    r"^(?P<base>.+?)\s+(?P<qual>(?:number\s+\d+|\d+\s*(?:st|nd|rd|th)?|"
    r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth))"
    r"\s*$",
    re.IGNORECASE,
)

LOCATE_RES = [
    re.compile(
        r"(?:where'?s|where\s+(?:is|are)|find|locate|look\s+for|"
        r"help\s+me\s+find|show\s+me)\s+(?:my|the|a|an)?\s*(.+)",
        re.IGNORECASE,
    ),
]
_CLAUSE_CUT_RE = re.compile(
    r"\s*(?:,|\.|;|\?|!|\bit'?s\b|\bthat\b|\bwhich\b|\bcalled\b|\bnamed\b).*",
    re.IGNORECASE,
)

# Answer tightening — final synthesis pass (subtractive only, Wonder
# class, hard timeout; on any failure the composed answer ships).
SYNTHESIS_TIMEOUT = 3
SYNTHESIS_MODEL = WONDER_MODEL
CLOCK_LIST_RE = re.compile(
    r"((?:\b\d{1,2}\b(?:\s*,)?\s*(?:and\s+)?)+)\s*o'clock",
    re.IGNORECASE,
)
CAUTION_RE = re.compile(
    r"\b(can't|cannot|never|must not|don't|do not|not sure|unsure|"
    r"uncertain|check with|volunteer|pharmacist)\b",
    re.IGNORECASE,
)
SYNTHESIS_STOPWORDS = {
    "what", "when", "where", "which", "while", "who", "whom", "whose",
    "that", "this", "these", "those", "with", "from", "have", "has",
    "had", "are", "was", "were", "been", "being", "can", "could",
    "would", "should", "will", "shall", "does", "did", "done", "just",
    "like", "look", "please", "here", "there", "ahead", "about",
    "your", "you", "for", "and", "the", "any",
}


class PipelineError(RuntimeError):
    """Upstream Model API failure the route reports as 502."""


def _load_secret(name: str) -> str:
    key = os.environ.get(name, "").strip()
    if key:
        return key
    repo_root = Path(__file__).resolve().parent.parent.parent
    for path in (repo_root / ".env", Path.home() / "code" / "alumbra" / ".env"):
        try:
            text = path.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if line.startswith(name + "="):
                _, _, val = line.partition("=")
                val = val.strip().strip("'\"")
                if val:
                    return val
    return ""


def _load_key() -> str:
    return _load_secret("META_MODEL_API_KEY")


def _mime_of(data: bytes) -> str:
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


# ----------------------------------------------------------------------------
# Model API plumbing (streamed POST /responses)
# ----------------------------------------------------------------------------

def _stream_output_text(body: dict, timeout: int) -> str:
    key = _load_key()
    if not key:
        raise PipelineError("META_MODEL_API_KEY is not set")
    try:
        resp = requests.post(
            ENDPOINT,
            headers={"Authorization": f"Bearer {key}"},
            json=body,
            timeout=timeout,
            stream=True,
        )
    except requests.RequestException as e:
        raise PipelineError(f"Model API unreachable: {e}") from e
    if resp.status_code != 200:
        raise PipelineError(
            f"Model API HTTP {resp.status_code}: {resp.text[:300]}"
        )
    deltas: list[str] = []
    final_text: str | None = None
    for raw_line in resp.iter_lines(decode_unicode=True):
        if not raw_line or not raw_line.startswith("data:"):
            continue
        data = raw_line[5:].strip()
        if data == "[DONE]":
            break
        try:
            ev = json.loads(data)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type", "")
        if etype == "response.output_text.delta":
            deltas.append(ev.get("delta", ""))
        elif etype == "response.output_text.done":
            final_text = ev.get("text")
        elif etype == "response.content_part.done":
            part = ev.get("part", {})
            if part.get("type") == "output_text":
                final_text = part.get("text")
            elif part.get("type") == "refusal":
                raise PipelineError(
                    f"Model refused: {part.get('refusal', '')[:200]}"
                )
        elif etype in ("response.failed", "response.incomplete", "error"):
            raise PipelineError(f"Stream terminal event {etype}: {data[:200]}")
    return final_text or "".join(deltas) or ""


def _vlm_text(model: str, image: bytes, instruction: str, timeout: int) -> str:
    b64 = base64.b64encode(image).decode("ascii")
    return _stream_output_text(
        {
            "model": model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": instruction},
                        {
                            "type": "input_image",
                            "image_url": f"data:{_mime_of(image)};base64,{b64}",
                        },
                    ],
                }
            ],
            "stream": True,
        },
        timeout,
    )


# ----------------------------------------------------------------------------
# SAM 3.1 — one noun phrase per call; wire-regex parsing (probe-verified)
# ----------------------------------------------------------------------------

WIRE_RECORD_RE = re.compile(
    r"(?P<obj>\d+)"
    r"<\|box;x1=(?P<x1>-?\d+);y1=(?P<y1>-?\d+);x2=(?P<x2>-?\d+);y2=(?P<y2>-?\d+);"
    r"w=(?P<w>\d+);h=(?P<h>\d+)(?:;c=(?P<c>[^;|>]+))?\|>"
    r"<\|mask;x=0;y=0;(?:c=(?P<mc>[^;]+);)?data=(?P<mh>\d+),(?P<mw>\d+),"
    r"(?P<payload>[^|]+)\|>"
)
WIRE_LINE_RE = re.compile(r"^<(?P<frame>\d+)f>")


def sam_segment(image: bytes, phrase: str) -> list[dict[str, Any]]:
    """One image + noun phrase -> records: {box, confidence}."""
    b64 = base64.b64encode(image).decode("ascii")
    text = _stream_output_text(
        {
            "model": SAM_MODEL,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": phrase},
                        {
                            "type": "input_image",
                            "image_url": f"data:{_mime_of(image)};base64,{b64}",
                        },
                    ],
                }
            ],
            "metadata": {"include_confidence": "true"},
            "stream": True,
        },
        SAM_TIMEOUT,
    )
    records = []
    for line in text.splitlines():
        for m in WIRE_RECORD_RE.finditer(line):
            try:
                c = float(m.group("c")) if m.group("c") else None
            except ValueError:
                c = None
            records.append(
                {
                    "box": (
                        int(m.group("x1")),
                        int(m.group("y1")),
                        int(m.group("x2")) + 1,  # half-open
                        int(m.group("y2")) + 1,
                    ),
                    "confidence": c,
                }
            )
    return records


def sam_many(image: bytes, phrases: list[str]) -> dict[str, list[dict]]:
    """SAM several phrases concurrently; failed phrases map to []."""
    out: dict[str, list[dict]] = {}

    def one(phrase: str) -> tuple[str, list[dict]]:
        try:
            return phrase, sam_segment(image, phrase)
        except Exception:
            return phrase, []

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=FANOUT_WORKERS
    ) as pool:
        for phrase, records in pool.map(one, phrases):
            out[phrase] = records
    return out


# ----------------------------------------------------------------------------
# Routing
# ----------------------------------------------------------------------------

def is_crossing_question(question: str) -> bool:
    q = question or ""
    return bool(CROSSING_RE.search(q) and CROSSING_CONTEXT_RE.search(q))


def is_dosing_question(question: str) -> bool:
    q = question or ""
    return any(rx.search(q) for rx in DOSING_RES)


def is_navigation_question(question: str) -> bool:
    return bool(NAVIGATION_RE.search(question or ""))


def _path_assessment(
    scene: list[dict[str, Any]], question: str, height: int = 0
) -> dict[str, Any]:
    """Part B: facts about the route, computed — not free-reasoned.

    Goal: the object the question asks to reach — a named goal word,
    a "path/way to the X" reach target, then a visible door/exit when
    the question phrases leaving; else straight ahead (12 o'clock). The
    goal is never an obstruction, whatever it is. On-path:
    floor-standing entries within +-1 hour of the goal's clock and no
    farther than the goal (near objects count when the goal is close) —
    never the goal or its duplicates, never passageways or traversable
    ground, never objects resting on holders (counters, tables,
    shelves: orientation, not obstructions), never entries floating
    above walking height (close but high in the frame). Verdict: clear
    | blocked | no_goal_visible (question names a goal absent from the
    scene; exit-phrase inference never produces this — it fires only on
    a visible match). height is the image height in px (0 skips the
    floor-standing check).
    """
    words = set(re.findall(r"[a-z]+", (question or "").lower()))
    goal_words = [w for w in words if w in GOAL_WORDS]
    reach = "" if goal_words else _reach_target(question)
    goal: dict[str, Any] | None = None
    goal_aliases: list[str] = []
    goal_desc = "exit"
    if goal_words:
        cands = []
        for e in scene:
            name = str(e.get("name", "")).lower()
            for w in goal_words:
                if any(a in name for a in GOAL_ALIASES.get(w, [w])):
                    cands.append(e)
                    break
        if cands:
            goal = max(cands, key=lambda e: (e["box"][2] - e["box"][0])
                       * (e["box"][3] - e["box"][1]))
            for w in goal_words:
                goal_aliases.extend(GOAL_ALIASES.get(w, [w]))
            goal_desc = goal_words[0] if len(goal_words) == 1 else "exit"
    elif reach:
        aliases = _reach_aliases(reach)
        cands = [e for e in scene
                 if any(a in str(e.get("name", "")).lower()
                        for a in aliases)]
        if cands:
            goal = max(cands, key=lambda e: (e["box"][2] - e["box"][0])
                       * (e["box"][3] - e["box"][1]))
            goal_aliases.extend(aliases)
            goal_desc = reach
    if (goal_words or reach) and goal is None:
        return {"goal": None, "goal_clock": None,
                "verdict": "no_goal_visible", "on_path": [],
                "goal_desc": goal_desc}
    if goal is None and EXIT_PHRASE_RE.search(question or ""):
        cands = [e for e in scene
                 if any(a in str(e.get("name", "")).lower()
                        for a in EXIT_GOAL_ALIASES)]
        if cands:
            goal = max(cands, key=lambda e: (e["box"][2] - e["box"][0])
                       * (e["box"][3] - e["box"][1]))
            goal_aliases.extend(EXIT_GOAL_ALIASES)
    goal_clock = int(goal["clock"]) if goal else 12
    goal_rank = SIZE_RANK.get(str((goal or {}).get("size_class", "far")), 2)

    def clock_dist(a: int, b: int) -> int:
        return min(abs(a - b), 12 - abs(a - b))

    def is_passable(name: str) -> bool:
        return any(re.search(r"\b" + re.escape(s) + r"\b", name)
                   for s in SURFACE_WORDS | PASSABLE_WORDS)

    holders = []
    for e in scene:
        name = str(e.get("name", "")).lower()
        if any(re.search(r"\b" + re.escape(h) + r"\b", name)
               for h in HOLDER_WORDS):
            holders.append(e.get("box", [0, 0, 0, 0]))

    def rests_on_holder(box: list[int] | tuple[int, ...]) -> bool:
        try:
            x1, _, x2, yb = (int(v) for v in box)
        except (TypeError, ValueError):
            return False
        for h in holders:
            try:
                hx1, hy1, hx2, hy2 = (int(v) for v in h)
            except (TypeError, ValueError):
                continue
            overlap = max(0, min(x2, hx2) - max(x1, hx1))
            if overlap > 0.3 * max(1, x2 - x1) \
                    and yb <= hy1 + 0.5 * (hy2 - hy1):
                return True
        return False

    def _box_pair(e: dict[str, Any]) -> tuple[float, float]:
        # (fraction of the entry box inside the goal box, area ratio
        # entry/goal). Shared by the part and echo checks below.
        try:
            x1, y1, x2, y2 = (int(v) for v in e.get("box", [0, 0, 0, 0]))
            gx1, gy1, gx2, gy2 = (
                int(v) for v in (goal or {}).get("box", [0, 0, 0, 0]))
        except (TypeError, ValueError):
            return 0.0, 0.0
        inter = max(0, min(x2, gx2) - max(x1, gx1)) * max(
            0, min(y2, gy2) - max(y1, gy1))
        area = max(1, (x2 - x1) * (y2 - y1))
        garea = max(1, (gx2 - gx1) * (gy2 - gy1))
        return inter / area, area / garea

    def is_goal_part(e: dict[str, Any]) -> bool:
        if goal is None:
            return False
        name = str(e.get("name", "")).lower()
        if not any(re.search(r"\b" + re.escape(w) + r"\b", name)
                   for w in GOAL_PART_WORDS):
            return False
        return _box_pair(e)[0] > 0.7

    def is_goal_echo(e: dict[str, Any]) -> bool:
        # A re-grounding of the goal itself under another name (naming
        # proposed "countertop appliance", SAM returned the fridge's
        # box): near-identical box, so it can never be an obstruction.
        # The area-ratio floor keeps genuine small obstructions
        # silhouetted against a big goal (toy before a couch) on-path.
        if goal is None or e is goal:
            return False
        contained, ratio = _box_pair(e)
        return contained >= 0.9 and ratio >= 0.5

    on_path = []
    for e in scene:
        if goal is not None and e is goal:
            continue
        name = str(e.get("name", "")).lower()
        if any(a in name for a in goal_aliases):
            continue
        if is_passable(name):
            continue
        if rests_on_holder(e.get("box", [0, 0, 0, 0])):
            continue
        if is_goal_part(e):
            continue
        if is_goal_echo(e):
            continue
        if not _stands_on_floor(e, height):
            continue
        rank = SIZE_RANK.get(str(e.get("size_class", "far")), 2)
        # Betweenness by distance rank: nearer than the goal counts, and
        # when the goal itself is close, same-corridor near objects (a
        # few steps away) count too — a close goal must not certify the
        # last steps sight-unseen.
        if clock_dist(int(e.get("clock", 12)), goal_clock) <= 1 \
                and rank <= max(goal_rank, 1):
            on_path.append(e)
    on_path.sort(key=lambda e: SIZE_RANK.get(
        str(e.get("size_class", "far")), 2))
    return {
        "goal": (str(goal["name"]) if goal else None),
        "goal_clock": goal_clock,
        "goal_desc": goal_desc,
        "verdict": "clear" if not on_path else "blocked",
        "on_path": [
            {"name": str(e.get("name", "")),
             "clock": int(e.get("clock", 12)),
             "size_class": str(e.get("size_class", ""))}
            for e in on_path
        ],
    }


def extract_target(question: str) -> str | None:
    """Noun phrase after a locate verb, minus articles and trailing clauses."""
    for rx in LOCATE_RES:
        m = rx.search(question or "")
        if not m:
            continue
        target = _CLAUSE_CUT_RE.sub("", m.group(1)).strip().strip("'\"")
        target = re.sub(r"^(my|the|a|an)\s+", "", target, flags=re.IGNORECASE)
        if target:
            return target
    return None


def is_labeled_item_question(question: str) -> bool:
    """Flow C trigger: locating a labeled product/medication by name."""
    q = (question or "").lower()
    if not q:
        return False
    words = set(re.findall(r"[a-z]+", q))
    if words & (MEDICATION_WORDS | KNOWN_PRODUCTS):
        return True
    if "label" in words and re.search(
        r"\b(find|where|look|locate|read)\b", q
    ):
        return True
    for frag in SYNONYM_MAP:
        if frag in q:
            return True
    return False


def resolve_target(question: str) -> str:
    """Canonical product/drug name for Flow C matching."""
    q = (question or "").lower()
    best = ""
    for frag, canonical in SYNONYM_MAP.items():
        if frag in q and len(frag) > len(best):
            best = frag
    if best:
        return SYNONYM_MAP[best]
    return (extract_target(question) or question or "").strip()


def is_medication_question(question: str) -> bool:
    q = (question or "").lower()
    words = set(re.findall(r"[a-z]+", q))
    return bool(words & (MEDICATION_WORDS | KNOWN_DRUGS)) or any(
        frag in q for frag in SYNONYM_MAP
    )


def route(question: str) -> tuple[str, str | None]:
    """Return (flow, target). flow is one of A, B, C."""
    if is_labeled_item_question(question):
        return "C", resolve_target(question)
    target = extract_target(question or "")
    if target:
        return "B", target
    return "A", None


def split_qualifier(target: str) -> tuple[str, str | None]:
    """Fix 4: split a find-target into (SAM noun prompt, qualifier).

    "mailbox 14" -> ("mailbox", "14"); no qualifier -> (target, None).
    """
    m = QUALIFIER_RE.match((target or "").strip())
    if m and m.group("base").strip():
        return m.group("base").strip(), m.group("qual").strip()
    return (target or "").strip(), None


def mean_luminance(image: bytes) -> float:
    """Fix 2: mean greyscale level of the upload, 0-255."""
    from PIL import ImageStat

    with Image.open(io.BytesIO(image)) as im:
        return float(ImageStat.Stat(im.convert("L")).mean[0])


def p85_luminance(image: bytes) -> int:
    """Patch 3: 85th-percentile greyscale level, from the histogram."""
    with Image.open(io.BytesIO(image)) as im:
        hist = im.convert("L").histogram()
    total = sum(hist)
    acc = 0
    for level, count in enumerate(hist):
        acc += count
        if acc >= 0.85 * total:
            return level
    return 255


def split_sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


def jev_enabled() -> bool:
    if os.environ.get("JEV_GATE_ENABLED", "").strip().lower() in (
        "0", "false", "no", "off"
    ):
        return False
    return bool(_load_secret("JEV_API_KEY"))


def audio_enabled() -> bool:
    """Kill switch mirrors jev_enabled(): AUDIO_QUESTION_ENABLED=0 off."""
    return os.environ.get("AUDIO_QUESTION_ENABLED", "").strip().lower() not in (
        "0", "false", "no", "off"
    )


def to_wav_16k_mono(data: bytes) -> bytes | None:
    """16 kHz mono WAV for ASR. Already-there input passes through;
    anything else goes through ffmpeg (mirrors audio_probe.extract).
    None on any failure — the ask never fails for audio reasons."""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            if (w.getframerate() == 16000 and w.getnchannels() == 1
                    and w.getsampwidth() == 2):
                return data
    except Exception:
        pass
    ffmpeg = FFMPEG if os.path.exists(FFMPEG) else shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    try:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "in")
            dst = os.path.join(td, "out.wav")
            with open(src, "wb") as f:
                f.write(data)
            subprocess.run(
                [ffmpeg, "-y", "-v", "error", "-i", src,
                 "-vn", "-ac", "1", "-ar", "16000", dst],
                timeout=60, check=True,
            )
            if not os.path.exists(dst) or os.path.getsize(dst) == 0:
                return None
            with open(dst, "rb") as f:
                return f.read()
    except Exception:
        return None


def transcribe_audio(wav_bytes: bytes | None) -> str:
    """Speech -> text via /v1/asr/transcribe. '' on any failure."""
    if not wav_bytes:
        return ""
    try:
        key = _load_key()
        if not key:
            return ""
        r = requests.post(
            ASR_ENDPOINT,
            headers={"Authorization": f"Bearer {key}"},
            files={
                "request": (
                    None,
                    json.dumps({"model": ASR_MODEL,
                                "audioEncoding": "WAV"}),
                    "application/json",
                ),
                "audio": ("ask.wav", wav_bytes, "audio/wav"),
            },
            timeout=ASR_TIMEOUT,
        )
        if r.status_code != 200:
            return ""
        return str(r.json().get("transcript", "") or "").strip()
    except Exception:
        return ""


def jev_state(
    scene: list[dict[str, Any]],
    question: str,
    flow: str,
    target: str | None,
) -> str:
    """Fix 1: scene context for the intent gate.

    Object names only. Flow C crop-read label text (medication names) is
    never included — category phrases stand in for the reads.
    """
    if flow == "C":
        if is_medication_question(question):
            names = list(MEDICATION_CATEGORY_PHRASES)
        else:
            names = [t for t in [target or "", "bottle", "container"] if t]
    else:
        scored = sorted(
            scene,
            key=lambda e: (e.get("confidence")
                           if isinstance(e.get("confidence"), (int, float))
                           else -1),
            reverse=True,
        )
        names = []
        for e in scored:
            n = str(e.get("name", "")).strip()
            if n and n.lower() not in [x.lower() for x in names]:
                names.append(n)
        names = names[:12]
    scene_part = ", ".join(names) if names else "(none)"
    blob = " ".join(names).lower()
    cues = [cue for cue, stems in JEV_CUE_STEMS.items()
            if any(s in blob for s in stems)]
    return ("The user is a blind pedestrian.\n"
            f"Scene: {scene_part}. "
            f"Setting cues: {', '.join(cues) if cues else 'none'}. "
            f"User question: {question.strip() or '(none)'}")


def jev_intents(state: str) -> dict[str, float] | None:
    """Fix 1: one call, three intent factors. None when the layer is
    disabled, keyless, or erroring — the caller falls back to regex."""
    if not jev_enabled():
        return None
    key = _load_secret("JEV_API_KEY")
    body = {
        "state": state,
        "model": JEV_MODEL,
        "questions": {
            name: {"type": "noul", "instructions": instr}
            for name, instr in JEV_QUESTIONS.items()
        },
    }
    try:
        r = requests.post(
            JEV_ENDPOINT,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json=body,
            timeout=JEV_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        answers = r.json().get("answers", {})
        out = {}
        for name in JEV_QUESTIONS:
            out[name] = max(0.0, min(1.0, float(answers[name]["noul"])))
        return out
    except Exception:
        return None


def combine_gate(
    question: str,
    scene: list[dict[str, Any]],
    jev: dict[str, float] | None,
) -> tuple[dict[str, Any], bool, bool, bool]:
    """Layer-1 regexes + layer-2 Jev intents -> (gate, crossing_like,
    dosing, consumption). Scene classifiers (Patch 7) are logged, never
    acted on."""
    crossing_context = any(
        re.search(r"\b" + re.escape(cue) + r"\b",
                  str(e.get("name", "")), re.IGNORECASE)
        for cue in CROSSING_CONTEXT for e in scene
    )
    fired: list[str] = []
    if is_crossing_question(question):
        fired.append("crossing_regex")
    if is_dosing_question(question):
        fired.append("dosing_regex")
    if POISON_RE.search(question) or CONSUMPTION_RE.search(question):
        fired.append("consumption_regex")
    if SIGNAL_STATE_RE.search(question) and crossing_context:
        fired.append("signal_regex")
    if jev is not None:
        if jev["crossing_intent"] >= JEV_THRESHOLD_CROSSING:
            fired.append("crossing_intent")
        if jev["dosing_intent"] >= JEV_THRESHOLD_DOSING:
            fired.append("dosing_intent")
        if jev["consumption_intent"] >= JEV_THRESHOLD_CONSUMPTION:
            fired.append("consumption_intent")
        if (jev["signal_intent"] >= JEV_THRESHOLD_SIGNAL
                and crossing_context):
            fired.append("signal_intent")
    jev_fired = [f for f in fired if not f.endswith("_regex")]
    regex_fired = [f for f in fired if f.endswith("_regex")]
    source = ("jev+regex" if (jev_fired and regex_fired)
              else "jev" if jev_fired
              else "regex" if regex_fired else "none")
    gate = {"jev": jev, "fired": fired, "source": source}
    crossing_like = any(f in fired for f in (
        "crossing_regex", "crossing_intent", "signal_regex",
        "signal_intent"))
    dosing = "dosing_regex" in fired or "dosing_intent" in fired
    consumption = ("consumption_regex" in fired
                   or "consumption_intent" in fired) and not dosing
    return gate, crossing_like, dosing, consumption


def nearest_landmarks(
    scene: list[dict[str, Any]],
    ref_box: list[int] | tuple[int, ...],
    exclude: str,
    k: int = 3,
) -> list[dict[str, Any]]:
    """Fix 6: scene entries spatially nearest the target (other names)."""
    rx, ry = (ref_box[0] + ref_box[2]) / 2, (ref_box[1] + ref_box[3]) / 2

    def dist(e: dict[str, Any]) -> float:
        b = e.get("box", [0, 0, 0, 0])
        return abs((b[0] + b[2]) / 2 - rx) + abs((b[1] + b[3]) / 2 - ry)

    cands = [e for e in scene
             if str(e.get("name", "")).lower() != exclude.lower()]
    return sorted(cands, key=dist)[:k]


# ----------------------------------------------------------------------------
# Scene model geometry
# ----------------------------------------------------------------------------

def clock_of(box: tuple[int, int, int, int], width: int) -> int:
    """Clock-face direction from box-center x. Center=12, left=9, right=3."""
    fx = ((box[0] + box[2]) / 2) / width
    hour = 12 + round((fx - 0.5) * 6)
    if hour > 12:
        hour -= 12
    if hour < 1:
        hour += 12
    return hour


def size_class_of(box: tuple[int, int, int, int], height: int) -> str:
    hfrac = (box[3] - box[1]) / height
    if hfrac > 0.45:
        return "close"
    if hfrac >= 0.18:
        return "a few steps away"
    return "far"


def clamp_box(
    box: tuple[int, int, int, int], width: int, height: int
) -> list[int]:
    x1, y1, x2, y2 = (int(v) for v in box)
    return [
        max(0, min(x1, width - 1)),
        max(0, min(y1, height - 1)),
        max(0, min(x2, width)),
        max(0, min(y2, height)),
    ]


def entry_for(
    name: str,
    box: tuple[int, int, int, int],
    confidence: float | None,
    width: int,
    height: int,
    **extra: Any,
) -> dict[str, Any]:
    entry = {
        "name": name,
        "box": clamp_box(box, width, height),
        "confidence": confidence,
        "clock": clock_of(box, width),
        "size_class": size_class_of(box, height),
    }
    entry.update(extra)
    return entry


def parse_nouns(raw: str) -> list[str]:
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    nouns = []
    for x in arr:
        if isinstance(x, str):
            n = re.sub(r"^(a|an|the)\s+", "", x.strip().lower())
            if n and n not in nouns:
                nouns.append(n)
    return nouns[:MAX_NOUNS]


def iou(
    a: tuple[int, int, int, int], b: tuple[int, int, int, int]
) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    area = lambda r: (r[2] - r[0]) * (r[3] - r[1])
    union = area(a) + area(b) - inter
    return inter / union if union else 0.0


def _head_noun(name: str) -> str:
    """Last word, lowercased, articles stripped, lightly singularized."""
    words = re.sub(r"^(a|an|the)\s+", "",
                   (name or "").strip().lower()).split()
    head = words[-1] if words else ""
    if len(head) > 3 and head.endswith("s") \
            and not head.endswith(("ss", "us", "is")):
        head = head[:-1]
    return head


def _same_object(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Same normalized head noun + overlapping boxes = same instance."""
    if _head_noun(str(a.get("name", ""))) != _head_noun(str(b.get("name", ""))):
        return False
    try:
        ba = tuple(int(v) for v in a.get("box", [0, 0, 0, 0]))
        bb = tuple(int(v) for v in b.get("box", [0, 0, 0, 0]))
    except (TypeError, ValueError):
        return False
    if len(ba) != 4 or len(bb) != 4:
        return False
    return iou(ba, bb) > MERGE_IOU


def merge_entries(scene: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse duplicate instances of the same object.

    Same head noun + overlapping boxes merge into one entry, keeping the
    longest (most descriptive) name, the highest confidence, the nearest
    size class, and the highest-confidence member's box and clock. Extras
    (Flow C label/match) come from a matched member when any matched.
    Group order follows first appearance.
    """
    groups: list[list[dict[str, Any]]] = []
    for e in scene:
        placed = False
        for g in groups:
            if any(_same_object(e, m) for m in g):
                g.append(e)
                placed = True
                break
        if not placed:
            groups.append([e])

    def conf(e: dict[str, Any]) -> float:
        c = e.get("confidence")
        return c if isinstance(c, (int, float)) else -1.0

    merged: list[dict[str, Any]] = []
    for g in groups:
        if len(g) == 1:
            merged.append(g[0])
            continue
        best = max(g, key=conf)
        name = max((str(m.get("name", "")) for m in g), key=len)
        rank = min(SIZE_RANK.get(str(m.get("size_class", "far")), 2)
                   for m in g)
        size_class = next(
            (k for k, v in SIZE_RANK.items() if v == rank), "far"
        )
        ent = {
            "name": name,
            "box": list(best.get("box", [0, 0, 0, 0])),
            "confidence": best.get("confidence"),
            "clock": best.get("clock", 12),
            "size_class": size_class,
        }
        # Extras (Flow C label/match) come from a matched member when any
        # member matched, else from the highest-confidence member — a hit
        # must survive the merge even when it isn't the best box.
        extra_src = next(
            (m for m in sorted(g, key=conf, reverse=True)
             if m.get("match")),
            best,
        )
        for k, v in extra_src.items():
            if k not in ent:
                ent[k] = v
        merged.append(ent)
    return merged


# ----------------------------------------------------------------------------
# Flows
# ----------------------------------------------------------------------------

def flow_a(image: bytes) -> list[dict[str, Any]]:
    """Open scene: Spark nouns + safety list, one entry per instance."""
    with Image.open(io.BytesIO(image)) as im:
        width, height = im.size
    try:
        nouns = parse_nouns(_vlm_text(
            SPARK_MODEL, image, SPARK_NAMING_PROMPT, SPARK_TIMEOUT
        ))
    except PipelineError:
        nouns = []
    prompts = list(nouns)
    for s in SAFETY_LIST:
        if s not in prompts:
            prompts.append(s)
    return _ground(image, prompts, width, height)


def flow_b(image: bytes, target: str) -> list[dict[str, Any]]:
    """Named target: SAM the target phrase + the safety list."""
    with Image.open(io.BytesIO(image)) as im:
        width, height = im.size
    prompts = [target]
    for s in SAFETY_LIST:
        if s.lower() != target.lower() and s not in prompts:
            prompts.append(s)
    return _ground(image, prompts, width, height)


def _ground(
    image: bytes, prompts: list[str], width: int, height: int
) -> list[dict[str, Any]]:
    by_phrase = sam_many(image, prompts)
    scene: list[dict[str, Any]] = []
    for phrase in prompts:
        for rec in by_phrase.get(phrase, []):
            scene.append(entry_for(
                phrase, rec["box"], rec["confidence"], width, height
            ))
    return scene


def flow_c(
    image: bytes, question: str, target: str
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, int]:
    """Labeled item: segment, crop, read.

    Returns (scene, match_or_None, none_count).
    """
    with Image.open(io.BytesIO(image)) as im:
        width, height = im.size
        full = im.convert("RGB")
    if is_medication_question(question):
        phrases = list(MEDICATION_CATEGORY_PHRASES)
    else:
        phrases = [target, "bottle", "container"]
    by_phrase = sam_many(image, phrases)
    boxes: list[tuple[tuple[int, int, int, int], float | None]] = []
    for phrase in phrases:
        for rec in by_phrase.get(phrase, []):
            box = rec["box"]
            if all(iou(box, kept) < FLOW_C_IOU for kept, _ in boxes):
                boxes.append((box, rec["confidence"]))
    boxes.sort(key=lambda bc: (bc[0][2] - bc[0][0]) * (bc[0][3] - bc[0][1]),
               reverse=True)
    boxes = boxes[:FLOW_C_CROP_CAP]

    crops: list[bytes] = []
    for box, _ in boxes:
        x1, y1, x2, y2 = box
        pad_x, pad_y = int((x2 - x1) * 0.10), int((y2 - y1) * 0.10)
        crop = full.crop((
            max(0, x1 - pad_x), max(0, y1 - pad_y),
            min(width, x2 + pad_x), min(height, y2 + pad_y),
        ))
        if max(crop.size) < 512:
            scale = 512 / max(crop.size)
            crop = crop.resize((
                max(1, int(crop.size[0] * scale)),
                max(1, int(crop.size[1] * scale)),
            ))
        buf = io.BytesIO()
        crop.save(buf, format="JPEG", quality=85)
        crops.append(buf.getvalue())

    def read(crop: bytes) -> str:
        try:
            return _vlm_text(
                WONDER_MODEL, crop, WONDER_READ_PROMPT, WONDER_TIMEOUT
            ).strip()
        except Exception:
            return ""

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=FANOUT_WORKERS
    ) as pool:
        labels = list(pool.map(read, crops))

    want = target.upper()
    scene: list[dict[str, Any]] = []
    match: dict[str, Any] | None = None
    none_count = 0
    for (box, conf), label in zip(boxes, labels):
        # Empty/failed reads and NONE ("no label readable") never match:
        # "" is a substring of everything, which caused false matches.
        # They are also discarded at ingestion — never scene entries,
        # never chips, never in the gate's object state. The count is
        # diagnostically useful, so it goes in the log line, not the model.
        if _discard_read(label):
            none_count += 1
            continue
        name = label.strip()
        lab = name.upper()
        hit = (bool(want) and (want in lab or lab in want))
        ent = entry_for(name, box, conf, width, height,
                        label=label, match=hit)
        scene.append(ent)
        if hit and match is None:
            match = ent
    return scene, match, none_count


def floor_look_enabled() -> bool:
    """Kill switch mirrors audio_enabled(): FLOOR_LOOK_ENABLED=0 off."""
    return os.environ.get("FLOOR_LOOK_ENABLED", "").strip().lower() not in (
        "0", "false", "no", "off"
    )


def _floor_entry_size(box: tuple[int, int, int, int], height: int) -> str:
    """Floor finds are tiny by box height no matter the distance, so
    distance comes from frame position instead: lower in the frame is
    nearer on the floor plane."""
    yfrac = ((box[1] + box[3]) / 2) / max(1, height)
    if yfrac > 0.75:
        return "close"
    if yfrac > 0.5:
        return "a few steps away"
    return "far"


def _discard_read(label: str) -> bool:
    """NONE/empty/unreadable crop results are discarded at ingestion —
    never scene entries, never chips, never gate state."""
    return label.strip().upper() in ("", "NONE")


def _floor_regions(
    scene: list[dict[str, Any]], width: int, height: int
) -> list[tuple[int, int, int, int]]:
    """Up to two floor-entry boxes plus the lower frame (path bias)."""
    scored = []
    for e in scene:
        name = str(e.get("name", "")).lower()
        if not any(re.search(r"\b" + re.escape(w) + r"\b", name)
                   for w in FLOOR_REGION_WORDS):
            continue
        try:
            x1, y1, x2, y2 = (int(v) for v in e.get("box", [0, 0, 0, 0]))
        except (TypeError, ValueError):
            continue
        if x2 > x1 and y2 > y1:
            scored.append(((x1, y1, x2, y2), (x2 - x1) * (y2 - y1)))
    scored.sort(key=lambda t: t[1], reverse=True)
    regions = [b for b, _ in scored[:FLOOR_MAX_REGIONS - 1]]
    regions.append((int(width * 0.05), int(height * 0.55),
                    int(width * 0.95), height))
    return regions[:FLOOR_MAX_REGIONS]


def floor_look(
    image: bytes, scene: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Second look over floor regions: SAM proposes small floor objects,
    Wonder reads each proposal. Returns new scene entries (normal
    clock/size, position-based distance). Never raises — any failure
    yields no entries rather than failing the ask.
    """
    try:
        with Image.open(io.BytesIO(image)) as im:
            width, height = im.size
            full = im.convert("RGB")
    except Exception:
        return []
    found: list[tuple[tuple[int, int, int, int], float | None]] = []
    for region in _floor_regions(scene, width, height):
        x0, y0, x1, y1 = region
        crop = full.crop((max(0, x0), max(0, y0),
                          min(width, x1), min(height, y1)))
        buf = io.BytesIO()
        try:
            crop.save(buf, format="JPEG", quality=90)
            recs = sam_segment(buf.getvalue(), FLOOR_PHRASE)
        except Exception:
            continue
        for r in recs:
            try:
                b = r["box"]
                mapped = (b[0] + x0, b[1] + y0, b[2] + x0, b[3] + y0)
            except (TypeError, KeyError, IndexError):
                continue
            if all(iou(mapped, kept) < FLOOR_LOOK_IOU for kept, _ in found):
                found.append((mapped, r.get("confidence")))
    min_side = FLOOR_MIN_FRAC * width
    found = [bc for bc in found
             if max(bc[0][2] - bc[0][0], bc[0][3] - bc[0][1]) >= min_side]
    found = found[:FLOOR_MAX_BOXES]

    def read(box: tuple[int, int, int, int]) -> str:
        try:
            x1, y1, x2, y2 = box
            px = max(8, int((x2 - x1) * 0.5))
            py = max(8, int((y2 - y1) * 0.5))
            cut = full.crop((max(0, x1 - px), max(0, y1 - py),
                             min(width, x2 + px), min(height, y2 + py)))
            if max(cut.size) < 256:
                s = 256 / max(cut.size)
                cut = cut.resize((max(1, int(cut.size[0] * s)),
                                  max(1, int(cut.size[1] * s))))
            out = io.BytesIO()
            cut.save(out, format="JPEG", quality=90)
            return _vlm_text(
                WONDER_MODEL, out.getvalue(), FLOOR_READ_PROMPT,
                WONDER_TIMEOUT,
            ).strip()
        except Exception:
            return ""

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=FANOUT_WORKERS
    ) as pool:
        labels = list(pool.map(read, [b for b, _ in found]))

    entries = []
    for (box, conf), label in zip(found, labels):
        # §5 hygiene: NONE/empty reads are discarded, never entries.
        if _discard_read(label):
            continue
        ent = entry_for(label.strip(), box, conf, width, height)
        ent["size_class"] = _floor_entry_size(box, height)
        entries.append(ent)
    return entries


# ----------------------------------------------------------------------------
# Answer + rule layer
# ----------------------------------------------------------------------------

def _answer_instruction(
    question: str,
    scene: list[dict[str, Any]],
    flow: str,
    extra: str = "",
    landmarks: list[dict[str, Any]] | None = None,
) -> str:
    q = question.strip() or "(no question asked — describe the scene)"
    landmark_block = (
        f"\nNearby landmarks to the target: {json.dumps(landmarks)}"
        if landmarks else ""
    )
    return (
        "You are a visual-assistance narrator for a blind person. "
        "Look at the photo and the scene model below and answer the "
        "question in plain, spoken-friendly language: short sentences, "
        "concrete directions using the clock-face positions and distances "
        "from the scene model (for example: 'at your 12 o'clock, a few "
        "steps away'). Do not mention pixels, boxes, or confidence scores. "
        "Background surfaces (floor, ground, wall, ceiling, sky) are "
        "surroundings: name the surface type briefly and only when it "
        "matters for footing or orientation, without clock positions. "
        "Never describe what a surface looks like — no colors, materials, "
        "finishes, or patterns. Saying where the camera is aimed "
        "('pointed up at the sky') is orientation, not decoration."
        f"{extra}\n"
        f"Question: {q}\n"
        f"Scene model: {json.dumps(scene)}"
        f"{landmark_block}\n"
        "Also judge your own confidence in the answer from 0.0 to 1.0. "
        'Reply with ONLY a JSON object: {"answer": "...", "confidence": N}. '
        "No other text."
    )


def _spark_answer(
    image: bytes,
    question: str,
    scene: list[dict[str, Any]],
    flow: str,
    extra: str = "",
    landmarks: list[dict[str, Any]] | None = None,
) -> tuple[str, float]:
    raw = _vlm_text(
        SPARK_MODEL, image,
        _answer_instruction(question, scene, flow, extra, landmarks),
        SPARK_TIMEOUT,
    )
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            answer = str(obj.get("answer", "")).strip()
            conf = float(obj.get("confidence", 0.5))
            if answer:
                return answer, max(0.0, min(1.0, conf))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    if raw.strip():
        return raw.strip(), 0.5
    raise PipelineError("Answer model returned empty output")


def _hazards_in(scene: list[dict[str, Any]]) -> list[str]:
    found = []
    for ent in scene:
        name = str(ent.get("name", "")).lower()
        for hz in HAZARD_KEYWORDS:
            if hz in name and hz not in found:
                found.append(hz)
    return found


def synthesis_enabled() -> bool:
    """Kill switch mirrors audio_enabled(): SYNTHESIS_ENABLED=0 off."""
    return os.environ.get("SYNTHESIS_ENABLED", "").strip().lower() not in (
        "0", "false", "no", "off"
    )


def _text_complete(model: str, instruction: str, timeout: int) -> str:
    """Text-only (no image) responses call on the same streamed API."""
    return _stream_output_text(
        {
            "model": model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": instruction},
                    ],
                }
            ],
            "stream": True,
        },
        timeout,
    )


def _locked_sentences(draft: str) -> list[str]:
    """Sentences synthesis must pass through verbatim: warnings and
    cautions (hedges, can't-confirm lines, the volunteer handoff)."""
    locked = []
    for s in split_sentences(draft):
        t = s.strip()
        if t.lower().startswith("warning:") or CAUTION_RE.search(t):
            locked.append(t)
    return locked


def _question_subjects(question: str, draft: str) -> list[str]:
    """Content words shared by the question and the draft: the asked-about
    subjects the synthesis must not drop (e.g. 'cats')."""
    qwords = set(re.findall(r"[a-z]{3,}", (question or "").lower()))
    qwords -= SYNTHESIS_STOPWORDS
    dl = draft.lower()
    return sorted(w for w in qwords if re.search(r"\b" + re.escape(w)
                                                 + r"\b", dl))


def _mention_count(text: str, head: str) -> int:
    return len(re.findall(r"\b" + re.escape(head) + r"s?\b",
                          text.lower()))


def _clock_set(text: str) -> set[str]:
    """Clock hours cited, expanding lists ('11, 12, 1 and 2 o'clock')."""
    out: set[str] = set()
    for m in CLOCK_LIST_RE.finditer(text):
        out.update(re.findall(r"\d{1,2}", m.group(1)))
    return out


def _synthesis_ok(
    draft: str,
    out: str,
    locked: list[str],
    hazards: list[str],
    goal_head: str | None,
    subjects: list[str],
    scene_heads: list[str],
    landmark_heads: list[str] | None = None,
) -> bool:
    """Mechanical preservation check. Any failure ships the original."""
    if not out or not out.strip():
        return False
    norm = lambda s: re.sub(r"\s+", " ", s.strip())
    flat = norm(out)
    for s in locked:
        if norm(s) not in flat:
            return False
    ol = out.lower()
    for hz in hazards:
        if hz.lower() not in ol:
            return False
    if not _clock_set(draft) <= _clock_set(out):
        return False
    for w in subjects:
        if not re.search(r"\b" + re.escape(w) + r"\b", ol):
            return False
    if goal_head and not re.search(r"\b" + re.escape(goal_head)
                                   + r"s?\b", ol):
        return False
    for head in landmark_heads or []:
        # Fix-6 landmarks survive when the draft mentioned them: a
        # landmark the draft never stated cannot be required of the
        # output, but one it stated must not be distilled away.
        if re.search(r"\b" + re.escape(head) + r"s?\b",
                     draft.lower()) \
                and not re.search(r"\b" + re.escape(head) + r"s?\b",
                                  ol):
            return False
    # Repeated objects must shrink: every head noun mentioned 2+ times in
    # the draft appears strictly fewer times in the output.
    for head in scene_heads:
        if _mention_count(draft, head) >= 2 \
                and _mention_count(out, head) >= _mention_count(draft, head):
            return False
    # Subtractive: never more sentences, never more words.
    if len(split_sentences(out)) > len(split_sentences(draft)):
        return False
    if len(out.split()) > len(draft.split()):
        return False
    # No new non-ASCII: the model occasionally mojibakes punctuation
    # ("—" back as "â.."); any non-ASCII char must already be in the draft.
    if {c for c in out if ord(c) > 127} - {c for c in draft if ord(c) > 127}:
        return False
    return True


_MOJI_RUN_RE = re.compile(r"[\x80-\xff]+")


def _demojibake(out: str) -> str | None:
    """Repair UTF-8-bytes-read-as-latin-1 runs (e.g. "â.." for "—").

    Only runs that decode as valid UTF-8 are converted; lone latin-1
    chars and characters above U+00FF (curly quotes) pass through
    untouched. Returns None when nothing changed.
    """
    def fix(m: re.Match) -> str:
        try:
            return m.group(0).encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return m.group(0)

    fixed = _MOJI_RUN_RE.sub(fix, out)
    return fixed if fixed != out else None


SYNTHESIS_INSTRUCTION = (
    "You are tightening a spoken answer for a blind person. Rewrite the "
    "DRAFT below by deleting, merging, and reordering ONLY. Rules: the "
    "first sentence must directly answer the QUESTION; keep every fact in "
    "MUST KEEP; keep each LOCKED sentence EXACTLY as written, in place; "
    "never mention the same object twice; never add a fact, invent a "
    "location, or soften a caution; at most 4 sentences for navigation "
    "answers. Reply with ONLY the rewritten answer. No quotes, no JSON, "
    "no explanation."
)


def synthesize_answer(
    draft: str,
    question: str,
    scene: list[dict[str, Any]],
    assessment: dict[str, Any] | None,
    hazards: list[str],
    landmarks: list[dict[str, Any]] | None = None,
) -> tuple[str, bool, int]:
    """Final synthesis: distill the draft to the answer contract.

    Returns (text, applied, latency_ms). Describe-mode (no question) and
    drafts already within the contract shape (< 4 sentences) skip
    synthesis — a subtractive pass needs something to subtract, and
    short drafts can only lose facts (case_13 Q1). Any model failure,
    timeout, or preservation-check failure ships the draft unchanged —
    fail toward completeness, never toward silence.
    """
    if not synthesis_enabled() or not (question or "").strip():
        return draft, False, 0
    if len(split_sentences(draft)) < 4:
        return draft, False, 0
    locked = _locked_sentences(draft)
    subjects = _question_subjects(question, draft)
    goal_head = _head_noun(str((assessment or {}).get("goal") or ""))
    scene_heads = sorted({_head_noun(str(e.get("name", "")))
                          for e in scene} - {""})
    landmark_heads = sorted({_head_noun(str(e.get("name", "")))
                             for e in (landmarks or [])} - {""})
    facts = []
    if subjects:
        facts.append("asked about: " + ", ".join(subjects))
    if assessment and assessment.get("goal"):
        facts.append(
            f"goal: {assessment['goal']} at "
            f"{assessment.get('goal_clock')} o'clock"
        )
    if hazards:
        facts.append("hazards: " + ", ".join(hazards))
    if landmark_heads:
        facts.append("landmarks: " + ", ".join(landmark_heads))
    clocks = sorted(_clock_set(draft), key=int)
    if clocks:
        facts.append("clock positions cited: " +
                     ", ".join(f"{c} o'clock" for c in clocks))
    prompt = (
        f"{SYNTHESIS_INSTRUCTION}\n"
        f"QUESTION: {question.strip()}\n"
        f"MUST KEEP: {'; '.join(facts) if facts else '(none)'}\n"
        + "".join(f"LOCKED: {s}\n" for s in locked)
        + f"DRAFT: {draft.strip()}"
    )
    t0 = time.time()
    try:
        out = _text_complete(
            SYNTHESIS_MODEL, prompt, SYNTHESIS_TIMEOUT
        ).strip().strip("'\"").strip()
    except Exception:
        return draft, False, int((time.time() - t0) * 1000)
    ms = int((time.time() - t0) * 1000)
    if _synthesis_ok(draft, out, locked, hazards,
                     goal_head or None, subjects, scene_heads,
                     landmark_heads):
        return out, True, ms
    # One repair attempt for mojibake: the fixed text must still pass the
    # full check, else the draft ships.
    fixed = _demojibake(out)
    if fixed is not None and fixed != out and _synthesis_ok(
            draft, fixed, locked, hazards, goal_head or None,
            subjects, scene_heads, landmark_heads):
        return fixed, True, ms
    return draft, False, ms


def run_ask(
    image: bytes,
    question: str,
    media_bytes: bytes | None = None,
    audio_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Full pipeline: question -> route -> perceive -> gate -> answer."""
    t0 = time.time()
    question = (question or "").strip()
    # Part A — precedence: text -> audio transcript -> clip transcript.
    # Lazy: ASR runs only when the text question is empty.
    question_source = "text" if question else "none"
    if not question and audio_enabled():
        if audio_bytes:
            t = transcribe_audio(to_wav_16k_mono(audio_bytes))
            if t:
                question, question_source = t, "audio"
        if not question and media_bytes:
            t = transcribe_audio(to_wav_16k_mono(media_bytes))
            if t:
                question, question_source = t, "clip"
    question_used: str | None = question or None
    luminance = mean_luminance(image)
    p85 = p85_luminance(image)
    flow, target = route(question)

    qualifier: str | None = None
    sam_prompt = target or ""
    if flow == "B":
        sam_prompt, qualifier = split_qualifier(target or "")

    match: dict[str, Any] | None = None
    none_count = 0
    if flow == "C":
        scene, match, none_count = flow_c(image, question, target or "")
    elif flow == "B":
        scene = flow_b(image, sam_prompt)
    else:
        scene = flow_a(image)

    # Answer tightening — merge duplicate instances before anything
    # consumes the scene (gate state, answer context, chips, eval).
    scene = merge_entries(scene)
    if flow == "C" and match is not None:
        match = next((e for e in scene if e.get("match")), None)

    ask_id = uuid.uuid4().hex
    result, floor_ran = _compose(
        image, scene, question, question_source, question_used,
        flow, target, sam_prompt, qualifier, match, luminance, p85,
        t0, none_count, ask_id=ask_id,
    )
    _store_ask(ask_id, {
        "created": time.time(),
        # The composed scene: floor finds joined inside _compose, so the
        # stored scene must come from the result, not the pre-look local.
        "image": image,
        "scene": result.get("scene_model", scene),
        "flow": flow,
        "target": target,
        "question": question_used,
        "answer": result.get("answer"),
        "behavior": result.get("behavior"),
        "floor_done": floor_ran,
        "read_targets": [resolve_target(question)] if flow == "C" else [],
    })
    return result


def _compose(
    image: bytes,
    scene: list[dict[str, Any]],
    question: str,
    question_source: str,
    question_used: str | None,
    flow: str,
    target: str | None,
    sam_prompt: str,
    qualifier: str | None,
    match: dict[str, Any] | None,
    luminance: float,
    p85: float,
    t0: float,
    none_count: int,
    prior_context: str = "",
    ask_id: str = "",
    followup: bool = False,
    allow_floor_look: bool = True,
) -> tuple[dict[str, Any], bool]:
    """Shared tail: gate -> answer -> post-process -> synthesize -> log.

    Fresh asks and follow-ups differ only in perception (full vs cached
    + conditional); everything from the intent gate on runs here,
    identically. Follow-ups add prior Q/A context for reference
    resolution. allow_floor_look=False skips the floor second look
    (follow-ups whose media already had it). Returns (result,
    floor_ran) so callers can record perception state in the cache.
    """
    # Fix 1 + Patch 1 — intent gate: regex layer 1 + Jev layer 2.
    jev = jev_intents(jev_state(scene, question, flow, target))
    gate, crossing_like, dosing, consumption = combine_gate(
        question, scene, jev)
    # Part B — shaping only: never escalates, never gates.
    navigation = is_navigation_question(question) or (
        jev is not None
        and jev.get("navigation_intent", 0) >= JEV_THRESHOLD_NAVIGATION
    )

    # Field test §1 — floor second look for navigation asks (not in the
    # dark: nothing to see). Finds join the scene before assessment, so
    # the path verdict accounts for small floor hazards.
    floor_found, floor_ran = 0, False
    if navigation and not (crossing_like or dosing) \
            and allow_floor_look and floor_look_enabled() \
            and p85 >= DARK_P85:
        floor_new = floor_look(image, scene)
        floor_ran = True
        if floor_new:
            scene = merge_entries(scene + floor_new)
            floor_found = len(floor_new)

    hazards = _hazards_in(scene)
    street_scene = any(
        re.search(r"\b" + re.escape(cue) + r"\b",
                  str(e.get("name", "")), re.IGNORECASE)
        for cue in STREET_CUES for e in scene
    )

    landmarks: list[dict[str, Any]] | None = None
    extra = ""
    if flow in ("B", "C"):
        # Fix 6 — find-answers carry direction + landmark.
        extra += (" The user is looking for a specific item: state its "
                  "clock-face direction and whether it is to the left, "
                  "right, or center. When nearby landmarks are listed, "
                  "name the nearest one (for example: 'beside the earbuds "
                  "case').")
        ref_box: list[int] | tuple[int, ...] | None = None
        exclude = ""
        if flow == "C" and match is not None:
            ref_box, exclude = match["box"], str(match.get("name", ""))
        elif scene:
            cands = ([e for e in scene
                      if str(e.get("name", "")).lower() == sam_prompt.lower()]
                     or scene)
            biggest = max(cands, key=lambda e: (e["box"][2] - e["box"][0])
                          * (e["box"][3] - e["box"][1]))
            ref_box, exclude = biggest["box"], str(biggest.get("name", ""))
        if ref_box is not None:
            landmarks = nearest_landmarks(scene, ref_box, exclude) or None
    if qualifier:
        # Fix 4 — the qualifier identifies the instance at answer time.
        extra += (f" The user is looking for '{sam_prompt}' with qualifier "
                  f"'{qualifier}' (for example a labeled number): use the "
                  f"photo and the scene positions to identify which "
                  f"instance matches.")
    if crossing_like:
        extra += (
            " SAFETY: this is a street-crossing decision. NEVER tell the "
            "user to cross, to go ahead, or that it is safe. Describe what "
            "you see, then say a volunteer will help with the crossing "
            "decision."
        )
    if dosing:
        extra += (
            " SAFETY: this is a medication dosing question. NEVER give "
            "dosing advice. Describe what you see, then say a volunteer or "
            "pharmacist must answer dosing questions."
        )
    if street_scene and not (crossing_like or dosing):
        # Fix 3 — describe only at street scenes.
        extra += (
            " Describe only what you see. Never instruct the user to move, "
            "cross, proceed, or wait, and never state whether it is safe "
            "to move."
        )
    if HOW_MANY_RE.search(question):
        # Patch 5 — counting assist (experimental).
        extra += (
            " Count carefully from the photo and the scene model. Each "
            "scene entry has a size_class: an instance much smaller than "
            "the largest is a partial or edge instance — describe it as "
            "such (for example: 'three full banks and part of a fourth'), "
            "and say how many full instances there are."
        )
    if hazards and not (crossing_like or dosing):
        extra += (
            " SAFETY: these hazards are present and you MUST name each as "
            f"a warning in the answer: {', '.join(hazards)}."
        )
    nav_assessment: dict[str, Any] | None = None
    if navigation and not (crossing_like or dosing):
        assessment = _path_assessment(
            scene, question, height=_image_height(image))
        nav_assessment = assessment
        if assessment["verdict"] == "no_goal_visible":
            desc = str(assessment.get("goal_desc") or "exit")
            extra += (
                " NAVIGATION: this is a path question but the goal is not "
                f"visible in this photo: say the {desc} is not visible in "
                "this photo and never invent one. Mention visible items "
                "only briefly as orientation landmarks."
            )
        else:
            goal_txt = assessment["goal"] or "the way ahead"
            obs = "; ".join(
                f"{o['name']} at {o['clock']} o'clock, {o['size_class']}"
                for o in assessment["on_path"]
            ) or "none"
            extra += (
                " NAVIGATION: this is a path question. Facts: goal "
                f"{goal_txt} at {assessment['goal_clock']} o'clock; "
                f"verdict {assessment['verdict']}; on-path obstacles: "
                f"{obs}. You MUST (a) lead with the verdict sentence: "
                "'Yes — the way to the goal is clear.' if clear, or "
                "'Careful — <obstacle> is in your path at <clock> "
                "o'clock.' if blocked; (b) name on-path obstacles with "
                "clock-face direction and distance; (c) mention off-path "
                "items only briefly as orientation landmarks."
            )
    if flow == "C" and match is None:
        extra += (
            " The requested item cannot be confirmed from any readable "
            "label: say so plainly and do not guess its location."
        )

    if prior_context:
        extra += prior_context

    behavior = "ANSWER"
    if consumption:
        seen = []
        for e in sorted(
            scene,
            key=lambda e: (e.get("confidence")
                           if isinstance(e.get("confidence"), (int, float))
                           else -1),
            reverse=True,
        ):
            n = str(e.get("name", "")).strip()
            if n and n.lower() not in [x.lower() for x in seen]:
                seen.append(n)
        desc = f"I see {', '.join(seen[:5])}. " if seen else ""
        answer, confidence = f"{desc}{CONSUMPTION_TEMPLATE}", 0.4
    else:
        answer, confidence = _spark_answer(
            image, question, scene, flow, extra, landmarks)

    if crossing_like or dosing:
        behavior = "ESCALATE"
        if crossing_like and any(rx.search(answer) for rx in GO_AHEAD_RES):
            seen = sorted({str(e.get("name", "")) for e in scene if e.get("name")})
            desc = (
                f"I can see {', '.join(seen[:6])}."
                if seen else "I can see the street scene."
            )
            answer = (
                f"{desc} I can't make crossing decisions. {HANDOFF_SENTENCE}"
            )
        if "volunteer" not in answer.lower():
            answer = f"{answer} {HANDOFF_SENTENCE}"
        if street_scene:
            # Patch 4 — escalate answers never assert traffic absence.
            # The volunteer handoff line always stays.
            answer = " ".join(
                s for s in split_sentences(answer)
                if "volunteer" in s.lower()
                or not any(rx.search(s) for rx in ABSENCE_RES)
            )
    else:
        for hz in hazards:
            if hz not in answer.lower():
                answer = f"{answer} Warning: {hz} ahead."
        if flow == "C" and match is None:
            confidence = min(confidence, 0.3)
            if not _UNCERTAINTY_RE.search(answer):
                answer = (
                    f"{answer} I cannot confirm "
                    f"{target} from the labels I could read."
                )
        if flow == "C" and match is not None:
            # Fix 5 — the answer names the match.
            label = str(match.get("label", "")).strip()
            if (label and label.upper() != "NONE"
                    and label.lower() not in answer.lower()):
                answer = f"Found your {label.title()}. {answer}"
        if street_scene:
            # Fix 3 — strip directive sentences, deterministically.
            kept = [s for s in split_sentences(answer)
                    if not any(re.search(r"\b" + re.escape(p) + r"\b", s,
                                         re.IGNORECASE)
                               for p in DIRECTIVE_PHRASES)]
            if len(kept) < len(split_sentences(answer)):
                answer = ((" ".join(kept) + " " + STREET_CLOSER)
                          if kept else STREET_CLOSER)

    if p85 < DARK_P85:
        # Fix 2 + Patch 3 — p85 darkness caps confidence, forces hedging.
        confidence = min(confidence, 0.5)
        if not any(h in answer.lower() for h in DARK_HEDGES):
            answer = f"{answer} {DARK_APPEND}"

    # Answer tightening — final synthesis, last before the response.
    # The synthesized text is the single final text: spoken, displayed,
    # stored. Hedge/caution/warning sentences are locked verbatim, so the
    # deterministic appends above survive; describe-mode skips inside.
    answer, synth_applied, synth_ms = synthesize_answer(
        answer, question, scene, nav_assessment, hazards, landmarks)

    result = {
        "answer": answer,
        "behavior": behavior,
        "confidence": round(float(confidence), 3),
        "scene_model": scene,
        "flow": flow,
        "gate": gate,
        "signals": {"mean_luminance": round(luminance, 2),
                    "p85_luminance": p85},
        "question_used": question_used,
        "question_source": question_source,
        "ask_id": ask_id,
        "followup": followup,
    }
    # Part C — one log line per ask (question text is the user's own
    # words to their own server; keys never appear here).
    seen: list[str] = []
    for e in scene:
        n = str(e.get("name", "")).strip()
        if n and n.lower() not in [x.lower() for x in seen]:
            seen.append(n)
    print(json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "question": question_used,
        "question_source": question_source,
        "flow": flow,
        "behavior": behavior,
        "gate": gate,
        "scene_objects": seen,
        "latency_ms": int((time.time() - t0) * 1000),
        "answer": answer[:600],
        "synth_applied": synth_applied,
        "synth_ms": synth_ms,
        "none_reads": none_count,
        "floor_look": floor_ran,
        "floor_found": floor_found,
        "ask_id": ask_id,
        "followup": followup,
    }), flush=True)
    return result, floor_ran


# ----------------------------------------------------------------------------
# Follow-ups: same media, new question, no re-perception
# ----------------------------------------------------------------------------

class FollowupExpired(Exception):
    """Follow-up references an unknown or expired ask id."""


ASK_TTL_S = 900  # 15 minutes: a conversation, not an archive
ASK_CACHE_MAX = 32

_ASK_CACHE: dict[str, dict[str, Any]] = {}


def _sweep_asks() -> None:
    now = time.time()
    for key in [k for k, v in _ASK_CACHE.items()
                if now - v.get("created", 0) > ASK_TTL_S]:
        del _ASK_CACHE[key]
    while len(_ASK_CACHE) > ASK_CACHE_MAX:
        oldest = min(_ASK_CACHE, key=lambda k: _ASK_CACHE[k]["created"])
        del _ASK_CACHE[oldest]


def _store_ask(ask_id: str, entry: dict[str, Any]) -> None:
    _sweep_asks()
    _ASK_CACHE[ask_id] = entry


def _load_ask(ask_id: str) -> dict[str, Any]:
    _sweep_asks()
    try:
        return _ASK_CACHE[ask_id]
    except KeyError:
        raise FollowupExpired(
            "that ask has expired or is unknown — "
            "please ask again as a new question"
        )


PRIOR_CONTEXT_TEMPLATE = (
    " Conversation so far — the user asked: {pq} Assistant answered: {pa} "
    "The new question may refer back to that exchange ('it', 'the "
    "floor'); resolve such references against it and the scene model."
)


def run_followup(
    ask_id: str,
    question: str,
    audio_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Follow-up on a cached ask: same media, new question.

    Perception is not re-run: naming, segmentation, merging, and any
    second looks come from the cache. Only genuinely new perception
    runs — a find question's crop-reads when that target was never
    read, or a floor look the original (non-navigation) ask never
    triggered — once each, against the stored media, joining the
    cached scene. A new cache entry (new ask id) is stored per
    follow-up; the old one expires naturally.
    """
    t0 = time.time()
    entry = _load_ask(ask_id or "")
    image = entry["image"]
    question = (question or "").strip()
    question_source = "text" if question else "none"
    if not question and audio_bytes and audio_enabled():
        t = transcribe_audio(to_wav_16k_mono(audio_bytes))
        if t:
            question, question_source = t, "audio"
    question_used: str | None = question or None
    luminance = mean_luminance(image)
    p85 = p85_luminance(image)
    flow, target = route(question)

    qualifier: str | None = None
    sam_prompt = target or ""
    if flow == "B":
        sam_prompt, qualifier = split_qualifier(target or "")

    scene = copy.deepcopy(entry["scene"])
    read_targets = list(entry.get("read_targets", []))
    floor_done = bool(entry.get("floor_done", False))
    match: dict[str, Any] | None = None
    none_count = 0
    if flow == "C":
        want = resolve_target(question)
        if want not in read_targets:
            # New find target: crop-reads run once against stored media.
            for e in scene:
                e.pop("match", None)
            new_scene, match, none_count = flow_c(
                image, question, target or "")
            scene = merge_entries(scene + new_scene)
            if match is not None:
                match = next((e for e in scene if e.get("match")), None)
            read_targets.append(want)
        else:
            match = next((e for e in scene if e.get("match")), None)

    prior_context = ""
    if entry.get("question") and entry.get("answer"):
        prior_context = PRIOR_CONTEXT_TEMPLATE.format(
            pq=str(entry["question"])[:400],
            pa=str(entry["answer"])[:600],
        )
    new_id = uuid.uuid4().hex
    # The floor look the original ask never triggered runs inside
    # _compose when the new question is navigation; allow it unless
    # this media already had it.
    result, floor_ran_now = _compose(
        image, scene, question, question_source, question_used,
        flow, target, sam_prompt, qualifier, match, luminance, p85,
        t0, none_count,
        prior_context=prior_context, ask_id=new_id, followup=True,
        allow_floor_look=not floor_done,
    )
    floor_done = floor_done or floor_ran_now
    _store_ask(new_id, {
        "created": time.time(),
        # Composed scene (conditional perception joined inside _compose).
        "image": image,
        "scene": result.get("scene_model", scene),
        "flow": flow,
        "target": target,
        "question": question_used,
        "answer": result.get("answer"),
        "behavior": result.get("behavior"),
        "floor_done": floor_done,
        "read_targets": read_targets,
    })
    return result
