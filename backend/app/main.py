"""Alumbra backend — FastAPI entry point.

Request flow (design doc, section 3 "Where the bytes go"):

    Android app --HTTPS--> POST /v1/clips --ffmpeg--> frames + audio
        --> SAM 3.1 --> scene model --> Muse Spark (Model API)
        --> answer, or escalate to a volunteer
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from .pipeline import PipelineError, run_ask

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "uploads"
ALLOWED_VIDEO_TYPES = {"video/mp4", "video/3gpp", "video/webm"}


def create_app() -> FastAPI:
    app = FastAPI(
        title="Alumbra",
        description="Visual assistance for blind and low-vision users.",
        version="0.1.0",
    )

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.post("/v1/clips")
    async def upload_clip(video: UploadFile = File(...)) -> JSONResponse:
        """Accept a 5-10s video clip. The ingest pipeline lands here.

        Saves the upload to uploads/ and returns 501 until the pipeline
        (extract_frames -> ... -> answer_with_muse) is wired up.
        """
        if video.content_type not in ALLOWED_VIDEO_TYPES:
            raise HTTPException(
                status_code=415,
                detail=f"unsupported media type: {video.content_type}",
            )
        dest = UPLOAD_DIR / f"{uuid4().hex}.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as f:
            shutil.copyfileobj(video.file, f)
        return JSONResponse(
            status_code=501,
            content={"status": "not_implemented", "clip_id": dest.stem},
        )

    @app.post("/v1/asks")
    async def ask() -> JSONResponse:
        """Snap & ask: image + question. Wired once the pipeline exists."""
        return JSONResponse(
            status_code=501,
            content={"status": "not_implemented"},
        )

    @app.post("/ask")
    async def ask_photo(
        image: UploadFile = File(...),
        question: str = Form(""),
        media: UploadFile | None = File(None),
        audio: UploadFile | None = File(None),
    ) -> JSONResponse:
        """Snap & ask: JPEG photo + optional question -> grounded answer.

        media/audio carry the ask's clip/window audio; when the text
        question is empty the pipeline transcribes them for the question.
        Response: {answer, behavior, confidence, scene_model, flow, gate,
        signals, question_used, question_source} where behavior is
        ANSWER or ESCALATE.
        """
        data = await image.read()
        if not data:
            raise HTTPException(status_code=400, detail="empty image upload")
        media_bytes = await media.read() if media and media.filename else None
        audio_bytes = await audio.read() if audio and audio.filename else None
        try:
            result = await asyncio.to_thread(
                run_ask, data, question or "", media_bytes, audio_bytes
            )
        except PipelineError as e:
            raise HTTPException(status_code=502, detail=str(e))
        return JSONResponse(content=result)

    return app


app = create_app()
