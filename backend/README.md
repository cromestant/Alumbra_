# Alumbra backend

Python FastAPI service. Ingests clips and questions from the Android app,
runs the AI pipeline (frames → SAM 3.1 → scene model → Muse Spark via the
Meta Model API), and dispatches volunteer escalations.

## Run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

- `GET /health` → `{"status": "ok"}`
- `POST /v1/clips` — multipart video upload. Saves the clip to `uploads/`
  and returns 501 until the pipeline is wired (hackathon days 1–2).
- `POST /v1/asks` — snap & ask. Stubbed with 501 for now.

## Pipeline stages

`app/pipeline.py` holds the stage contracts:

1. `extract_frames` — ffmpeg pulls key frames (~1 fps) from the clip.
2. `transcribe_audio` — the clip's audio track → the question text.
3. `segment_frames` — SAM 3.1 over each frame → masks + regions.
4. `build_scene_model` — merged scene model: objects, clock-face directions,
   distances, OCR text. Pure logic, unit-testable.
5. `answer_with_muse` — Muse Spark (multimodal, Model API) → answer +
   confidence. Below threshold, or on a hard-escalate category
   (navigation, medication, …), escalate to a volunteer instead.

All stages currently raise `NotImplementedError` — that is deliberate.
The hackathon build order is in the design doc, section 6.
