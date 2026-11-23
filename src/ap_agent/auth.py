"""Identity, roles and permissions.

Humans authenticate through your identity provider; the app verifies the
cryptographic assertion itself (it never trusts a plain header):

* ``oidc``     Bearer JWT from Entra ID / Okta / Google / Auth0, checked against the issuer's JWKS
* ``iap``      Google Cloud IAP ``x-goog-iap-jwt-assertion`` (ES256, audience-bound)
* ``alb``      AWS ALB ``x-amzn-oidc-data`` (ES256, signer must be our ALB)
* ``easyauth`` Azure Easy Auth ``X-MS-CLIENT-PRINCIPAL`` (injected by the platform sidecar,
               which is the only ingress path to the container)
* ``none``     dev only - ``X-Dev-User`` / ``X-Dev-Groups`` headers

Machines (schedulers, webhooks, integrations) use scoped, hashed API keys.

Roles come from IdP groups (or explicit user entries) mapped in
``config/rbac.yaml``; each role is a fixed set of permissions defined here, and
each mapping also limits which legal entities the person can see.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
import jwt
import yaml
from sqlalchemy import select

from ap_agent.config import Settings
from ap_agent.storage.db import ApiKey, Database

log = logging.getLogger(__name__)

PERMISSIONS = {
    "invoices:read", "invoices:submit", "review:read", "review:resolve", "approvals:decide",
    "ask", "vendors:read", "vendors:contacts:write", "vendors:bank:verify", "delegations:write",
    "fx:write", "policy:propose", "policy:approve", "audit:read", "reports:read",
    "apikeys:manage", "jobs:run", "webhooks:receive", "metrics:read",
}

_CLERK = {"invoices:read", "invoices:submit", "review:read", "review:resolve", "ask", "vendors:read"}
_MANAGER = _CLERK | {"approvals:decide", "vendors:contacts:write", "delegations:write", "reports:read"}
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "auditor": {"invoices:read", "review:read", "audit:read", "reports:read", "vendors:read"},
    "ap_clerk": _CLERK,
    "ap_manager": _MANAGER,
    "controller": _MANAGER | {"vendors:bank:verify", "policy:approve", "fx:write", "audit:read"},
    # IT/platform admins run the system but hold no financial authority (SoD).
    "admin": {"apikeys:manage", "policy:propose", "jobs:run", "audit:read", "metrics:read"},
}


@dataclass
class Principal:
    id: str
    kind: str  # user | service
    email: str | None = None
    groups: list[str] = field(default_factory=list)
    roles: set[str] = field(default_factory=set)
    permissions: set[str] = field(default_factory=set)
    entities: set[str] = field(default_factory=set)  # {"*"} = all
    mfa: bool = False

    @property
    def actor(self) -> str:
        return f"user:{self.email}" if self.kind == "user" else f"key:{self.id}"

    def can(self, permission: str, entity: str | None = None) -> bool:
        return permission in self.permissions and (entity is None or self.sees(entity))

    def sees(self, entity: str) -> bool:
        return "*" in self.entities or entity in self.entities


class RBAC:
    def __init__(self, group_roles: dict[str, dict], users: dict[str, dict]):
        self.group_roles = {k.lower(): v for k, v in group_roles.items()}
        self.users = {k.lower(): v for k, v in users.items()}
        for mapping in [*self.group_roles.values(), *self.users.values()]:
            unknown = set(mapping.get("roles", [])) - ROLE_PERMISSIONS.keys()
            if unknown:
                raise ValueError(f"Unknown roles in RBAC config: {unknown}")

    @classmethod
    def load(cls, path: Path | None) -> "RBAC":
        data = yaml.safe_load(Path(path).read_text()) if path and Path(path).exists() else {}
        data = data or {}
        return cls(data.get("group_roles") or {}, data.get("users") or {})

    def principal(self, *, email: str, groups: list[str], mfa: bool) -> Principal:
        roles: set[str] = set()
        entities: set[str] = set()
        for g in groups:
            m = self.group_roles.get(g.lower())
            if m:
                roles |= set(m.get("roles", []))
                entities |= set(m.get("entities", ["*"]))
        m = self.users.get(email.lower())
        if m:
            roles |= set(m.get("roles", []))
            entities |= set(m.get("entities", ["*"]))
        perms = set().union(*(ROLE_PERMISSIONS[r] for r in roles)) if roles else set()
        return Principal(id=email, kind="user", email=email.lower(), groups=groups, roles=roles, permissions=perms, entities=entities, mfa=mfa)


# ---------------------------------------------------------------- API keys


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create_api_key(db: Database, name: str, scopes: list[str], created_by: str, entities: list[str] | None = None, expires_at: datetime | None = None) -> str:
    bad = set(scopes) - PERMISSIONS
    if bad:
        raise ValueError(f"Unknown scopes: {bad}")
    if "approvals:decide" in scopes or "vendors:bank:verify" in scopes or "policy:approve" in scopes:
        raise ValueError("Financial-authority permissions can't be granted to API keys")
    raw = "apk_" + secrets.token_urlsafe(32)
    with db.session() as s, s.begin():
        s.add(ApiKey(name=name, key_hash=hash_key(raw), scopes=scopes, entities=entities or ["*"], created_by=created_by, expires_at=expires_at))
    db.audit(created_by, "apikey_created", name, scopes=scopes, entities=entities or ["*"])
    return raw


def revoke_api_key(db: Database, name: str, actor: str) -> bool:
    with db.session() as s, s.begin():
        key = s.scalar(select(ApiKey).where(ApiKey.name == name, ApiKey.revoked_at.is_(None)))
        if not key:
            return False
        key.revoked_at = datetime.now(timezone.utc)
    db.audit(actor, "apikey_revoked", name)
    return True


# ---------------------------------------------------------------- verification


class AuthError(Exception):
    pass


@lru_cache(maxsize=8)
def _jwks_client(url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=3600)


@lru_cache(maxsize=4)
def _discover_jwks(issuer: str) -> str:
    return httpx.get(issuer.rstrip("/") + "/.well-known/openid-configuration", timeout=10).json()["jwks_uri"]


@lru_cache(maxsize=32)
def _alb_key(region: str, kid: str) -> str:
    return httpx.get(f"https://public-keys.auth.elb.{region}.amazonaws.com/{kid}", timeout=10).text


def _mfa_from(claims: dict[str, Any]) -> bool:
    amr = claims.get("amr") or []
    if isinstance(amr, str):
        amr = [amr]
    return any(a in ("mfa", "otp", "hwk", "swk", "fido", "ngcmfa") for a in amr) or "mfa" in str(claims.get("acr", "")).lower()


class Authenticator:
    def __init__(self, settings: Settings, db: Database, rbac: RBAC):
        self.s, self.db, self.rbac = settings, db, rbac

    def authenticate(self, headers: dict[str, str]) -> Principal:
        h = {k.lower(): v for k, v in headers.items()}
        authz = h.get("authorization", "")
        bearer = authz[7:].strip() if authz.lower().startswith("bearer ") else ""
        if bearer.startswith("apk_"):
            return self._api_key(bearer)
        if bearer and self.s.api_token and hmac.compare_digest(bearer, self.s.api_token):
            if not (self.s.is_dev() or self.s.allow_bootstrap_token):
                raise AuthError("bootstrap token disabled outside dev")
            perms = ROLE_PERMISSIONS["admin"] | {"webhooks:receive"}
            return Principal(id="bootstrap", kind="service", permissions=perms, entities={"*"})
        mode = self.s.auth_mode
        if mode == "oidc" and bearer:
            return self._oidc(bearer)
        if mode == "iap" and "x-goog-iap-jwt-assertion" in h:
            return self._iap(h["x-goog-iap-jwt-assertion"])
        if mode == "alb" and "x-amzn-oidc-data" in h:
            return self._alb(h["x-amzn-oidc-data"])
        if mode == "easyauth" and "x-ms-client-principal" in h:
            return self._easyauth(h["x-ms-client-principal"])
        if mode == "none" and self.s.is_dev() and "x-dev-user" in h:
            groups = [g.strip() for g in h.get("x-dev-groups", "").split(",") if g.strip()]
            return self.rbac.principal(email=h["x-dev-user"], groups=groups, mfa=h.get("x-dev-mfa", "true") == "true")
        raise AuthError("not authenticated")

    def _api_key(self, raw: str) -> Principal:
        now = datetime.now(timezone.utc)
        with self.db.session() as s, s.begin():
            key = s.scalar(select(ApiKey).where(ApiKey.key_hash == hash_key(raw)))
            if key is None or key.revoked_at is not None:
                raise AuthError("invalid api key")
            if key.expires_at and (key.expires_at.replace(tzinfo=timezone.utc) if key.expires_at.tzinfo is None else key.expires_at) < now:
                raise AuthError("api key expired")
            key.last_used_at = now
            return Principal(id=key.name, kind="service", permissions=set(key.scopes), entities=set(key.entities))

    def _user(self, claims: dict[str, Any], email_claim: str, groups_claim: str, mfa: bool | None = None) -> Principal:
        email = claims.get(email_claim) or claims.get("preferred_username") or claims.get("upn")
        if not email:
            raise AuthError("token has no email")
        groups = list(claims.get(groups_claim) or []) + list(claims.get("roles") or [])
        return self.rbac.principal(email=str(email), groups=[str(g) for g in groups], mfa=_mfa_from(claims) if mfa is None else mfa)

    def _oidc(self, token: str) -> Principal:
        url = self.s.oidc_jwks_url or _discover_jwks(self.s.oidc_issuer)
        try:
            key = _jwks_client(url).get_signing_key_from_jwt(token)
            claims = jwt.decode(token, key.key, algorithms=["RS256", "ES256", "PS256"], audience=self.s.oidc_audience, issuer=self.s.oidc_issuer)
        except jwt.PyJWTError as e:
            raise AuthError(f"invalid token: {e}") from e
        return self._user(claims, self.s.oidc_email_claim, self.s.oidc_groups_claim)

    def _iap(self, token: str) -> Principal:
        try:
            key = _jwks_client("https://www.gstatic.com/iap/verify/public_key-jwk").get_signing_key_from_jwt(token)
            claims = jwt.decode(token, key.key, algorithms=["ES256"], audience=self.s.iap_audience, issuer="https://cloud.google.com/iap")
        except jwt.PyJWTError as e:
            raise AuthError(f"invalid IAP assertion: {e}") from e
        # IAP carries no groups or amr: map users in rbac.yaml and enforce 2-step verification in Google Workspace.
        return self._user(claims, "email", "groups", mfa=True)

    def _alb(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
            if header.get("signer") != self.s.alb_arn:
                raise AuthError("unexpected ALB signer")
            region = self.s.alb_arn.split(":")[3]
            claims = jwt.decode(token, _alb_key(region, header["kid"]), algorithms=["ES256"], options={"verify_aud": False})
        except jwt.PyJWTError as e:
            raise AuthError(f"invalid ALB assertion: {e}") from e
        return self._user(claims, self.s.oidc_email_claim, self.s.oidc_groups_claim)

    def _easyauth(self, value: str) -> Principal:
        try:
            data = json.loads(base64.b64decode(value))
        except Exception as e:
            raise AuthError("bad client principal") from e
        claims: dict[str, Any] = {}
        for c in data.get("claims", []):
            typ, val = c.get("typ", ""), c.get("val")
            short = typ.rsplit("/", 1)[-1]
            if short in ("groups", "roles", "amr"):
                claims.setdefault(short, []).append(val)
            else:
                claims.setdefault(short, val)
        claims.setdefault("email", claims.get("emailaddress") or claims.get("preferred_username"))
        return self._user(claims, "email", "groups")
