"""Structured-output schemas for every model call, plus the deterministic scoring rules.

Scores that drive a decision (overall fit, verdict, recommendation) are computed here in
code from the model's per-dimension / per-question scores, so the same inputs always map
to the same outcome and the rules are visible to the candidate.
"""
from __future__ import annotations

import copy
from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Resume <-> job-description fit
# ---------------------------------------------------------------------------


class RequirementMatch(BaseModel):
    requirement: str = Field(description="One requirement taken from the job description, in its own words.")
    importance: Literal["must_have", "nice_to_have"]
    status: Literal["met", "partially_met", "not_met", "unclear"]
    evidence: str = Field(
        description="Verbatim quote from the resume supporting the status, or exactly 'No evidence in resume.'"
    )
    reasoning: str


class DimensionScore(BaseModel):
    score: int = Field(ge=0, le=100)
    reasoning: str


class ScoreBreakdown(BaseModel):
    skills: DimensionScore
    experience: DimensionScore
    domain: DimensionScore
    education_certifications: DimensionScore


class FitAnalysis(BaseModel):
    role_title: str
    candidate_summary: str = Field(description="2-3 sentence factual summary of the candidate's relevant background.")
    requirements: list[RequirementMatch]
    score_breakdown: ScoreBreakdown
    strengths: list[str]
    gaps: list[str]
    risks_to_probe: list[str] = Field(description="Claims or gaps an interviewer should verify.")
    overall_reasoning: str = Field(description="Why the profile does or does not fit. Do not state a final score.")


FIT_WEIGHTS = {"skills": 0.40, "experience": 0.30, "domain": 0.15, "education_certifications": 0.15}

FIT_BANDS = [  # (min score, verdict, label)
    (80, "strong_fit", "Strong fit"),
    (65, "good_fit", "Good fit"),
    (50, "partial_fit", "Partial fit"),
    (35, "weak_fit", "Weak fit"),
    (0, "not_a_fit", "Not a fit"),
]


def score_fit(analysis: FitAnalysis) -> dict:
    """Weighted overall score + verdict. Any unmet must-have caps the verdict at partial fit."""
    b = analysis.score_breakdown
    overall = round(sum(getattr(b, k).score * w for k, w in FIT_WEIGHTS.items()))
    unmet = [r.requirement for r in analysis.requirements if r.importance == "must_have" and r.status == "not_met"]
    verdict, label = next((v, lbl) for floor, v, lbl in FIT_BANDS if overall >= floor)
    capped = False
    if unmet and overall >= 65:
        verdict, label, capped = "partial_fit", "Partial fit", True
    rules = [
        "Overall = 40% skills + 30% experience + 15% domain + 15% education/certifications.",
        "Bands: 80+ strong, 65-79 good, 50-64 partial, 35-49 weak, below 35 not a fit.",
        "Any unmet must-have requirement caps the verdict at 'Partial fit'.",
    ]
    return {
        "overall_score": overall,
        "verdict": verdict,
        "verdict_label": label,
        "capped_by_must_haves": capped,
        "unmet_must_haves": unmet,
        "rules": rules,
    }


# ---------------------------------------------------------------------------
# Interview questions (rubric fixed before any answer is heard)
# ---------------------------------------------------------------------------


class Rubric(BaseModel):
    excellent: str = Field(description="What a 9-10 answer contains.")
    adequate: str = Field(description="What a 5-6 answer contains.")
    poor: str = Field(description="What a 0-3 answer looks like.")


class InterviewQuestion(BaseModel):
    id: str = Field(description="q1, q2, ... in order.")
    question: str = Field(description="Spoken question, max ~45 words, one question only.")
    type: Literal["technical", "behavioral", "situational", "experience_verification"]
    assesses: str = Field(description="Which JD requirement or resume claim this question tests.")
    key_points: list[str] = Field(description="Concrete points a strong answer should cover.")
    red_flags: list[str]
    rubric: Rubric


class QuestionSet(BaseModel):
    questions: list[InterviewQuestion]


# ---------------------------------------------------------------------------
# Answer evaluation
# ---------------------------------------------------------------------------


class AnswerEvaluation(BaseModel):
    question_id: str
    score: int = Field(ge=0, le=10)
    key_points_covered: list[str]
    key_points_missed: list[str]
    evidence_quotes: list[str] = Field(description="Verbatim quotes from the transcript that justify the score.")
    red_flags_observed: list[str]
    reasoning: str = Field(description="Step-by-step justification mapping the answer to the rubric level.")
    improvement_advice: str


class InterviewEvaluation(BaseModel):
    answers: list[AnswerEvaluation]
    strengths: list[str]
    concerns: list[str]
    overall_reasoning: str = Field(description="Synthesis across answers. Do not state a final recommendation.")


REC_BANDS = [
    (80, "strong_advance", "Strongly recommend for next round"),
    (65, "advance", "Recommend for next round"),
    (50, "borderline", "Borderline - needs further assessment"),
    (0, "do_not_advance", "Do not advance"),
]


def rating_for(score: int) -> str:
    if score >= 9:
        return "excellent"
    if score >= 7:
        return "good"
    if score >= 5:
        return "adequate"
    if score >= 1:
        return "weak"
    return "no_credit"


def score_interview(evaluation: InterviewEvaluation, num_questions: int) -> dict:
    total = sum(a.score for a in evaluation.answers)
    overall = round(total * 10 / max(num_questions, 1))
    rec, label = next((r, lbl) for floor, r, lbl in REC_BANDS if overall >= floor)
    zero = [a.question_id for a in evaluation.answers if a.score == 0]
    capped = False
    # A candidate who gives no creditable answer to 2+ questions cannot be advanced outright.
    if len(zero) >= 2 and rec in ("strong_advance", "advance"):
        rec, label, capped = "borderline", REC_BANDS[2][2], True
    return {
        "overall_score": overall,
        "recommendation": rec,
        "recommendation_label": label,
        "capped": capped,
        "rules": [
            "Each answer is scored 0-10 against the rubric written before the interview started.",
            "Interview score = sum of answer scores / (10 x number of questions) x 100. Unanswered questions score 0.",
            "Bands: 80+ strongly recommend, 65-79 recommend, 50-64 borderline, below 50 do not advance.",
            "Two or more zero-score answers cap the result at 'Borderline'.",
        ],
    }


# ---------------------------------------------------------------------------
# JSON-schema conversion for structured outputs
# ---------------------------------------------------------------------------

_DROP_KEYS = {"title", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "default"}


def strict_schema(model: type[BaseModel]) -> dict:
    """Pydantic schema -> structured-outputs schema (all fields required, no extra keys).

    Numeric bounds are dropped from the wire schema (they are re-checked by Pydantic
    when the response is validated) and moved into the description instead.
    """
    schema = copy.deepcopy(model.model_json_schema())

    def walk(node):
        if isinstance(node, dict):
            if "minimum" in node and "maximum" in node:
                hint = f"Integer from {node['minimum']} to {node['maximum']}."
                node["description"] = f"{node.get('description', '')} {hint}".strip()
            for key in list(node):
                if key in _DROP_KEYS and not isinstance(node[key], dict):
                    node.pop(key)
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return schema


def inline_schema(model: type[BaseModel]) -> dict:
    """Tool-input schema for Bedrock Converse (Nova): `$ref`s inlined, only basic keywords kept."""
    schema = strict_schema(model)
    defs = schema.pop("$defs", {})
    keep = {"type", "properties", "required", "items", "enum", "description"}

    def resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                target = resolve(defs[node["$ref"].split("/")[-1]])
                if node.get("description"):
                    target = {**target, "description": node["description"]}
                return target
            if "allOf" in node and len(node["allOf"]) == 1:  # pydantic wraps described refs in allOf
                merged = {**resolve(node["allOf"][0])}
                if node.get("description"):
                    merged["description"] = node["description"]
                return merged
            out = {}
            for k, v in node.items():
                if k == "properties":
                    out[k] = {name: resolve(sub) for name, sub in v.items()}
                elif k in keep:
                    out[k] = resolve(v)
            return out
        if isinstance(node, list):
            return [resolve(x) for x in node]
        return node

    return resolve(schema)
