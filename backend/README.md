# Alumbra backend

Python FastAPI service. Ingests clips and questions from the Android app,
runs the AI pipeline (frames → SAM 3.1 → scene model → Muse Spark via the
Meta Model API), and dispatches volunteer escalations.

## Run

Needs Python 3.12 or newer (tested on 3.12 and 3.14) and ffmpeg on the PATH (`apt install ffmpeg`).

```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
cp backend/.env.example .env   # repo root; fill in the keys
cd backend && ../.venv/bin/python -m uvicorn app.main:app --port 8123
```

Production runs under systemd from `deploy/alumbra-api.service`: one
worker only (escalations and follow-ups are per-process state).

- `GET /health` → `{"status": "ok"}`
- `POST /ask`, `POST /followup` — the app's endpoints.
- `GET /volunteer` — the volunteer console (needs `VOLUNTEER_TOKEN`).

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
