"""Alumbra ask pipeline — days 1-2 skeleton.

Photo + question in, spatially grounded answer out. Consolidates the
day-one throwaway probes (repo root: sam31-dayone-probe.py, flow_test.py,
crop_read.py) into one module behind POST /ask.

Flows (brief 2026-10-26):
  A — open scene: Spark names nouns, SAM grounds each + the safety list.
  B — named target: SAM the target phrase + the safety list.
  C — labeled item: SAM category boxes, crop each, Spark reads labels.

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
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageChops, ImageFilter, ImageStat

from . import capture, metric

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

API_BASE = os.environ.get("META_MODEL_API_BASE", "https://api.meta.ai/v1")
ENDPOINT = f"{API_BASE.rstrip('/')}/responses"
SAM_MODEL = "sam-3.1"
# Steering 2026-10-27: every Spark workload (naming, answer composition,
# crop reads) and synthesis run on the contributor tier for cost. That
# tier TRAINS ON INPUTS (photos included); Charles consented for his test
# data for this event only. SPARK_MODEL=muse-spark-1.3 reverts. Wonder
# (frontier agent model, $4/$20 per M, 20-28 s first byte) is no longer
# used. SAM and ASR have no contributor tier and are unchanged.
SPARK_MODEL = os.environ.get("SPARK_MODEL", "muse-spark-1.3-contributor")
CROP_READ_MODEL = SPARK_MODEL

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
READ_TIMEOUT = 60
FANOUT_WORKERS = 5
SAM_CONCURRENCY_DEFAULT = 6


def sam_concurrency() -> int:
    """Bound on concurrent SAM calls per fan-out (env SAM_CONCURRENCY)."""
    try:
        n = int(os.environ.get("SAM_CONCURRENCY", SAM_CONCURRENCY_DEFAULT))
    except ValueError:
        return SAM_CONCURRENCY_DEFAULT
    return n if n >= 1 else SAM_CONCURRENCY_DEFAULT

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
# Wall trim is the wall: a baseboard at the corridor's far end is not
# an obstruction (passage geometry reaches it; the clock band did not).
SURFACE_WORDS = {"floor", "ground", "wall", "ceiling", "sky",
                 "baseboard", "baseboards", "skirting", "molding",
                 "moulding"}
SIZE_RANK = {"close": 0, "a few steps away": 1, "far": 2}

# Answer tightening — segmentation echoes (the same object returned
# again, under the same or another phrase with the same head noun)
# collapse before answering. Only near-duplicate boxes are echoes:
# IoU >= MERGE_IOU, or the smaller box almost wholly inside the larger
# at a near-equal size. Distinct instances whose boxes merely overlap
# (cats curled together, adjacent mailbox banks) stay separate entries —
# counting reads the scene (live test 2026-10-27).
MERGE_IOU = 0.8
MERGE_CONTAIN = 0.9
MERGE_AREA_RATIO = 0.7

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
# A ramp for a pet or a toy is a thing on the floor, not a way you walk
# (live 2026-10-29: "cat scratcher ramp" at 0.9 m read as passable).
PET_TOY_WORDS = {
    "cat", "cats", "kitten", "dog", "dogs", "puppy", "pet", "pets",
    "scratcher", "scratching", "toy", "toys", "hamster", "rabbit",
}

# Rebuild 2026-10-27 — ground cover is what you walk on, never an
# obstruction (table walk: "Careful — patio is in your path"). Exact
# words, so "mattress" is not "mat" and "grassy" needs its own entry.
GROUND_WORDS = {
    "floor", "ground", "tile", "tiles", "tiled", "rug", "carpet",
    "lawn", "grass", "grassy", "verge", "turf", "sidewalk", "pavement",
    "paved", "path", "pathway", "footpath", "walkway", "patio", "deck",
    "driveway", "terrace", "concrete", "asphalt", "gravel", "dirt",
    "soil", "mulch", "sand", "field", "yard", "straw",
}
# A ground mention earns its place only when it matters for footing.
FOOTING_RE = re.compile(
    # "a few steps away" is a distance, not a step: only a step up/down,
    # "a step", or "watch your step" is footing.
    r"\b(wet|slippery|slick|icy|ice|uneven|cracked|cracks?|loose|bumpy|"
    r"stairs?|curbs?|kerbs?|edges?|drop|slopes?|sloped|ramps?|"
    r"transition|changes?|footing|trip|puddles?|mud|muddy)\b|"
    r"\bsteps?\s+(?:up|down)\b|\ba\s+step\b|\bwatch\s+your\s+step\b",
    re.IGNORECASE,
)
# Questions about the ground itself ("what am I stepping on?"): the
# surface is the answer, not a route verdict.
SURFACE_Q_RE = re.compile(
    r"\bstep(?:ping)?\s+on\b|\bstanding\s+on\b|\bwalking\s+on\b|"
    r"\bunder\s?foot\b|\bwhat(?:'s| is) the (?:ground|floor|surface)\b",
    re.IGNORECASE,
)
# Rule 3 — distance questions lead with the distance.
DISTANCE_RE = re.compile(
    r"\bhow\s+(?:far|close|near)\b|"
    r"\bhow\s+many\s+(?:steps|feet|meters|metres|yards)\b",
    re.IGNORECASE,
)
DISTANCE_TO_RE = re.compile(
    r"\bhow\s+(?:far|close|near)\b.*?\bto\s+(?:the|my|a|an)?\s*"
    r"([a-z][a-z' ]+?)(?:\?|$|,|\.|!)", re.IGNORECASE,
)
DISTANCE_SUBJECT_RE = re.compile(
    r"\bhow\s+(?:far|close|near)\s+(?:away\s+)?(?:is|are|'s)\s+"
    r"(?:the|my|a|an)?\s*([a-z][a-z' ]+?)(?:\?|$|,|\.|!)", re.IGNORECASE,
)
DISTANCE_TAIL_WORDS = {"away", "from", "me", "now", "here", "there"}
# A pronoun in the goal slot ("how far are they", "path to it") names
# the prior referent — the follow-up's subject.
PRONOUN_WORDS = {"it", "they", "them", "those", "these"}
DIST_PHRASE = {"close": "close", "a few steps away": "a few steps away",
               "far": "far away"}
# A sentence that re-locates a known object at a known clock adds
# nothing; one that relates it to something else adds a fact.
RELATION_RE = re.compile(
    r"\b(beside|below|above|under|underneath|next to|between|behind|"
    r"on top of|on the|in the|near|left of|right of|against|inside)\b",
    re.IGNORECASE,
)
PRONOUN_LEAD_RE = re.compile(
    r"^(?:it|they|these|those)(?:'s|'re|\s)", re.IGNORECASE)
# Trailing position words are not head nouns ("patio ahead" is a patio).
POSITION_TAIL_WORDS = {
    "ahead", "nearby", "behind", "overhead", "beyond", "below", "above",
    "outside", "inside",
}
# Code-written openings (navigation/distance) leave Spark at most this
# many body sentences, and at most one orientation landmark.
BODY_MAX_SENTENCES = 2
ORIENTATION_MAX = 1

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

# Live test 2026-10-28 — the route is tested against the PASSAGE, not the
# goal's clock. The corridor runs from the user's feet (frame bottom,
# CORRIDOR_NEAR of the width) to the passage's base: for a door, the
# opening you walk through — never the door leaf, which stands beside
# it. A floor object whose footprint (box base) is inside the corridor,
# or that lies in the opening itself, is on the route whatever its clock
# ("cat lying in the doorway at 10 o'clock" cleared while wall-side
# guitars at the leaf's clock were flagged).
CORRIDOR_NEAR = (0.3, 0.7)
# No goal: straight ahead to the far end of the frame (the old +-1 hour
# band around 12 o'clock).
CORRIDOR_AHEAD = (0.36, 0.64)
PASSAGE_TOL = 0.03          # of image height: just past the base counts
PASSAGE_BAND = 0.25         # lower part of the opening: lying in it
# The overlap must be this much of the narrower of footprint and corridor:
# a box edge grazing it (a chair arm at the frame edge) is beside you,
# while a small thing (an ice cube) wholly inside is on the route.
CORRIDOR_MIN_OVERLAP = 0.1
DOOR_GOAL_WORDS = {"door", "doors", "doorway", "exit", "entrance",
                   "entry", "gate"}
OPENING_WORDS = {"doorway", "hallway", "corridor", "opening", "entrance",
                 "entryway", "hall"}
OPENING_PHRASE = "open doorway"


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
# pass, so SAM proposes directly over floor regions and Spark reads
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
# Counting (live test 2026-10-27): a count is the number of scene
# instances, decided in code — never the model's own tally. The subject
# is the noun phrase after "how many" / "count the", cut at the verb.
COUNT_RE = re.compile(
    r"\b(?:how\s+many|count\s+(?:all\s+)?(?:the|my|of\s+the))\s+(.+)",
    re.IGNORECASE,
)
COUNT_STOP_WORDS = {
    "are", "is", "do", "does", "did", "can", "could", "were", "was",
    "there", "here", "in", "on", "at", "near", "around", "by", "under",
    "you", "i", "we", "have", "has", "that", "which", "to", "from",
    "of", "with", "left", "right", "visible", "total", "altogether",
    "please", "for", "me", "now", "should", "would", "will", "must",
    "need",
}
# Group nouns: "mailbox banks" are counted as the scene's mailbox units
# when no entry is named for the group noun itself.
COUNT_GROUP_NOUNS = {"bank", "row", "stack", "pile", "group", "cluster",
                     "set", "bunch", "column"}
IRREGULAR_SINGULAR = {"people": "person", "children": "child",
                      "men": "man", "women": "woman", "mice": "mouse",
                      "geese": "goose", "feet": "foot", "teeth": "tooth"}
# Plural-only nouns keep their s: eyeglasses are not drinking glasses.
PLURAL_ONLY = {"glasses", "pants", "scissors", "jeans", "shorts",
               "trousers", "binoculars", "headphones", "earbuds"}
NUMBER_WORDS = ["zero", "one", "two", "three", "four", "five", "six",
                "seven", "eight", "nine", "ten", "eleven", "twelve"]
ORDINAL_WORDS = ["", "first", "second", "third", "fourth", "fifth",
                 "sixth", "seventh", "eighth", "ninth", "tenth",
                 "eleventh", "twelfth"]
# A box this close to the frame border, and this much smaller than the
# group's largest, is an instance cut off by the frame.
EDGE_PX = 3
PARTIAL_AREA_RATIO = 0.6
COUNT_LIST_MAX = 6  # beyond this the lead gives a span, not every clock

# Live test 2026-10-28 — a count over a clip is the union of the asked
# class's instances across frames: a dark cat flat in a shadowed doorway
# was missing from the single sent frame. The clip is scanned densely
# and locally (no model calls): consecutive scan frames always overlap,
# so the camera's pan links the whole sweep (thumbnail search,
# translation only). Key frames are cut where the view has moved
# CLIP_KEY_SHIFT of a frame since the last one, sharpest frame per view
# — SAM runs on key frames only, so cost follows how much was filmed,
# not how long. Boxes are mapped into the sent photo's coordinates and
# clustered across frames; never within one (two cats in one frame are
# two cats). Second live test: 6 evenly spaced frames could not link a
# fast turn (chairs -> desk -> window), dropping the cat on the sill.
CLIP_SCAN_FPS = 8
CLIP_SCAN_MAX = 64     # scan frames (local only)
CLIP_KEY_SHIFT = 0.6   # of a frame: a new view, a new key frame
CLIP_KEY_MAX = 8       # SAM calls per clip, at most
CLIP_ANCHOR_SPAN = 6   # scan frames either side of the middle tried
# More unlinked segments than this: the sweep is not a sweep (shake,
# blank wall) — count the best single key frame instead of summing.
CLIP_MAX_SEGMENTS = 3
CLIP_MATCH_IOU = 0.3
# A frame edge cuts an instance to a sliver: the sliver lies inside the
# full view's box from another frame, so containment matches too.
CLIP_MATCH_CONTAIN = 0.6
REG_THUMB_W = 64
REG_MAX_DX = 0.6      # of width: search range for the pan
REG_MAX_DY = 0.3      # of height
REG_MIN_OVERLAP = 0.3  # of the thumbnail area
REG_MAX_RATIO = 0.6   # best diff / median diff: below this, a real match
SHARP_W = 160         # sharpness (edge energy) is measured at this width

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

# Answer tightening — final synthesis pass (subtractive only). The old
# 3 s budget could never succeed (Wonder first byte 20-28 s, measured
# 2026-10-27), so the draft always shipped. Budget is per read gap.
# Baseten (verified live 2026-10-28, 2.45 s for a full rewrite) is the
# first attempt; contributor-tier Spark (11-46 s) is the retry. The
# HTTP timeout is per read, so a trickling stream once ran 111 s; each
# Spark attempt also has a TOTAL wall-clock deadline, and one not done
# by then has failed (the ask errors per the failure rule).
# BASETEN_ENABLED=0 (or no key) makes both attempts Spark.
SYNTHESIS_TIMEOUT = 30
SYNTHESIS_DEADLINE_S = 30
SYNTHESIS_MODEL = SPARK_MODEL
BASETEN_API_BASE = "https://inference.baseten.co/v1"
BASETEN_SYNTH_MODEL = os.environ.get(
    "BASETEN_SYNTH_MODEL", "meta-models/muse-spark-open-1026")
BASETEN_SYNTH_TIMEOUT = 8
# At default reasoning effort the model spends the whole token budget
# reasoning and returns empty content (verified 2026-10-28). Even at
# "low" it reasons 630-1340 tokens; at 1200 one call in ~13 was cut off
# empty (finish_reason length), so the budget has headroom.
BASETEN_REASONING_EFFORT = "low"
BASETEN_MAX_TOKENS = 2000
BASETEN_TEMPERATURE = 0.2
CLOCK_LIST_RE = re.compile(
    r"((?:\b\d{1,2}\b(?:\s*,)?\s*(?:and\s+)?)+)\s*o'clock",
    re.IGNORECASE,
)
CAUTION_RE = re.compile(
    r"\b(can't|cannot|never|must not|don't|do not|not sure|unsure|"
    r"uncertain|check with|volunteer|pharmacist)\b",
    re.IGNORECASE,
)
METRIC_PHRASE_RE = re.compile(
    r"within arm's reach|about half a meter|"
    r"about \d+(?: and a half)? meters?", re.IGNORECASE)


def _metric_set(text: str) -> set[str]:
    return {m.lower() for m in METRIC_PHRASE_RE.findall(text)}


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


class StageFailed(PipelineError):
    """A pipeline stage did not run, even after its retry (route: 503).

    Failure rule (Charles, 2026-10-27): a stage that does not run errors
    the ask and the user retries — a degraded answer is never shipped as
    if nothing happened. A stage switched off by its kill switch is a
    configuration, not a failure, and never raises this.
    """

    def __init__(self, stage: str, cause: BaseException | None):
        self.stage = stage
        super().__init__(f"stage {stage} failed after retry: "
                         f"{type(cause).__name__}: {str(cause)[:200]}")


class _BadOutput(RuntimeError):
    """A model call returned, but nothing usable (counts as a failure)."""


STAGE_ATTEMPTS = 2  # one bounded retry, each attempt with a fresh budget


def _run_stage(stage: str, attempt: Any) -> Any:
    """Run attempt() up to STAGE_ATTEMPTS times; StageFailed after that.

    Every model-backed stage goes through here, so "retry once, then
    error" is stated once instead of per call site.
    """
    last: BaseException | None = None
    for _ in range(STAGE_ATTEMPTS):
        try:
            return attempt()
        except Exception as e:
            last = e
    raise StageFailed(stage, last)


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

# One session for every Meta API call: fan-out threads reuse its pooled
# connections instead of a TLS handshake per call. The pool is sized
# above any fan-out so no thread waits on a connection.
_HTTP = requests.Session()
_HTTP.mount("https://", requests.adapters.HTTPAdapter(pool_maxsize=32))
_HTTP.mount("http://", requests.adapters.HTTPAdapter(pool_maxsize=32))


def _meta_post(url: str, **kw: Any) -> requests.Response:
    """POST to the Meta API on the shared session. A 429 is logged (one
    JSON line) before the caller turns it into an error and _run_stage
    retries, so rate limiting is visible in the server log."""
    resp = _HTTP.post(url, **kw)
    if resp.status_code == 429:
        print(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": "rate_limited", "url": url,
            "retry_after": resp.headers.get("Retry-After"),
        }), flush=True)
    return resp


def _stream_output_text(body: dict, timeout: int) -> str:
    key = _load_key()
    if not key:
        raise PipelineError("META_MODEL_API_KEY is not set")
    try:
        resp = _meta_post(
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
    try:
        return _read_stream(resp)
    except requests.RequestException as e:
        # Mid-stream read timeouts surface here (not at post()); they
        # escaped as raw ConnectionError -> HTTP 500 before.
        raise PipelineError(f"Model API stream broke: {e}") from e


def _read_stream(resp: requests.Response) -> str:
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
    """SAM several phrases concurrently. A phrase that fails its retry
    fails the ask (StageFailed) — zero records is an answer, an error is
    not, and a missing phrase is a hole in the scene nobody hears about."""
    out: dict[str, list[dict]] = {}

    def one(phrase: str) -> tuple[str, list[dict]]:
        return phrase, _run_stage(
            "segmentation", lambda: sam_segment(image, phrase))

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=sam_concurrency()
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


def _has_word(name: str, words: set[str]) -> bool:
    return any(re.search(r"\b" + re.escape(w) + r"\b", name) for w in words)


def _area(e: dict[str, Any]) -> int:
    b = e["box"]
    return (b[2] - b[0]) * (b[3] - b[1])


def _distance_subject(question: str) -> str:
    """What a distance question asks about ('how far is the X' /
    'how far to the X'), trailing filler dropped; '' when none."""
    for rx in (DISTANCE_TO_RE, DISTANCE_SUBJECT_RE):
        m = rx.search(question or "")
        if not m:
            continue
        words = m.group(1).strip().lower().split()
        while words and words[-1] in (DISTANCE_TAIL_WORDS
                                      | REACH_TAIL_WORDS):
            words.pop()
        if words:
            return " ".join(words[:4])
    return ""


def _resolve_goal(
    scene: list[dict[str, Any]], question: str, hint: str = ""
) -> dict[str, Any]:
    """Rule 1 — the goal is what the question names.

    In order: the reach target ('path to the X', 'get to X', 'reach X'),
    a goal word (door, exit, stairs...), the subject of a distance
    question ('how far is the X'). A pronoun in that slot ('how far are
    they', 'path to it') names the prior referent (hint, the follow-up's
    subject). Only a question that names nothing falls back to exit
    inference: a leaving phrase picks a visible door/exit. named=True
    means a goal was named, so its absence is no_goal_visible.
    """
    q = question or ""
    words = set(re.findall(r"[a-z]+", q.lower()))
    phrase, aliases = "", []
    reach = _reach_target(q)
    subject = _distance_subject(q)
    goal_words = sorted(w for w in words if w in GOAL_WORDS)

    def pronoun(slot: str) -> bool:
        return bool(slot) and slot.split()[0] in PRONOUN_WORDS

    if reach and not pronoun(reach):
        phrase, aliases = reach, _reach_aliases(reach)
    elif goal_words:
        aliases = [a for w in goal_words for a in GOAL_ALIASES.get(w, [w])]
        phrase = goal_words[0] if len(goal_words) == 1 else "exit"
    elif subject and not pronoun(subject):
        phrase, aliases = subject, _reach_aliases(subject)
    elif hint and (pronoun(reach) or pronoun(subject)):
        phrase = hint.strip().lower()
        aliases = _reach_aliases(phrase)
    if phrase:
        cands = [e for e in scene
                 if any(a in str(e.get("name", "")).lower() for a in aliases)]
        goal = max(cands, key=_area) if cands else None
        if goal is not None and phrase in GOAL_WORDS | {"exit"}:
            # A goal word is a category ("exit"): name the thing found
            # ("door"). A named target keeps the user's word ("fridge").
            phrase = _head_noun(str(goal.get("name", ""))) or phrase
        return {"goal": goal, "aliases": aliases, "desc": phrase,
                "named": True}
    if EXIT_PHRASE_RE.search(q):
        cands = [e for e in scene
                 if any(a in str(e.get("name", "")).lower()
                        for a in EXIT_GOAL_ALIASES)]
        if cands:
            goal = max(cands, key=_area)
            return {"goal": goal, "aliases": list(EXIT_GOAL_ALIASES),
                    "desc": _head_noun(str(goal.get("name", ""))) or "exit",
                    "named": False}
    return {"goal": None, "aliases": [], "desc": "exit", "named": False}


def _box_pair(e: dict[str, Any], goal: dict[str, Any]) -> tuple[float, float]:
    """(fraction of e's box inside the goal box, area ratio e/goal)."""
    try:
        x1, y1, x2, y2 = (int(v) for v in e.get("box", [0, 0, 0, 0]))
        gx1, gy1, gx2, gy2 = (int(v) for v in goal.get("box", [0, 0, 0, 0]))
    except (TypeError, ValueError):
        return 0.0, 0.0
    inter = max(0, min(x2, gx2) - max(x1, gx1)) * max(
        0, min(y2, gy2) - max(y1, gy1))
    area = max(1, (x2 - x1) * (y2 - y1))
    garea = max(1, (gx2 - gx1) * (gy2 - gy1))
    return inter / area, area / garea


def _rests_on(e: dict[str, Any], holders: list[Any]) -> bool:
    """Base at/above a holder's vertical middle, with horizontal overlap."""
    try:
        x1, _, x2, yb = (int(v) for v in e.get("box", [0, 0, 0, 0]))
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


def _not_obstruction(
    e: dict[str, Any],
    goal: dict[str, Any] | None,
    goal_aliases: list[str],
    holders: list[Any],
    height: int,
    camera: dict[str, float] | None = None,
) -> str:
    """Rule 2 — why e is not an obstruction ('' when it may be one).

    An obstruction is a floor-standing object. Never obstructions, in
    order: the goal and its duplicates (rule 1); parts of the goal
    (drawer, handle... mostly inside its box); goal echoes (the goal
    re-grounded under another name: >=90% inside its box at >=0.5 of its
    area — the floor keeps a toy before a couch on-path); surfaces;
    ground cover (patio, lawn, sidewalk...); passageways; objects resting
    on a holder (counter, table, shelf); objects whose base floats above
    walking height for their distance (with capture metadata: whose
    base is above the horizon).
    """
    name = str(e.get("name", "")).lower()
    if goal is not None and e is goal:
        return "goal"
    if any(a in name for a in goal_aliases):
        return "goal"
    if goal is not None:
        contained, ratio = _box_pair(e, goal)
        if _has_word(name, GOAL_PART_WORDS) and contained > 0.7:
            return "goal_part"
        if contained >= 0.9 and ratio >= 0.5:
            return "goal_echo"
    if _has_word(name, SURFACE_WORDS):
        return "surface"
    if _has_word(name, GROUND_WORDS):
        return "ground_cover"
    if _has_word(name, PASSABLE_WORDS) and not (
            _has_word(name, {"ramp", "ramps"})
            and _has_word(name, PET_TOY_WORDS)):
        return "passage"
    if _rests_on(e, holders):
        return "supported"
    # With capture metadata the horizon is known, and the floor test is
    # the same as the distance's: a label-keyed band ruled out a saucer
    # chair at 45% of a 30 deg-tilted frame while its "toy" echo passed.
    if camera is not None and metric.enabled() and height > 0:
        if not _metric_floor(e, height, camera):
            return "elevated"
    elif not _stands_on_floor(e, height):
        return "elevated"
    return ""


def _is_door_goal(goal: dict[str, Any] | None) -> bool:
    # Head noun, not any word: a "french-door refrigerator" is a fridge.
    return goal is not None and \
        _head_noun(str(goal.get("name", ""))) in DOOR_GOAL_WORDS


def _passage(
    goal: dict[str, Any] | None,
    scene: list[dict[str, Any]],
    openings: list[Any],
    width: int,
    height: int,
) -> tuple[tuple[int, int, int, int], str]:
    """The box the route ends at, and where it came from.

    No goal: straight ahead to the top of the frame. A door: the opening
    beside (or under) the leaf — a segmented 'open doorway' or a
    doorway/hallway entry next to it, nearest first; with none found, an
    open leaf is widened by its own width each side (the opening is on
    one side, unknown which), a closed one is its own opening. Any other
    goal: its own box.
    """
    if goal is None:
        return ((int(CORRIDOR_AHEAD[0] * width), 0,
                 int(CORRIDOR_AHEAD[1] * width), 0), "ahead")
    gx1, gy1, gx2, gy2 = (int(v) for v in goal["box"])
    if not _is_door_goal(goal):
        return (gx1, gy1, gx2, gy2), "goal"
    gw = max(1, gx2 - gx1)
    cands = [tuple(int(v) for v in b) for b in openings]
    cands += [tuple(int(v) for v in e["box"]) for e in scene
              if e is not goal
              and _has_word(str(e.get("name", "")).lower(), OPENING_WORDS)]
    near = [b for b in cands
            if max(b[0], gx1) - min(b[2], gx2) <= 0.25 * gw  # beside/over
            and b[3] >= gy2 - 0.15 * height]                 # same depth
    if near:
        return max(near, key=lambda b: b[3]), "opening"
    if "open" in str(goal.get("name", "")).lower():
        return ((max(0, gx1 - gw), gy1, min(width, gx2 + gw), gy2),
                "leaf_widened")
    return (gx1, gy1, gx2, gy2), "leaf"


def _in_corridor(
    e: dict[str, Any],
    passage: tuple[int, int, int, int],
    width: int,
    height: int,
) -> bool:
    """Footprint (box base) inside the corridor from the user's feet to
    the passage base — or lying in the opening's lower band."""
    x1, y1, x2, y2 = (int(v) for v in e["box"])
    px1, py1, px2, py2 = passage
    if y2 < py2 - PASSAGE_TOL * height:
        # Past the passage base: on the route only when lying in the
        # opening itself (the threshold and just beyond).
        band = py2 - PASSAGE_BAND * max(1, py2 - py1)
        return y2 >= band and min(x2, px2) - max(x1, px1) \
            > CORRIDOR_MIN_OVERLAP * max(1, min(x2 - x1, px2 - px1))
    # No margin at the passage: things beside the opening (wall-side
    # guitars) are not in it.
    t = min(1.0, max(0.0, (y2 - py2) / max(1, height - py2)))
    left = px1 + t * (CORRIDOR_NEAR[0] * width - px1)
    right = px2 + t * (CORRIDOR_NEAR[1] * width - px2)
    return min(x2, right) - max(x1, left) \
        > CORRIDOR_MIN_OVERLAP * max(1.0, min(x2 - x1, right - left))


def passage_look(
    image: bytes, scene: list[dict[str, Any]], question: str,
    hint: str = "",
) -> list[tuple[int, int, int, int]]:
    """A door goal's opening, segmented: the leaf is not the way
    through. [] for any other goal (one SAM call only for doors)."""
    if not _is_door_goal(_resolve_goal(scene, question, hint)["goal"]):
        return []
    records = _run_stage(
        "segmentation", lambda: sam_segment(image, OPENING_PHRASE))
    return [r["box"] for r in records]


def _path_assessment(
    scene: list[dict[str, Any]], question: str, height: int = 0,
    hint: str = "", width: int = 0, openings: list[Any] | None = None,
    camera: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Facts about the route, computed — not free-reasoned.

    Goal: rule 1 (_resolve_goal); no goal means straight ahead.
    Obstructions: rule 2 (_not_obstruction) whose footprint is inside the
    corridor to the passage (_passage, _in_corridor) — geometry, not
    clock offset. Verdict: clear | blocked | no_goal_visible (a named
    goal absent from the scene; exit inference never produces this).
    width/height are the image size in px (0: taken from the boxes'
    extent, for callers without an image; height 0 also skips the
    floor-standing check). openings are segmented door openings
    (passage_look). hint is the prior referent a pronoun resolves to.
    """
    g = _resolve_goal(scene, question, hint)
    goal, goal_desc = g["goal"], g["desc"]
    if g["named"] and goal is None:
        return {"goal": None, "goal_clock": None, "goal_size": None,
                "verdict": "no_goal_visible", "on_path": [],
                "goal_desc": goal_desc}
    goal_clock = int(goal["clock"]) if goal else 12
    w = width or max([int(e["box"][2]) for e in scene] or [1])
    h = height or max([int(e["box"][3]) for e in scene] or [1])
    passage, passage_src = _passage(goal, scene, openings or [], w, h)
    goal_size = str(goal.get("size_class", "")) if goal else None
    goal_dist = goal.get("distance_m") if goal else None
    if passage_src == "opening":
        # Point the user at the way through, not at the leaf.
        goal_clock = clock_of(passage, w)
        goal_size = size_class_of(passage, h)
        # The opening's base is on the floor by construction.
        goal_dist = None
        if camera is not None and metric.enabled():
            goal_dist = metric.ground_distance(
                passage, h, camera["pitch_deg"], camera["focal_px"],
                metric.cam_height_m())
            goal_dist = None if goal_dist is None else round(goal_dist, 2)

    holders = [e.get("box", [0, 0, 0, 0]) for e in scene
               if _has_word(str(e.get("name", "")).lower(), HOLDER_WORDS)]
    on_path = []
    for e in scene:
        if _not_obstruction(e, goal, g["aliases"], holders, height,
                            camera):
            continue
        if _in_corridor(e, passage, w, h):
            on_path.append(e)
    on_path.sort(key=lambda e: SIZE_RANK.get(
        str(e.get("size_class", "far")), 2))
    on_path = _one_per_box(on_path)
    if on_path and all("distance_m" in e for e in on_path):
        # Nearest first when every mention has a distance. Order only:
        # distance never gates an object in or out of the path.
        on_path.sort(key=lambda e: float(e["distance_m"]))
    out = {
        "goal": (str(goal["name"]) if goal else None),
        "goal_clock": goal_clock,
        "goal_size": goal_size,
        "goal_desc": goal_desc,
        "through": passage_src == "opening",
        "verdict": "clear" if not on_path else "blocked",
        "passage": list(passage),
        "passage_source": passage_src,
        "on_path": [
            {"name": str(e.get("name", "")),
             "clock": int(e.get("clock", 12)),
             "size_class": str(e.get("size_class", "")),
             **({"distance_m": e["distance_m"]} if "distance_m" in e
                else {})}
            for e in on_path
        ],
    }
    if goal_dist is not None:
        out["goal_distance_m"] = goal_dist
    return out


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


# A labeled number stated in a trailing clause ("find my mailbox, it's
# number 16") — the target cut drops that clause, and with it the only
# thing that identifies the instance.
CLAUSE_NUMBER_RE = re.compile(
    r"\b(?:number|no\.?|#)\s*(\d+[a-z]?)\b", re.IGNORECASE)


def clause_qualifier(question: str) -> str | None:
    m = CLAUSE_NUMBER_RE.search(question or "")
    return f"number {m.group(1)}" if m else None


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


def to_wav_16k_mono(data: bytes) -> bytes:
    """16 kHz mono WAV for ASR. Already-there input passes through;
    anything else goes through ffmpeg (mirrors audio_probe.extract).
    b"" when the media has no audio stream (nothing was said — not a
    failure); raises on conversion failure so the transcription stage
    retries and then errors the ask."""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            if (w.getframerate() == 16000 and w.getnchannels() == 1
                    and w.getsampwidth() == 2):
                return data
    except Exception:
        pass
    ffmpeg = FFMPEG if os.path.exists(FFMPEG) else shutil.which("ffmpeg")
    if not ffmpeg:
        raise PipelineError("ffmpeg not found")
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "in")
        dst = os.path.join(td, "out.wav")
        with open(src, "wb") as f:
            f.write(data)
        proc = subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-i", src,
             "-vn", "-ac", "1", "-ar", "16000", dst],
            timeout=60, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            if "does not contain any stream" in (proc.stderr or ""):
                return b""
            raise PipelineError(
                f"ffmpeg failed: {(proc.stderr or '').strip()[:200]}")
        if not os.path.exists(dst) or os.path.getsize(dst) == 0:
            return b""
        with open(dst, "rb") as f:
            return f.read()


def transcribe_audio(wav_bytes: bytes | None) -> str:
    """Speech -> text via /v1/asr/transcribe. '' when there is nothing
    to transcribe or nothing was said; raises on API failure — "I
    didn't catch a question" must mean silence, not a dead ASR."""
    if not wav_bytes:
        return ""
    key = _load_key()
    if not key:
        raise PipelineError("META_MODEL_API_KEY is not set")
    r = _meta_post(
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
        raise PipelineError(f"ASR HTTP {r.status_code}: {r.text[:200]}")
    return str(r.json().get("transcript", "") or "").strip()


def transcribe_question(media: bytes) -> str:
    """Transcription stage: convert + transcribe, retry once, then error."""
    return _run_stage(
        "transcription", lambda: transcribe_audio(to_wav_16k_mono(media)))


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
    disabled or keyless (configuration — the regex layer stands alone).
    An enabled layer that errors is a stage that did not run: retry
    once, then StageFailed — never a silent drop to regex-only."""
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

    def attempt() -> dict[str, float]:
        r = requests.post(
            JEV_ENDPOINT,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json=body,
            timeout=JEV_TIMEOUT,
        )
        if r.status_code != 200:
            raise PipelineError(f"Jev HTTP {r.status_code}")
        answers = r.json().get("answers", {})
        out = {}
        for name in JEV_QUESTIONS:
            out[name] = max(0.0, min(1.0, float(answers[name]["noul"])))
        return out

    return _run_stage("intent_gate", attempt)


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


def apply_distances(
    scene: list[dict[str, Any]],
    camera: dict[str, float] | None,
    height: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Metric distance per instance from the app's capture metadata
    (metric.py). Returns (scene, log). Entries gain distance_m and
    distance_src when an estimator holds; size_class is untouched (the
    fallback, and what eval reads). No metadata (old app) or the kill
    switch: the scene comes back as-is, the same objects."""
    if camera is None or not metric.enabled():
        return scene, []
    cam_h = metric.cam_height_m()
    holders = [e.get("box", [0, 0, 0, 0]) for e in scene
               if _has_word(str(e.get("name", "")).lower(), HOLDER_WORDS)]
    out: list[dict[str, Any]] = []
    log: list[dict[str, Any]] = []
    for e in scene:
        name = str(e.get("name", "")).lower()
        box = e.get("box")
        # Surfaces and ground cover are where things stand, not things.
        if not box or len(box) != 4 or _has_word(name, SURFACE_WORDS) \
                or _has_word(name, GROUND_WORDS):
            out.append(e)
            continue
        floor = not _rests_on(e, holders) and _metric_floor(e, height,
                                                           camera)
        ground = (metric.ground_distance(box, height, camera["pitch_deg"],
                                         camera["focal_px"], cam_h)
                  if floor else None)
        prior = metric.prior_distance(_head_noun(name), box, height,
                                      camera["focal_px"])
        d, src, disagreed = metric.reconcile(ground, prior, floor)
        if ground is not None or prior is not None:
            rec = {"name": e.get("name"),
                   "ground_m": None if ground is None else round(ground, 2),
                   "prior_m": None if prior is None else round(prior, 2),
                   "distance_m": None if d is None else round(d, 2),
                   "distance_src": src}
            if disagreed:
                rec["disagree"] = True
            log.append(rec)
        if d is not None:
            e = dict(e, distance_m=round(d, 2), distance_src=src)
        out.append(e)
    return out, log


def _metric_floor(entry: dict[str, Any], height: int,
                  camera: dict[str, float]) -> bool:
    """Floor contact for distance only: the base is below the horizon,
    which the capture metadata places exactly (a fixed band cannot: a
    phone tilted 33 deg down puts the horizon near the top of the frame).
    _stands_on_floor's label-keyed band (path verdicts, untouched)
    dropped a draft stopper at 54% of the frame, and a kitchen island
    5 m off at 32% kept its wrong "a few steps away" (live 2026-10-29).
    The ground estimator's 5 deg depression and range gates do the rest."""
    if height <= 0:
        return False
    try:
        yb = float(entry.get("box", [0, 0, 0, 0])[3])
    except (TypeError, ValueError, IndexError):
        return False
    horizon = height / 2 - camera["focal_px"] * math.tan(
        math.radians(camera["pitch_deg"]))
    return yb > horizon


def _dist_words(size: str, distance_m: Any = None) -> str:
    """How far, in words: the metric figure when there is one, else the
    label. Never both for one object."""
    if isinstance(distance_m, (int, float)):
        return metric.spoken(float(distance_m))
    return DIST_PHRASE.get(size, size or "ahead")


def _prompt_scene(scene: list[dict[str, Any]],
                  kind: str) -> list[dict[str, Any]]:
    """The scene as Spark sees it. A metric distance replaces the label
    with its coarse spoken words (Spark must never see 2.13 and say it);
    the find flow is untouched (separate steer)."""
    if kind == "find" or not any("distance_m" in e for e in scene):
        return scene
    out = []
    for e in scene:
        if "distance_m" not in e:
            out.append(e)
            continue
        p = {k: v for k, v in e.items()
             if k not in ("distance_m", "distance_src", "size_class")}
        p["distance"] = metric.spoken(float(e["distance_m"]))
        out.append(p)
    return out


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
    while len(words) > 1 and words[-1] in POSITION_TAIL_WORDS:
        words.pop()
    head = words[-1] if words else ""
    if len(head) > 3 and head.endswith("s") \
            and not head.endswith(("ss", "us", "is")):
        head = head[:-1]
    return head


def _same_object(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Same head noun + near-duplicate boxes = a segmentation echo."""
    if _head_noun(str(a.get("name", ""))) != _head_noun(str(b.get("name", ""))):
        return False
    try:
        ba = tuple(int(v) for v in a.get("box", [0, 0, 0, 0]))
        bb = tuple(int(v) for v in b.get("box", [0, 0, 0, 0]))
    except (TypeError, ValueError):
        return False
    if len(ba) != 4 or len(bb) != 4:
        return False
    if iou(ba, bb) >= MERGE_IOU:
        return True
    small, big = sorted(({"box": ba}, {"box": bb}), key=_area)
    contained, ratio = _box_pair(small, big)
    return contained >= MERGE_CONTAIN and ratio >= MERGE_AREA_RATIO


SAME_BOX_IOU = 0.9


def _one_per_box(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One mention per box: SAM can return one box under several names
    (live 2026-10-29: "scratcher mat", "cat bed" and "mop head" for one
    draft stopper, spoken as two hazards). The first-listed name stays;
    the object stays on the route either way."""
    kept: list[dict[str, Any]] = []
    for e in entries:
        try:
            box = tuple(int(v) for v in e.get("box", []))
        except (TypeError, ValueError):
            box = ()
        if len(box) == 4 and any(
                iou(box, tuple(int(v) for v in k["box"])) >= SAME_BOX_IOU
                for k in kept if len(k.get("box", [])) == 4):
            continue
        kept.append(e)
    return kept


def merge_entries(scene: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse duplicate instances of the same object.

    Echoes (_same_object) merge into one entry, keeping the longest
    (most descriptive) name, the highest confidence, the nearest size
    class, and the highest-confidence member's box and clock. Extras
    (Flow C label/match) come from a matched member when any matched.
    An entry joins a group only when it echoes every member, so
    overlapping instances never chain into one. Group order follows
    first appearance.
    """
    groups: list[list[dict[str, Any]]] = []
    for e in scene:
        placed = False
        for g in groups:
            if all(_same_object(e, m) for m in g):
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


def ship_scene(scene: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The scene as shipped (chips, eval, History): entries with the same
    name and clock collapse to one, carrying "instances" when > 1
    ("off-white wall" x6 is one chip). Display only — counting, the path
    and follow-ups use the full scene."""
    out: list[dict[str, Any]] = []
    seen: dict[tuple[str, Any], dict[str, Any]] = {}
    for e in scene:
        key = (str(e.get("name", "")).strip().lower(), e.get("clock"))
        if key in seen:
            seen[key]["instances"] = seen[key].get("instances", 1) + 1
            continue
        ent = dict(e)
        seen[key] = ent
        out.append(ent)
    return out


# ----------------------------------------------------------------------------
# Counting — the scene's instances are the count
# ----------------------------------------------------------------------------

def _singular(word: str) -> str:
    w = (word or "").lower()
    if w in IRREGULAR_SINGULAR:
        return IRREGULAR_SINGULAR[w]
    if w in PLURAL_ONLY or len(w) <= 3:
        return w
    if w.endswith("ies"):
        return w[:-3] + "y"
    if w.endswith(("ches", "shes", "xes", "sses", "zes")):
        return w[:-2]
    if w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def count_subject(question: str) -> str | None:
    """'How many cats are in this room?' -> 'cats'. None when the
    question is not a count, or asks how far ('how many steps')."""
    q = question or ""
    if DISTANCE_RE.search(q):
        return None
    m = COUNT_RE.search(q)
    if not m:
        return None
    words: list[str] = []
    for w in re.findall(r"[a-z][a-z'-]*", m.group(1).lower()):
        if w in COUNT_STOP_WORDS:
            break
        words.append(w)
    while words and words[0] in {"the", "my", "a", "an", "of"}:
        words.pop(0)
    return " ".join(words) or None


def _count_unit(name: str) -> str:
    """Singular head word of a scene name, position words dropped."""
    words = re.sub(r"^(a|an|the)\s+", "",
                   (name or "").strip().lower()).split()
    while len(words) > 1 and words[-1] in POSITION_TAIL_WORDS:
        words.pop()
    return _singular(words[-1]) if words else ""


def count_instances(
    scene: list[dict[str, Any]], subject: str
) -> list[dict[str, Any]]:
    """Scene entries that are instances of the asked noun. A group noun
    with no entries of its own ('mailbox banks') counts its member noun."""
    words = subject.split()
    if not words:
        return []
    head = _singular(words[-1])
    hits = [e for e in scene if _count_unit(str(e.get("name", ""))) == head]
    if not hits and head in COUNT_GROUP_NOUNS and len(words) > 1:
        member = _singular(words[-2])
        hits = [e for e in scene
                if _count_unit(str(e.get("name", ""))) == member]
    return hits


def count_look(
    image: bytes, scene: list[dict[str, Any]], question: str,
    media: bytes | None = None,
) -> list[dict[str, Any]]:
    """Segment the asked noun so its instances are in the scene even
    when naming never listed it (a follow-up 'how many cups'). With the
    ask's clip, the class's instances become the union across its
    frames (clip_union)."""
    subject = count_subject(question)
    if not subject:
        return scene
    words = subject.split()
    phrase = " ".join(words[:-1] + [_singular(words[-1])])
    with Image.open(io.BytesIO(image)) as im:
        width, height = im.size
    records = _run_stage("segmentation", lambda: sam_segment(image, phrase))
    found = [entry_for(phrase, r["box"], r["confidence"], width, height)
             for r in records]
    scene = merge_entries(scene + found) if found else scene
    if media:
        scene = clip_union(image, scene, subject, phrase, media)
    return scene


def clip_frames(media: bytes, fps: int = CLIP_SCAN_FPS,
                n: int = CLIP_SCAN_MAX) -> list[bytes]:
    """Up to n JPEG frames at fps, spread over the clip (upright: ffmpeg
    applies the rotation). [] when the media has no video stream."""
    ffmpeg = FFMPEG if os.path.exists(FFMPEG) else shutil.which("ffmpeg")
    if not ffmpeg:
        raise PipelineError("ffmpeg not found")
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "in")
        with open(src, "wb") as f:
            f.write(media)
        proc = subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-i", src, "-an",
             "-vf", f"fps={fps}", "-q:v", "3",
             os.path.join(td, "f_%04d.jpg")],
            timeout=60, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            if "does not contain any stream" in (proc.stderr or "") \
                    or "matches no streams" in (proc.stderr or ""):
                return []
            raise PipelineError(
                f"ffmpeg frames failed: {(proc.stderr or '').strip()[:200]}")
        names = sorted(f for f in os.listdir(td) if f.startswith("f_"))
        if len(names) > n:
            names = [names[round(i * (len(names) - 1) / (n - 1))]
                     for i in range(n)]
        out = []
        for name in names:
            with open(os.path.join(td, name), "rb") as f:
                out.append(f.read())
        return out


def _thumb(data: bytes, size: tuple[int, int]) -> Image.Image:
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("L").resize(size)
        return im.copy()


def _sharpness(data: bytes) -> float:
    """Edge energy at SHARP_W wide: motion blur scores low."""
    with Image.open(io.BytesIO(data)) as im:
        g = im.convert("L")
        g = g.resize((SHARP_W, max(1, round(SHARP_W * g.size[1]
                                            / g.size[0]))))
    return ImageStat.Stat(g.filter(ImageFilter.FIND_EDGES)).mean[0]


def _pan_scored(
    a: Image.Image, b: Image.Image
) -> tuple[tuple[int, int] | None, float]:
    """(offset, best/median diff ratio). Offset (dx, dy), in thumbnail
    px, such that b's pixel (x, y) shows what a shows at (x + dx,
    y + dy); None when no shift matches clearly (the frames don't
    overlap, or the scene is featureless)."""
    w, h = a.size

    def diff(dx: int, dy: int) -> float | None:
        ax1, ay1 = max(0, dx), max(0, dy)
        ax2, ay2 = min(w, w + dx), min(h, h + dy)
        if (ax2 - ax1) * (ay2 - ay1) < REG_MIN_OVERLAP * w * h:
            return None
        ca = a.crop((ax1, ay1, ax2, ay2))
        cb = b.crop((ax1 - dx, ay1 - dy, ax2 - dx, ay2 - dy))
        return ImageStat.Stat(ImageChops.difference(ca, cb)).mean[0]

    rx, ry = int(REG_MAX_DX * w), int(REG_MAX_DY * h)
    scores = {}
    for dy in range(-ry, ry + 1, 2):
        for dx in range(-rx, rx + 1, 2):
            d = diff(dx, dy)
            if d is not None:
                scores[(dx, dy)] = d
    if not scores:
        return None, 1.0
    (bx, by), _ = min(scores.items(), key=lambda kv: kv[1])
    for dy in range(by - 1, by + 2):
        for dx in range(bx - 1, bx + 2):
            d = diff(dx, dy)
            if d is not None:
                scores[(dx, dy)] = d
    best_at, best = min(scores.items(), key=lambda kv: kv[1])
    median = sorted(scores.values())[len(scores) // 2]
    ratio = best / median if median > 0 else 1.0
    return (best_at if ratio <= REG_MAX_RATIO else None), ratio


def _pan(a: Image.Image, b: Image.Image) -> tuple[int, int] | None:
    return _pan_scored(a, b)[0]


def _edge(box: Any, w: int, h: int) -> bool:
    x1, y1, x2, y2 = box
    return (x1 <= EDGE_PX or y1 <= EDGE_PX
            or x2 >= w - EDGE_PX or y2 >= h - EDGE_PX)


def key_frames(
    frames: list[bytes], tsize: tuple[int, int]
) -> tuple[list[int], list[int], list[tuple[float, float]], list[Any]]:
    """Scan the clip locally: link consecutive frames by pan, cut views.

    Returns (keys, segment, pos, thumbs): key frame indices; each frame's
    segment id (a new segment where a pair cannot be linked — a whip pan
    or a blur too strong to match); each frame's position, in thumbnail
    px, relative to its segment's first frame; the thumbnails. A view
    ends once the camera has moved CLIP_KEY_SHIFT of a frame from where
    it began; its sharpest frame is its key frame. At most CLIP_KEY_MAX
    keys (the shift grows until they fit)."""
    thumbs = [_thumb(f, tsize) for f in frames]
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=FANOUT_WORKERS
    ) as pool:
        steps = list(pool.map(lambda k: _pan(thumbs[k], thumbs[k + 1]),
                              range(len(frames) - 1)))
        sharp = list(pool.map(_sharpness, frames))
    seg = [0] * len(frames)
    pos: list[tuple[float, float]] = [(0.0, 0.0)] * len(frames)
    for k, st in enumerate(steps):
        if st is None:
            seg[k + 1] = seg[k] + 1
        else:
            seg[k + 1] = seg[k]
            pos[k + 1] = (pos[k][0] + st[0], pos[k][1] + st[1])
    tw, th = tsize
    shift = CLIP_KEY_SHIFT
    while True:
        views: list[list[int]] = []
        for k in range(len(frames)):
            if views and seg[views[-1][0]] == seg[k]:
                x0, y0 = pos[views[-1][0]]
                if abs(pos[k][0] - x0) <= shift * tw \
                        and abs(pos[k][1] - y0) <= shift * th:
                    views[-1].append(k)
                    continue
            views.append([k])
        if len(views) <= CLIP_KEY_MAX or shift > 8:
            break
        shift *= 1.25
    if len(views) > CLIP_KEY_MAX:  # broken sweep: views cannot merge
        views = [views[round(i * (len(views) - 1) / (CLIP_KEY_MAX - 1))]
                 for i in range(CLIP_KEY_MAX)]
    keys = [max(v, key=lambda k: sharp[k]) for v in views]
    return keys, seg, pos, thumbs


def clip_union(
    image: bytes, scene: list[dict[str, Any]], subject: str, phrase: str,
    media: bytes,
) -> list[dict[str, Any]]:
    """Replace the counted class's scene entries with its instances
    across the clip (key_frames). Boxes are in the sent photo's
    coordinates; an instance seen only off that frame keeps its mapped
    position (clock clamped to 9 or 3). Segments the scan could not link
    to the photo are counted but cannot be placed: their entries carry
    linked=False and the count is said as approximate."""
    frames = _run_stage("clip_frames", lambda: clip_frames(media))
    if not frames:
        return scene
    with Image.open(io.BytesIO(image)) as im:
        width, height = im.size
    with Image.open(io.BytesIO(frames[0])) as im:
        fw, fh = im.size
    sx, sy = width / fw, height / fh  # frame px -> photo px
    tsize = (REG_THUMB_W, max(1, round(REG_THUMB_W * height / width)))
    tscale = width / REG_THUMB_W

    keys, seg, pos, thumbs = key_frames(frames, tsize)
    # The photo is the clip's middle frame (the app's pick): anchor it to
    # the best-matching scan frame near the middle.
    photo = _thumb(image, tsize)
    mid = len(frames) // 2
    near = range(max(0, mid - CLIP_ANCHOR_SPAN),
                 min(len(frames), mid + CLIP_ANCHOR_SPAN + 1))
    tries = [(k,) + _pan_scored(photo, thumbs[k]) for k in near]
    tries = [t for t in tries if t[1] is not None]
    anchor = min(tries, key=lambda t: t[2]) if tries else None

    by_key = sam_many_images([frames[k] for k in keys], phrase)
    photo_hits = count_instances(scene, subject)
    if not any(by_key) and not photo_hits:
        return scene
    if seg[-1] + 1 > CLIP_MAX_SEGMENTS:
        # Unlinkable sweep: the best single frame, never a sum of guesses.
        best = max(range(len(keys)), key=lambda i: len(by_key[i]))
        if len(by_key[best]) <= len(photo_hits):
            return scene
        return [e for e in scene if e not in photo_hits] + [
            entry_for(phrase, (int(b[0] * sx), int(b[1] * sy),
                               int(b[2] * sx), int(b[3] * sy)),
                      c, width, height, frame=keys[best],
                      clip_union="max_frame")
            for b, c in by_key[best]]
    home = seg[anchor[0]] if anchor else None

    def to_photo(k: int, b: Any) -> tuple[int, int, int, int]:
        # frame k -> anchor frame (same segment) -> photo
        ax, ay = anchor[1]
        ox = (pos[k][0] - pos[anchor[0]][0] + ax) * tscale
        oy = (pos[k][1] - pos[anchor[0]][1] + ay) * tscale
        return (int(b[0] * sx + ox), int(b[1] * sy + oy),
                int(b[2] * sx + ox), int(b[3] * sy + oy))

    # Observations: (frame id, box, conf, cut by its frame edge, segment).
    obs: list[tuple[int, tuple[int, ...], Any, bool, Any]] = []
    if anchor is not None:
        for e in photo_hits:
            obs.append((-1, tuple(int(v) for v in e["box"]),
                        e.get("confidence"),
                        _edge(e["box"], width, height), home))
    for k, recs in zip(keys, by_key):
        for b, c in recs:
            if seg[k] == home:
                pb = to_photo(k, b)
            else:  # unlinked: own frame coords, scaled; never placed
                pb = (int(b[0] * sx), int(b[1] * sy),
                      int(b[2] * sx), int(b[3] * sy))
            obs.append((k, pb, c, _edge(b, fw, fh), seg[k]))
    if anchor is None and not obs:
        return scene

    def match(a: tuple[int, ...], b: tuple[int, ...]) -> float:
        small, big = sorted(({"box": a}, {"box": b}), key=_area)
        contained, _ = _box_pair(small, big)
        v = iou(a, b)
        return max(v if v >= CLIP_MATCH_IOU else 0.0,
                   contained if contained >= CLIP_MATCH_CONTAIN else 0.0)

    clusters: list[list[tuple[int, tuple[int, ...], Any, bool, Any]]] = []
    for o in obs:
        best_c, best_v = None, 0.0
        for c in clusters:
            if c[0][4] != o[4] or any(m[0] == o[0] for m in c):
                continue  # same segment only; one box per frame
            v = max(match(o[1], m[1]) for m in c)
            if v > best_v:
                best_c, best_v = c, v
        if best_c is None:
            clusters.append([o])
        else:
            best_c.append(o)

    name = (str(photo_hits[0].get("name")) if photo_hits else phrase)
    biggest = max(_area({"box": m[1]}) for c in clusters for m in c)
    out = [e for e in scene if e not in photo_hits]
    for c in clusters:
        rep = max(c, key=lambda m: _area({"box": m[1]}))
        linked = c[0][4] == home
        cx = min(width - 1, max(0, (rep[1][0] + rep[1][2]) // 2))
        ent = entry_for(name, rep[1], rep[2], width, height,
                        frames_seen=len(c), clip_union="registered",
                        linked=linked,
                        partial=all(m[3] for m in c)
                        and _area({"box": rep[1]})
                        < PARTIAL_AREA_RATIO * biggest)
        ent["clock"] = clock_of((cx, 0, cx, 0), width)
        if linked and (rep[1][2] <= 0 or rep[1][0] >= width):
            ent["off_frame"] = True  # seen in the sweep, outside the photo
        out.append(ent)
    return out


def sam_many_images(
    frames: list[bytes], phrase: str
) -> list[list[tuple[tuple[int, int, int, int], Any]]]:
    """SAM one phrase over several images concurrently: per frame, a
    list of (box, confidence). A frame that fails its retry fails the
    ask (StageFailed), as in sam_many."""
    def one(f: bytes) -> list[tuple[tuple[int, int, int, int], Any]]:
        recs = _run_stage("segmentation", lambda: sam_segment(f, phrase))
        return [(r["box"], r["confidence"]) for r in recs]

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=sam_concurrency()
    ) as pool:
        return list(pool.map(one, frames))


def _number(n: int) -> str:
    return NUMBER_WORDS[n] if 0 <= n < len(NUMBER_WORDS) else str(n)


def _clock_list(clocks: list[Any]) -> str:
    uniq: list[str] = []
    for c in clocks:
        if str(c) not in uniq:
            uniq.append(str(c))
    if len(uniq) == 1:
        return uniq[0]
    return ", ".join(uniq[:-1]) + " and " + uniq[-1]


def _count_opening(
    scene: list[dict[str, Any]], subject: str, width: int, height: int
) -> list[str]:
    """The count lead, from scene instances only. Instances cut off by
    the frame edge are said as partial ('part of a fourth')."""
    words = subject.split()
    singular = " ".join(words[:-1] + [_singular(words[-1])])
    if _is_plural(subject):
        plural = subject
    elif re.search(r"[^aeiou]y$", singular):
        plural = singular[:-1] + "ies"
    elif singular.endswith(("ch", "sh", "x", "s", "z")):
        plural = singular + "es"
    else:
        plural = singular + "s"
    hits = sorted(count_instances(scene, subject),
                  key=lambda e: e["box"][0] + e["box"][2])
    n = len(hits)
    # A count over a clip's frames covers the whole sweep, not one photo.
    place = ("in this clip" if any(e.get("clip_union") for e in hits)
             else "in this photo")
    if n == 0:
        return [f"I don't see any {plural} {place}."]
    if any(e.get("linked") is False for e in hits):
        # Part of the sweep could not be linked to the rest: the count
        # may hold one twice, and those positions are unknown.
        noun = singular if n == 1 else plural
        return [f"I can see about {_number(n)} {noun} {place}. The camera "
                f"moved too fast for me to place them all."]
    biggest = max(_area(e) for e in hits)

    def partial(e: dict[str, Any]) -> bool:
        if "partial" in e:  # decided across a clip's frames
            return bool(e["partial"])
        x1, y1, x2, y2 = e["box"]
        at_edge = (x1 <= EDGE_PX or y1 <= EDGE_PX
                   or x2 >= width - EDGE_PX or y2 >= height - EDGE_PX)
        return at_edge and _area(e) < PARTIAL_AREA_RATIO * biggest

    full = [e for e in hits if not partial(e)]
    part = [e for e in hits if partial(e)]
    noun = singular if n == 1 else plural
    if not part:
        if n > COUNT_LIST_MAX:
            first, last = hits[0]["clock"], hits[-1]["clock"]
            span = (f"at your {first} o'clock" if first == last
                    else f"from your {first} to your {last} o'clock")
            return [f"I can see {_number(n)} {noun} {place}, "
                    f"{span}."]
        return [f"I can see {_number(n)} {noun} {place}, at your "
                f"{_clock_list([e['clock'] for e in hits])} o'clock."]
    pclocks = _clock_list([e["clock"] for e in part])
    if not full:
        if len(part) == 1:
            return [f"I can only see part of one {singular}, at the edge "
                    f"of the photo at your {pclocks} o'clock."]
        return [f"I can only see parts of {_number(len(part))} {plural}, "
                f"at the edge of the photo at your {pclocks} o'clock."]
    full_desc = "one" if len(full) == 1 else f"{_number(len(full))} full"
    if len(part) == 1:
        part_desc = ("part of another" if len(full) == 1 else
                     f"part of a {ORDINAL_WORDS[n]}"
                     if n < len(ORDINAL_WORDS) else "part of one more")
    else:
        part_desc = f"parts of {_number(len(part))} more"
    return [f"I can see {_number(n)} {noun} {place}: {full_desc} at "
            f"your {_clock_list([e['clock'] for e in full])} o'clock, and "
            f"{part_desc} at the edge, at your {pclocks} o'clock."]


# ----------------------------------------------------------------------------
# Flows
# ----------------------------------------------------------------------------

def flow_a(image: bytes) -> list[dict[str, Any]]:
    """Open scene: Spark nouns + safety list, one entry per instance."""
    with Image.open(io.BytesIO(image)) as im:
        width, height = im.size
    def name_once() -> list[str]:
        raw = _vlm_text(SPARK_MODEL, image, SPARK_NAMING_PROMPT,
                        SPARK_TIMEOUT)
        # "[]" is an answer (nothing nameable); no array at all is not.
        if not re.search(r"\[.*\]", raw, re.S):
            raise _BadOutput(f"naming returned no array: {raw[:80]!r}")
        return parse_nouns(raw)

    nouns = _run_stage("naming", name_once)
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
        # A failed read is not "no label": treating it as NONE turned
        # API errors into "I cannot confirm ibuprofen".
        return _run_stage("label_read", lambda: _vlm_text(
            CROP_READ_MODEL, crop, WONDER_READ_PROMPT, READ_TIMEOUT
        ).strip())

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
    Spark reads each proposal. Returns new scene entries (normal
    clock/size, position-based distance). A SAM or read call that fails
    its retry fails the ask (StageFailed): a floor look that silently
    found nothing would certify a path it never checked.
    """
    with Image.open(io.BytesIO(image)) as im:
        width, height = im.size
        full = im.convert("RGB")
    found: list[tuple[tuple[int, int, int, int], float | None]] = []
    regions = _floor_regions(scene, width, height)

    def seg(region: tuple[int, int, int, int]) -> list[dict[str, Any]]:
        x0, y0, x1, y1 = region
        crop = full.crop((max(0, x0), max(0, y0),
                          min(width, x1), min(height, y1)))
        buf = io.BytesIO()
        crop.save(buf, format="JPEG", quality=90)
        return _run_stage("floor_look", lambda: sam_segment(
            buf.getvalue(), FLOOR_PHRASE))

    # Regions segment concurrently; merged in region order, as before.
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=sam_concurrency()
    ) as pool:
        per_region = list(pool.map(seg, regions))
    for (x0, y0, _, _), recs in zip(regions, per_region):
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
        return _run_stage("floor_look", lambda: _vlm_text(
            CROP_READ_MODEL, out.getvalue(), FLOOR_READ_PROMPT,
            READ_TIMEOUT,
        ).strip())

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
        "('pointed up at the sky') is orientation, not decoration. "
        "Rule 4 — the first sentence directly answers the question; if "
        "the photo cannot answer it (a sound, a smell, something out of "
        "view), the first sentence says so plainly — never guess. "
        "Mention each object at most once. Never walk through the scene "
        "object by object: name only what answers the question, plus at "
        "most one nearby landmark for orientation. Use at most three "
        "short sentences."
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
    allow_empty: bool = False,
) -> tuple[str, float]:
    """One composition attempt. allow_empty: the opening is code-written
    and Spark only adds detail, so "nothing to add" is a valid answer.
    Unusable output (no answer, broken JSON) raises for the retry."""
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
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
            # Shipping raw '{"answer": ...' text would be read aloud.
            raise _BadOutput(f"answer JSON unparseable: {raw[:80]!r}")
        if answer or allow_empty:
            return answer, max(0.0, min(1.0, conf))
        raise _BadOutput("answer model returned an empty answer")
    if raw.strip():
        return raw.strip(), 0.5
    if allow_empty:
        return "", 0.5
    raise _BadOutput("Answer model returned empty output")


# ----------------------------------------------------------------------------
# Answer assembly — rules 3 and 4
# ----------------------------------------------------------------------------

def question_kind(question: str, flow: str, navigation: bool) -> str:
    """Rule 3 — what the lead sentence must answer.

    distance ('how far') > count ('how many') > path (route words) >
    find (locate/labeled item) > path by intent alone (Jev navigation,
    no route words) > other. Questions about the ground underfoot are 'other' — the
    surface is the answer, not a route verdict. '' is describe.
    """
    q = (question or "").strip()
    if not q:
        return "describe"
    if DISTANCE_RE.search(q):
        return "distance"
    if count_subject(q):
        return "count"
    if NAVIGATION_RE.search(q):
        return "path"
    if flow in ("B", "C"):
        return "find"
    if SURFACE_Q_RE.search(q):
        return "other"
    if navigation:
        return "path"
    return "other"


def _is_plural(name: str) -> bool:
    words = (name or "").strip().lower().split()
    while len(words) > 1 and words[-1] in POSITION_TAIL_WORDS:
        words.pop()
    w = words[-1] if words else ""
    return (w.endswith("s") and not w.endswith(("ss", "us", "is"))) \
        or w in {"people", "feet", "children"}


def _where(clock: Any, size: str, distance_m: Any = None) -> str:
    return f"{clock} o'clock, {_dist_words(size, distance_m)}"


def _far_apart(a: Any, b: Any) -> bool:
    """Two metric distances that cannot be one object (beyond
    metric.AGREE_FRAC of the nearer). Without both, never apart."""
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return False
    return abs(a - b) > metric.AGREE_FRAC * min(a, b)


def _path_opening(a: dict[str, Any]) -> list[str]:
    """Lead for a route question: the verdict, each object once.

    'Careful —' only for a rule-2 obstruction on the route (rule 3);
    the goal is named once, after the obstruction."""
    desc = str(a.get("goal_desc") or "exit")
    if a["verdict"] == "no_goal_visible":
        return [f"I can't see the {desc} in this photo."]
    # One mention per object: entries sharing a head noun (two ice
    # boxes SAM did not merge) are one obstacle, at every clock cited —
    # unless their metric distances differ beyond metric.AGREE_FRAC
    # (two cats at 1 m and 3 m are two obstacles, each with its own).
    groups: list[dict[str, Any]] = []
    for o in a["on_path"]:
        head, d = _head_noun(o["name"]), o.get("distance_m")
        g = next((g for g in groups if g["head"] == head
                  and not _far_apart(g["distance_m"], d)), None)
        if g is None:
            g = {"head": head, "name": o["name"], "clocks": [],
                 "size_class": o["size_class"], "distance_m": d}
            groups.append(g)
        if _is_plural(o["name"]) and not _is_plural(g["name"]):
            g["name"] = o["name"]
        if o["clock"] not in g["clocks"]:
            g["clocks"].append(o["clock"])
    obs = [dict(g, clock=" and ".join(str(c) for c in g["clocks"]))
           for g in groups[:2]]
    goal_where = (_where(a["goal_clock"], a.get("goal_size") or "",
                         a.get("goal_distance_m"))
                  if a.get("goal") else "")
    way = "the way through the" if a.get("through") else "the way to the"
    if a["verdict"] == "clear":
        if goal_where:
            return [f"Yes — {way} {desc} is clear, at your "
                    f"{goal_where}."]
        return ["Yes — the way ahead is clear."]
    if len(obs) == 1:
        o = obs[0]
        verb = "are" if _is_plural(o["name"]) else "is"
        lead = (f"Careful — {o['name']} {verb} in your path at "
                f"{_where(o['clock'], o['size_class'], o['distance_m'])}.")
    else:
        parts = ", and ".join(
            f"{o['name']} at "
            f"{_where(o['clock'], o['size_class'], o['distance_m'])}"
            for o in obs)
        lead = f"Careful — {parts}, are in your path."
    out = [lead]
    if goal_where and a.get("through"):
        out.append(f"The way through the {desc} is at your {goal_where}.")
    elif goal_where:
        verb = "are" if _is_plural(desc) else "is"
        out.append(f"The {desc} {verb} at your {goal_where}.")
    return out


def _distance_opening(a: dict[str, Any], question: str) -> list[str]:
    """Lead for 'how far': the distance first, then where (rule 3). A
    true obstruction between the user and the referent follows — said
    plainly, not as a walking-hazard lead."""
    desc = str(a.get("goal_desc") or "")
    if a["verdict"] == "no_goal_visible":
        return [f"I can't see the {desc} in this photo."]
    if not a.get("goal"):
        return []
    plural = _is_plural(desc) or _is_plural(str(a["goal"]))
    used_pronoun = bool(set(re.findall(r"[a-z]+", question.lower()))
                        & PRONOUN_WORDS)
    subj = ("They" if plural else "It") if used_pronoun else f"The {desc}"
    verb = "are" if plural else "is"
    dist = (_dist_words(a.get("goal_size") or "", a["goal_distance_m"])
            if a.get("goal_distance_m") is not None
            else DIST_PHRASE.get(a.get("goal_size") or "", a.get("goal_size")))
    out = [f"{subj} {verb} {dist}, at your {a['goal_clock']} o'clock."]
    for o in a["on_path"][:1]:
        ov = "are" if _is_plural(o["name"]) else "is"
        out.append(f"On the way, {o['name']} {ov} at your "
                   f"{_where(o['clock'], o['size_class'], o.get('distance_m'))}.")
    return out


def _mention_heads(text: str, heads: set[str]) -> set[str]:
    tl = text.lower()
    return {h for h in heads
            if re.search(r"\b" + re.escape(h) + r"(?:s|es)?\b", tl)}


def _tidy(
    opening: list[str],
    body: str,
    kind: str,
    scene: list[dict[str, Any]],
    assessment: dict[str, Any] | None,
    hazards: list[str],
    question: str,
    count_words: set[str] | None = None,
) -> str:
    """Rule 4 — make the draft right before synthesis, deterministically.

    The opening (code-written lead, or Spark's own first sentence when
    there is none) always stands. Each later sentence is dropped when:
    it re-locates an already-located object (or a pronoun) at an
    already-stated clock and relates it to nothing new; it is only about the ground and says
    nothing about footing (unless the question asks about the ground);
    or, after a code-written opening, it is a second orientation
    landmark or past the body budget; for a count, it mentions the
    counted noun again (the code-written count is the only count).
    Warning/caution sentences always stay.
    """
    sents = split_sentences(body)
    if not opening:
        opening, sents = sents[:1], sents[1:]
    heads = {_head_noun(str(e.get("name", ""))) for e in scene} - {""}
    if assessment and assessment.get("goal_desc"):
        heads.add(_head_noun(str(assessment["goal_desc"])))
    ground = {h for h in heads if h in GROUND_WORDS}
    key = set(hazards)
    if assessment:
        key |= {_head_noun(str(o["name"])) for o in assessment["on_path"]}
        key.add(_head_noun(str(assessment.get("goal") or "")))
        key.add(_head_noun(str(assessment.get("goal_desc") or "")))
    surface_question = bool(SURFACE_Q_RE.search(question or ""))
    coded = kind in ("path", "distance", "count")
    counted = [re.compile(r"\b" + re.escape(w) + r"(?:e?s)?\b", re.IGNORECASE)
               for w in (count_words or set())]
    stated: dict[str, set[str]] = {}

    said_clocks: set[str] = set()

    def note(s: str) -> None:
        said_clocks.update(_clock_set(s))
        for h in _mention_heads(s, heads):
            stated.setdefault(h, set()).update(_clock_set(s) or {""})

    for s in opening:
        note(s)
    kept: list[str] = []
    body_count = orientation = 0
    for s in sents:
        t = s.strip()
        if t.lower().startswith("warning:") or CAUTION_RE.search(t):
            kept.append(t)
            note(t)
            continue
        if any(rx.search(t) for rx in counted):
            continue  # a second count, or the counted things re-listed
        hs = _mention_heads(t, heads)
        clocks = _clock_set(t)
        if clocks and not RELATION_RE.search(t) and (
                all(clocks <= stated.get(h, set()) for h in hs) if hs
                # Pronoun subject ("They are at your 12 o'clock"): a
                # restatement when every clock it cites is already said.
                else PRONOUN_LEAD_RE.match(t) and clocks <= said_clocks):
            continue  # restatement
        if hs and hs <= ground and not FOOTING_RE.search(t) \
                and not surface_question:
            continue  # ground mention with no footing information
        if coded:
            if body_count >= BODY_MAX_SENTENCES:
                continue
            if hs and not (hs & key):
                if orientation >= ORIENTATION_MAX:
                    continue
                orientation += 1
            body_count += 1
        kept.append(t)
        note(t)
    return " ".join([s.strip() for s in opening] + kept).strip()


def _hazards_in(scene: list[dict[str, Any]]) -> list[str]:
    found: list[str] = []
    near: dict[str, float] = {}
    for ent in scene:
        name = str(ent.get("name", "")).lower()
        for hz in HAZARD_KEYWORDS:
            if hz in name:
                if hz not in found:
                    found.append(hz)
                if "distance_m" in ent:
                    near[hz] = min(near.get(hz, math.inf),
                                   float(ent["distance_m"]))
    # Nearest first when every hazard has a distance (order only).
    if found and all(hz in near for hz in found):
        found.sort(key=lambda hz: near[hz])
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


def baseten_enabled() -> bool:
    """Kill switch mirrors synthesis_enabled(): BASETEN_ENABLED=0 off.
    No key in .env is off too (Spark serves both attempts)."""
    return os.environ.get("BASETEN_ENABLED", "").strip().lower() not in (
        "0", "false", "no", "off"
    ) and bool(_load_secret("BASETEN_API_KEY"))


def _baseten_complete(instruction: str, timeout: int) -> str:
    """Text-only chat completion on Baseten (OpenAI-compatible,
    non-streaming)."""
    try:
        resp = requests.post(
            f"{BASETEN_API_BASE}/chat/completions",
            headers={"Authorization":
                     f"Bearer {_load_secret('BASETEN_API_KEY')}"},
            json={
                "model": BASETEN_SYNTH_MODEL,
                "messages": [{"role": "user", "content": instruction}],
                "reasoning_effort": BASETEN_REASONING_EFFORT,
                "max_tokens": BASETEN_MAX_TOKENS,
                "temperature": BASETEN_TEMPERATURE,
                "stream": False,
            },
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise PipelineError(f"Baseten unreachable: {e}") from e
    if resp.status_code != 200:
        raise PipelineError(
            f"Baseten HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        content = resp.json()["choices"][0]["message"].get("content")
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise PipelineError(f"Baseten bad response: {e}") from e
    if not (content or "").strip():
        raise _BadOutput("Baseten returned empty content")
    return content


def _within(deadline_s: float, fn: Any, *args: Any) -> Any:
    """fn(*args), or PipelineError once deadline_s of wall clock pass.

    The call runs on a daemon thread; past the deadline the ask moves on
    and the abandoned request dies on its own per-read timeout."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn(*args)
        except BaseException as e:
            box["error"] = e

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(deadline_s)
    if worker.is_alive():
        raise PipelineError(f"no answer within the {deadline_s:g} s deadline")
    if "error" in box:
        raise box["error"]
    return box["value"]


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
    # A spoken distance is a fact: kept exactly, never re-rounded, never
    # invented.
    if _metric_set(draft) != _metric_set(out):
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
    meta: dict[str, Any] | None = None,
) -> tuple[str, bool, int]:
    """Final synthesis: distill the draft to the answer contract.

    Returns (text, applied, latency_ms). Skips are contract, not
    failure: the kill switch (configuration), describe-mode (no
    question), and drafts with fewer than 4 subtractable (non-locked)
    sentences — a subtractive pass needs something to subtract, and
    short drafts can only lose facts (case_13 Q1). Otherwise the stage
    must run: model failure, timeout, or preservation-check failure
    retries once with a fresh budget, then raises StageFailed. The
    unsynthesized draft is never shipped in its place (failure rule,
    2026-10-27). The first attempt is Baseten when enabled, the retry
    contributor Spark; meta["synth_provider"] names the one that served.
    """
    if not synthesis_enabled() or not (question or "").strip():
        return draft, False, 0
    locked = _locked_sentences(draft)
    if len(split_sentences(draft)) - len(locked) < 4:
        return draft, False, 0
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
    metrics = sorted(_metric_set(draft))
    if metrics:
        facts.append("distances cited, keep each exactly: " +
                     ", ".join(metrics))
    prompt = (
        f"{SYNTHESIS_INSTRUCTION}\n"
        f"QUESTION: {question.strip()}\n"
        f"MUST KEEP: {'; '.join(facts) if facts else '(none)'}\n"
        + "".join(f"LOCKED: {s}\n" for s in locked)
        + f"DRAFT: {draft.strip()}"
    )
    lead = split_sentences(draft)[0].strip() if draft.strip() else ""
    t0 = time.time()

    def ok(text: str) -> bool:
        # Rule 3 holds through the polish: the draft's lead stays first.
        return _synthesis_ok(
            draft, text, locked, hazards, goal_head or None, subjects,
            scene_heads, landmark_heads,
        ) and re.sub(r"\s+", " ", text.strip()).startswith(
            re.sub(r"\s+", " ", lead))

    providers = (["baseten"] if baseten_enabled() else []) + \
        ["spark"] * STAGE_ATTEMPTS
    served = iter(providers)

    def attempt() -> str:
        provider = next(served)
        if provider == "baseten":
            raw = _baseten_complete(prompt, BASETEN_SYNTH_TIMEOUT)
        else:
            raw = _within(SYNTHESIS_DEADLINE_S, _text_complete,
                          SYNTHESIS_MODEL, prompt, SYNTHESIS_TIMEOUT)
        # One line: Baseten puts newlines between sentences.
        out = re.sub(r"\s+", " ", raw.strip().strip("'\"").strip())
        if meta is not None:
            meta["synth_provider"] = provider
        if ok(out):
            return out
        # Mojibake repair ("â.." for "—"); the fixed text must pass too.
        fixed = _demojibake(out)
        if fixed is not None and ok(fixed):
            return fixed
        raise _BadOutput(f"synthesis failed preservation: {out[:80]!r}")

    out = _run_stage("synthesis", attempt)
    return out, True, int((time.time() - t0) * 1000)


def run_ask(
    image: bytes,
    question: str,
    media_bytes: bytes | None = None,
    audio_bytes: bytes | None = None,
    trace: dict[str, Any] | None = None,
    capture_dir: Path | None = None,
    camera: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Full pipeline: question -> route -> perceive -> gate -> answer.
    trace, when given, receives what the response does not carry (the
    full uncollapsed scene) for the dev capture store. capture_dir is
    this ask's capture folder (store on): its photo/clip back the cached
    entry on disk. camera: the app's capture metadata (metric.parse_camera),
    None for an old app."""
    t0 = time.time()
    question = (question or "").strip()
    # Part A — precedence: text -> audio transcript -> clip transcript.
    # Lazy: ASR runs only when the text question is empty.
    question_source = "text" if question else "none"
    if not question and audio_enabled():
        if audio_bytes:
            t = transcribe_question(audio_bytes)
            if t:
                question, question_source = t, "audio"
        if not question and media_bytes:
            t = transcribe_question(media_bytes)
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
        qualifier = qualifier or clause_qualifier(question)

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
    # A count question segments its noun, so every instance is counted
    # — across the clip's frames when the ask came with one.
    scene = count_look(image, scene, question, media_bytes)
    if flow == "C" and match is not None:
        match = next((e for e in scene if e.get("match")), None)

    ask_id = uuid.uuid4().hex
    result, floor_ran, referent, full_scene = _compose(
        image, scene, question, question_source, question_used,
        flow, target, sam_prompt, qualifier, match, luminance, p85,
        t0, none_count, ask_id=ask_id, camera=camera,
    )
    _store_ask(ask_id, {
        "created": time.time(),
        # The composed scene: floor finds joined inside _compose, so the
        # stored scene must come from the result, not the pre-look local.
        # Every instance (not the collapsed chips); the clip for counts.
        "image": image,
        "media": media_bytes,
        "scene": full_scene,
        "flow": flow,
        "target": target,
        "question": question_used,
        "answer": result.get("answer"),
        "behavior": result.get("behavior"),
        "floor_done": floor_ran,
        "read_targets": [resolve_target(question)] if flow == "C" else [],
        "referent": referent,
        "camera": camera,
    }, capture_dir=capture_dir, media_dir=capture_dir)
    if trace is not None:
        trace.update({"ask_id": ask_id, "scene_full": full_scene,
                      "referent": referent})
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
    prior_referent: str = "",
    camera: dict[str, float] | None = None,
) -> tuple[dict[str, Any], bool, str, list[dict[str, Any]]]:
    """Shared tail: gate -> answer -> post-process -> synthesize -> log.

    Fresh asks and follow-ups differ only in perception (full vs cached
    + conditional); everything from the intent gate on runs here,
    identically. Follow-ups add prior Q/A context for reference
    resolution. allow_floor_look=False skips the floor second look
    (follow-ups whose media already had it). prior_referent is the
    previous ask's subject, which a pronoun resolves to. camera is the
    app's capture metadata (metric distance); None is today's answers,
    unchanged. Returns
    (result, floor_ran, referent, scene) so callers can record perception
    state, the subject, and the full scene (every instance — the shipped
    scene_model is collapsed for display) in the cache.
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
    kind = question_kind(question, flow, navigation)

    # Field test §1 — floor second look for navigation asks (not in the
    # dark: nothing to see). Finds join the scene before assessment, so
    # the path verdict accounts for small floor hazards.
    floor_found, floor_ran = 0, False
    if kind == "path" and not (crossing_like or dosing) \
            and allow_floor_look and floor_look_enabled() \
            and p85 >= DARK_P85:
        floor_new = floor_look(image, scene)
        floor_ran = True
        if floor_new:
            scene = merge_entries(scene + floor_new)
            floor_found = len(floor_new)

    # Metric distance, once, on the final (merged) instances.
    distance_log: list[dict[str, Any]] = []
    if camera is not None:
        with Image.open(io.BytesIO(image)) as im:
            scene, distance_log = apply_distances(scene, camera, im.size[1])

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
                  f"instance matches, and say where it sits within its "
                  f"group (for example: 'the right-hand column, second "
                  f"from the bottom', or 'just above the parcel locker').")
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
    if hazards and not (crossing_like or dosing):
        extra += (
            " SAFETY: these hazards are present and you MUST name each as "
            f"a warning in the answer: {', '.join(hazards)}."
        )
    # Rules 1-3: the goal is what the question names, obstructions are
    # rule-2 floor objects, and the lead answers the question asked —
    # written here, from computed facts, so Spark only adds detail.
    nav_assessment: dict[str, Any] | None = None
    opening: list[str] = []
    referent = prior_referent
    if flow == "C":
        referent = (str(match.get("name", "")) if match
                    else resolve_target(question))
    elif flow == "B" and sam_prompt:
        referent = sam_prompt
    if kind in ("path", "distance") and not (crossing_like or dosing):
        with Image.open(io.BytesIO(image)) as im:
            img_w, img_h = im.size
        assessment = _path_assessment(
            scene, question, height=img_h, hint=prior_referent,
            width=img_w,
            openings=passage_look(image, scene, question, prior_referent),
            camera=camera)
        nav_assessment = assessment
        if assessment["goal"] or assessment["verdict"] == "no_goal_visible":
            referent = str(assessment["goal_desc"])
        if kind == "path":
            opening = _path_opening(assessment)
            if assessment["verdict"] == "clear" and p85 < DARK_P85:
                # Nothing certified a clear path in the dark (the floor
                # look does not run there): say so instead of "Yes —".
                opening = ["It's too dark for me to tell whether the way "
                           "ahead is clear."]
            obs = "; ".join(
                f"{o['name']} at {o['clock']} o'clock, "
                + (_dist_words("", o["distance_m"]) if "distance_m" in o
                   else o["size_class"])
                for o in assessment["on_path"]) or "none"
            extra += (
                " NAVIGATION: this is a route question. Facts: goal "
                f"{assessment['goal'] or 'the way ahead'}; verdict "
                f"{assessment['verdict']}; on-path obstacles: {obs}. "
                "Ground you walk on (floor, patio, lawn, sidewalk) and "
                "things resting on tables or counters are never "
                "obstacles."
            )
        else:
            opening = _distance_opening(assessment, question)
            if not opening:
                extra += (" DISTANCE: lead with how far away the thing "
                          "asked about is, then where it is.")
    count_words: set[str] = set()
    if kind == "count" and not (crossing_like or dosing):
        subject = count_subject(question) or ""
        with Image.open(io.BytesIO(image)) as im:
            width, height = im.size
        opening = _count_opening(scene, subject, width, height)
        words = subject.split()
        count_words = {_singular(w) for w in words[-2:]}
        referent = " ".join(words[:-1] + [_singular(words[-1])])
        extra += (" COUNT: the count in the opening is final, taken from "
                  "the scene; never state a number of them yourself.")
    if opening:
        extra += (
            " OPENING: the answer begins with these sentences, already "
            f"written and spoken first: \"{' '.join(opening)}\" Do not "
            "repeat them and do not mention again any object they name. "
            "Add at most two short sentences, only with what matters for "
            "this question: footing (wet, uneven, a step or a change of "
            "surface — never color or material) or one orientation "
            "landmark. If nothing more matters, the answer is empty."
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
        body, confidence = _run_stage("answer", lambda: _spark_answer(
            image, question, _prompt_scene(scene, kind), flow, extra,
            landmarks, allow_empty=bool(opening)))
        # Rule 4: the draft is right before synthesis ever sees it.
        answer = _tidy(opening, body, kind, scene, nav_assessment,
                       hazards, question, count_words)

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
                # Replace Spark's own bare "Found your ADHD medicine."
                # rather than stacking two found-sentences.
                parts = split_sentences(answer)
                if parts and re.fullmatch(
                        r"(?:i\s+)?found\s+(?:it|your\s[^.!]*)[.!]?",
                        parts[0].strip(), re.IGNORECASE):
                    parts = parts[1:]
                answer = " ".join(
                    [f"Found your {label.title()}."] + parts)
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
    # Escalations skip it: a polish pass (and its failure) must never
    # stand between the user and the volunteer handoff.
    synth_applied, synth_ms, synth_meta = False, 0, {}
    if behavior != "ESCALATE":
        answer, synth_applied, synth_ms = synthesize_answer(
            answer, question, scene, nav_assessment, hazards, landmarks,
            synth_meta)

    result = {
        "answer": answer,
        "behavior": behavior,
        "confidence": round(float(confidence), 3),
        "scene_model": ship_scene(scene),
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
    # Instances per name ("tortoiseshell cat x2"): a deduplicated list
    # hid how many entries the scene held.
    tally: dict[str, int] = {}
    for e in scene:
        n = str(e.get("name", "")).strip()
        if n:
            tally[n] = tally.get(n, 0) + 1
    seen = [n if c == 1 else f"{n} x{c}" for n, c in tally.items()]
    print(json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "question": question_used,
        "question_source": question_source,
        "flow": flow,
        "kind": kind,
        "verdict": (nav_assessment or {}).get("verdict"),
        "passage": (nav_assessment or {}).get("passage_source"),
        "behavior": behavior,
        "gate": gate,
        "scene_objects": seen,
        "latency_ms": int((time.time() - t0) * 1000),
        "answer": answer[:600],
        "synth_applied": synth_applied,
        "synth_ms": synth_ms,
        "synth_provider": synth_meta.get("synth_provider"),
        "none_reads": none_count,
        "floor_look": floor_ran,
        "floor_found": floor_found,
        "ask_id": ask_id,
        "followup": followup,
        # Only with capture metadata: old-app log lines stay as they were.
        **({"camera": camera, "distances": distance_log}
           if camera is not None else {}),
    }), flush=True)
    return result, floor_ran, referent, scene


# ----------------------------------------------------------------------------
# Follow-ups: same media, new question, no re-perception
# ----------------------------------------------------------------------------

class FollowupExpired(Exception):
    """Follow-up references an unknown or expired ask id."""


# The TTL lives with the store, whose count cap must honor it too.
ASK_TTL_DEFAULT_S = capture.ASK_TTL_DEFAULT_S
ask_ttl_s = capture.ask_ttl_s
ASK_CACHE_MAX = 32  # in memory; the disk layer is capped by the captures

# Hot layer. With the capture store on (dev), every entry is also on disk
# (capture.persist_ask) and survives a restart; with it off (production)
# the cache is memory-only, exactly as before. Production follow-up
# persistence is a separate future decision.
_ASK_CACHE: dict[str, dict[str, Any]] = {}

ASK_EXPIRED_MESSAGE = (
    "that ask has expired or is unknown — "
    "please ask again as a new question"
)


def _sweep_asks() -> None:
    now, ttl = time.time(), ask_ttl_s()
    for key in [k for k, v in _ASK_CACHE.items()
                if now - v.get("created", 0) > ttl]:
        del _ASK_CACHE[key]
    while len(_ASK_CACHE) > ASK_CACHE_MAX:
        oldest = min(_ASK_CACHE, key=lambda k: _ASK_CACHE[k]["created"])
        del _ASK_CACHE[oldest]  # still on disk when persisted
    # Expiry deletes on disk too, store on or off: whatever an earlier
    # run persisted must not outlive its conversation.
    capture.sweep_asks(ttl, now)


def _store_ask(
    ask_id: str, entry: dict[str, Any],
    capture_dir: Path | None = None, media_dir: Path | None = None,
) -> None:
    """Cache an ask. With a capture folder (store on) and the folder
    holding its photo/clip, the entry is written through to disk; a
    failed write leaves it memory-only. Written before the sweep: a
    follow-up whose parent expired while it was being answered must
    hold the root's media before the sweep looks for holders."""
    _ASK_CACHE[ask_id] = entry
    if capture_dir is not None and media_dir is not None \
            and capture.enabled():
        meta = {k: v for k, v in entry.items()
                if k not in ("image", "media", "media_dir")}
        if capture.persist_ask(capture_dir, ask_id, meta, media_dir):
            entry["media_dir"] = media_dir
    _sweep_asks()


def sweep_ask_store() -> None:
    """Expire cached asks now (server start: entries that expired while
    it was down lose their media without waiting for the next ask)."""
    _sweep_asks()


def _load_ask(ask_id: str) -> dict[str, Any]:
    """The cached ask: memory first, then disk (after a restart or a
    memory eviction). Unknown, expired, or unreadable is the same 410."""
    _sweep_asks()
    entry = _ASK_CACHE.get(ask_id)
    if entry is None and capture.enabled():
        found = capture.load_ask(ask_id)
        if found is not None \
                and time.time() - found[0]["created"] <= ask_ttl_s():
            entry = found[0]
            entry["media_dir"] = found[1]
            _ASK_CACHE[ask_id] = entry
    if entry is None:
        raise FollowupExpired(ASK_EXPIRED_MESSAGE)
    return entry


PRIOR_CONTEXT_TEMPLATE = (
    " Conversation so far — the user asked: {pq} Assistant answered: {pa} "
    "The new question may refer back to that exchange ('it', 'the "
    "floor'); resolve such references against it and the scene model."
)


def run_followup(
    ask_id: str,
    question: str,
    audio_bytes: bytes | None = None,
    trace: dict[str, Any] | None = None,
    capture_dir: Path | None = None,
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
        t = transcribe_question(audio_bytes)
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
        qualifier = qualifier or clause_qualifier(question)

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
    # A count names a noun the cached scene may never have segmented.
    scene = count_look(image, scene, question, entry.get("media"))

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
    result, floor_ran_now, referent, full_scene = _compose(
        image, scene, question, question_source, question_used,
        flow, target, sam_prompt, qualifier, match, luminance, p85,
        t0, none_count,
        prior_context=prior_context, ask_id=new_id, followup=True,
        allow_floor_look=not floor_done,
        prior_referent=str(entry.get("referent") or ""),
        camera=entry.get("camera"),
    )
    floor_done = floor_done or floor_ran_now
    _store_ask(new_id, {
        "created": time.time(),
        # Composed scene (conditional perception joined inside _compose).
        "image": image,
        "media": entry.get("media"),
        "scene": full_scene,
        "flow": flow,
        "target": target,
        "question": question_used,
        "answer": result.get("answer"),
        "behavior": result.get("behavior"),
        "floor_done": floor_done,
        "read_targets": read_targets,
        "referent": referent,
        "camera": entry.get("camera"),
    }, capture_dir=capture_dir, media_dir=entry.get("media_dir"))
    if trace is not None:
        trace.update({"ask_id": new_id, "parent_ask_id": ask_id,
                      "scene_full": full_scene, "referent": referent})
    return result
