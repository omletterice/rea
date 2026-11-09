from __future__ import annotations

from decimal import Decimal

import httpx

from .models import Account


class AdsError(Exception):
    def __init__(self, message: str, code: str = "invalid_request", status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


class ProviderError(AdsError):
    def __init__(
        self, message="Provider request failed; inspect account and permissions", *, uncertain=False
    ):
        super().__init__(message, "provider_error", 502)
        self.uncertain = uncertain


def money(value) -> str:
    return format(Decimal(str(value)), "f")


def to_units(value, digits: int) -> int:
    scaled = Decimal(str(value)) * (10**digits)
    if scaled != scaled.to_integral_value():
        raise AdsError(f"Amount has more than {digits} supported decimal places")
    return int(scaled)


def metrics(campaign_id, *, spend, impressions, clicks, conversions=None, revenue=None, **extra):
    cost = Decimal(str(spend))
    conversions = None if conversions is None else Decimal(str(conversions))
    revenue = None if revenue is None else Decimal(str(revenue))
    return {
        "campaign_id": str(campaign_id),
        "spend": money(cost),
        "impressions": int(impressions),
        "clicks": int(clicks),
        "conversions": None if conversions is None else money(conversions),
        "revenue": None if revenue is None else money(revenue),
        "ctr": float(Decimal(str(clicks)) / Decimal(str(impressions)))
        if int(impressions)
        else None,
        "cpa": money(cost / conversions) if conversions else None,
        "roas": float(revenue / cost) if revenue is not None and cost else None,
        **extra,
    }


class Provider:
    capabilities = ["campaigns", "performance", "pause", "resume", "set_budget"]

    def __init__(self, account: Account, secrets: dict, client: httpx.AsyncClient):
        self.account, self.secrets, self.client = account, secrets, client

    def require(self, *names):
        missing = [n for n in names if not self.secrets.get(n)]
        if missing:
            raise AdsError(
                f"{self.account.alias}: configure {', '.join(missing)}", "not_connected", 409
            )

    async def request(self, method, url, *, headers=None, params=None, body=None, form=None):
        try:
            r = await self.client.request(
                method, url, headers=headers, params=params, json=body, data=form
            )
        except (httpx.HTTPError, TimeoutError):
            # Never echo URLs/headers/tokens or provider response text into the chat/log.
            raise ProviderError(
                "Provider transport failed; a write may have been applied",
                uncertain=method != "GET",
            ) from None
        if r.status_code == 204:
            return {}
        try:
            data = r.json()
        except ValueError:
            raise ProviderError(
                "Provider returned a non-JSON response", uncertain=method != "GET"
            ) from None
        if r.is_error or (
            isinstance(data, dict)
            and (("error" in data) or (isinstance(data.get("code"), int) and data["code"] != 0))
        ):
            raise ProviderError(
                f"Provider rejected request (HTTP {r.status_code}); check permissions, API version and platform validation",
                uncertain=r.status_code >= 500,
            )
        return data

    async def verify_account(self):
        raise NotImplementedError

    async def get_campaign(self, campaign_id):
        raise NotImplementedError

    async def campaigns(self, limit=50, cursor=None):
        raise NotImplementedError

    async def performance(self, dates, limit=50, cursor=None):
        raise NotImplementedError

    async def create(self, draft, receipt):
        raise AdsError("Campaign creation is not supported on this platform", "unsupported", 422)

    async def change(self, draft, baseline, receipt):
        raise NotImplementedError
