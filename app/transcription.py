"""Amazon Transcribe batch jobs for recorded answers."""
from __future__ import annotations

import json
import time
from functools import lru_cache

import boto3

import config
import storage

MEDIA_FORMATS = {
    "audio/webm": "webm",
    "video/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mp4": "mp4",
    "video/mp4": "mp4",
    "audio/x-m4a": "m4a",
    "audio/m4a": "m4a",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/flac": "flac",
}


@lru_cache(maxsize=1)
def client():
    return boto3.client("transcribe", region_name=config.AWS_REGION)


def media_format(content_type: str) -> str | None:
    return MEDIA_FORMATS.get((content_type or "").split(";")[0].strip().lower())


def start_job(job_name: str, audio_key: str, fmt: str, output_key: str) -> None:
    params = dict(
        TranscriptionJobName=job_name,
        Media={"MediaFileUri": f"s3://{config.BUCKET_NAME}/{audio_key}"},
        MediaFormat=fmt,
        OutputBucketName=config.BUCKET_NAME,
        OutputKey=output_key,
    )
    if len(config.TRANSCRIBE_LANGUAGES) == 1:
        params["LanguageCode"] = config.TRANSCRIBE_LANGUAGES[0]
    else:
        params["IdentifyLanguage"] = True
        params["LanguageOptions"] = config.TRANSCRIBE_LANGUAGES
    client().start_transcription_job(**params)


def wait_for(job_names: list[str], timeout: int = 900, interval: int = 5) -> dict[str, str]:
    """Poll until every job finishes. Returns {job_name: COMPLETED|FAILED|TIMEOUT}."""
    deadline = time.time() + timeout
    status: dict[str, str] = {}
    pending = set(job_names)
    while pending and time.time() < deadline:
        for name in list(pending):
            job = client().get_transcription_job(TranscriptionJobName=name)["TranscriptionJob"]
            if job["TranscriptionJobStatus"] in ("COMPLETED", "FAILED"):
                status[name] = job["TranscriptionJobStatus"]
                pending.discard(name)
        if pending:
            time.sleep(interval)
    for name in pending:
        status[name] = "TIMEOUT"
    return status


def read_result(output_key: str) -> dict:
    """Parse Transcribe's JSON output into text + average word confidence."""
    data = json.loads(storage.get_text(output_key))
    results = data.get("results", {})
    transcripts = results.get("transcripts") or [{}]
    text = " ".join(t.get("transcript", "") for t in transcripts).strip()
    confidences = [
        float(item["alternatives"][0]["confidence"])
        for item in results.get("items", [])
        if item.get("type") == "pronunciation" and item.get("alternatives")
    ]
    return {
        "text": text,
        "confidence": round(sum(confidences) / len(confidences), 3) if confidences else None,
        "word_count": len(confidences),
        "language": results.get("language_code") or data.get("language_code"),
    }
