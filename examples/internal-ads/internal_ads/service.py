from __future__ import annotations

import asyncio
import os
import time
from decimal import Decimal

import httpx

from .config import Settings
from .demo import Demo
from .models import CampaignDraft, ChangeDraft
from .providers_base import AdsError, ProviderError, to_units
from .providers_core import Google, Meta
from .store import Store, digest


class Service:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.store = Store(settings.database, settings.encryption_key)
        self.store.bind_mode(settings.demo)
        self.accounts = {a.alias: a for a in settings.accounts}
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(45, connect=10),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        self.refresh_locks = {a.alias: asyncio.Lock() for a in settings.accounts}
        self.active_accounts: set[str] = set()

    def account(self, alias):
        if alias not in self.accounts:
            raise AdsError("Unknown account alias; list connections first", "not_found", 404)
        return self.accounts[alias]

    def secrets(self, account):
        names = ["ACCESS_TOKEN", "CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN", "DEVELOPER_TOKEN"]
        env = {
            n: os.environ[account.credentials_prefix + "_" + n]
            for n in names
            if os.environ.get(account.credentials_prefix + "_" + n)
        }
        return {**env, **self.store.tokens(account.alias)}

    async def provider(self, alias):
        account = self.account(alias)
        if self.settings.demo:
            return Demo(account, self.store)
        async with self.refresh_locks[alias]:
            secrets = self.secrets(account)
            if (
                account.platform in ("google", "amazon")
                and secrets.get("REFRESH_TOKEN")
                and (
                    not secrets.get("ACCESS_TOKEN")
                    or float(secrets.get("EXPIRES_AT", 0)) <= time.time() + 60
                )
            ):
                required = ["CLIENT_ID", "CLIENT_SECRET"]
                if not all(secrets.get(n) for n in required):
                    raise AdsError(
                        "OAuth refresh needs CLIENT_ID and CLIENT_SECRET", "not_connected", 409
                    )
                endpoint = (
                    "https://oauth2.googleapis.com/token"
                    if account.platform == "google"
                    else "https://api.amazon.com/auth/o2/token"
                )
                try:
                    r = await self.client.post(
                        endpoint,
                        data={
                            "grant_type": "refresh_token",
                            "refresh_token": secrets["REFRESH_TOKEN"],
                            "client_id": secrets["CLIENT_ID"],
                            "client_secret": secrets["CLIENT_SECRET"],
                        },
                    )
                    r.raise_for_status()
                    data = r.json()
                    secrets.update(
                        ACCESS_TOKEN=data["access_token"],
                        EXPIRES_AT=time.time() + int(data.get("expires_in", 3600)),
                    )
                    if data.get("refresh_token"):
                        secrets["REFRESH_TOKEN"] = data["refresh_token"]
                    # Only token material is stored; never copy unrelated env.
                    self.store.save_tokens(
                        alias,
                        {
                            k: secrets[k]
                            for k in ("ACCESS_TOKEN", "REFRESH_TOKEN", "EXPIRES_AT")
                            if k in secrets
                        },
                    )
                except (httpx.HTTPError, ValueError, KeyError):
                    raise AdsError(
                        "OAuth refresh failed; reconnect account", "not_connected", 409
                    ) from None
        from .providers_extra import AmazonProvider, LinkedInProvider, TikTokProvider

        cls = {
            "google": Google,
            "meta": Meta,
            "tiktok": TikTokProvider,
            "linkedin": LinkedInProvider,
            "amazon": AmazonProvider,
        }[account.platform]
        return cls(account, secrets, self.client)

    async def connections(self):
        result = []
        for account in self.accounts.values():
            from .providers_extra import AmazonProvider, LinkedInProvider, TikTokProvider

            cls = {
                "google": Google,
                "meta": Meta,
                "tiktok": TikTokProvider,
                "linkedin": LinkedInProvider,
                "amazon": AmazonProvider,
            }[account.platform]
            token_fields = self.secrets(account)
            has_token = self.settings.demo or bool(
                token_fields.get("ACCESS_TOKEN") or token_fields.get("REFRESH_TOKEN")
            )
            result.append(
                {
                    "account": account.alias,
                    "platform": account.platform,
                    "account_id": account.account_id,
                    "currency": account.currency,
                    "daily_budget_limit": str(account.daily_budget_limit),
                    "credentials_configured": has_token,
                    "verified": False,
                    "capabilities": cls.capabilities,
                }
            )
        return {
            "mode": "demo" if self.settings.demo else "live",
            "writes_enabled": self.settings.enable_writes,
            "accounts": result,
            "unsupported_platforms": {
                "chatgpt": "No implemented/verified public ads API connector"
            },
            "note": "Configured credentials do not establish access. Use verify_connection.",
        }

    async def verify(self, alias):
        p = await self.provider(alias)
        return await p.verify_account()

    async def campaigns(self, alias, limit=50, cursor=None):
        p = await self.provider(alias)
        await p.verify_account()
        return {
            "account": alias,
            "currency": p.account.currency,
            "simulated": self.settings.demo,
            **await p.campaigns(limit, cursor),
        }

    async def performance(self, alias, dates, limit=50, cursor=None):
        p = await self.provider(alias)
        meta = await p.verify_account()
        return {
            "account": alias,
            "currency": p.account.currency,
            "timezone": meta.get("timezone"),
            "date_range": dates.model_dump(),
            "simulated": self.settings.demo,
            **await p.performance(dates, limit, cursor),
        }

    async def keywords(self, request):
        p = await self.provider(request.account)
        if p.account.platform != "google":
            raise AdsError("Keyword research requires a Google account", "unsupported", 422)
        await p.verify_account()
        return await p.keywords(request)

    def budget(self, account, value):
        if value > account.daily_budget_limit:
            raise AdsError(
                f"Daily budget exceeds {account.alias} configured limit {account.daily_budget_limit} {account.currency}",
                "budget_limit",
                422,
            )
        to_units(value, account.minor_units)

    def binding(self, account):
        return {
            "account": account.model_dump(mode="json"),
            "mode": "demo" if self.settings.demo else "live",
            "versions": {
                k: os.getenv(k, default)
                for k, default in [
                    ("ADS_GOOGLE_VERSION", "v26"),
                    ("ADS_META_VERSION", "v24.0"),
                    ("ADS_LINKEDIN_VERSION", "202610"),
                ]
            },
        }

    async def prepare_campaign(self, draft: CampaignDraft, key: str):
        a = self.account(draft.account)
        self.store.assert_no_ambiguity(a.alias)
        self.budget(a, draft.daily_budget)
        if (a.platform, draft.kind) not in [("google", "google_search"), ("meta", "meta_traffic")]:
            raise AdsError("Campaign kind does not match account platform", "unsupported", 422)
        p = await self.provider(a.alias)
        verified = await p.verify_account()
        payload = (
            p.create_payload(draft)
            if isinstance(p, Google)
            else p.preview(draft)
            if isinstance(p, Meta)
            else {"simulated": True, "draft": draft.model_dump(mode="json")}
        )
        # Google offers a no-mutation validation endpoint. Meta does not offer an
        # atomic cross-hierarchy equivalent; that distinction remains visible.
        if isinstance(p, Google):
            await p.validate_create(draft)
        plan = {
            "request": {"type": "create", "draft": draft.model_dump(mode="json")},
            "binding": self.binding(a),
            "verified_account": verified,
            "provider_payload": payload,
            "validation": "Google validateOnly succeeded"
            if isinstance(p, Google)
            else "Local schema only; platform validates each step during execution",
            "effect": "Create campaign PAUSED; child ad groups/ad sets and ads enabled behind paused parent. A later approved resume can launch.",
            "spend_note": "Daily budget is a platform pacing budget, not a guaranteed hard spend cap. No launch in this operation.",
        }
        return self.store.prepare(key, plan, self.settings.approval_ttl)

    async def prepare_change(self, draft: ChangeDraft, key: str):
        a = self.account(draft.account)
        self.store.assert_no_ambiguity(a.alias)
        p = await self.provider(a.alias)
        await p.verify_account()
        baseline = await p.get_campaign(draft.campaign_id)
        if draft.action not in p.capabilities:
            raise AdsError("Action unsupported by this connector", "unsupported", 422)
        if draft.action == "set_budget":
            self.budget(a, draft.daily_budget)
            if baseline.get("daily_budget") is None or baseline.get("shared_budget"):
                raise AdsError(
                    "Campaign budget is unknown/shared/lifetime/ad-set owned; cannot change safely",
                    "unsupported",
                    422,
                )
        if draft.action == "resume":
            if baseline.get("daily_budget") is None:
                raise AdsError(
                    "Cannot resume a campaign with unknown daily budget through this tool",
                    "unsupported",
                    422,
                )
            self.budget(a, Decimal(baseline["daily_budget"]))
        plan = {
            "request": {"type": "change", "draft": draft.model_dump(mode="json")},
            "binding": self.binding(a),
            "baseline": baseline,
            "provider_payload": p.change_payload(draft, baseline)
            if not self.settings.demo
            else {"simulated": True, "draft": draft.model_dump(mode="json")},
            "effect": {
                "action": draft.action,
                "daily_budget": str(draft.daily_budget) if draft.daily_budget else None,
            },
            "spend_note": "Resume may spend. Platform daily budgets are not guaranteed hard caps; child status/bids remain unchanged.",
        }
        return self.store.prepare(key, plan, self.settings.approval_ttl)

    async def execute(self, op_id):
        existing = self.store.get(op_id)
        if existing["state"] == "succeeded":
            return existing
        if not self.settings.enable_writes:
            raise AdsError(
                "Writes disabled. Operator must set ADS_ENABLE_WRITES=true", "writes_disabled", 409
            )
        request = existing["plan"]["request"]
        a = self.account(request["draft"]["account"])
        if a.alias in self.active_accounts:
            raise AdsError("Another operation is executing on this account", "conflict", 409)
        # No await between membership check and add. One process owns this DB.
        self.active_accounts.add(a.alias)
        claimed = False
        try:
            op, claimed = self.store.claim(op_id)
            if not claimed:
                return op
            plan = op["plan"]
            self.store.assert_no_ambiguity(a.alias)
            if digest(self.binding(a)) != digest(plan["binding"]):
                raise AdsError(
                    "Account policy, mode or API version changed; prepare a new preview",
                    "stale_preview",
                    409,
                )
            p = await self.provider(a.alias)
            await p.verify_account()

            async def receipt(step, body):
                self.store.receipt(op_id, step, body)

            if request["type"] == "create":
                draft = CampaignDraft.model_validate(request["draft"])
                self.budget(a, draft.daily_budget)
                payload = (
                    p.create_payload(draft)
                    if isinstance(p, Google)
                    else p.preview(draft)
                    if isinstance(p, Meta)
                    else {"simulated": True, "draft": draft.model_dump(mode="json")}
                )
                if digest(payload) != digest(plan["provider_payload"]):
                    raise AdsError(
                        "Provider payload changed since review; prepare a fresh preview",
                        "stale_preview",
                        409,
                    )
                result = await p.create(draft, receipt)
            else:
                draft = ChangeDraft.model_validate(request["draft"])
                current = await p.get_campaign(draft.campaign_id)
                if digest(current) != digest(plan["baseline"]):
                    raise AdsError(
                        "Campaign changed since preview; prepare a new operation",
                        "stale_preview",
                        409,
                    )
                payload = (
                    p.change_payload(draft, current)
                    if not self.settings.demo
                    else {"simulated": True, "draft": draft.model_dump(mode="json")}
                )
                if digest(payload) != digest(plan["provider_payload"]):
                    raise AdsError(
                        "Provider payload changed since review; prepare a fresh preview",
                        "stale_preview",
                        409,
                    )
                result = await p.change(draft, current, receipt)
                # Post-write verification is shared across connectors.
                observed = await p.get_campaign(draft.campaign_id)
                result["campaign"] = observed
                if draft.action == "set_budget" and (
                    observed.get("daily_budget") is None
                    or Decimal(observed["daily_budget"]) != draft.daily_budget
                ):
                    raise ProviderError("Post-write budget verification failed", uncertain=True)
                if draft.action == "pause" and observed.get("status") not in (
                    "PAUSED",
                    "DISABLE",
                    "CAMPAIGN_STATUS_DISABLE",
                ):
                    raise ProviderError("Post-write pause verification failed", uncertain=True)
                if draft.action == "resume" and observed.get("status") not in (
                    "ACTIVE",
                    "ENABLED",
                    "ENABLE",
                    "CAMPAIGN_STATUS_ENABLE",
                ):
                    raise ProviderError("Post-write resume verification failed", uncertain=True)
            return self.store.finish(op_id, "succeeded", result=result)
        except BaseException as e:
            if claimed:
                has_receipts = bool(self.store.get(op_id)["receipts"])
                uncertain = (
                    has_receipts or not isinstance(e, AdsError) or getattr(e, "uncertain", False)
                )
                state = "needs_reconciliation" if uncertain else "failed"
                # Only known sanitized error messages cross the external boundary.
                message = (
                    str(e)
                    if isinstance(e, AdsError)
                    else "Execution interrupted; inspect receipts and platform before replacement"
                )
                self.store.finish(op_id, state, error=message)
            raise
        finally:
            self.active_accounts.discard(a.alias)
