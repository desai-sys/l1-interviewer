"""Background jobs: resume screen + question generation, and transcription + evaluation."""
from __future__ import annotations

import base64
import json
import logging
import math
import re
import time
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor

import config
import prompts
import storage
import transcription
from llm import structured_call, web_search
from schemas import (
    FitAnalysis,
    AnswerEvaluation,
    InterviewEvaluation,
    QuestionSet,
    WebQuestion,
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
            {"resume_technologies": fit.resume_technologies, "experience_level": fit.experience_level,
             "years_experience": fit.years_experience,
             "gaps": fit.gaps, "risks_to_probe": fit.risks_to_probe,
             "unmet_must_haves": fit_doc["scoring"]["unmet_must_haves"]},
            indent=2,
        )

        # Real interview questions from the web (best effort - the interview still works without it).
        bank, research = [], {"status": "off"}
        if config.WEB_RESEARCH:
            storage.update_session(session_id, status="researching_questions")
            try:
                bank, research = research_questions(fit, jd)
            except Exception as exc:  # noqa: BLE001
                log.warning("web research failed for %s: %s", session_id, exc)
                research = {"status": "failed", "error": str(exc)[:500]}
            storage.update_session(session_id, web_research=research, status="generating_questions")

        n = config.NUM_QUESTIONS
        half = n // 2
        web_min = min(3, half) if bank else 0
        bank_text = "\n".join(f"- {b.question} [topic: {b.topic}] (source_url: {b.source_url})" for b in bank)
        system = (prompts.QUESTIONS_SYSTEM.replace("{n}", str(n)).replace("{half}", str(half))
                  .replace("{half_next}", str(half + 1)).replace("{web_min}", str(web_min)))
        base_content = [
            resume_block,
            jd_block,
            {"type": "text", "text": f"<screen_notes>\n{screen_notes}\n</screen_notes>"},
            {"type": "text", "text": f"<web_question_bank>\n{bank_text}\n</web_question_bank>"},
            {"type": "text", "text": f"Generate exactly {n} interview questions now."},
        ]
        resume_text = "" if session.get("resume_as_pdf") else storage.get_text(f"sessions/{session_id}/resume.txt")
        feedback: list[dict] = []
        for attempt in range(2):  # regenerate once if the code checks fail
            qs = structured_call(system=system, content=base_content + feedback, output_model=QuestionSet)
            problems = check_questions(qs.questions, resume_text, n, half)
            if not problems:
                break
            log.warning("question check failed for %s (attempt %s): %s", session_id, attempt + 1, problems)
            feedback = [{"type": "text", "text": "Your previous question set was rejected by automated checks:\n- "
                         + "\n- ".join(problems) + "\nFix every problem and generate the full set again."}]
        if len(qs.questions) < n:
            raise RuntimeError(f"The model returned {len(qs.questions)} questions instead of {n}.")

        bank_urls = {b.source_url: b for b in bank}
        questions = [q.model_dump() for q in qs.questions[:n]]
        for i, q in enumerate(questions, 1):
            q["id"] = f"q{i}"  # normalise ids regardless of model output
            q["section"] = "concept_foundation" if i <= half else "application_scenario"
            entry = bank_urls.get(q.get("source_url", ""))
            if q["source"] == "web" and entry is None:
                q["source"], q["source_url"] = "generated", ""  # never show a URL that was not in the bank
            q["source_verified"] = bool(entry and entry.verified)
            q["resume_verified"] = (not resume_text) or _in_resume(q["technology"], q["resume_evidence"], resume_text)
        storage.update_session(session_id, intro=qs.intro, question_checks=problems)
        storage.update_session(session_id, questions=questions, status="ready")
    except Exception as exc:
        log.exception("screen failed for %s", session_id)
        storage.update_session(session_id, status="failed", error={"stage": "screen", "message": str(exc)})


_Q_LINE = re.compile(r"Q:\s*(?P<q>.+?)\s*\|\|\s*TOPIC:\s*(?P<t>.+?)\s*\|\|\s*SOURCE:\s*(?P<u>https?://\S+)", re.I)


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _in_resume(technology: str, evidence: str, resume_text: str) -> bool:
    """Both the technology name and the quoted resume phrase must literally occur in the resume."""
    raw = resume_text.lower()
    tech = re.sub(r"\s+", " ", technology.lower()).strip()
    ev = _norm(evidence)
    return bool(tech) and tech in re.sub(r"\s+", " ", raw) and len(ev.split()) >= 1 and ev in _norm(resume_text)


def check_questions(questions, resume_text: str, n: int, half: int) -> list[str]:
    problems = []
    if len(questions) != n:
        problems.append(f"Expected exactly {n} questions, got {len(questions)}.")
    for i, q in enumerate(questions[:n], 1):
        want = "concept_foundation" if i <= half else "application_scenario"
        if q.section != want:
            problems.append(f"Question {i} must be in section {want}.")
        if want == "concept_foundation" and q.type != "concept":
            problems.append(f"Question {i} must have type 'concept'.")
        if want == "application_scenario" and q.type == "concept":
            problems.append(f"Question {i} must be an application, scenario, troubleshooting or architecture question.")
        if resume_text and not _in_resume(q.technology, q.resume_evidence, resume_text):
            problems.append(f"Question {i}: technology '{q.technology}' or resume_evidence '{q.resume_evidence}' "
                            "does not appear verbatim in the resume. Only ask about technologies in the resume.")
    if not any(q.type == "troubleshooting" for q in questions[half:n]):
        problems.append("Include at least one troubleshooting question in the second half.")
    return problems


def research_questions(fit: FitAnalysis, jd: str) -> tuple[list[WebQuestion], dict]:
    techs = fit.resume_technologies[:10]
    prompt = (
        f"Role: {fit.role_title}\nCandidate level: {fit.experience_level} (~{fit.years_experience:g} years)\n"
        f"Technologies on the candidate's resume: {', '.join(techs)}\n\n"
        "Search the web and list 15-25 distinct, real L1 interview questions on these technologies: "
        "conceptual questions on how they work, plus practical scenario, debugging and design questions, "
        f"pitched at a {fit.experience_level} candidate."
    )
    text, cited = web_search(prompts.RESEARCH_SYSTEM, prompt)
    cited_domains = {_domain(u) for u in cited}
    bank, seen = [], set()
    for m in _Q_LINE.finditer(text):
        q, url = m["q"].strip(), m["u"].rstrip(".,);]")
        key = q.lower()
        if key in seen or len(q) < 15:
            continue
        seen.add(key)
        verified = url in cited or _domain(url) in cited_domains
        if cited and not verified:
            continue  # the search returned citations and this URL is not among them: drop it
        bank.append(WebQuestion(question=q, topic=m["t"].strip(), source_url=url, verified=verified))
    info = {"status": "ok" if bank else "empty", "model": config.WEB_RESEARCH_MODEL,
            "questions_found": len(bank), "sources": sorted({_domain(b.source_url) for b in bank})}
    return bank[:30], info


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
    if not answer or not answer.get("served_at") and not answer.get("audio_key"):
        return "[NOT ANSWERED - the interview ended before this question]"
    if not answer.get("audio_key"):
        return "[NOT ANSWERED - no answer was submitted within the time limit]"
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


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9']+", " ", (text or "").lower()).split())


def _verified_quotes(quotes: list[str], transcript: str) -> tuple[list[str], list[str]]:
    """Split quotes into those that literally occur in the transcript and those that don't."""
    nt = _norm(transcript)
    ok, bad = [], []
    for quote in quotes:
        nq = _norm(quote)
        (ok if len(nq.split()) >= 3 and nq in nt else bad).append(quote)
    return ok, bad


def _grade_one(jd: str, q: dict, answer: dict | None) -> tuple[AnswerEvaluation, dict]:
    """Grade one answer in its own request, then enforce the evidence rules in code."""
    checks: dict = {"quotes_rejected": [], "regraded": False, "caps": []}
    if _answer_text(answer).startswith(("[NOT ANSWERED", "[NO SPEECH")):
        return _no_answer(q), checks  # nothing to grade - no model call, guaranteed zero
    transcript = _answer_text(answer)
    conf = (answer or {}).get("confidence")
    conf_note = f"Average transcription confidence: {conf:.0%}." if conf is not None else ""
    payload = {
        "question_id": q["id"],
        "question": q["question"],
        "type": q["type"],
        "assesses": q["assesses"],
        "evaluator_note": q.get("evaluator_note", ""),
        "key_points": q["key_points"],
        "red_flags": q["red_flags"],
        "rubric": q["rubric"],
    }
    content = [
        {"type": "text", "text": f"<job_description>\n{jd}\n</job_description>"},
        {"type": "text", "text": f"<question_and_rubric>\n{json.dumps(payload, indent=2)}\n</question_and_rubric>"},
        {"type": "text", "text": f"<candidate_answer_transcript>\n{transcript}\n</candidate_answer_transcript>\n{conf_note}"},
        {"type": "text", "text": f"Grade this single answer. Return exactly one item in `answers` with question_id {q['id']}."},
    ]

    def grade(extra: list[dict]) -> AnswerEvaluation:
        result = structured_call(system=prompts.EVAL_SYSTEM, content=content + extra, output_model=InterviewEvaluation)
        g = next((a for a in result.answers if a.question_id == q["id"]), result.answers[0] if result.answers else None)
        if g is None:
            raise RuntimeError(f"No grade returned for {q['id']}")
        g.question_id = q["id"]
        return g

    graded = grade([])
    ok, bad = _verified_quotes(graded.evidence_quotes, transcript)
    if graded.score > 2 and not ok:
        # Every quote was fabricated or paraphrased: re-grade once, telling the grader exactly why.
        checks["regraded"] = True
        feedback = ("Your previous grade was rejected: none of these evidence quotes appear verbatim in the "
                    f"transcript: {json.dumps(bad)}. Re-grade, quoting the transcript exactly.")
        graded = grade([{"type": "text", "text": feedback}])
        ok, bad = _verified_quotes(graded.evidence_quotes, transcript)
    graded.evidence_quotes = ok
    checks["quotes_rejected"] = bad

    if graded.score > 2 and not ok:
        graded.score = 2
        checks["caps"].append("No grader quote could be verified against the transcript, so the score is capped at 2.")
    words = len(transcript.split())
    if words < config.MIN_WORDS_FOR_CREDIT and graded.score > 2:
        graded.score = 2
        checks["caps"].append(f"The answer has only {words} words (minimum {config.MIN_WORDS_FOR_CREDIT}), so the score is capped at 2.")
    return graded, checks


def evaluate(session_id: str, questions: list[dict], answers: dict) -> dict:
    jd = storage.get_text(f"sessions/{session_id}/jd.txt")

    # 1) Independent per-answer grading (parallel).
    with ThreadPoolExecutor(max_workers=min(5, len(questions))) as pool:
        results = list(pool.map(lambda q: _grade_one(jd, q, answers.get(q["id"])), questions))
    graded = [g for g, _ in results]
    checks = {g.question_id: c for g, c in results}

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
        a["checks"] = checks.get(a["question_id"], {})
    doc["scoring"] = score_interview(final, len(questions))
    return doc
