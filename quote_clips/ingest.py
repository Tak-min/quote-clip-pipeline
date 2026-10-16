"""quote_clips/ingest.py — Download and transcribe a video (remixed from clip_pipeline/ingest.py).

Adds sentence-level segments alongside word timestamps so subtitles and
translation can work per phrase.

Downloads with yt-dlp and transcribes with faster-whisper to produce
word-level timestamps. Output is written to a JSON file alongside the video.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
from loguru import logger


def download_video(url: str, out_dir: str) -> str:
    """Download the highest-resolution streams via yt-dlp (merged to mp4), return local file path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(Path(sys.executable).parent / "yt-dlp"),
        "--no-playlist",
        "--format", "bv*+ba/b",
        "--format-sort", "res,fps",
        "--merge-output-format", "mp4",
        "--output", str(out_dir / "%(id)s.%(ext)s"),
        "--print", "after_move:filepath",
        url,
    ]
    logger.info(f"Downloading: {url}")
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    path = result.stdout.strip().splitlines()[-1]
    logger.info(f"Downloaded to: {path}")
    height = _probe_height(path)
    if height and height < 480:
        logger.warning(f"Low resolution source ({height}p): output will look soft. Prefer a higher-quality upload.")
    return path


def _probe_height(video_path: str) -> int:
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=height", "-of", "csv=p=0", video_path]
    try:
        return int(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        return 0


def transcribe_video(
    video_path: str,
    model_size: str = "base",
    language: Optional[str] = None,
    device: str = "cpu",
) -> Dict:
    """Transcribe audio with faster-whisper and return word-level timestamps.

    Returns:
        {
            "words": [{"text": str, "start": float, "end": float}, ...],
            "language": str,
            "duration": float,
        }
    """
    from faster_whisper import WhisperModel  # deferred import

    logger.info(f"Transcribing {video_path} with model={model_size}, device={device}")
    model = WhisperModel(model_size, device=device, compute_type="int8")

    # Decode with ffmpeg ourselves: faster-whisper's PyAV path breaks on PyAV>=15
    # (removed `metadata_errors` kwarg).
    pcm = subprocess.run(
        ["ffmpeg", "-nostdin", "-i", video_path, "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
        check=True, capture_output=True,
    ).stdout
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

    segments, info = model.transcribe(
        audio,
        language=language,
        word_timestamps=True,
        vad_filter=True,
    )

    words: List[Dict] = []
    segs: List[Dict] = []
    for segment in segments:
        text = segment.text.strip()
        if text:
            segs.append({"text": text, "start": round(segment.start, 3), "end": round(segment.end, 3)})
        if segment.words is None:
            continue
        for w in segment.words:
            words.append({"text": w.word.strip(), "start": round(w.start, 3), "end": round(w.end, 3)})

    duration = words[-1]["end"] if words else _probe_duration(video_path)

    result = {
        "words": words,
        "segments": segs,
        "language": info.language,
        "duration": duration,
    }
    logger.info(f"Transcribed {len(words)} words, language={info.language}, duration={duration:.1f}s")
    return result


def _probe_duration(video_path: str) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
        return float(out.strip())
    except Exception:
        return 0.0


def download_and_transcribe(
    url: str,
    work_dir: str,
    model_size: str = "base",
    language: Optional[str] = None,
    device: str = "cpu",
    force_retranscribe: bool = False,
) -> Dict:
    """Download video and transcribe; cache transcript as <video_id>.words.json.

    Returns the transcript dict + injects "video_path" and "video_id" keys.
    """
    video_path = download_video(url, work_dir)
    video_id = Path(video_path).stem

    transcript_path = Path(work_dir) / f"{video_id}.{model_size}.transcript.json"
    if transcript_path.exists() and not force_retranscribe:
        logger.info(f"Loading cached transcript: {transcript_path}")
        with open(transcript_path, encoding="utf-8") as f:
            result = json.load(f)
    else:
        result = transcribe_video(video_path, model_size=model_size, language=language, device=device)
        with open(transcript_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        logger.info(f"Transcript saved: {transcript_path}")

    result["video_path"] = video_path
    result["video_id"] = video_id
    return result
