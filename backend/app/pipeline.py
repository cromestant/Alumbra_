"""Alumbra AI pipeline — stage stubs.

Contracts mirror design doc section 3 ("Where the bytes go"). Every stage
raises NotImplementedError until hackathon days 1-2 wire it up.

Stage order:
    extract_frames -> transcribe_audio -> segment_frames
        -> build_scene_model -> answer_with_muse
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def extract_frames(video_path: str | Path, fps: float = 1.0) -> list[Path]:
    """Pull key frames from a clip with ffmpeg (~1 fps).

    Input: path to the uploaded clip.
    Output: paths to extracted JPEG frames, in temporal order.
    """
    raise NotImplementedError(
        "extract_frames: ffmpeg key-frame extraction not wired yet"
    )


def transcribe_audio(video_path: str | Path) -> str:
    """Transcribe the clip's audio track into the user's question text.

    Input: path to the uploaded clip.
    Output: the transcribed question ("" if no speech detected).
    Server-side transcription; Android SpeechRecognizer is the on-device
    fallback when the question is asked separately from the clip.
    """
    raise NotImplementedError(
        "transcribe_audio: speech-to-text not wired yet"
    )


def segment_frames(frame_paths: list[Path]) -> list[dict[str, Any]]:
    """Run SAM 3.1 over each frame.

    Input: frame paths from extract_frames.
    Output: per frame, a list of regions:
        {"mask": <mask>, "label": <str>, "bbox": (x1, y1, x2, y2)}.
    """
    raise NotImplementedError(
        "segment_frames: SAM 3.1 not wired yet"
    )


def build_scene_model(
    segmentations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Merge per-frame segmentations into one structured scene model.

    Input: segment_frames output across the clip's frames.
    Output: {
        "objects": [{"label", "clock_face", "distance_m", "bbox"}],
        "ocr_text": [<str>],
    }
    Pure logic, unit-testable: known image in -> expected object list out.
    """
    raise NotImplementedError(
        "build_scene_model: scene model builder not written yet"
    )


def answer_with_muse(
    frames: list[Path],
    scene_model: dict[str, Any],
    question: str,
) -> tuple[str, float]:
    """Ask Muse Spark (multimodal, via the Meta Model API).

    Input: key frames + scene model + the user's question.
    Output: (answer text, confidence 0.0-1.0).

    The caller escalates to a volunteer instead of returning the answer
    when confidence is below threshold, or on a hard-escalate category
    regardless of confidence: street crossing / navigation, medication
    dosing, anything where a wrong answer causes harm.
    """
    raise NotImplementedError(
        "answer_with_muse: Model API call not wired yet"
    )
