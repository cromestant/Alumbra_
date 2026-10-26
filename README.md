# Alumbra

Visual assistance for blind and low-vision users — Charles's entry for the
Meta AI Developer Hackathon (Oct 26 – Nov 4, 2026).

The user points their phone camera (or sends a photo / short clip) and asks a
question. The system segments the scene with SAM 3.1, builds a structured
scene model (objects, clock-face directions, distances, OCR text), and Muse
answers with spatially grounded descriptions — not just "a kitchen," but
"the red mug is at your 2 o'clock, about a meter away, left of the sink."
When Muse isn't confident, it escalates to a human volunteer with full
context attached. AI handles the everyday; humans handle the hard.

Design doc (source of truth, lives on the agent VM, not in this repo):
`~/workspace/goals/meta-ai-developer-hackathon-brainstorm/files/visual-assistance-design-doc.md`

## Layout

- `backend/` — Python FastAPI service: clip/ask ingest, AI pipeline stages
  (frames → SAM 3.1 → scene model → Muse Spark via the Meta Model API),
  volunteer dispatch.
- `android/` — native Android app (Kotlin + CameraX): hold-to-record capture,
  ask by voice/text, TTS readout. TalkBack-labeled from day one.

## Quick start

Backend:

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
# GET http://localhost:8000/health
```

Android:

```bash
cd android
./gradlew assembleDebug
# APK lands at app/build/outputs/apk/debug/app-debug.apk
```

Requires JDK 17+ and the Android SDK (via `local.properties` or
`ANDROID_HOME`). CameraX wiring, pipeline stages, and volunteer dispatch are
TODOs for hackathon time — see the READMEs in `backend/` and `android/`.
