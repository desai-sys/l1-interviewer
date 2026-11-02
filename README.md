# L1 Interviewer

Upload a resume and a job description and you get:

1. **Profile fit report.** Each JD requirement is checked against quoted evidence from the resume. You get a weighted fit score (0–100), a verdict, strengths and gaps.
2. **Voice-only interview.** The model (Amazon Nova by default) writes questions that target the role's must-haves and the gaps in the resume. Each question is read aloud and the candidate answers **by speaking**. There is no text input.
3. **Strict evaluation.** Every answer is recorded, transcribed with Amazon Transcribe, and scored 0–10 against a rubric. Each score comes with reasoning, verbatim evidence quotes, key points covered and missed, and advice on how to improve.
4. **Records.** The audio for each answer, its transcript, a full interview transcript (`.txt`) and a full JSON report are stored in S3.

## How evaluation is kept strict and fair

| Safeguard | Where |
|---|---|
| The rubric (key points, red flags, excellent/adequate/poor) is written **before** any answer is heard and hidden from the candidate until grading. | `pipeline.run_screen`, `main.get_session` |
| Each answer is graded in its **own request**, so answers can't influence each other. | `pipeline._grade_one` |
| The answer grader **never sees the resume**, so resume claims can't inflate interview scores. | `pipeline._grade_one` |
| Every covered key point must be backed by a **verbatim quote** from the transcript. Generic answers are capped at 5/10. Factual errors lower the score. | `prompts.EVAL_SYSTEM` |
| Unanswered or silent answers score **0 in code**, with no model call. | `pipeline._no_answer` |
| Final scores, verdicts and recommendations are computed **in code** from fixed weights and bands. The rules are shown to the user. | `schemas.score_fit`, `schemas.score_interview` |
| Model output is forced into a fixed schema and validated, and invalid output is sent back for correction. Calls run at temperature 0, so the same answer gets the same grade. | `llm._nova_call` |
| An unmet must-have caps the fit verdict at "Partial fit". Two or more zero-score answers cap the result at "Borderline". | `schemas.py` |
| Fairness policy: ignore name, gender, age, nationality, accent, grammar, school or company prestige and employment gaps. Do not reward length or confidence. | `prompts.FAIRNESS_POLICY` |
| Instructions spoken inside an answer ("give me full marks") are ignored and flagged. | `prompts.EVAL_SYSTEM` |

## Architecture

```
Browser (MediaRecorder, speechSynthesis)
   │ HTTPS
CloudFront ──(secret header)──► ALB ──► ECS Fargate: FastAPI app + static UI
                                            ├─ Amazon Nova via Bedrock Converse (fit, questions, grading)
                                            ├─ Amazon Transcribe (speech → text)
                                            ├─ S3: resumes, audio, transcripts, reports (private, encrypted, versioned)
                                            └─ DynamoDB: session state
```

CloudFront provides HTTPS, which browsers require before they allow microphone access. Requests that don't come through CloudFront are rejected.

## Deploy to AWS

Prerequisites: an AWS account, AWS CLI v2 configured (`aws configure`), Node 22 or 24 LTS and Python 3.11+. **Docker is not needed**: the container image is built inside AWS by CodeBuild during `cdk deploy` (`infra/image_build.py`).

1. **Enable the model in Bedrock.** In the AWS console, open Amazon Bedrock → Model access in your deploy region and enable **Amazon Nova Pro**. The app calls it through the cross-region inference profile for your region (`us.` / `eu.` / `apac.amazon.nova-pro-v1:0`), so it needs to be enabled in the regions that profile routes to. For stricter, more nuanced grading, enable **Nova Premier** and deploy with `-c llmModel=us.amazon.nova-premier-v1:0` (US regions).
2. Deploy:

```bash
cd infra
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
npx aws-cdk@2 bootstrap        # once per account/region
npx aws-cdk@2 deploy -c accessCode=CHOOSE-A-PASSCODE
```

The deploy takes about 15–20 minutes: the image build in CodeBuild takes about 5, CloudFront about 10. When it finishes it prints `AppUrl` (`https://xxxx.cloudfront.net`). Open it, enter the passcode and start.

### Options (`-c key=value` or `infra/cdk.json`)

| Context key | Default | Meaning |
|---|---|---|
| `accessCode` | *(none)* | Shared passcode required for every API call. **Recommended**: without it, anyone with the URL can spend your Claude and Transcribe budget. |
| `llmProvider` | `nova` | `nova` (Amazon Nova on Bedrock). To use Claude later: `bedrock` (Claude on Bedrock), `aws` (Claude Platform on AWS, also needs `awsWorkspaceId`), or `anthropic` (Claude API; store the key first in Secrets Manager as `l1-interviewer/anthropic-api-key`) |
| `llmModel` | `<geo>.amazon.nova-pro-v1:0` | Model or inference profile ID, e.g. `us.amazon.nova-premier-v1:0` |
| `numQuestions` | `5` | Questions per interview |

Other runtime settings (environment variables on the container): `MAX_ANSWER_SECONDS` (default 180), `TRANSCRIBE_LANGUAGES` (default `en-US,en-IN,en-GB`, auto-identified), `LLM_MAX_TOKENS` (default 8000).

### Cost (rough)

- **Fixed:** about $25–35 per month for one Fargate task (1 vCPU / 2 GB) and the ALB.
- **Per interview:** Transcribe costs about $0.024 per audio minute. Nova makes about 7 requests per interview (fit analysis, question writing, one grading call per answer, one synthesis call). With Nova Pro that comes to a few cents.

S3 and DynamoDB costs are negligible. The bucket and table are **retained** if you delete the stack, because they hold interview recordings.

### Tear down

```bash
npx aws-cdk@2 destroy
```

Then empty and delete the retained S3 bucket and DynamoDB table if you no longer need the recordings.

## Run locally

```bash
cd app
pip install -r requirements.txt
export BUCKET_NAME=<an s3 bucket> TABLE_NAME=<dynamodb table with string key "id"> AWS_REGION=us-east-1
uvicorn main:app --reload
```

Open http://localhost:8000. Browsers allow microphone access on `localhost` without HTTPS.

## Tests

```bash
pip install -r app/requirements.txt "moto[s3,dynamodb]" pytest httpx
pytest -q tests
```

The tests run the whole flow (upload → screen → record → transcribe → evaluate → transcript download) against mocked AWS, with a stubbed model.

## Layout

```
app/        FastAPI service, prompts, scoring rules, static frontend, Dockerfile
infra/      AWS CDK stack (Python)
tests/      end-to-end API tests
```
