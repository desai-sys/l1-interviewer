"""HTTP API + static frontend."""
from __future__ import annotations

import hmac
import logging
import time
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

import config
import pipeline
import storage
import transcription
from extract import ExtractionError, extract_text, is_pdf

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = FastAPI(title="L1 Interviewer", docs_url=None, redoc_url=None)

# Questions are visible to the candidate; the grading rubric is hidden until the interview is graded.
_HIDDEN_UNTIL_DONE = ("key_points", "red_flags", "rubric")


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path == "/api/health":
        return await call_next(request)
    if config.ORIGIN_VERIFY_SECRET and not hmac.compare_digest(
        request.headers.get("x-origin-verify", ""), config.ORIGIN_VERIFY_SECRET
    ):
        return JSONResponse({"detail": "Forbidden"}, status_code=403)
    if config.ACCESS_CODE and path.startswith("/api/") and path != "/api/config":
        if not hmac.compare_digest(request.headers.get("x-access-code", ""), config.ACCESS_CODE):
            return JSONResponse({"detail": "Access code required"}, status_code=401)
    return await call_next(request)


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/config")
def public_config():
    return {
        "access_code_required": bool(config.ACCESS_CODE),
        "max_answer_seconds": config.MAX_ANSWER_SECONDS,
        "num_questions": config.NUM_QUESTIONS,
    }


def _load(session_id: str) -> dict:
    try:
        uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(404, "Session not found")
    session = storage.get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    return session


def _accepts_answers(s: dict) -> bool:
    if s["status"] in ("ready", "interviewing"):
        return True
    return s["status"] == "failed" and (s.get("error") or {}).get("stage") == "evaluation"


@app.post("/api/sessions")
async def create_session(
    background: BackgroundTasks,
    resume: UploadFile = File(...),
    jd_text: str = Form(""),
    jd_file: UploadFile | None = File(None),
):
    data = await resume.read()
    if not data:
        raise HTTPException(400, "Resume file is empty")
    if len(data) > config.MAX_RESUME_BYTES:
        raise HTTPException(413, "Resume must be under 5 MB")
    try:
        resume_text = extract_text(resume.filename, data)
        jd = jd_text.strip()
        if jd_file is not None and jd_file.filename:
            jd = (jd + "\n\n" + extract_text(jd_file.filename, await jd_file.read())).strip()
    except ExtractionError as exc:
        raise HTTPException(400, str(exc))
    if len(jd) < 100:
        raise HTTPException(400, "Please provide the full job description (at least a few sentences).")

    as_pdf = is_pdf(resume.filename, data) and len(resume_text) < 300
    if len(resume_text) < 100 and not as_pdf:
        raise HTTPException(400, "Could not read enough text from the resume.")

    session_id = str(uuid.uuid4())
    ext = Path(resume.filename or "resume").suffix.lower() or ".bin"
    prefix = f"sessions/{session_id}"
    storage.put_bytes(f"{prefix}/resume{ext}", data, resume.content_type or "application/octet-stream")
    storage.put_text(f"{prefix}/resume.txt", resume_text)
    storage.put_text(f"{prefix}/jd.txt", jd)
    storage.create_session(session_id, status="analyzing", resume_key=f"{prefix}/resume{ext}",
                           resume_filename=resume.filename, resume_as_pdf=as_pdf)
    background.add_task(pipeline.run_screen, session_id)
    return {"id": session_id}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str):
    s = _load(session_id)
    done = s["status"] == "completed"
    questions = [
        q if done else {k: v for k, v in q.items() if k not in _HIDDEN_UNTIL_DONE}
        for q in (s.get("questions") or [])
    ]
    answers = {}
    for qid, a in (s.get("answers") or {}).items():
        view = {k: a.get(k) for k in ("duration_seconds", "transcription_status", "submitted_at")}
        view["submitted"] = bool(a.get("audio_key"))
        if done:
            view.update(transcript=a.get("transcript"), confidence=a.get("confidence"),
                        audio_url=storage.presigned_url(a["audio_key"]) if a.get("audio_key") else None)
        answers[qid] = view
    return {
        "id": s["id"],
        "status": s["status"],
        "created_at": s["created_at"],
        "resume_filename": s.get("resume_filename"),
        "fit": s.get("fit"),
        "questions": questions,
        "answers": answers,
        "evaluation": s.get("evaluation"),
        "error": s.get("error"),
        "max_answer_seconds": config.MAX_ANSWER_SECONDS,
    }


@app.post("/api/sessions/{session_id}/answers/{qid}")
async def submit_answer(session_id: str, qid: str, audio: UploadFile = File(...), duration_seconds: float = Form(0)):
    s = _load(session_id)
    if not _accepts_answers(s):
        raise HTTPException(409, "This interview is not accepting answers.")
    if qid not in {q["id"] for q in s.get("questions") or []}:
        raise HTTPException(404, "Unknown question")
    fmt = transcription.media_format(audio.content_type)
    if not fmt:
        raise HTTPException(415, f"Unsupported audio format: {audio.content_type}")
    data = await audio.read()
    if len(data) < 1000:
        raise HTTPException(400, "Recording is empty. Check your microphone and record again.")
    if len(data) > config.MAX_AUDIO_BYTES:
        raise HTTPException(413, "Recording is too large.")

    ts = int(time.time() * 1000)
    audio_key = f"sessions/{session_id}/audio/{qid}-{ts}.{fmt}"
    transcript_key = f"sessions/{session_id}/transcripts/{qid}-{ts}.json"
    job_name = f"l1-{session_id}-{qid}-{ts}"
    storage.put_bytes(audio_key, data, audio.content_type)
    transcription.start_job(job_name, audio_key, fmt, transcript_key)
    storage.set_answer(session_id, qid, {
        "audio_key": audio_key, "content_type": audio.content_type, "job_name": job_name,
        "transcript_key": transcript_key, "duration_seconds": round(min(duration_seconds, 3600)),
        "submitted_at": int(time.time()), "transcription_status": "IN_PROGRESS",
    })
    if s["status"] == "ready":
        storage.update_session(session_id, status="interviewing", started_at=int(time.time()))
    elif s["status"] == "failed":  # re-recording after a failed transcription
        storage.update_session(session_id, status="interviewing", error=None)
    return {"ok": True}


@app.post("/api/sessions/{session_id}/finish")
def finish(session_id: str, background: BackgroundTasks):
    s = _load(session_id)
    if not _accepts_answers(s):
        raise HTTPException(409, f"Interview cannot be finished from status '{s['status']}'.")
    if not any(a.get("audio_key") for a in (s.get("answers") or {}).values()):
        raise HTTPException(400, "Answer at least one question before finishing.")
    storage.update_session(session_id, status="transcribing", finished_at=int(time.time()))
    background.add_task(pipeline.run_evaluation, session_id)
    return {"ok": True}


@app.post("/api/sessions/{session_id}/retry")
def retry(session_id: str, background: BackgroundTasks):
    s = _load(session_id)
    if s["status"] != "failed":
        raise HTTPException(409, "Only a failed step can be retried.")
    stage = (s.get("error") or {}).get("stage")
    if stage == "screen":
        storage.update_session(session_id, status="analyzing", error=None)
        background.add_task(pipeline.run_screen, session_id)
    else:
        storage.update_session(session_id, status="transcribing", error=None)
        background.add_task(pipeline.run_evaluation, session_id)
    return {"ok": True}


@app.get("/api/sessions/{session_id}/transcript.txt")
def transcript(session_id: str):
    s = _load(session_id)
    if s["status"] != "completed":
        raise HTTPException(409, "Transcript is available once the interview is evaluated.")
    text = storage.get_text(f"sessions/{session_id}/interview_transcript.txt")
    return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="interview-{session_id[:8]}.txt"'})


@app.get("/api/sessions/{session_id}/report.json")
def report(session_id: str):
    s = _load(session_id)
    if s["status"] != "completed":
        raise HTTPException(409, "Report is available once the interview is evaluated.")
    return Response(storage.get_bytes(f"sessions/{session_id}/report.json"), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="report-{session_id[:8]}.json"'})


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
