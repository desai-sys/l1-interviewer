"""Runtime configuration, read once from environment variables."""
import os


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


AWS_REGION = _env("AWS_REGION", _env("AWS_DEFAULT_REGION", "us-east-1"))

# Storage
BUCKET_NAME = _env("BUCKET_NAME")
TABLE_NAME = _env("TABLE_NAME")

# LLM provider: "bedrock" (Amazon Bedrock, IAM auth - default),
# "aws" (Claude Platform on AWS, IAM auth + workspace id) or
# "anthropic" (Claude API with ANTHROPIC_API_KEY).
LLM_PROVIDER = _env("LLM_PROVIDER", "bedrock").lower()
_default_model = "anthropic.claude-opus-5-5" if LLM_PROVIDER == "bedrock" else "claude-opus-5-5"
LLM_MODEL = _env("LLM_MODEL", _default_model)
# Bedrock has no server-side fallbacks; on a safety refusal the SDK middleware retries on this model.
BEDROCK_FALLBACK_MODEL = _env("BEDROCK_FALLBACK_MODEL", "anthropic.claude-opus-5")
LLM_EFFORT = _env("LLM_EFFORT", "high")

# Interview
NUM_QUESTIONS = int(_env("NUM_QUESTIONS", "5"))
MAX_ANSWER_SECONDS = int(_env("MAX_ANSWER_SECONDS", "180"))
# Languages Amazon Transcribe may auto-identify between (comma separated).
TRANSCRIBE_LANGUAGES = [s.strip() for s in _env("TRANSCRIBE_LANGUAGES", "en-US,en-IN,en-GB").split(",") if s.strip()]

# Security
# Optional shared passcode. When set, every /api call needs header X-Access-Code.
ACCESS_CODE = _env("ACCESS_CODE")
# Set by the CDK stack: CloudFront adds this header, so requests that bypass CloudFront are rejected.
ORIGIN_VERIFY_SECRET = _env("ORIGIN_VERIFY_SECRET")

MAX_RESUME_BYTES = 5 * 1024 * 1024
MAX_AUDIO_BYTES = 30 * 1024 * 1024
