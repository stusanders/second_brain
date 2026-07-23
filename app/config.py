import json
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Entra ID
    entra_tenant_id: str = ""
    entra_client_id: str = ""
    entra_client_secret: str = ""
    oauth_redirect_uri: str = "http://localhost:8000/auth/callback"

    # Azure OpenAI.
    # Deployment defaults match what's actually deployed on the POC resource
    # (see docs/AZURE_SETUP_GUIDE.md §2): a reasoning-family chat model and the
    # 1536-dim embedding model the Cosmos vector policy assumes (db.py
    # EMBED_DIMENSIONS). .env overrides these per environment — the defaults
    # exist so a missing var fails toward the correct model, not a 3072-dim one
    # that would silently mismatch the index.
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_chat_deployment: str = "llm-wiki-chat"  # gpt-5-nano
    azure_openai_embed_deployment: str = "llm-wiki-embed"  # text-embedding-3-small (1536-dim)
    azure_openai_api_version: str = "2024-10-21"
    # gpt-5-nano is a reasoning-family model and rejects `temperature != 1`;
    # the query path only forwards temperature when this is True. Flip it on
    # when a non-reasoning chat model is deployed (see call_model). Until then
    # fixed retrieval — not sampling — is the reproducibility lever.
    chat_supports_temperature: bool = False

    # Cosmos DB (derived index — see docs/BUILD_SPEC.md storage model).
    # Entra ID auth (via the llm_wiki_poc app registration's own credentials
    # below), not an account key — key-based auth is disabled on the account.
    cosmos_endpoint: str = ""
    cosmos_database: str = "llmwiki"

    # Blob Storage (canonical content store)
    blob_account_name: str = ""
    blob_account_key: str = ""
    blob_wiki_container: str = "wiki"
    blob_sources_container: str = "sources"

    # Corpus pipeline (docs/MVP_SPEC.md — one-shot document corpus → wiki).
    # corpus_chat_deployment lets the synthesis stages run on a stronger model
    # than the default chat deployment without a code change: the MVP tests
    # whether synthesis is *good*, and a weak eval on the cheapest model must
    # be distinguishable from "the premise fails". Empty = fall back to
    # azure_openai_chat_deployment.
    corpus_chat_deployment: str = ""
    corpus_concurrency: int = 4  # Stage 2 parallel extraction cap
    corpus_dedup_threshold: float = 0.82  # cosine cutoff for near-duplicate concept clustering
    corpus_consolidate_batch: int = 8  # docs per group in hierarchical consolidation
    corpus_max_page_chars: int = 24000  # passage budget per page before per-source summarization

    # App
    session_secret: str = "change-me"
    team_groups: str = "{}"  # JSON: group id -> team display name
    auth_dev_bypass: bool = False

    @property
    def team_group_map(self) -> dict[str, str]:
        return json.loads(self.team_groups)


@lru_cache
def get_settings() -> Settings:
    return Settings()
