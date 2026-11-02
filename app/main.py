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
    return s["status"] in ("ready", "interviewing")


# ----- Interview rules (enforced here, not in the browser) -------------------
# Questions are revealed one at a time, each can be answered exactly once, and only
# before its deadline. A question left unanswered past its deadline is forfeited.

UPLOAD_ALLOWANCE = 15  # seconds of the grace period reserved for uploading the recording


def _deadline(answer: dict) -> float:
    return answer["served_at"] + config.MAX_ANSWER_SECONDS + config.ANSWER_GRACE_SECONDS


def _open_question(s: dict):
    """(index, question, answer) for the question that is served but not yet answered or forfeited."""
    answers = s.get("answers") or {}
    for i, q in enumerate(s.get("questions") or []):
        a = answers.get(q["id"]) or {}
        if a.get("served_at") and not a.get("audio_key") and not a.get("expired"):
            return i, q, a
    return None


def _expire_if_overdue(s: dict, now: float) -> bool:
    cur = _open_question(s)
    if cur and now > _deadline(cur[2]):
        storage.set_answer(s["id"], cur[1]["id"], {"expired": True})
        s["answers"][cur[1]["id"]]["expired"] = True
        return True
    return False


def _public_question(q: dict) -> dict:
    return {k: v for k, v in q.items() if k not in _HIDDEN_UNTIL_DONE and k not in ("source", "source_url", "source_verified")}


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
    answers_raw = s.get("answers") or {}
    all_q = s.get("questions") or []
    # Before grading, only questions already served are visible (no previewing upcoming questions).
    questions = all_q if done else [_public_question(q) for q in all_q if (answers_raw.get(q["id"]) or {}).get("served_at")]
    answers = {}
    for qid, a in answers_raw.items():
        view = {k: a.get(k) for k in ("duration_seconds", "transcription_status", "submitted_at", "expired")}
        view["submitted"] = bool(a.get("audio_key"))
        if done:
            view.update(transcript=a.get("transcript"), confidence=a.get("confidence"),
                        audio_url=storage.presigned_url(a["audio_key"]) if a.get("audio_key") else None,
                        integrity={k: a.get(k) for k in ("tab_switches", "hidden_seconds", "fullscreen_exits",
                                                         "time_to_submit_seconds")})
        answers[qid] = view
    return {
        "id": s["id"],
        "status": s["status"],
        "created_at": s["created_at"],
        "resume_filename": s.get("resume_filename"),
        "fit": s.get("fit"),
        "questions": questions,
        "question_count": len(all_q),
        "answers": answers,
        "web_research": s.get("web_research"),
        "evaluation": s.get("evaluation"),
        "error": s.get("error"),
        "max_answer_seconds": config.MAX_ANSWER_SECONDS,
    }


@app.post("/api/sessions/{session_id}/next")
def next_question(session_id: str):
    """Serve the next question (or the one still open after a page reload), starting its clock."""
    s = _load(session_id)
    if not _accepts_answers(s):
        raise HTTPException(409, "This interview is not accepting answers.")
    now = time.time()
    questions = s.get("questions") or []
    _expire_if_overdue(s, now)

    def payload(i, q, a, resumed):
        left = min(config.MAX_ANSWER_SECONDS, _deadline(a) - now - UPLOAD_ALLOWANCE)
        return {"done": False, "index": i, "total": len(questions), "question": _public_question(q),
                "seconds_left": max(0, int(left)), "resumed": resumed}

    cur = _open_question(s)
    if cur:
        return payload(*cur, resumed=True)
    for i, q in enumerate(questions):
        if not (s["answers"].get(q["id"]) or {}).get("served_at"):
            storage.set_answer(session_id, q["id"], {"served_at": now})
            if s["status"] == "ready":
                storage.update_session(session_id, status="interviewing", started_at=int(now))
            return payload(i, q, {"served_at": now}, resumed=False)
    return {"done": True, "total": len(questions)}


@app.post("/api/sessions/{session_id}/answers/{qid}")
async def submit_answer(
    session_id: str,
    qid: str,
    audio: UploadFile = File(...),
    duration_seconds: float = Form(0),
    tab_switches: int = Form(0),
    hidden_seconds: float = Form(0),
    fullscreen_exits: int = Form(0),
):
    s = _load(session_id)
    if not _accepts_answers(s):
        raise HTTPException(409, "This interview is not accepting answers.")
    if qid not in {q["id"] for q in s.get("questions") or []}:
        raise HTTPException(404, "Unknown question")
    existing = s["answers"].get(qid) or {}
    if existing.get("audio_key"):
        raise HTTPException(409, "This question has already been answered. Answers cannot be re-recorded.")
    now = time.time()
    if _expire_if_overdue(s, now) or existing.get("expired"):
        raise HTTPException(409, "Time for this question has expired. It is scored as unanswered.")
    cur = _open_question(s)
    if not cur or cur[1]["id"] != qid:
        raise HTTPException(409, "This is not the current question.")
    fmt = transcription.media_format(audio.content_type)
    if not fmt:
        raise HTTPException(415, f"Unsupported audio format: {audio.content_type}")
    data = await audio.read()
    if len(data) > config.MAX_AUDIO_BYTES:
        raise HTTPException(413, "Recording is too large.")

    integrity = {
        "tab_switches": max(0, int(tab_switches)),
        "hidden_seconds": round(max(0.0, float(hidden_seconds)), 1),
        "fullscreen_exits": max(0, int(fullscreen_exits)),
        "time_to_submit_seconds": round(now - existing["served_at"]),
    }
    if len(data) < 1000:  # nothing usable was recorded: counts as an unanswered question
        storage.set_answer(session_id, qid, {"expired": True, "empty_recording": True, **integrity})
        return {"ok": True, "empty": True}

    ts = int(now * 1000)
    audio_key = f"sessions/{session_id}/audio/{qid}-{ts}.{fmt}"
    transcript_key = f"sessions/{session_id}/transcripts/{qid}-{ts}.json"
    job_name = f"l1-{session_id}-{qid}-{ts}"
    storage.put_bytes(audio_key, data, audio.content_type)
    transcription.start_job(job_name, audio_key, fmt, transcript_key)
    storage.set_answer(session_id, qid, {
        "audio_key": audio_key, "content_type": audio.content_type, "job_name": job_name,
        "transcript_key": transcript_key,
        "duration_seconds": round(min(max(duration_seconds, 0), config.MAX_ANSWER_SECONDS + 5)),
        "submitted_at": int(now), "transcription_status": "IN_PROGRESS", **integrity,
    })
    return {"ok": True}


@app.post("/api/sessions/{session_id}/finish")
def finish(session_id: str, background: BackgroundTasks):
    s = _load(session_id)
    if not _accepts_answers(s):
        raise HTTPException(409, f"Interview cannot be finished from status '{s['status']}'.")
    if not any(a.get("served_at") for a in (s.get("answers") or {}).values()):
        raise HTTPException(400, "The interview has not started yet.")
    cur = _open_question(s)
    if cur:  # ending early forfeits the open question
        storage.set_answer(session_id, cur[1]["id"], {"expired": True})
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
