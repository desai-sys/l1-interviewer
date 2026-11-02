"""End-to-end API flow with AWS mocked (moto) and the model + Transcribe stubbed."""
import io
import json
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
os.environ.update(BUCKET_NAME="test-bucket", TABLE_NAME="test-table", AWS_REGION="us-east-1",
                  AWS_ACCESS_KEY_ID="x", AWS_SECRET_ACCESS_KEY="x", NUM_QUESTIONS="3")

import schemas  # noqa: E402
from schemas import (  # noqa: E402
    AnswerEvaluation, FitAnalysis, InterviewEvaluation, QuestionSet, strict_schema,
)

JD = ("Senior Backend Engineer. Required: 5+ years Python, AWS (Lambda, DynamoDB), designing REST APIs, "
      "mentoring. Nice to have: Kafka. You will own services end to end and lead design reviews.")
RESUME = ("Jane Doe. Backend engineer 2017-2024 at Acme. Built Python REST APIs on AWS Lambda and DynamoDB "
          "serving 2M requests/day. Mentored 4 engineers. Skills: Python, AWS, Docker.")


def fake_fit():
    dim = {"score": 80, "reasoning": "r"}
    return FitAnalysis.model_validate({
        "role_title": "Senior Backend Engineer", "candidate_summary": "s",
        "requirements": [{"requirement": "Kafka", "importance": "nice_to_have", "status": "not_met",
                          "evidence": "No evidence in resume.", "reasoning": "r"}],
        "score_breakdown": {"skills": dim, "experience": dim, "domain": dim, "education_certifications": dim},
        "strengths": ["a"], "gaps": ["b"], "risks_to_probe": ["c"], "overall_reasoning": "o"})


BANK_URL = "https://www.glassdoor.com/Interview/backend-q1"
GOOD_ANSWER = ("I would design it with an API gateway in front of Lambda functions, store sessions in DynamoDB "
               "with a partition key per tenant, and add idempotency keys so retries are safe.")


def fake_questions(n=3):
    srcs = [("web", BANK_URL), ("web", "https://made-up.example.com/x"), ("generated", "")]
    return QuestionSet.model_validate({"questions": [{
        "id": f"x{i}", "question": f"Question {i}?", "type": "technical", "assesses": "a",
        "source": srcs[i][0], "source_url": srcs[i][1],
        "key_points": ["k"], "red_flags": ["r"],
        "rubric": {"excellent": "e", "adequate": "a", "poor": "p"}} for i in range(n)]})


@pytest.fixture
def client(monkeypatch):
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="test-bucket")
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName="test-table", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}])

        import pipeline
        import storage
        import transcription
        from fastapi.testclient import TestClient
        from main import app

        calls = []

        def fake_llm(*, system, content, output_model, effort=None, max_tokens=0):
            calls.append((output_model.__name__, content))
            if output_model is FitAnalysis:
                return fake_fit()
            if output_model is QuestionSet:
                return fake_questions()
            text = json.dumps(content)
            if "graded_answers" in text:
                return InterviewEvaluation(answers=[], strengths=["s"], concerns=["c"], overall_reasoning="o")
            qid = next(q for q in ("q1", "q2", "q3") if f'\\"question_id\\": \\"{q}\\"' in text)
            # q1: honest quote from the transcript. q2: a fabricated quote (must be rejected and capped).
            quote = "partition key per tenant" if qid == "q1" else "I have ten years of Kafka experience"
            return InterviewEvaluation(answers=[AnswerEvaluation(
                question_id=qid, score=8, key_points_covered=["k"], key_points_missed=[], evidence_quotes=[quote],
                red_flags_observed=[], reasoning="good", improvement_advice="more")],
                strengths=[], concerns=[], overall_reasoning="")

        monkeypatch.setattr(pipeline, "structured_call", fake_llm)
        monkeypatch.setattr(pipeline.config, "WEB_RESEARCH", True)
        monkeypatch.setattr(pipeline, "web_search", lambda system, prompt: (
            f"Q: How would you design a multi-tenant API on Lambda? || TOPIC: AWS || SOURCE: {BANK_URL}\n"
            "Q: Explain DynamoDB partition keys and hot partitions. || TOPIC: DynamoDB || SOURCE: https://fake.example.org/a\n",
            [BANK_URL]))

        def fake_start(job_name, audio_key, fmt, output_key):
            body = {"results": {"transcripts": [{"transcript": GOOD_ANSWER}],
                                "items": [{"type": "pronunciation", "alternatives": [{"confidence": "0.9", "content": "w"}]}],
                                "language_code": "en-US"}}
            storage.put_text(output_key, json.dumps(body))

        monkeypatch.setattr(transcription, "start_job", fake_start)
        monkeypatch.setattr(transcription, "wait_for", lambda names, **kw: {n: "COMPLETED" for n in names})
        c = TestClient(app)
        c.calls = calls
        yield c


AUDIO = b"\x1aE\xdf\xa3" + b"0" * 4000


def _answer(client, sid, qid, **extra):
    return client.post(f"/api/sessions/{sid}/answers/{qid}", files={"audio": (f"{qid}.webm", AUDIO, "audio/webm;codecs=opus")},
                       data={"duration_seconds": "42", **extra})


def _new_session(client):
    r = client.post("/api/sessions", files={"resume": ("cv.txt", RESUME.encode(), "text/plain")}, data={"jd_text": JD})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_full_flow(client):
    sid = _new_session(client)
    s = client.get(f"/api/sessions/{sid}").json()
    assert s["status"] == "ready"
    assert s["fit"]["scoring"]["overall_score"] == 80
    assert s["question_count"] == 3 and s["questions"] == [], "questions must not be visible before they are served"
    assert s["web_research"]["status"] == "ok" and s["web_research"]["questions_found"] == 1  # uncited URL dropped

    # Answering ahead of the served question is refused.
    assert _answer(client, sid, "q2").status_code == 409

    n = client.post(f"/api/sessions/{sid}/next").json()
    assert n["index"] == 0 and n["question"]["id"] == "q1" and n["seconds_left"] == 180
    assert "rubric" not in n["question"] and "source_url" not in n["question"]
    assert client.post(f"/api/sessions/{sid}/next").json()["resumed"] is True  # reload: same question, clock running

    assert _answer(client, sid, "q1", tab_switches="2", hidden_seconds="7").status_code == 200
    r = _answer(client, sid, "q1")
    assert r.status_code == 409 and "re-recorded" in r.json()["detail"]
    assert client.get(f"/api/sessions/{sid}").json()["status"] == "interviewing"

    assert client.post(f"/api/sessions/{sid}/next").json()["question"]["id"] == "q2"
    assert _answer(client, sid, "q2").status_code == 200
    assert client.post(f"/api/sessions/{sid}/next").json()["question"]["id"] == "q3"
    # q3 is served but the candidate ends the interview: it is forfeited.
    assert client.post(f"/api/sessions/{sid}/finish").status_code == 200

    s = client.get(f"/api/sessions/{sid}").json()
    assert s["status"] == "completed", s.get("error")
    ev = s["evaluation"]
    by = {a["question_id"]: a for a in ev["answers"]}
    assert by["q1"]["score"] == 8 and by["q1"]["evidence_quotes"] == ["partition key per tenant"]
    assert by["q2"]["score"] == 2 and by["q2"]["checks"]["regraded"] and by["q2"]["evidence_quotes"] == []
    assert by["q3"]["score"] == 0
    assert ev["scoring"]["overall_score"] == 33 and ev["scoring"]["recommendation"] == "do_not_advance"

    qs = {q["id"]: q for q in s["questions"]}
    assert qs["q1"]["source"] == "web" and qs["q1"]["source_url"] == BANK_URL and qs["q1"]["source_verified"]
    assert qs["q2"]["source"] == "generated" and qs["q2"]["source_url"] == "", "URL not in the bank must be dropped"
    assert s["answers"]["q1"]["integrity"]["tab_switches"] == 2
    assert s["answers"]["q3"]["expired"] is True

    # The resume is never sent to the answer grader.
    grader_inputs = [json.dumps(c) for name, c in client.calls if name == "InterviewEvaluation"]
    assert grader_inputs and all("Acme" not in g for g in grader_inputs)

    t = client.get(f"/api/sessions/{sid}/transcript.txt")
    assert t.status_code == 200 and "[Q1] CANDIDATE (42s, confidence 90%): I would design" in t.text
    assert "no answer was submitted within the time limit" in t.text
    assert client.get(f"/api/sessions/{sid}/report.json").json()["evaluation"]["scoring"]["overall_score"] == 33


def test_answer_after_deadline_is_refused(client, monkeypatch):
    import main
    sid = _new_session(client)
    client.post(f"/api/sessions/{sid}/next")
    real = main.time.time
    monkeypatch.setattr(main.time, "time", lambda: real() + 180 + 75 + 1)
    r = _answer(client, sid, "q1")
    assert r.status_code == 409 and "expired" in r.json()["detail"]
    n = client.post(f"/api/sessions/{sid}/next").json()
    assert n["question"]["id"] == "q2"


def test_short_answer_is_capped(monkeypatch):
    import pipeline
    monkeypatch.setattr(pipeline, "structured_call", lambda **kw: InterviewEvaluation(answers=[AnswerEvaluation(
        question_id="q1", score=9, key_points_covered=[], key_points_missed=[], evidence_quotes=["use a cache"],
        red_flags_observed=[], reasoning="r", improvement_advice="a")], strengths=[], concerns=[], overall_reasoning=""))
    q = fake_questions().questions[0].model_dump() | {"id": "q1"}
    graded, checks = pipeline._grade_one("jd", q, {"served_at": 1, "audio_key": "k", "transcript": "I would use a cache."})
    assert graded.score == 2 and any("words" in c for c in checks["caps"])


def test_rejects_bad_input(client):
    r = client.post("/api/sessions", files={"resume": ("cv.txt", RESUME.encode(), "text/plain")}, data={"jd_text": "short"})
    assert r.status_code == 400
    assert client.get("/api/sessions/not-a-uuid").status_code == 404


def test_must_have_cap():
    fit = fake_fit()
    fit.requirements[0].importance = "must_have"
    sc = schemas.score_fit(fit)
    assert sc["verdict"] == "partial_fit" and sc["capped_by_must_haves"]


def test_strict_schemas_are_closed():
    for model in (FitAnalysis, QuestionSet, InterviewEvaluation):
        sch = strict_schema(model)

        def check(node):
            if isinstance(node, dict):
                assert "minimum" not in node and "maximum" not in node
                if node.get("type") == "object" and "properties" in node:
                    assert node["additionalProperties"] is False
                    assert set(node["required"]) == set(node["properties"])
                for v in node.values():
                    check(v)
            elif isinstance(node, list):
                for v in node:
                    check(v)
        check(sch)


def test_inline_schema_has_no_refs():
    from schemas import inline_schema
    for model in (FitAnalysis, QuestionSet, InterviewEvaluation):
        text = json.dumps(inline_schema(model))
        assert "$ref" not in text and "$defs" not in text and "allOf" not in text
    fit = inline_schema(FitAnalysis)
    assert fit["properties"]["score_breakdown"]["properties"]["skills"]["properties"]["score"]["type"] == "integer"


def test_nova_call_retries_on_invalid_output(monkeypatch):
    import llm

    good = {"answers": [{"question_id": "q1", "score": 7, "key_points_covered": ["k"], "key_points_missed": [],
                         "evidence_quotes": ["q"], "red_flags_observed": [], "reasoning": "r", "improvement_advice": "a"}],
            "strengths": [], "concerns": [], "overall_reasoning": ""}
    bad = json.loads(json.dumps(good)); bad["answers"][0]["score"] = 11  # out of range
    sent = []

    class FakeBedrock:
        def converse(self, **kw):
            sent.append(kw)
            body = bad if len(sent) == 1 else good
            return {"stopReason": "tool_use", "output": {"message": {"role": "assistant", "content": [
                {"toolUse": {"toolUseId": f"t{len(sent)}", "name": "submit_interviewevaluation", "input": body}}]}}}

    monkeypatch.setattr(llm.config, "LLM_PROVIDER", "nova")
    monkeypatch.setattr(llm, "_bedrock", lambda: FakeBedrock())
    out = llm.structured_call(system="s", content=[
        {"type": "text", "text": "hi"},
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERg=="}}],
        output_model=InterviewEvaluation)
    assert out.answers[0].score == 7 and len(sent) == 2
    first = sent[0]
    assert first["toolConfig"]["toolChoice"] == {"tool": {"name": "submit_interviewevaluation"}}
    assert first["inferenceConfig"]["temperature"] == 0.0
    assert first["messages"][0]["content"][1]["document"]["source"]["bytes"] == b"%PDF"
    retry_msg = sent[1]["messages"][-1]["content"][0]["toolResult"]
    assert retry_msg["toolUseId"] == "t1" and "Validation failed" in retry_msg["content"][0]["text"]


def test_default_model_is_regional_nova():
    import config
    assert config._geo_prefix("ap-south-1") == "apac" and config._geo_prefix("us-east-1") == "us"
    assert config._geo_prefix("eu-west-1") == "eu"
