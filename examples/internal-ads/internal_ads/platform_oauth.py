from __future__ import annotations

import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode

from .providers_base import AdsError


async def start(service, alias):
    a = service.account(alias)
    if service.settings.demo:
        raise AdsError("OAuth is disabled in demo mode")
    if a.platform not in ("google", "meta"):
        raise AdsError(
            "Use an operator-managed token/refresh token for this connector; see README",
            "unsupported",
            422,
        )
    creds = service.secrets(a)
    if not creds.get("CLIENT_ID") or not creds.get("CLIENT_SECRET"):
        raise AdsError(
            f"Set {a.credentials_prefix}_CLIENT_ID and _CLIENT_SECRET first", "not_connected", 409
        )
    state, verifier = secrets.token_urlsafe(48), secrets.token_urlsafe(64)
    service.store.oauth_state(alias, state, verifier)
    redirect = service.settings.public_url + "/oauth/platform/callback"
    args = {
        "client_id": creds["CLIENT_ID"],
        "redirect_uri": redirect,
        "state": state,
        "response_type": "code",
    }
    if a.platform == "google":
        args.update(
            scope="https://www.googleapis.com/auth/adwords",
            access_type="offline",
            prompt="consent",
            code_challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("="),
            code_challenge_method="S256",
        )
        root = "https://accounts.google.com/o/oauth2/v2/auth"
    else:
        from .providers_core import Meta

        root = (
            "https://www.facebook.com/"
            + Meta(a, creds, service.client).root.rsplit("/", 1)[-1]
            + "/dialog/oauth"
        )
        args["scope"] = "ads_read,ads_management"
    return {"authorization_url": root + "?" + urlencode(args), "callback_url": redirect}


async def callback(service, state, code):
    record = service.store.consume_state(state)
    a = service.account(record["account"])
    creds = service.secrets(a)
    redirect = service.settings.public_url + "/oauth/platform/callback"
    body = {
        "client_id": creds.get("CLIENT_ID"),
        "client_secret": creds.get("CLIENT_SECRET"),
        "code": code,
        "redirect_uri": redirect,
    }
    try:
        if a.platform == "google":
            r = await service.client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    **body,
                    "grant_type": "authorization_code",
                    "code_verifier": record["verifier"],
                },
            )
        else:
            from .providers_core import Meta

            # Token exchange uses body, not query parameters that could enter logs.
            r = await service.client.post(
                Meta(a, creds, service.client).root + "/oauth/access_token", data=body
            )
        r.raise_for_status()
        data = r.json()
        value = {"ACCESS_TOKEN": data["access_token"]}
        if data.get("refresh_token"):
            value["REFRESH_TOKEN"] = data["refresh_token"]
        if data.get("expires_in"):
            value["EXPIRES_AT"] = time.time() + int(data["expires_in"])
        # Preserve a previously granted Google offline refresh token if omitted.
        value = {**service.store.tokens(a.alias), **value}
        service.store.save_tokens(a.alias, value)
    except Exception:
        raise AdsError(
            "Platform OAuth exchange failed; restart connection", "oauth_failed", 400
        ) from None
    return {
        "account": a.alias,
        "connected": True,
        "note": "Token saved encrypted. Verify account access before use.",
    }
