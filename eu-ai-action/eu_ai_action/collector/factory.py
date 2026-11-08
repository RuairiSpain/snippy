from __future__ import annotations

from ..settings import Settings
from .base import EvidenceSource


def make_source(s: Settings) -> list[EvidenceSource]:
    """Returns sources in order; the first one is the list of record for agents."""
    if s.use_fixtures:
        from .fixtures import FixtureSource

        return [FixtureSource(s.fixtures_dir)]
    if not (s.dataverse_environment_url and s.target_tenant_id and s.collector_client_id):
        raise RuntimeError("Set DATAVERSE_ENVIRONMENT_URL, TARGET_TENANT_ID and COLLECTOR_CLIENT_ID, or USE_FIXTURES=true")
    from .auth import token_provider
    from .dataverse import DataverseSource
    from .graph_registry import GraphRegistrySource
    from .purview import PurviewSource

    dv_token = token_provider(s, f"{s.dataverse_environment_url.rstrip('/')}/.default")
    graph_token = token_provider(s, f"{s.graph_base_url.rstrip('/')}/.default")
    sources: list[EvidenceSource] = [
        DataverseSource(s.dataverse_environment_url, s.dataverse_api_version, dv_token),
        GraphRegistrySource(s.graph_base_url, s.graph_api_version, s.graph_registry_path, graph_token),
    ]
    if s.purview_audit_enabled:
        sources.append(PurviewSource(s.graph_base_url, s.graph_api_version, s.purview_audit_path, graph_token))
    return sources
