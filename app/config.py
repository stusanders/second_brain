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

    # Azure OpenAI
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_chat_deployment: str = "gpt-4o"
    azure_openai_embed_deployment: str = "text-embedding-3-large"
    azure_openai_api_version: str = "2024-10-21"

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
