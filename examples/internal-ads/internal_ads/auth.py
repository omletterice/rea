"""Small single-owner OAuth server for MCP clients supporting OAuth 2.1/PKCE.

Operator consent is separate from operation approval. It grants draft/read/execute-
previously-approved tools, never approval authority or ad-platform credentials.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import secrets
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .providers_base import AdsError
from .store import canonical


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


class ClientAuth:
    def __init__(self, service):
        self.s = service
        self.url = service.settings.public_url
        self.router = APIRouter()
        self.routes()

    def put(self, key, value):
        with self.s.store.db() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, canonical(value)))

    def get(self, key, consume=False):
        with self.s.store.db() as db:
            query = (
                "DELETE FROM meta WHERE key=? RETURNING value"
                if consume
                else "SELECT value FROM meta WHERE key=?"
            )
            row = db.execute(query, (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def allowed(self, token):
        value = self.get("access:" + token_hash(token))
        return bool(value and value["expires"] > time.time())

    @staticmethod
    def redirect_ok(uri):
        u = urlsplit(uri)
        return bool(
            u.hostname
            and not u.username
            and not u.password
            and not u.fragment
            and (
                u.scheme == "https"
                or (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1"))
            )
        )

    def tokens(self, client_id):
        access, refresh = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
        self.put(
            "access:" + token_hash(access), {"client_id": client_id, "expires": time.time() + 3600}
        )
        self.put(
            "refresh:" + token_hash(refresh),
            {"client_id": client_id, "expires": time.time() + 30 * 86400},
        )
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "ads",
        }

    def validate_authorize(self, args):
        client = self.get("client:" + args.get("client_id", ""))
        if not client or args.get("redirect_uri") not in client["redirect_uris"]:
            raise AdsError("Unknown OAuth client or redirect URI", "invalid_client", 400)
        import re

        if (
            args.get("response_type") != "code"
            or args.get("code_challenge_method") != "S256"
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", args.get("code_challenge", ""))
        ):
            raise AdsError("Use authorization code with PKCE S256", "invalid_request", 400)
        if args.get("scope", "ads") != "ads" or len(args.get("state", "")) > 2048:
            raise AdsError("Unsupported scope or state", "invalid_scope", 400)
        if args.get("resource") not in (None, "", self.url + "/mcp"):
            raise AdsError("Invalid OAuth resource", "invalid_target", 400)
        return client

    def routes(self):
        r = self.router

        @r.get("/.well-known/oauth-protected-resource/mcp")
        @r.get("/.well-known/oauth-protected-resource")
        async def protected():
            return {
                "resource": self.url + "/mcp",
                "authorization_servers": [self.url],
                "scopes_supported": ["ads"],
                "bearer_methods_supported": ["header"],
            }

        @r.get("/.well-known/oauth-authorization-server")
        async def metadata():
            return {
                "issuer": self.url,
                "authorization_endpoint": self.url + "/oauth/authorize",
                "token_endpoint": self.url + "/oauth/token",
                "registration_endpoint": self.url + "/oauth/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
                "scopes_supported": ["ads"],
            }

        @r.post("/oauth/register", status_code=201)
        async def register(request: Request):
            body = await request.json()
            uris = body.get("redirect_uris", [])
            if (
                not isinstance(uris, list)
                or not 1 <= len(uris) <= 10
                or not all(
                    isinstance(u, str) and len(u) < 2048 and self.redirect_ok(u) for u in uris
                )
            ):
                raise AdsError(
                    "Register 1–10 HTTPS/loopback redirect URIs without fragments",
                    "invalid_redirect_uri",
                    400,
                )
            if body.get("token_endpoint_auth_method", "none") != "none":
                raise AdsError("Public clients with PKCE only", "invalid_client_metadata", 400)
            client_id = secrets.token_urlsafe(24)
            record = {
                "client_id": client_id,
                "client_name": str(body.get("client_name", "AI client"))[:100],
                "redirect_uris": uris,
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            }
            self.put("client:" + client_id, record)
            return record

        @r.get("/oauth/authorize", response_class=HTMLResponse)
        async def authorize(request: Request):
            args = dict(request.query_params)
            client = self.validate_authorize(args)
            fields = "".join(
                f'<input type="hidden" name="{html.escape(k, quote=True)}" value="{html.escape(v, quote=True)}">'
                for k, v in args.items()
            )
            return (
                '<!doctype html><meta name="referrer" content="no-referrer"><title>Connect AI · Riality Ads</title><h1>Connect '
                + html.escape(client["client_name"])
                + '</h1><p>This client can read your configured ad accounts, save drafts and execute operations you separately approve. It cannot approve operations or retrieve platform tokens.</p><form method="post">'
                + fields
                + '<label>Operator key <input type="password" name="admin_key" required autocomplete="off"></label><button>Authorize client</button></form>'
            )

        @r.post("/oauth/authorize")
        async def consent(request: Request):
            args = dict(await request.form())
            self.validate_authorize(args)
            key = args.pop("admin_key", "")
            if not secrets.compare_digest(str(key), self.s.settings.admin_key):
                raise AdsError("Invalid operator key", "unauthorized", 401)
            code = secrets.token_urlsafe(48)
            self.put(
                "code:" + token_hash(code),
                {
                    "client_id": args["client_id"],
                    "redirect_uri": args["redirect_uri"],
                    "challenge": args["code_challenge"],
                    "expires": time.time() + 120,
                },
            )
            self.s.store.audit("oauth_client_authorized", args["client_id"], {})
            u = urlsplit(args["redirect_uri"])
            query = (
                parse_qsl(u.query)
                + [("code", code)]
                + ([("state", args["state"])] if "state" in args else [])
            )
            return RedirectResponse(
                urlunsplit((u.scheme, u.netloc, u.path, urlencode(query), "")), status_code=303
            )

        @r.post("/oauth/token")
        async def token(request: Request):
            args = dict(await request.form())
            client_id = str(args.get("client_id", ""))
            if not self.get("client:" + client_id):
                raise AdsError("Unknown OAuth client", "invalid_client", 400)
            if args.get("grant_type") == "authorization_code":
                value = self.get("code:" + token_hash(str(args.get("code", ""))), consume=True)
                verifier = str(args.get("code_verifier", ""))
                import re

                if not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier):
                    raise AdsError("Invalid PKCE verifier", "invalid_grant", 400)
                challenge = (
                    base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                    .decode()
                    .rstrip("=")
                )
                if (
                    not value
                    or value["expires"] < time.time()
                    or value["client_id"] != client_id
                    or value["redirect_uri"] != args.get("redirect_uri")
                    or not secrets.compare_digest(value["challenge"], challenge)
                ):
                    raise AdsError("Code or PKCE verification failed", "invalid_grant", 400)
            elif args.get("grant_type") == "refresh_token":
                value = self.get(
                    "refresh:" + token_hash(str(args.get("refresh_token", ""))), consume=True
                )
                if not value or value["expires"] < time.time() or value["client_id"] != client_id:
                    raise AdsError("Refresh token is invalid or expired", "invalid_grant", 400)
            else:
                raise AdsError("Unsupported grant", "unsupported_grant_type", 400)
            return JSONResponse(
                self.tokens(client_id), headers={"Cache-Control": "no-store", "Pragma": "no-cache"}
            )
