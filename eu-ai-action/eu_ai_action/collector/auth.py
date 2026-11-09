from __future__ import annotations

from ..settings import Settings


def collector_secret(s: Settings) -> str:
    """Local override first; otherwise the Key Vault secret named in settings."""
    if s.collector_client_secret:
        return s.collector_client_secret
    if not s.key_vault_name:
        raise RuntimeError("Set COLLECTOR_CLIENT_SECRET or KEY_VAULT_NAME")
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient

    client = SecretClient(f"https://{s.key_vault_name}.vault.azure.net", DefaultAzureCredential())
    return client.get_secret(s.collector_client_secret_name).value or ""


def token_provider(s: Settings, scope: str):
    """Returns a callable that yields a bearer token for the target tenant (client credentials)."""
    from azure.identity import ClientSecretCredential

    cred = ClientSecretCredential(s.target_tenant_id, s.collector_client_id, collector_secret(s))
    return lambda: cred.get_token(scope).token
