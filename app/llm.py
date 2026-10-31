"""Claude client + a single helper for schema-constrained calls."""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

import config
from schemas import strict_schema

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def get_client():
    """Build the client for the configured provider. Credentials come from the environment / IAM role."""
    if config.LLM_PROVIDER == "bedrock":
        # Bedrock has no server-side `fallbacks`; the SDK middleware retries a refused request client-side.
        fallback = anthropic.BetaRefusalFallbackMiddleware([{"model": config.BEDROCK_FALLBACK_MODEL}])
        return anthropic.AnthropicBedrockMantle(aws_region=config.AWS_REGION, middleware=[fallback])
    if config.LLM_PROVIDER == "aws":
        return anthropic.AnthropicAWS()
    if config.LLM_PROVIDER == "anthropic":
        return anthropic.Anthropic()
    raise LLMError(f"Unknown LLM_PROVIDER {config.LLM_PROVIDER!r}")


def structured_call(
    *,
    system: str,
    content: list[dict],
    output_model: type[T],
    effort: str | None = None,
    max_tokens: int = 32000,
) -> T:
    """Run one Claude request whose final text must validate against `output_model`."""
    client = get_client()
    params: dict = {
        "model": config.LLM_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": content}],
        "thinking": {"type": "adaptive"},
        "output_config": {
            "effort": effort or config.LLM_EFFORT,
            "format": {"type": "json_schema", "schema": strict_schema(output_model)},
        },
    }
    if config.LLM_PROVIDER != "bedrock":
        # Server-side refusal fallback, routed by refusal category.
        params["betas"] = ["server-side-fallback-2026-07-01"]
        params["fallbacks"] = "default"

    try:
        with client.beta.messages.stream(**params) as stream:
            message = stream.get_final_message()
    except anthropic.RateLimitError as exc:
        raise LLMError("The AI service is rate limited right now. Please retry in a minute.") from exc
    except anthropic.APIStatusError as exc:
        raise LLMError(f"AI service error ({exc.status_code}): {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise LLMError("Could not reach the AI service.") from exc

    if message.stop_reason == "refusal":
        detail = getattr(message, "stop_details", None)
        raise LLMError(f"The model declined this request ({getattr(detail, 'category', None) or 'unspecified'}).")
    if message.stop_reason == "max_tokens":
        raise LLMError("The model response was cut off (max_tokens).")

    text = "".join(b.text for b in message.content if b.type == "text")
    try:
        return output_model.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as exc:
        log.error("Invalid structured output: %s", text[:2000])
        raise LLMError(f"The model returned an invalid response: {exc}") from exc
