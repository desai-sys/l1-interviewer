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


def fake_questions(n=3):
    return QuestionSet.model_validate({"questions": [{
        "id": f"x{i}", "question": f"Question {i}?", "type": "technical", "assesses": "a",
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
            return InterviewEvaluation(answers=[AnswerEvaluation(
                question_id=qid, score=8, key_points_covered=["k"], key_points_missed=[], evidence_quotes=["quote"],
                red_flags_observed=[], reasoning="good", improvement_advice="more")],
                strengths=[], concerns=[], overall_reasoning="")

        monkeypatch.setattr(pipeline, "structured_call", fake_llm)

        def fake_start(job_name, audio_key, fmt, output_key):
            words = [] if "q3" in job_name else [{"type": "pronunciation", "alternatives": [{"confidence": "0.9", "content": "w"}]}]
            body = {"results": {"transcripts": [{"transcript": "" if "q3" in job_name else "I would design it with..."}],
                                "items": words, "language_code": "en-US"}}
            storage.put_text(output_key, json.dumps(body))

        monkeypatch.setattr(transcription, "start_job", fake_start)
        monkeypatch.setattr(transcription, "wait_for", lambda names, **kw: {n: "COMPLETED" for n in names})
        c = TestClient(app)
        c.calls = calls
        yield c


def test_full_flow(client):
    r = client.post("/api/sessions", files={"resume": ("cv.txt", RESUME.encode(), "text/plain")}, data={"jd_text": JD})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]

    s = client.get(f"/api/sessions/{sid}").json()
    assert s["status"] == "ready"
    assert s["fit"]["scoring"]["overall_score"] == 80
    assert s["fit"]["scoring"]["verdict"] == "strong_fit"
    assert [q["id"] for q in s["questions"]] == ["q1", "q2", "q3"]
    assert "rubric" not in s["questions"][0], "rubric must be hidden before grading"

    audio = io.BytesIO(b"\x1aE\xdf\xa3" + b"0" * 4000)
    for qid in ("q1", "q2", "q3"):
        r = client.post(f"/api/sessions/{sid}/answers/{qid}", files={"audio": (f"{qid}.webm", audio.getvalue(), "audio/webm;codecs=opus")},
                        data={"duration_seconds": "42"})
        assert r.status_code == 200, r.text
    assert client.get(f"/api/sessions/{sid}").json()["status"] == "interviewing"

    assert client.post(f"/api/sessions/{sid}/finish").status_code == 200
    s = client.get(f"/api/sessions/{sid}").json()
    assert s["status"] == "completed", s.get("error")
    ev = s["evaluation"]
    scores = {a["question_id"]: a["score"] for a in ev["answers"]}
    assert scores == {"q1": 8, "q2": 8, "q3": 0}, "empty transcript must be forced to 0"
    assert ev["scoring"]["overall_score"] == 53
    assert ev["scoring"]["recommendation"] == "borderline"
    assert s["answers"]["q1"]["transcript"].startswith("I would")
    assert s["answers"]["q1"]["audio_url"].startswith("https://")
    assert "rubric" in s["questions"][0]

    # The resume is never sent to the answer grader.
    grader_inputs = [json.dumps(c) for name, c in client.calls if name == "InterviewEvaluation"]
    assert grader_inputs and all("Acme" not in g for g in grader_inputs)

    t = client.get(f"/api/sessions/{sid}/transcript.txt")
    assert t.status_code == 200 and "[Q1] CANDIDATE (42s, confidence 90%): I would design" in t.text
    assert "[NO SPEECH DETECTED" in t.text
    assert client.get(f"/api/sessions/{sid}/report.json").json()["evaluation"]["scoring"]["overall_score"] == 53


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
