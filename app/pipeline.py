"""Background jobs: resume screen + question generation, and transcription + evaluation."""
from __future__ import annotations

import base64
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import config
import prompts
import storage
import transcription
from llm import structured_call
from schemas import (
    FitAnalysis,
    AnswerEvaluation,
    InterviewEvaluation,
    QuestionSet,
    rating_for,
    score_fit,
    score_interview,
)

log = logging.getLogger(__name__)


def _resume_block(session_id: str, session: dict) -> dict:
    """Resume as text, or as the original PDF when text extraction found almost nothing (scanned PDF)."""
    if session.get("resume_as_pdf"):
        data = storage.get_bytes(session["resume_key"])
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(data).decode()},
            "title": "Candidate resume",
        }
    return {"type": "text", "text": f"<resume>\n{storage.get_text(f'sessions/{session_id}/resume.txt')}\n</resume>"}


def run_screen(session_id: str) -> None:
    """Resume <-> JD fit analysis, then question generation informed by it."""
    session = storage.get_session(session_id)
    try:
        jd = storage.get_text(f"sessions/{session_id}/jd.txt")
        jd_block = {"type": "text", "text": f"<job_description>\n{jd}\n</job_description>"}
        resume_block = _resume_block(session_id, session)

        fit = structured_call(
            system=prompts.FIT_SYSTEM,
            content=[resume_block, jd_block, {"type": "text", "text": "Assess how well this resume fits this job."}],
            output_model=FitAnalysis,
        )
        fit_doc = {**fit.model_dump(), "scoring": score_fit(fit)}
        storage.update_session(session_id, fit=fit_doc, status="generating_questions")

        screen_notes = json.dumps(
            {"gaps": fit.gaps, "risks_to_probe": fit.risks_to_probe,
             "unmet_must_haves": fit_doc["scoring"]["unmet_must_haves"]},
            indent=2,
        )
        qs = structured_call(
            system=prompts.QUESTIONS_SYSTEM.replace("{n}", str(config.NUM_QUESTIONS)),
            content=[
                resume_block,
                jd_block,
                {"type": "text", "text": f"<screen_notes>\n{screen_notes}\n</screen_notes>\n\n"
                 f"Write exactly {config.NUM_QUESTIONS} interview questions."},
            ],
            output_model=QuestionSet,
        )
        questions = [q.model_dump() for q in qs.questions[: config.NUM_QUESTIONS]]
        for i, q in enumerate(questions, 1):
            q["id"] = f"q{i}"  # normalise ids regardless of model output
        storage.update_session(session_id, questions=questions, status="ready")
    except Exception as exc:
        log.exception("screen failed for %s", session_id)
        storage.update_session(session_id, status="failed", error={"stage": "screen", "message": str(exc)})


def run_evaluation(session_id: str) -> None:
    """Wait for transcripts, build the full interview transcript, then grade every answer."""
    session = storage.get_session(session_id)
    questions = session["questions"]
    answers = session.get("answers", {})
    try:
        storage.update_session(session_id, status="transcribing")
        jobs = {qid: a["job_name"] for qid, a in answers.items() if a.get("job_name")}
        statuses = transcription.wait_for(list(jobs.values()))

        for qid, job in jobs.items():
            if statuses.get(job) == "COMPLETED":
                result = transcription.read_result(answers[qid]["transcript_key"])
                answers[qid].update(transcript=result["text"], confidence=result["confidence"],
                                    word_count=result["word_count"], language=result["language"],
                                    transcription_status="COMPLETED")
            else:
                answers[qid].update(transcript="", transcription_status=statuses.get(job, "FAILED"))
            storage.set_answer(session_id, qid, answers[qid])

        failed = [qid for qid, a in answers.items() if a.get("transcription_status") not in ("COMPLETED", None)]
        if failed:
            raise RuntimeError(f"Transcription failed for {', '.join(failed)}. Retry evaluation, or re-record those answers.")

        transcript_text = build_transcript(session, questions, answers)
        storage.put_text(f"sessions/{session_id}/interview_transcript.txt", transcript_text)

        storage.update_session(session_id, status="evaluating")
        evaluation = evaluate(session_id, questions, answers)
        storage.put_text(f"sessions/{session_id}/report.json",
                         json.dumps({"fit": session.get("fit"), "questions": questions, "answers": answers,
                                     "evaluation": evaluation}, indent=2),
                         "application/json")
        storage.update_session(session_id, evaluation=evaluation, status="completed", completed_at=int(time.time()))
    except Exception as exc:
        log.exception("evaluation failed for %s", session_id)
        storage.update_session(session_id, status="failed", error={"stage": "evaluation", "message": str(exc)})


def _answer_text(answer: dict | None) -> str:
    if not answer or not answer.get("audio_key"):
        return "[NOT ANSWERED - the candidate skipped this question]"
    text = (answer.get("transcript") or "").strip()
    return text if text else "[NO SPEECH DETECTED in the recording]"


def build_transcript(session: dict, questions: list[dict], answers: dict) -> str:
    lines = [
        "L1 INTERVIEW TRANSCRIPT",
        f"Session: {session['id']}",
        f"Role: {(session.get('fit') or {}).get('role_title', '')}",
        f"Date: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(session['created_at']))}",
        "Transcribed automatically by Amazon Transcribe.",
        "",
    ]
    for q in questions:
        a = answers.get(q["id"])
        lines += [f"[{q['id'].upper()}] INTERVIEWER: {q['question']}"]
        meta = ""
        if a and a.get("duration_seconds"):
            meta = f" ({a['duration_seconds']}s"
            meta += f", confidence {a['confidence']:.0%})" if a.get("confidence") is not None else ")"
        lines += [f"[{q['id'].upper()}] CANDIDATE{meta}: {_answer_text(a)}", ""]
    return "\n".join(lines)


def _no_answer(q: dict) -> AnswerEvaluation:
    return AnswerEvaluation(
        question_id=q["id"], score=0, key_points_covered=[], key_points_missed=list(q["key_points"]),
        evidence_quotes=[], red_flags_observed=[],
        reasoning="No answer was recorded or no speech was detected, so no credit can be given.",
        improvement_advice="Answer every question; a partial answer still earns credit for the points it covers.",
    )


def _grade_one(jd: str, q: dict, answer: dict | None):
    """Grade one answer in its own request so answers cannot influence each other."""
    if _answer_text(answer).startswith(("[NOT ANSWERED", "[NO SPEECH")):
        return _no_answer(q)  # nothing to grade - no model call, guaranteed zero
    conf = (answer or {}).get("confidence")
    conf_note = f"Average transcription confidence: {conf:.0%}." if conf is not None else ""
    payload = {
        "question_id": q["id"],
        "question": q["question"],
        "type": q["type"],
        "assesses": q["assesses"],
        "key_points": q["key_points"],
        "red_flags": q["red_flags"],
        "rubric": q["rubric"],
    }
    content = [
        {"type": "text", "text": f"<job_description>\n{jd}\n</job_description>"},
        {"type": "text", "text": f"<question_and_rubric>\n{json.dumps(payload, indent=2)}\n</question_and_rubric>"},
        {"type": "text", "text": f"<candidate_answer_transcript>\n{_answer_text(answer)}\n</candidate_answer_transcript>\n{conf_note}"},
        {"type": "text", "text": f"Grade this single answer. Return exactly one item in `answers` with question_id {q['id']}."},
    ]
    result = structured_call(system=prompts.EVAL_SYSTEM, content=content, output_model=InterviewEvaluation)
    graded = next((a for a in result.answers if a.question_id == q["id"]), result.answers[0] if result.answers else None)
    if graded is None:
        raise RuntimeError(f"No grade returned for {q['id']}")
    graded.question_id = q["id"]
    return graded


def evaluate(session_id: str, questions: list[dict], answers: dict) -> dict:
    jd = storage.get_text(f"sessions/{session_id}/jd.txt")

    # 1) Independent per-answer grading (parallel).
    with ThreadPoolExecutor(max_workers=min(5, len(questions))) as pool:
        graded = list(pool.map(lambda q: _grade_one(jd, q, answers.get(q["id"])), questions))

    # 2) Synthesis over the already-fixed scores (it cannot change them).
    summary_input = [
        {"question_id": g.question_id, "question": next(q["question"] for q in questions if q["id"] == g.question_id),
         "score": g.score, "covered": g.key_points_covered, "missed": g.key_points_missed,
         "red_flags": g.red_flags_observed, "reasoning": g.reasoning}
        for g in graded
    ]
    synthesis = structured_call(
        system=prompts.EVAL_SYSTEM,
        content=[
            {"type": "text", "text": f"<job_description>\n{jd}\n</job_description>"},
            {"type": "text", "text": f"<graded_answers>\n{json.dumps(summary_input, indent=2)}\n</graded_answers>"},
            {"type": "text", "text": "The answers above are already graded and their scores are final. Return an "
             "empty `answers` list and write strengths, concerns and overall_reasoning across the whole interview, "
             "relative to the job description."},
        ],
        output_model=InterviewEvaluation,
        effort="medium",
    )

    final = InterviewEvaluation(answers=graded, strengths=synthesis.strengths, concerns=synthesis.concerns,
                                overall_reasoning=synthesis.overall_reasoning)
    doc = final.model_dump()
    for a in doc["answers"]:
        a["rating"] = rating_for(a["score"])
    doc["scoring"] = score_interview(final, len(questions))
    return doc
