"""Model calls that must return JSON matching a Pydantic schema.

Default backend: Amazon Nova on Bedrock via the Converse API. The schema is passed
as the only tool and the model is forced to call it; the tool input is the result.
Claude backends (Bedrock / Claude Platform on AWS / Claude API) are kept for later.
"""
from __future__ import annotations

import base64
import json
import logging
from functools import lru_cache
from typing import TypeVar

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from pydantic import BaseModel, ValidationError

import config
from schemas import inline_schema, strict_schema

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


def structured_call(
    *,
    system: str,
    content: list[dict],
    output_model: type[T],
    effort: str | None = None,
    max_tokens: int | None = None,
) -> T:
    """Run one request whose result must validate against `output_model`.

    `content` uses simple blocks: {"type": "text", "text": ...} or
    {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": ...}}.
    """
    if config.LLM_PROVIDER == "nova":
        return _nova_call(system, content, output_model, max_tokens or config.LLM_MAX_TOKENS)
    return _claude_call(system, content, output_model, effort, max_tokens or 32000)


# ---------------------------------------------------------------------------
# Amazon Nova (Bedrock Converse)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _bedrock():
    return boto3.client(
        "bedrock-runtime",
        region_name=config.AWS_REGION,
        config=Config(read_timeout=300, retries={"max_attempts": 6, "mode": "adaptive"}),
    )


def _to_converse(block: dict) -> dict:
    if block["type"] == "text":
        return {"text": block["text"]}
    if block["type"] == "document":
        return {"document": {
            "format": "pdf",
            "name": "Candidate resume",
            "source": {"bytes": base64.b64decode(block["source"]["data"])},
        }}
    raise ValueError(f"Unsupported content block {block['type']}")


def _nova_call(system: str, content: list[dict], output_model: type[T], max_tokens: int) -> T:
    tool_name = "submit_" + output_model.__name__.lower()
    tool_config = {
        "tools": [{"toolSpec": {
            "name": tool_name,
            "description": f"Submit the final {output_model.__name__} result. Every field is required.",
            "inputSchema": {"json": inline_schema(output_model)},
        }}],
        "toolChoice": {"tool": {"name": tool_name}},
    }
    messages = [{"role": "user", "content": [_to_converse(b) for b in content]}]

    last_error = ""
    for attempt in range(3):  # retry with the validation error fed back to the model
        try:
            resp = _bedrock().converse(
                modelId=config.LLM_MODEL,
                system=[{"text": system}],
                messages=messages,
                toolConfig=tool_config,
                # Greedy decoding: the same answer gets the same grade.
                inferenceConfig={"maxTokens": max_tokens, "temperature": 0.0, "topP": 1.0},
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            msg = exc.response.get("Error", {}).get("Message", str(exc))
            if code == "AccessDeniedException":
                raise LLMError(f"No access to model {config.LLM_MODEL}. Enable it under Bedrock > Model access. ({msg})") from exc
            if code == "ThrottlingException":
                raise LLMError("Bedrock is throttling requests right now. Please retry in a minute.") from exc
            raise LLMError(f"Bedrock error ({code}): {msg}") from exc

        stop = resp.get("stopReason")
        if stop in ("guardrail_intervened", "content_filtered"):
            raise LLMError(f"The model's content filter blocked this request ({stop}).")
        out = resp["output"]["message"]
        tool_use = next((c["toolUse"] for c in out["content"] if "toolUse" in c), None)
        if tool_use is None:
            last_error = "You must call the tool with the complete result."
            if stop == "max_tokens":
                raise LLMError("The model response was cut off (max tokens). Increase LLM_MAX_TOKENS.")
            messages += [out, {"role": "user", "content": [{"text": last_error}]}]
            continue
        try:
            return output_model.model_validate(tool_use["input"])
        except ValidationError as exc:
            last_error = str(exc)
            log.warning("Invalid tool input (attempt %s): %s", attempt + 1, last_error[:500])
            messages += [out, {"role": "user", "content": [{"toolResult": {
                "toolUseId": tool_use["toolUseId"],
                "content": [{"text": f"Validation failed, fix these errors and call the tool again:\n{last_error}"}],
            }}]}]
    raise LLMError(f"The model returned an invalid response after 3 attempts: {last_error[:300]}")


# ---------------------------------------------------------------------------
# Claude (optional)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _claude_client():
    import anthropic

    if config.LLM_PROVIDER == "bedrock":
        fallback = anthropic.BetaRefusalFallbackMiddleware([{"model": config.BEDROCK_FALLBACK_MODEL}])
        return anthropic.AnthropicBedrockMantle(aws_region=config.AWS_REGION, middleware=[fallback])
    if config.LLM_PROVIDER == "aws":
        return anthropic.AnthropicAWS()
    if config.LLM_PROVIDER == "anthropic":
        return anthropic.Anthropic()
    raise LLMError(f"Unknown LLM_PROVIDER {config.LLM_PROVIDER!r}")


def _claude_call(system: str, content: list[dict], output_model: type[T], effort: str | None, max_tokens: int) -> T:
    import anthropic

    params: dict = {
        "model": config.LLM_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": content}],
        "thinking": {"type": "adaptive"},
        "output_config": {
            "effort": effort or "high",
            "format": {"type": "json_schema", "schema": strict_schema(output_model)},
        },
    }
    if config.LLM_PROVIDER != "bedrock":
        params["betas"] = ["server-side-fallback-2026-07-01"]
        params["fallbacks"] = "default"
    try:
        with _claude_client().beta.messages.stream(**params) as stream:
            message = stream.get_final_message()
    except anthropic.RateLimitError as exc:
        raise LLMError("The AI service is rate limited right now. Please retry in a minute.") from exc
    except anthropic.APIStatusError as exc:
        raise LLMError(f"AI service error ({exc.status_code}): {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise LLMError("Could not reach the AI service.") from exc

    if message.stop_reason == "refusal":
        raise LLMError("The model declined this request.")
    if message.stop_reason == "max_tokens":
        raise LLMError("The model response was cut off (max_tokens).")
    text = "".join(b.text for b in message.content if b.type == "text")
    try:
        return output_model.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise LLMError(f"The model returned an invalid response: {exc}") from exc
