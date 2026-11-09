"""All environment-dependent values. Nothing else in the code base reads os.environ."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # application
    app_env: str = "dev"
    log_level: str = "INFO"
    default_language: str = "es"
    supported_languages: str = "es,en"

    # collector
    target_tenant_id: str = ""
    collector_client_id: str = ""
    collector_client_secret: str = ""
    collector_client_secret_name: str = "collector-client-secret"
    dataverse_environment_url: str = ""
    dataverse_api_version: str = "v9.2"
    graph_base_url: str = "https://graph.microsoft.com"
    graph_api_version: str = "beta"
    graph_registry_path: str = ""
    purview_audit_path: str = ""
    # Paths for the v1/v2 evidence sources. Empty = source disabled. Confirm each in a spike before enabling.
    entra_access_path: str = ""
    retention_path: str = ""
    foundry_path: str = ""
    defender_incidents_path: str = ""
    learning_path: str = ""
    teams_webhook_url: str = ""
    purview_audit_enabled: bool = True
    key_vault_name: str = ""

    # storage
    cosmos_endpoint: str = ""
    cosmos_database: str = "euaiaction"
    storage_account_name: str = ""
    documents_container: str = "documents"
    files_dir: str = ".data/files"
    use_memory_store: bool = False

    # llm
    azure_openai_endpoint: str = ""
    azure_openai_deployment: str = ""
    azure_openai_api_version: str = "2024-10-21"
    llm_temperature: float = 0.0
    narrative_enabled: bool = True

    # portal and auth
    portal_entra_client_id: str = ""
    portal_allowed_tenant_ids: str = ""
    auth_disabled: bool = False  # local development only

    # rules and documents
    rules_dir: str = Field(default=str(PACKAGE_DIR / "rules" / "content"))
    templates_dir: str = Field(default=str(PACKAGE_DIR / "documents" / "templates"))
    document_types_file: str = Field(default=str(PACKAGE_DIR / "documents" / "content"))  # directory or file
    require_counsel_review_for_release: bool = True
    render_formats: str = "docx,pdf,md"

    # local development and tests
    use_fixtures: bool = False
    fixtures_dir: str = "tests/fixtures"

    @property
    def languages(self) -> list[str]:
        return [x.strip() for x in self.supported_languages.split(",") if x.strip()]

    @property
    def formats(self) -> list[str]:
        return [x.strip() for x in self.render_formats.split(",") if x.strip()]

    @property
    def allowed_tenants(self) -> set[str]:
        return {x.strip() for x in self.portal_allowed_tenant_ids.split(",") if x.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
