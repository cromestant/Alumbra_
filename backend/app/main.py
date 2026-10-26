"""Alumbra backend — FastAPI entry point.

Request flow (design doc, section 3 "Where the bytes go"):

    Android app --HTTPS--> POST /v1/clips --ffmpeg--> frames + audio
        --> SAM 3.1 --> scene model --> Muse Spark (Model API)
        --> answer, or escalate to a volunteer
"""
from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

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

    return app


app = create_app()
