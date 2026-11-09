"""Account-scoped TikTok, LinkedIn and Amazon SP adapters; no HTTP passthrough."""

from __future__ import annotations

import json
import os
import re
from datetime import date

from .providers_base import AdsError, Provider, ProviderError, metrics, money, to_units


def _identifier(value):
    value = str(value)
    if not re.fullmatch(r"\d{1,30}", value):
        raise AdsError("Use the exact numeric campaign identifier", "invalid_request", 422)
    return value


def _page(cursor):
    try:
        value = int(cursor or "1")
    except ValueError:
        raise AdsError("Invalid numeric pagination cursor", "invalid_request", 422) from None
    if value < 1:
        raise AdsError("Pagination cursor must be positive", "invalid_request", 422)
    return value


def _currency(account, actual):
    if actual != account.currency:
        raise AdsError(
            "Configured currency differs from the provider account; update the account manifest",
            "account_mismatch",
            409,
        )


def _matched(values, key, target):
    matches = [v for v in values if str(v.get(key)) == target]
    if len(matches) != 1:
        raise AdsError(
            "The exact configured account/campaign was not returned by the provider",
            "account_mismatch",
            409,
        )
    return matches[0]


def _daily_change(draft, baseline):
    if draft.action == "set_budget" and baseline.get("daily_budget") is None:
        raise AdsError(
            "Campaign has no supported daily budget; budget-mode conversion is unsupported",
            "unsupported",
            422,
        )


class TikTokProvider(Provider):
    """Marketing API v1.3 campaign control and BASIC aggregate reporting."""

    base = "https://business-api.tiktok.com/open_api/v1.3"

    async def api(self, method, path, *, params=None, body=None):
        self.require("ACCESS_TOKEN")
        return await self.request(
            method,
            self.base + path,
            params=params,
            body=body,
            headers={"Access-Token": self.secrets["ACCESS_TOKEN"]},
        )

    async def verify_account(self):
        data = await self.api(
            "GET",
            "/advertiser/info/",
            params={
                "advertiser_ids": json.dumps([self.account.account_id]),
                "fields": json.dumps(["advertiser_id", "name", "currency"]),
            },
        )
        item = _matched(
            data.get("data", {}).get("list", []), "advertiser_id", self.account.account_id
        )
        _currency(self.account, item.get("currency"))
        return {
            "id": self.account.account_id,
            "currency": item["currency"],
            "name": item.get("name"),
        }

    def normalize(self, item):
        if (
            item.get("advertiser_id") is not None
            and str(item["advertiser_id"]) != self.account.account_id
        ):
            raise AdsError(
                "Provider returned a campaign owned by another advertiser", "account_mismatch", 409
            )
        budget_mode = item.get("budget_mode")
        return {
            "id": _identifier(item["campaign_id"]),
            "name": item.get("campaign_name"),
            "status": {"ENABLE": "ACTIVE", "DISABLE": "PAUSED"}.get(
                item.get("operation_status"), item.get("operation_status")
            ),
            "daily_budget": money(item["budget"])
            if budget_mode == "BUDGET_MODE_DAY" and item.get("budget") is not None
            else None,
            "budget_mode": budget_mode,
            "currency": self.account.currency,
        }

    async def get_campaign(self, campaign_id):
        campaign_id = _identifier(campaign_id)
        data = await self.api(
            "GET",
            "/campaign/get/",
            params={
                "advertiser_id": self.account.account_id,
                "filtering": json.dumps({"campaign_ids": [campaign_id]}),
                "page": 1,
                "page_size": 1,
            },
        )
        return self.normalize(
            _matched(data.get("data", {}).get("list", []), "campaign_id", campaign_id)
        )

    async def campaigns(self, limit=50, cursor=None):
        page = _page(cursor)
        data = (
            await self.api(
                "GET",
                "/campaign/get/",
                params={"advertiser_id": self.account.account_id, "page": page, "page_size": limit},
            )
        ).get("data", {})
        total_page = data.get("page_info", {}).get("total_page")
        next_cursor = str(page + 1) if total_page is not None and page < int(total_page) else None
        return {
            "items": [self.normalize(item) for item in data.get("list", [])],
            "next_cursor": next_cursor,
            "complete": total_page is not None and next_cursor is None,
        }

    async def performance(self, dates, limit=50, cursor=None):
        await self.verify_account()
        page = _page(cursor)
        data = (
            await self.api(
                "GET",
                "/report/integrated/get/",
                params={
                    "advertiser_id": self.account.account_id,
                    "report_type": "BASIC",
                    "data_level": "AUCTION_CAMPAIGN",
                    "dimensions": json.dumps(["campaign_id"]),
                    "metrics": json.dumps(["spend", "impressions", "clicks", "conversion"]),
                    "start_date": dates.start,
                    "end_date": dates.end,
                    "page": page,
                    "page_size": limit,
                },
            )
        ).get("data", {})
        rows = []
        for item in data.get("list", []):
            m = item["metrics"]
            rows.append(
                metrics(
                    _identifier(item["dimensions"]["campaign_id"]),
                    spend=m["spend"],
                    impressions=m["impressions"],
                    clicks=m["clicks"],
                    conversions=m.get("conversion"),
                    revenue=None,
                    currency=self.account.currency,
                )
            )
        total_page = data.get("page_info", {}).get("total_page")
        next_cursor = str(page + 1) if total_page is not None and page < int(total_page) else None
        return {
            "items": rows,
            "next_cursor": next_cursor,
            "complete": total_page is not None and next_cursor is None,
            "attribution": "TikTok BASIC auction campaign report; account timezone/configured attribution. Revenue not requested; ROAS unknown.",
        }

    def change_payload(self, draft, baseline):
        campaign_id = _identifier(draft.campaign_id)
        _daily_change(draft, baseline)
        if draft.action == "set_budget":
            to_units(draft.daily_budget, self.account.minor_units)
            path = "/campaign/update/"
            body = {
                "advertiser_id": self.account.account_id,
                "campaign_id": campaign_id,
                "budget": float(draft.daily_budget),
            }
        else:
            path = "/campaign/status/update/"
            body = {
                "advertiser_id": self.account.account_id,
                "campaign_ids": [campaign_id],
                "operation_status": "DISABLE" if draft.action == "pause" else "ENABLE",
            }
        return {"method": "POST", "path": path, "body": body, "api_version": "v1.3"}

    async def change(self, draft, baseline, receipt):
        plan = self.change_payload(draft, baseline)
        data = await self.api(plan["method"], plan["path"], body=plan["body"])
        await receipt(
            "campaign_change",
            {
                "campaign_id": draft.campaign_id,
                "action": draft.action,
                "request_id": data.get("request_id"),
            },
        )
        return {"campaign_id": draft.campaign_id, "action": draft.action}


class LinkedInProvider(Provider):
    """Versioned REST API with cursor inventory and campaign-paged analytics."""

    base = "https://api.linkedin.com/rest"

    @property
    def account_path(self):
        return "/adAccounts/" + self.account.account_id

    @property
    def version(self):
        version = os.getenv("ADS_LINKEDIN_VERSION", "202610")
        if not re.fullmatch(r"20\d{4}", version):
            raise AdsError("ADS_LINKEDIN_VERSION must use YYYYMM", "invalid_request", 422)
        return version

    async def api(self, method, path, *, params=None, body=None, partial=False):
        self.require("ACCESS_TOKEN")
        version = self.version
        headers = {
            "Authorization": "Bearer " + self.secrets["ACCESS_TOKEN"],
            "LinkedIn-Version": version,
            "X-Restli-Protocol-Version": "2.0.0",
        }
        if partial:
            headers["X-RestLi-Method"] = "PARTIAL_UPDATE"
        return await self.request(
            method, self.base + path, headers=headers, params=params, body=body
        )

    async def verify_account(self):
        data = await self.api("GET", self.account_path)
        if str(data.get("id")) != self.account.account_id:
            raise AdsError(
                "Provider account ID differs from the configured account", "account_mismatch", 409
            )
        _currency(self.account, data.get("currency"))
        return {
            "id": self.account.account_id,
            "currency": data["currency"],
            "name": data.get("name"),
        }

    def normalize(self, item):
        if item.get("account") != "urn:li:sponsoredAccount:" + self.account.account_id:
            raise AdsError(
                "Provider returned a campaign owned by another account", "account_mismatch", 409
            )
        budget = item.get("dailyBudget")
        if budget is not None:
            _currency(self.account, budget.get("currencyCode"))
        return {
            "id": _identifier(item["id"]),
            "name": item.get("name"),
            "status": item.get("status"),
            "daily_budget": money(budget["amount"]) if budget is not None else None,
            "budget_mode": "DAILY" if budget is not None else "OTHER",
            "currency": self.account.currency,
        }

    async def get_campaign(self, campaign_id):
        campaign_id = _identifier(campaign_id)
        data = await self.api("GET", self.account_path + "/adCampaigns/" + campaign_id)
        if str(data.get("id")) != campaign_id:
            raise AdsError(
                "Provider campaign ID differs from the requested campaign", "account_mismatch", 409
            )
        return self.normalize(data)

    async def campaigns(self, limit=50, cursor=None):
        params = {
            "q": "search",
            "search": "(status:(values:List(ACTIVE,PAUSED,ARCHIVED,COMPLETED,CANCELED,DRAFT,PENDING_DELETION,REMOVED)))",
            "pageSize": limit,
            "sortOrder": "ASCENDING",
        }
        if cursor:
            params["pageToken"] = cursor
        data = await self.api("GET", self.account_path + "/adCampaigns", params=params)
        next_cursor = data.get("metadata", {}).get("nextPageToken")
        return {
            "items": [self.normalize(item) for item in data.get("elements", [])],
            "next_cursor": next_cursor,
            "complete": next_cursor is None,
        }

    async def performance(self, dates, limit=50, cursor=None):
        await self.verify_account()
        # adAnalytics cannot page. Request one bounded inventory page of exact
        # campaign URNs per call; use the campaign inventory's provider cursor.
        inventory = await self.campaigns(limit, cursor)
        ids = [item["id"] for item in inventory["items"]]
        if not ids:
            return {
                **inventory,
                "attribution": "LinkedIn campaign report; absent rows mean no activity or no access, not verified zero.",
            }

        def restli_date(value):
            d = date.fromisoformat(value)
            return f"(year:{d.year},month:{d.month},day:{d.day})"

        data = await self.api(
            "GET",
            "/adAnalytics",
            params={
                "q": "analytics",
                "pivot": "CAMPAIGN",
                "timeGranularity": "ALL",
                "dateRange": f"(start:{restli_date(dates.start)},end:{restli_date(dates.end)})",
                "campaigns": "List(" + ",".join("urn:li:sponsoredCampaign:" + v for v in ids) + ")",
                "fields": "dateRange,pivotValues,impressions,clicks,costInLocalCurrency,externalWebsiteConversions,conversionValueInLocalCurrency",
            },
        )
        rows = []
        for row in data.get("elements", []):
            pivots = row.get("pivotValues", [])
            if len(pivots) != 1 or not str(pivots[0]).startswith("urn:li:sponsoredCampaign:"):
                raise ProviderError("LinkedIn returned an unexpected campaign pivot")
            campaign_id = _identifier(str(pivots[0]).rsplit(":", 1)[-1])
            if campaign_id not in ids:
                raise AdsError(
                    "Analytics returned a campaign outside the requested page",
                    "account_mismatch",
                    409,
                )
            rows.append(
                metrics(
                    campaign_id,
                    spend=row["costInLocalCurrency"],
                    impressions=row["impressions"],
                    clicks=row["clicks"],
                    conversions=row.get("externalWebsiteConversions"),
                    revenue=row.get("conversionValueInLocalCurrency"),
                    currency=self.account.currency,
                    provider_date_range=row.get("dateRange"),
                )
            )
        return {
            "items": rows,
            "next_cursor": inventory["next_cursor"],
            "complete": inventory["complete"],
            "campaign_ids_examined": ids,
            "attribution": "LinkedIn configured attribution. Missing rows mean no activity or no access, not verified zero; inventory includes current campaigns. LinkedIn can round date ranges older than six months to month boundaries; inspect provider_date_range.",
        }

    def change_payload(self, draft, baseline):
        campaign_id = _identifier(draft.campaign_id)
        _daily_change(draft, baseline)
        if draft.action == "set_budget":
            to_units(draft.daily_budget, self.account.minor_units)
            fields = {
                "dailyBudget": {
                    "amount": money(draft.daily_budget),
                    "currencyCode": self.account.currency,
                }
            }
        else:
            fields = {"status": "PAUSED" if draft.action == "pause" else "ACTIVE"}
        return {
            "method": "POST",
            "path": self.account_path + "/adCampaigns/" + campaign_id,
            "body": {"patch": {"$set": fields}},
            "api_version": self.version,
            "partial_update": True,
        }

    async def change(self, draft, baseline, receipt):
        plan = self.change_payload(draft, baseline)
        await self.api(
            plan["method"], plan["path"], partial=plan["partial_update"], body=plan["body"]
        )
        await receipt("campaign_change", {"campaign_id": draft.campaign_id, "action": draft.action})
        return {"campaign_id": draft.campaign_id, "action": draft.action}


class AmazonProvider(Provider):
    """Sponsored Products v3 campaigns only. Async reporting is not exposed."""

    capabilities = ["campaigns", "pause", "resume", "set_budget"]
    hosts = {
        "na": "https://advertising-api.amazon.com",
        "eu": "https://advertising-api-eu.amazon.com",
        "fe": "https://advertising-api-fe.amazon.com",
    }
    media_type = "application/vnd.spCampaign.v3+json"

    async def api(self, method, path, *, body=None, campaign=True):
        self.require("ACCESS_TOKEN", "CLIENT_ID")
        headers = {
            "Authorization": "Bearer " + self.secrets["ACCESS_TOKEN"],
            "Amazon-Advertising-API-ClientId": self.secrets["CLIENT_ID"],
            "Amazon-Advertising-API-Scope": self.account.account_id,
        }
        if campaign:
            headers.update({"Accept": self.media_type, "Content-Type": self.media_type})
        return await self.request(
            method, self.hosts[self.account.region] + path, headers=headers, body=body
        )

    async def verify_account(self):
        data = await self.api("GET", "/v2/profiles", campaign=False)
        if not isinstance(data, list):
            raise ProviderError("Amazon returned an unexpected profiles response")
        item = _matched(data, "profileId", self.account.account_id)
        _currency(self.account, item.get("currencyCode"))
        return {
            "id": self.account.account_id,
            "currency": item["currencyCode"],
            "name": item.get("accountInfo", {}).get("name"),
        }

    def normalize(self, item):
        # Campaigns endpoint is scoped through Amazon-Advertising-API-Scope.
        budget = item.get("budget", {})
        return {
            "id": _identifier(item["campaignId"]),
            "name": item.get("name"),
            "status": item.get("state"),
            "daily_budget": money(budget["budget"])
            if budget.get("budgetType") == "DAILY" and budget.get("budget") is not None
            else None,
            "budget_mode": budget.get("budgetType"),
            "currency": self.account.currency,
            "ad_product": "SPONSORED_PRODUCTS",
        }

    async def get_campaign(self, campaign_id):
        campaign_id = _identifier(campaign_id)
        data = await self.api(
            "POST",
            "/sp/campaigns/list",
            body={"campaignIdFilter": {"include": [campaign_id]}, "maxResults": 1},
        )
        return self.normalize(_matched(data.get("campaigns", []), "campaignId", campaign_id))

    async def campaigns(self, limit=50, cursor=None):
        body = {"maxResults": limit}
        if cursor:
            body["nextToken"] = cursor
        data = await self.api("POST", "/sp/campaigns/list", body=body)
        next_cursor = data.get("nextToken")
        return {
            "items": [self.normalize(item) for item in data.get("campaigns", [])],
            "next_cursor": next_cursor,
            "complete": next_cursor is None,
        }

    async def performance(self, dates, limit=50, cursor=None):
        raise AdsError(
            "Amazon performance needs the asynchronous Reporting v3 create/poll/download workflow; not implemented in this connector",
            "unsupported",
            422,
        )

    def change_payload(self, draft, baseline):
        campaign_id = _identifier(draft.campaign_id)
        _daily_change(draft, baseline)
        if draft.action == "set_budget":
            to_units(draft.daily_budget, self.account.minor_units)
            fields = {
                "campaignId": campaign_id,
                "budget": {"budget": float(draft.daily_budget), "budgetType": "DAILY"},
            }
        else:
            fields = {
                "campaignId": campaign_id,
                "state": "PAUSED" if draft.action == "pause" else "ENABLED",
            }
        return {
            "method": "PUT",
            "path": "/sp/campaigns",
            "body": {"campaigns": [fields]},
            "api_version": "sp-v3",
        }

    async def change(self, draft, baseline, receipt):
        campaign_id = _identifier(draft.campaign_id)
        plan = self.change_payload(draft, baseline)
        data = await self.api(plan["method"], plan["path"], body=plan["body"])
        results = data.get("campaigns", {})
        successes, errors = results.get("success", []), results.get("error", [])
        if errors or len(successes) != 1 or str(successes[0].get("campaignId")) != campaign_id:
            raise ProviderError(
                "Amazon did not confirm the exact campaign update; reconcile in the platform before retrying",
                uncertain=True,
            )
        await receipt("campaign_change", {"campaign_id": campaign_id, "action": draft.action})
        return {"campaign_id": campaign_id, "action": draft.action}
