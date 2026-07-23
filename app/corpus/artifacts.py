"""Shared plumbing for the corpus pipeline: `_corpus/` blob artifact paths,
JSON blob read/write, the configurable pipeline deployment, and a strict-JSON
model-call helper with one retry (structured outputs are the pipeline's
contract; a malformed reply is retried once, then surfaced to the caller)."""

import json

from app import abstractions as ab
from app import blob_store
from app.config import get_settings


def corpus_prefix(tier: str, owner: str) -> str:
    return f"{tier}/{owner}/_corpus/"


def concept_cache_path(tier: str, owner: str, source_hash: str) -> str:
    return f"{corpus_prefix(tier, owner)}concepts/{source_hash}.json"


def pageset_path(tier: str, owner: str) -> str:
    return f"{corpus_prefix(tier, owner)}pageset.json"


def frontier_path(tier: str, owner: str) -> str:
    return f"{corpus_prefix(tier, owner)}frontier.json"


def status_path(tier: str, owner: str) -> str:
    return f"{corpus_prefix(tier, owner)}status.json"


def read_json(path: str) -> dict | None:
    raw = blob_store.read_text(path)
    return json.loads(raw) if raw else None


def write_json(path: str, data: dict) -> None:
    blob_store.write_text(path, json.dumps(data, ensure_ascii=False, indent=1))


def pipeline_deployment() -> str:
    """The chat deployment the synthesis stages run on. Empty string falls
    back to the default deployment inside call_model — so a stronger model is
    a .env change (corpus_chat_deployment), never a code change."""
    return get_settings().corpus_chat_deployment


def call_json(prompt: str, *, system: str, max_tokens: int = 4000) -> dict:
    """call_model in JSON mode, parsed; one retry on malformed output. Raises
    ValueError after the second failure — callers decide the fallback."""
    last_err: Exception | None = None
    for _ in range(2):
        raw = ab.call_model(
            prompt,
            system=system,
            max_tokens=max_tokens,
            json_mode=True,
            deployment=pipeline_deployment(),
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError as err:  # noqa: PERF203 — two attempts only
            last_err = err
    raise ValueError(f"Model returned malformed JSON twice: {last_err}")
