"""Runtime configuration, read once from environment variables."""
import os


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


AWS_REGION = _env("AWS_REGION", _env("AWS_DEFAULT_REGION", "us-east-1"))

# Storage
BUCKET_NAME = _env("BUCKET_NAME")
TABLE_NAME = _env("TABLE_NAME")

# LLM provider:
#   "nova"      - Amazon Nova on Bedrock via the Converse API (default)
#   "bedrock"   - Claude on Amazon Bedrock
#   "aws"       - Claude Platform on AWS (IAM auth + ANTHROPIC_AWS_WORKSPACE_ID)
#   "anthropic" - Claude API (ANTHROPIC_API_KEY)
LLM_PROVIDER = _env("LLM_PROVIDER", "nova").lower()


def _geo_prefix(region: str) -> str:
    """Cross-region inference profile prefix for the deploy region (Nova needs a profile in most regions)."""
    if region.startswith("us-gov"):
        return "us-gov"
    if region.startswith(("us-", "ca-")):
        return "us"
    if region.startswith("eu-"):
        return "eu"
    if region.startswith("ap-"):
        return "apac"
    return "us"


_DEFAULT_MODELS = {
    "nova": f"{_geo_prefix(AWS_REGION)}.amazon.nova-pro-v1:0",
    "bedrock": "anthropic.claude-opus-5-5",
}
LLM_MODEL = _env("LLM_MODEL", _DEFAULT_MODELS.get(LLM_PROVIDER, "claude-opus-5-5"))
# Max output tokens per call (Nova Pro allows up to 10k).
LLM_MAX_TOKENS = int(_env("LLM_MAX_TOKENS", "8000"))
# Claude on Bedrock has no server-side `fallbacks`; on a safety refusal the SDK middleware retries on this model.
BEDROCK_FALLBACK_MODEL = _env("BEDROCK_FALLBACK_MODEL", "anthropic.claude-opus-5")
LLM_EFFORT = _env("LLM_EFFORT", "high")

# Web question research (Amazon Nova Web Grounding - US regions / US inference profiles only).
WEB_RESEARCH = _env("WEB_RESEARCH", "on" if AWS_REGION.startswith("us-") else "off").lower() == "on"
WEB_RESEARCH_MODEL = _env("WEB_RESEARCH_MODEL", "us.amazon.nova-premier-v1:0")

# Interview
NUM_QUESTIONS = int(_env("NUM_QUESTIONS", "5"))
MAX_ANSWER_SECONDS = int(_env("MAX_ANSWER_SECONDS", "180"))
# Extra time allowed on top of the answer limit for the question being read aloud and the upload.
ANSWER_GRACE_SECONDS = int(_env("ANSWER_GRACE_SECONDS", "75"))
# Answers with fewer words than this cannot score above 2/10.
MIN_WORDS_FOR_CREDIT = int(_env("MIN_WORDS_FOR_CREDIT", "15"))
# Languages Amazon Transcribe may auto-identify between (comma separated).
TRANSCRIBE_LANGUAGES = [s.strip() for s in _env("TRANSCRIBE_LANGUAGES", "en-US,en-IN,en-GB").split(",") if s.strip()]

# Security
# Optional shared passcode. When set, every /api call needs header X-Access-Code.
ACCESS_CODE = _env("ACCESS_CODE")
# Set by the CDK stack: CloudFront adds this header, so requests that bypass CloudFront are rejected.
ORIGIN_VERIFY_SECRET = _env("ORIGIN_VERIFY_SECRET")

MAX_RESUME_BYTES = 5 * 1024 * 1024
MAX_AUDIO_BYTES = 30 * 1024 * 1024
