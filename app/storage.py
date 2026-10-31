"""S3 (files: resume, audio, transcripts, reports) and DynamoDB (session state)."""
from __future__ import annotations

import json
import time
from functools import lru_cache

import boto3
from botocore.config import Config

import config

# Fields stored as JSON strings (avoids DynamoDB's Decimal handling for nested data).
_JSON_FIELDS = ("fit", "questions", "answers", "evaluation", "error")


@lru_cache(maxsize=1)
def s3():
    return boto3.client("s3", region_name=config.AWS_REGION, config=Config(signature_version="s3v4"))


@lru_cache(maxsize=1)
def table():
    return boto3.resource("dynamodb", region_name=config.AWS_REGION).Table(config.TABLE_NAME)


# ----- S3 ------------------------------------------------------------------


def put_bytes(key: str, data: bytes, content_type: str) -> None:
    s3().put_object(Bucket=config.BUCKET_NAME, Key=key, Body=data, ContentType=content_type)


def put_text(key: str, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
    put_bytes(key, text.encode("utf-8"), content_type)


def get_bytes(key: str) -> bytes:
    return s3().get_object(Bucket=config.BUCKET_NAME, Key=key)["Body"].read()


def get_text(key: str) -> str:
    return get_bytes(key).decode("utf-8")


def presigned_url(key: str, expires: int = 3600) -> str:
    return s3().generate_presigned_url(
        "get_object", Params={"Bucket": config.BUCKET_NAME, "Key": key}, ExpiresIn=expires
    )


# ----- DynamoDB --------------------------------------------------------------


def create_session(session_id: str, **fields) -> None:
    now = int(time.time())
    item = {"id": session_id, "created_at": now, "updated_at": now, "answers": {}, **fields}
    table().put_item(Item=_encode(item), ConditionExpression="attribute_not_exists(id)")


def get_session(session_id: str) -> dict | None:
    item = table().get_item(Key={"id": session_id}, ConsistentRead=True).get("Item")
    return _decode(item) if item else None


def update_session(session_id: str, **fields) -> None:
    fields["updated_at"] = int(time.time())
    enc = _encode(fields)
    names = {f"#{k}": k for k in enc}
    values = {f":{k}": v for k, v in enc.items()}
    expr = "SET " + ", ".join(f"#{k} = :{k}" for k in enc)
    table().update_item(
        Key={"id": session_id},
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def set_answer(session_id: str, qid: str, answer: dict) -> None:
    """Merge one answer into the answers map (read-modify-write guarded by a version check)."""
    for _ in range(5):
        current = table().get_item(Key={"id": session_id}, ConsistentRead=True)["Item"]
        answers = json.loads(current.get("answers") or "{}")
        answers[qid] = {**answers.get(qid, {}), **answer}
        try:
            table().update_item(
                Key={"id": session_id},
                UpdateExpression="SET answers = :new, updated_at = :t",
                ConditionExpression="answers = :old",
                ExpressionAttributeValues={
                    ":new": json.dumps(answers),
                    ":old": current.get("answers"),
                    ":t": int(time.time()),
                },
            )
            return
        except table().meta.client.exceptions.ConditionalCheckFailedException:
            time.sleep(0.1)
    raise RuntimeError("Could not save answer (concurrent updates)")


def _encode(fields: dict) -> dict:
    return {k: (json.dumps(v) if k in _JSON_FIELDS and v is not None else v) for k, v in fields.items()}


def _decode(item: dict) -> dict:
    out = dict(item)
    for k in _JSON_FIELDS:
        if isinstance(out.get(k), str):
            out[k] = json.loads(out[k])
    for k in ("created_at", "updated_at"):
        if k in out:
            out[k] = int(out[k])
    return out
