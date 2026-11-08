"""Principal check. Azure App Service authentication (Easy Auth) validates the token and sets
X-MS-CLIENT-PRINCIPAL; it also strips any client-supplied copy, so the header can be trusted only
when Easy Auth is enabled on the Function App (see infra/main.bicep)."""
from __future__ import annotations

import base64
import json
from collections.abc import Mapping

from ..settings import Settings

TID_CLAIMS = ("tid", "http://schemas.microsoft.com/identity/claims/tenantid")


class AuthError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


def authorize(headers: Mapping[str, str], tenant_id: str, s: Settings) -> str:
    """Returns the user name or raises AuthError. The caller's Entra tenant must equal the path tenant."""
    if s.auth_disabled:
        return "dev-user"
    raw = {k.lower(): v for k, v in headers.items()}.get("x-ms-client-principal")
    if not raw:
        raise AuthError(401, "authentication required")
    try:
        principal = json.loads(base64.b64decode(raw))
    except Exception as exc:  # noqa: BLE001
        raise AuthError(401, "invalid principal") from exc
    claims = {c.get("typ"): c.get("val") for c in principal.get("claims", [])}
    tid = next((claims[k] for k in TID_CLAIMS if k in claims), None)
    if not tid or tid != tenant_id:
        raise AuthError(403, "not authorised for this tenant")
    if s.allowed_tenants and tid not in s.allowed_tenants:
        raise AuthError(403, "tenant not allowed")
    return principal.get("userDetails") or claims.get("preferred_username") or "unknown"
