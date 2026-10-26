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
import io
import json
import os
import re
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


class PipelineError(RuntimeError):
    """Upstream Model API failure the route reports as 502."""


def _load_key() -> str:
    key = os.environ.get("META_MODEL_API_KEY", "").strip()
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
            if line.startswith("META_MODEL_API_KEY"):
                _, _, val = line.partition("=")
                val = val.strip().strip("'\"")
                if val:
                    return val
    return ""


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
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Labeled item: segment, crop, read. Returns (scene, match_or_None)."""
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
    for (box, conf), label in zip(boxes, labels):
        name = label.strip() or "unreadable label"
        hit = bool(want) and (want in label.upper() or label.upper() in want)
        ent = entry_for(name, box, conf, width, height,
                        label=label, match=hit)
        scene.append(ent)
        if hit and match is None:
            match = ent
    return scene, match


# ----------------------------------------------------------------------------
# Answer + rule layer
# ----------------------------------------------------------------------------

def _answer_instruction(
    question: str,
    scene: list[dict[str, Any]],
    flow: str,
    extra: str = "",
) -> str:
    q = question.strip() or "(no question asked — describe the scene)"
    return (
        "You are a visual-assistance narrator for a blind person. "
        "Look at the photo and the scene model below and answer the "
        "question in plain, spoken-friendly language: short sentences, "
        "concrete directions using the clock-face positions and distances "
        "from the scene model (for example: 'at your 12 o'clock, a few "
        "steps away'). Do not mention pixels, boxes, or confidence scores."
        f"{extra}\n"
        f"Question: {q}\n"
        f"Scene model: {json.dumps(scene)}\n"
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
) -> tuple[str, float]:
    raw = _vlm_text(
        SPARK_MODEL, image,
        _answer_instruction(question, scene, flow, extra), SPARK_TIMEOUT,
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


def run_ask(image: bytes, question: str) -> dict[str, Any]:
    """Full pipeline: route -> perceive -> answer -> rules."""
    question = (question or "").strip()
    crossing = is_crossing_question(question)
    dosing = is_dosing_question(question)
    flow, target = route(question)

    match: dict[str, Any] | None = None
    if flow == "C":
        scene, match = flow_c(image, question, target or "")
    elif flow == "B":
        scene = flow_b(image, target or "")
    else:
        scene = flow_a(image)

    extra = ""
    hazards = _hazards_in(scene)
    if crossing:
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
    if hazards and not (crossing or dosing):
        extra += (
            " SAFETY: these hazards are present and you MUST name each as "
            f"a warning in the answer: {', '.join(hazards)}."
        )
    if flow == "C" and match is None:
        extra += (
            " The requested item could NOT be confirmed from any readable "
            "label: say so plainly and do not guess its location."
        )

    answer, confidence = _spark_answer(image, question, scene, flow, extra)
    behavior = "ANSWER"

    if crossing or dosing:
        behavior = "ESCALATE"
        if crossing and any(rx.search(answer) for rx in GO_AHEAD_RES):
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
    else:
        for hz in hazards:
            if hz not in answer.lower():
                answer = f"{answer} Warning: {hz} ahead."
        if flow == "C" and match is None:
            confidence = min(confidence, 0.3)
            if not _UNCERTAINTY_RE.search(answer):
                answer = (
                    f"{answer} I couldn't confirm "
                    f"{target} from the labels I could read."
                )

    return {
        "answer": answer,
        "behavior": behavior,
        "confidence": round(float(confidence), 3),
        "scene_model": scene,
        "flow": flow,
    }
