"""Login check for the API.

The frontend signs in with Supabase Auth and sends the access token as a
bearer token. Each token is verified here against the project's public
signing keys, so the API holds no Supabase secret. The reviewer recorded
in the audit trail comes from the verified token, never from the request.
"""
import logging
import time
from functools import lru_cache

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .settings import settings

log = logging.getLogger("agent.auth")

_bearer = HTTPBearer(auto_error=False)
REVIEWER_MAX_LENGTH = 128  # audit.human_feedback.corrected_by
KEYS_RETRY_SECONDS = 30
_keys_unavailable_until = 0.0


@lru_cache
def _signing_keys() -> jwt.PyJWKClient:
    url = f"{settings().supabase_url}/auth/v1/.well-known/jwks.json"
    # No per-key cache: it never expires, so a revoked key would stay trusted until a restart.
    return jwt.PyJWKClient(url, cache_keys=False, lifespan=600, timeout=10)


def _verify(token: str) -> dict:
    issuer = f"{settings().supabase_url}/auth/v1"
    client = _signing_keys()
    if time.monotonic() < _keys_unavailable_until:
        # During an outage, only tokens signed by a key we already hold may wait on the key set.
        # Others would each hold a worker thread for a fetch that will fail.
        known = (client.jwk_set_cache.get() or {}).get("keys", []) if client.jwk_set_cache else []
        if jwt.get_unverified_header(token).get("kid") not in {k.get("kid") for k in known}:
            raise jwt.PyJWKClientConnectionError("Signing keys are unavailable")
    key = client.get_signing_key_from_jwt(token).key
    return jwt.decode(token, key, algorithms=["ES256"], audience="authenticated", issuer=issuer,
                      options={"require": ["exp", "sub", "iss", "aud"]})


def current_reviewer(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> str:
    """Returns the signed-in user's email, or refuses the request."""
    refused = HTTPException(401, "Please sign in", headers={"WWW-Authenticate": "Bearer"})
    if credentials is None:
        raise refused
    try:
        claims = _verify(credentials.credentials)
    except jwt.PyJWKClientConnectionError as exc:
        global _keys_unavailable_until
        if time.monotonic() >= _keys_unavailable_until:
            _keys_unavailable_until = time.monotonic() + KEYS_RETRY_SECONDS
            log.error("Could not fetch Supabase signing keys: %s", exc)
        raise HTTPException(503, "Sign-in could not be checked right now") from exc
    except jwt.PyJWTError as exc:
        log.info("Refused a token: %s", type(exc).__name__)
        raise refused from exc
    email = claims.get("email")
    if claims.get("role") != "authenticated" or claims.get("is_anonymous") or not isinstance(email, str) or not email:
        raise refused
    if email.lower() not in settings().reviewer_emails:
        log.warning("Refused a signed-in account that is not on the reviewer list")
        raise HTTPException(403, "This account is not allowed to use the invoice auditor")
    return email[:REVIEWER_MAX_LENGTH]
