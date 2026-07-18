"""Entra ID sign-in (OIDC auth-code flow, confidential client).

Team membership comes from the ID token's groups claim (no Graph call, no
admin consent — see docs/AZURE_SETUP_GUIDE.md §1.5), filtered to the group
ids configured in TEAM_GROUPS.
"""

import msal
from fastapi import HTTPException, Request
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import get_settings
from app.models import User

SCOPES = ["User.Read"]
SESSION_COOKIE = "llmwiki_session"


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(get_settings().session_secret, salt="session")


def _msal_app() -> msal.ConfidentialClientApplication:
    s = get_settings()
    return msal.ConfidentialClientApplication(
        s.entra_client_id,
        client_credential=s.entra_client_secret,
        authority=f"https://login.microsoftonline.com/{s.entra_tenant_id}",
    )


def build_auth_url(state: str) -> str:
    return _msal_app().get_authorization_request_url(
        SCOPES, state=state, redirect_uri=get_settings().oauth_redirect_uri
    )


def complete_auth(code: str) -> User:
    result = _msal_app().acquire_token_by_authorization_code(
        code, scopes=SCOPES, redirect_uri=get_settings().oauth_redirect_uri
    )
    if "id_token_claims" not in result:
        raise HTTPException(401, f"Sign-in failed: {result.get('error_description', 'unknown')}")
    claims = result["id_token_claims"]
    team_map = get_settings().team_group_map
    groups = claims.get("groups", [])
    return User(
        id=claims.get("oid", claims["sub"]),
        name=claims.get("name", "Unknown"),
        email=claims.get("preferred_username", ""),
        teams={g: team_map[g] for g in groups if g in team_map},
    )


def session_cookie_value(user: User) -> str:
    return _serializer().dumps(user.model_dump())


def current_user(request: Request) -> User:
    """FastAPI dependency: resolve the signed session cookie to a User."""
    s = get_settings()
    if s.auth_dev_bypass:
        return User(
            id="dev-user",
            name="Dev User",
            email="dev@example.com",
            teams=dict(s.team_group_map) or {"dev-team": "Dev Team"},
        )
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        raise HTTPException(status_code=307, headers={"Location": "/auth/login"})
    try:
        return User(**_serializer().loads(raw))
    except BadSignature as e:
        raise HTTPException(status_code=307, headers={"Location": "/auth/login"}) from e


def require_team(user: User, team_id: str) -> str:
    """Access control: user must be in the team's backing M365 group."""
    if team_id not in user.teams:
        raise HTTPException(403, "You are not a member of this team.")
    return team_id
