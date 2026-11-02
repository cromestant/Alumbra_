"""Alumbra backend — FastAPI entry point.

Request flow (design doc, section 3 "Where the bytes go"):

    Android app --HTTPS--> POST /ask --ffmpeg--> frames + audio
        --> SAM 3.1 --> scene model --> Muse Spark (Model API)
        --> answer, or escalate to a volunteer
"""
from __future__ import annotations

import asyncio
import hmac
from pathlib import Path

from fastapi import (
    Body, FastAPI, File, Form, Header, HTTPException, Query, UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse, Response

from . import capture, escalation, metric
from .pipeline import (
    FollowupExpired, PipelineError, StageFailed, _load_secret, case_photo,
    run_ask, run_followup, sweep_ask_store,
)

CONSOLE_PAGE = Path(__file__).resolve().parent / "static" / "volunteer.html"
# Failure rule (2026-10-27): a stage that did not run errors the ask
# (503) with a line the app speaks as-is; the user retries.
TRY_AGAIN = "Something went wrong — please try again."


def _stage_failed(e: StageFailed) -> HTTPException:
    return HTTPException(status_code=503, detail={
        "error": "stage_failed", "stage": e.stage,
        "message": TRY_AGAIN, "cause": str(e),
    })


def _app_token(given: str | None) -> None:
    """APP_TOKEN gates /ask and /followup (X-App-Token header, entered
    in the app's Settings). Unset: the check is off."""
    want = _load_secret("APP_TOKEN").strip()
    if want and not hmac.compare_digest(
            (given or "").encode(), want.encode()):
        raise HTTPException(status_code=401, detail="bad app token")


def _escalation_on() -> None:
    """Off (kill switch or no VOLUNTEER_TOKEN): every escalation route
    is a plain 404."""
    if not escalation.enabled():
        raise HTTPException(status_code=404, detail="Not Found")


def _volunteer(header: str | None, query: str | None) -> None:
    """The console's shared demo token (header, or ?token= for <img>)."""
    _escalation_on()
    if not escalation.token_ok(header or query):
        raise HTTPException(status_code=401, detail="bad volunteer token")


def _attach_case(result: dict, cap: Path | None) -> None:
    case = result.get("escalation")
    if case:
        escalation.attach_folder(case["id"], cap)


def create_app() -> FastAPI:
    # Persisted follow-ups that expired while the server was down go now.
    sweep_ask_store()
    app = FastAPI(
        title="Alumbra",
        description="Visual assistance for blind and low-vision users.",
        version="0.1.0",
    )

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

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
        pitch_deg: str | None = Form(None),
        focal_px: str | None = Form(None),
        x_app_token: str | None = Header(None),
    ) -> JSONResponse:
        """Snap & ask: JPEG photo + optional question -> grounded answer.

        media/audio carry the ask's clip/window audio; when the text
        question is empty the pipeline transcribes them for the question.
        pitch_deg/focal_px are the app's capture metadata (metric.py);
        without both, the answer is exactly as before.
        Response: {answer, behavior, confidence, scene_model, flow, gate,
        signals, question_used, question_source} where behavior is
        ANSWER or ESCALATE.
        """
        _app_token(x_app_token)
        data = await image.read()
        if not data:
            raise HTTPException(status_code=400, detail="empty image upload")
        media_bytes = await media.read() if media and media.filename else None
        audio_bytes = await audio.read() if audio and audio.filename else None
        camera = metric.parse_camera(pitch_deg, focal_px)
        # The capture keeps the upload as received; the pipeline sees it
        # upright and capped (a follow-up's reload does the same).
        cap = capture.begin("ask", question or "", data, media_bytes,
                            audio_bytes, camera=camera)
        data, scale = metric.upright_photo(data)
        camera = metric.scale_camera(camera, scale)
        trace: dict = {}
        try:
            result = await asyncio.to_thread(
                run_ask, data, question or "", media_bytes, audio_bytes,
                trace, cap, camera,
            )
        except StageFailed as e:
            err = _stage_failed(e)
            capture.fail(cap, err.status_code, err.detail)
            raise err
        except PipelineError as e:
            capture.fail(cap, 502, str(e))
            raise HTTPException(status_code=502, detail=str(e))
        except Exception as e:
            capture.fail(cap, 500, repr(e))
            raise
        capture.finish(cap, result, trace)
        _attach_case(result, cap)
        return JSONResponse(content=result)

    @app.post("/followup")
    async def ask_followup(
        ask_id: str = Form(...),
        question: str = Form(""),
        audio: UploadFile | None = File(None),
        x_app_token: str | None = Header(None),
    ) -> JSONResponse:
        """Follow-up on a cached ask: same media, new question.

        No image: perception (naming, segmentation, merging, second
        looks) is reused from the cached ask. 410 when the ask id is
        unknown or its cache entry expired (ASK_TTL_S, default an hour) — the app then
        says so plainly and offers a fresh ask.
        """
        _app_token(x_app_token)
        audio_bytes = await audio.read() if audio and audio.filename else None
        # The photo/clip live in the parent ask's capture.
        cap = capture.begin("followup", question or "", audio=audio_bytes,
                            parent_ask_id=ask_id)
        trace: dict = {}
        try:
            result = await asyncio.to_thread(
                run_followup, ask_id or "", question or "", audio_bytes,
                trace, cap,
            )
        except FollowupExpired as e:
            capture.fail(cap, 410, str(e))
            raise HTTPException(status_code=410, detail=str(e))
        except StageFailed as e:
            err = _stage_failed(e)
            capture.fail(cap, err.status_code, err.detail)
            raise err
        except PipelineError as e:
            capture.fail(cap, 502, str(e))
            raise HTTPException(status_code=502, detail=str(e))
        except Exception as e:
            capture.fail(cap, 500, repr(e))
            raise
        capture.finish(cap, result, trace)
        _attach_case(result, cap)
        return JSONResponse(content=result)

    # ------------------------------------------------------------------
    # Escalation: volunteer console + the app's wait (escalation.py).
    # Console routes need VOLUNTEER_TOKEN (a demo boundary, not a
    # security model); the app's poll/cancel hold only the case id.
    # ------------------------------------------------------------------

    @app.get("/volunteer")
    async def volunteer_console() -> FileResponse:
        _escalation_on()
        return FileResponse(CONSOLE_PAGE, media_type="text/html")

    @app.get("/v1/escalations")
    async def escalations_list(
        x_volunteer_token: str | None = Header(None),
        token: str | None = Query(None),
    ) -> dict:
        _volunteer(x_volunteer_token, token)
        return {"cases": escalation.list_cases(),
                "poll_s": escalation.POLL_S}

    @app.get("/v1/escalations/{case_id}/photo")
    async def escalation_photo(
        case_id: str,
        x_volunteer_token: str | None = Header(None),
        token: str | None = Query(None),
    ) -> Response:
        _volunteer(x_volunteer_token, token)
        ask_id = escalation.ask_id_of(case_id)
        photo = None
        if ask_id:
            photo = await asyncio.to_thread(case_photo, ask_id)
        if photo is None:
            raise HTTPException(status_code=404, detail="photo gone")
        return Response(content=photo, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    @app.post("/v1/escalations/{case_id}/reply")
    async def escalation_reply(
        case_id: str,
        body: dict = Body(...),
        x_volunteer_token: str | None = Header(None),
        token: str | None = Query(None),
    ) -> dict:
        _volunteer(x_volunteer_token, token)
        try:
            case = escalation.reply(case_id, str(body.get("volunteer", "")),
                                    str(body.get("reply", "")))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except escalation.CaseClosed as e:
            raise HTTPException(status_code=409,
                                detail=f"case is {e.args[0]}")
        if case is None:
            raise HTTPException(status_code=404, detail="unknown case")
        return case

    @app.get("/v1/escalations/{case_id}")
    async def escalation_status(case_id: str) -> dict:
        _escalation_on()
        out = escalation.status(case_id)
        if out is None:
            raise HTTPException(status_code=404, detail="unknown case")
        return out

    @app.post("/v1/escalations/{case_id}/cancel")
    async def escalation_cancel(case_id: str) -> dict:
        _escalation_on()
        out = escalation.cancel(case_id)
        if out is None:
            raise HTTPException(status_code=404, detail="unknown case")
        return out

    return app


app = create_app()
