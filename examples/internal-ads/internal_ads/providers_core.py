from __future__ import annotations

import json
import os
from decimal import Decimal

from .providers_base import AdsError, Provider, ProviderError, metrics, money, to_units


class Google(Provider):
    capabilities = Provider.capabilities + ["google_search", "keywords"]

    @property
    def root(self):
        version = os.getenv("ADS_GOOGLE_VERSION", "v26")
        if not version.removeprefix("v").isdigit():
            raise AdsError("Invalid ADS_GOOGLE_VERSION")
        return f"https://googleads.googleapis.com/{version}/customers/{self.account.account_id}"

    def headers(self):
        self.require("ACCESS_TOKEN", "DEVELOPER_TOKEN")
        h = {
            "Authorization": "Bearer " + self.secrets["ACCESS_TOKEN"],
            "developer-token": self.secrets["DEVELOPER_TOKEN"],
        }
        if self.account.login_customer_id:
            h["login-customer-id"] = self.account.login_customer_id
        return h

    async def search(self, query, cursor=None):
        body = {"query": query}
        if cursor:
            body["pageToken"] = cursor
        return await self.request(
            "POST", self.root + "/googleAds:search", headers=self.headers(), body=body
        )

    async def verify_account(self):
        rows = (
            await self.search(
                "SELECT customer.id, customer.currency_code, customer.time_zone FROM customer LIMIT 1"
            )
        ).get("results", [])
        if not rows:
            raise AdsError("Google account was not returned", "account_mismatch", 409)
        c = rows[0]["customer"]
        if str(c["id"]) != self.account.account_id or c["currencyCode"] != self.account.currency:
            raise AdsError(
                "Google account ID/currency differs from accounts.json", "account_mismatch", 409
            )
        return {"id": str(c["id"]), "currency": c["currencyCode"], "timezone": c.get("timeZone")}

    @staticmethod
    def campaign(row):
        c, b = row["campaign"], row.get("campaignBudget", {})
        return {
            "id": str(c["id"]),
            "name": c["name"],
            "status": c["status"],
            "daily_budget": money(Decimal(b["amountMicros"]) / 1000000)
            if "amountMicros" in b
            else None,
            "budget_id": b.get("resourceName"),
            "shared_budget": b.get("explicitlyShared", False),
            "currency": row.get("customer", {}).get("currencyCode"),
            "channel": c.get("advertisingChannelType"),
        }

    campaign_fields = "campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type, campaign_budget.resource_name, campaign_budget.amount_micros, campaign_budget.explicitly_shared, customer.currency_code"

    async def get_campaign(self, campaign_id):
        if not str(campaign_id).isdigit():
            raise AdsError("Numeric campaign_id required")
        rows = (
            await self.search(
                f"SELECT {self.campaign_fields} FROM campaign WHERE campaign.id = {campaign_id}"
            )
        ).get("results", [])
        if not rows:
            raise AdsError("Campaign not in this account", "not_found", 404)
        return self.campaign(rows[0])

    async def campaigns(self, limit=50, cursor=None):
        # Google controls its own page size. Service returns all rows in that page,
        # never slices away rows that the next token would skip.
        data = await self.search(
            f"SELECT {self.campaign_fields} FROM campaign WHERE campaign.status != 'REMOVED' ORDER BY campaign.id",
            cursor,
        )
        return {
            "items": [self.campaign(r) for r in data.get("results", [])],
            "next_cursor": data.get("nextPageToken"),
            "complete": not bool(data.get("nextPageToken")),
            "page_size_control": "provider",
        }

    async def performance(self, dates, limit=50, cursor=None):
        data = await self.search(
            f"SELECT campaign.id, campaign.name, metrics.cost_micros, metrics.impressions, metrics.clicks, metrics.conversions, metrics.conversions_value FROM campaign WHERE segments.date BETWEEN '{dates.start}' AND '{dates.end}' ORDER BY campaign.id",
            cursor,
        )
        items = []
        for row in data.get("results", []):
            m = row.get("metrics", {})
            items.append(
                metrics(
                    row["campaign"]["id"],
                    name=row["campaign"]["name"],
                    spend=Decimal(m.get("costMicros", 0)) / 1000000,
                    impressions=m.get("impressions", 0),
                    clicks=m.get("clicks", 0),
                    conversions=m.get("conversions"),
                    revenue=m.get("conversionsValue"),
                )
            )
        return {
            "items": items,
            "next_cursor": data.get("nextPageToken"),
            "complete": not bool(data.get("nextPageToken")),
            "attribution": "Google account conversion settings; values need not equal booked revenue",
        }

    async def keywords(self, request):
        body = {
            "keywordSeed": {"keywords": request.seeds},
            "language": f"languageConstants/{request.language_id}",
            "geoTargetConstants": [f"geoTargetConstants/{x}" for x in request.location_ids],
            "keywordPlanNetwork": "GOOGLE_SEARCH",
            "pageSize": 100,
        }
        if request.page_token:
            body["pageToken"] = request.page_token
        data = await self.request(
            "POST", self.root + ":generateKeywordIdeas", headers=self.headers(), body=body
        )
        return {
            "items": data.get("results", []),
            "currency": self.account.currency,
            "bid_units": "micros (divide by 1,000,000)",
            "next_cursor": data.get("nextPageToken"),
            "complete": not bool(data.get("nextPageToken")),
            "source": "Google KeywordPlanIdeaService historical estimates, not forecasts",
        }

    def create_payload(self, draft):
        if draft.kind != "google_search":
            raise AdsError("Use google_search for Google accounts")
        base = f"customers/{self.account.account_id}"
        budget, campaign, group = (
            base + "/campaignBudgets/-1",
            base + "/campaigns/-2",
            base + "/adGroups/-3",
        )
        ops = [
            {
                "campaignBudgetOperation": {
                    "create": {
                        "resourceName": budget,
                        "name": draft.name + " budget",
                        "amountMicros": str(to_units(draft.daily_budget, 6)),
                        "deliveryMethod": "STANDARD",
                        "explicitlyShared": False,
                    }
                }
            },
            {
                "campaignOperation": {
                    "create": {
                        "resourceName": campaign,
                        "name": draft.name,
                        "status": "PAUSED",
                        "advertisingChannelType": "SEARCH",
                        "campaignBudget": budget,
                        "manualCpc": {},
                        "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
                        "networkSettings": {
                            "targetGoogleSearch": True,
                            "targetSearchNetwork": False,
                            "targetContentNetwork": False,
                            "targetPartnerSearchNetwork": False,
                        },
                        "geoTargetTypeSetting": {"positiveGeoTargetType": "PRESENCE"},
                    }
                }
            },
            {
                "adGroupOperation": {
                    "create": {
                        "resourceName": group,
                        "name": draft.name + " group",
                        "campaign": campaign,
                        "status": "ENABLED",
                        "type": "SEARCH_STANDARD",
                        "cpcBidMicros": str(to_units(draft.cpc_bid, 6)),
                    }
                }
            },
        ]
        for kw in draft.keywords:
            ops.append(
                {
                    "adGroupCriterionOperation": {
                        "create": {
                            "adGroup": group,
                            "status": "ENABLED",
                            "keyword": {"text": kw, "matchType": "EXACT"},
                        }
                    }
                }
            )
        for geo in draft.location_ids:
            ops.append(
                {
                    "campaignCriterionOperation": {
                        "create": {
                            "campaign": campaign,
                            "location": {"geoTargetConstant": "geoTargetConstants/" + geo},
                        }
                    }
                }
            )
        for lang in draft.language_ids:
            ops.append(
                {
                    "campaignCriterionOperation": {
                        "create": {
                            "campaign": campaign,
                            "language": {"languageConstant": "languageConstants/" + lang},
                        }
                    }
                }
            )
        ops.append(
            {
                "adGroupAdOperation": {
                    "create": {
                        "adGroup": group,
                        "status": "ENABLED",
                        "ad": {
                            "finalUrls": [draft.final_url],
                            "responsiveSearchAd": {
                                "headlines": [{"text": x} for x in draft.headlines],
                                "descriptions": [{"text": x} for x in draft.descriptions],
                            },
                        },
                    }
                }
            }
        )
        return {"mutateOperations": ops, "partialFailure": False}

    async def validate_create(self, draft):
        await self.request(
            "POST",
            self.root + "/googleAds:mutate",
            headers=self.headers(),
            body={**self.create_payload(draft), "validateOnly": True},
        )

    async def create(self, draft, receipt):
        data = await self.request(
            "POST",
            self.root + "/googleAds:mutate",
            headers=self.headers(),
            body=self.create_payload(draft),
        )
        await receipt("google_atomic_create", data)
        resources = [
            r["campaignResult"]["resourceName"]
            for r in data.get("mutateOperationResponses", [])
            if "campaignResult" in r
        ]
        if len(resources) != 1:
            raise ProviderError(
                "Mutation succeeded but campaign identity is missing; reconcile receipts",
                uncertain=True,
            )
        result = await self.get_campaign(resources[0].split("/")[-1])
        if result["status"] != "PAUSED":
            raise ProviderError("Created campaign did not verify as PAUSED", uncertain=True)
        return {
            "campaign": result,
            "hierarchy": "campaign PAUSED; ad group/ad ENABLED behind paused parent",
        }

    def change_payload(self, draft, baseline):
        base = f"customers/{self.account.account_id}"
        if draft.action == "set_budget":
            if baseline.get("shared_budget") or not baseline.get("budget_id"):
                raise AdsError(
                    "Shared/unknown budgets cannot be changed through a single-campaign operation",
                    "unsupported",
                    422,
                )
            url = self.root + "/campaignBudgets:mutate"
            body = {
                "operations": [
                    {
                        "update": {
                            "resourceName": baseline["budget_id"],
                            "amountMicros": str(to_units(draft.daily_budget, 6)),
                        },
                        "updateMask": "amount_micros",
                    }
                ]
            }
        else:
            url = self.root + "/campaigns:mutate"
            body = {
                "operations": [
                    {
                        "update": {
                            "resourceName": base + "/campaigns/" + draft.campaign_id,
                            "status": "PAUSED" if draft.action == "pause" else "ENABLED",
                        },
                        "updateMask": "status",
                    }
                ]
            }
        return {"method": "POST", "url": url, "body": body}

    async def change(self, draft, baseline, receipt):
        plan = self.change_payload(draft, baseline)
        data = await self.request(
            plan["method"], plan["url"], headers=self.headers(), body=plan["body"]
        )
        await receipt("google_change", data)
        return {"campaign_id": draft.campaign_id, "action": draft.action}


class Meta(Provider):
    capabilities = Provider.capabilities + ["meta_traffic"]

    @property
    def root(self):
        version = os.getenv("ADS_META_VERSION", "v24.0")
        import re

        if not re.fullmatch(r"v\d+\.0", version):
            raise AdsError("Invalid ADS_META_VERSION")
        return "https://graph.facebook.com/" + version

    def headers(self):
        self.require("ACCESS_TOKEN")
        return {"Authorization": "Bearer " + self.secrets["ACCESS_TOKEN"]}

    async def get(self, path, params=None):
        return await self.request(
            "GET", self.root + "/" + path, headers=self.headers(), params=params
        )

    async def post(self, path, body):
        return await self.request("POST", self.root + "/" + path, headers=self.headers(), body=body)

    async def verify_account(self):
        c = await self.get(
            "act_" + self.account.account_id, {"fields": "account_id,currency,timezone_name"}
        )
        if (
            str(c.get("account_id")) != self.account.account_id
            or c.get("currency") != self.account.currency
        ):
            raise AdsError(
                "Meta account ID/currency differs from accounts.json", "account_mismatch", 409
            )
        return {
            "id": c["account_id"],
            "currency": c["currency"],
            "timezone": c.get("timezone_name"),
        }

    def campaign(self, c):
        if str(c.get("account_id")) != self.account.account_id:
            raise AdsError("Campaign not in this account", "account_mismatch", 409)
        return {
            "id": c["id"],
            "name": c["name"],
            "status": c["status"],
            "effective_status": c.get("effective_status"),
            "daily_budget": money(Decimal(c["daily_budget"]) / (10**self.account.minor_units))
            if "daily_budget" in c
            else None,
            "currency": self.account.currency,
            "objective": c.get("objective"),
        }

    fields = "id,account_id,name,status,effective_status,daily_budget,objective"

    async def get_campaign(self, campaign_id):
        if not str(campaign_id).isdigit():
            raise AdsError("Numeric campaign_id required")
        return self.campaign(await self.get(campaign_id, {"fields": self.fields}))

    async def campaigns(self, limit=50, cursor=None):
        params = {"fields": self.fields, "limit": limit}
        if cursor:
            params["after"] = cursor
        data = await self.get("act_" + self.account.account_id + "/campaigns", params)
        paging = data.get("paging", {})
        next_cursor = paging.get("cursors", {}).get("after") if paging.get("next") else None
        return {
            "items": [self.campaign(c) for c in data.get("data", [])],
            "next_cursor": next_cursor,
            "complete": not bool(next_cursor),
        }

    async def performance(self, dates, limit=50, cursor=None):
        params = {
            "level": "campaign",
            "fields": "campaign_id,campaign_name,spend,impressions,clicks,actions,action_values",
            "time_range": json.dumps({"since": dates.start, "until": dates.end}),
            "limit": limit,
            "action_report_time": "conversion",
        }
        if cursor:
            params["after"] = cursor
        data = await self.get("act_" + self.account.account_id + "/insights", params)
        items = []
        for row in data.get("data", []):
            # Use one exact conversion event; never sum overlapping purchase aliases.
            purchases = next(
                (a["value"] for a in row.get("actions", []) if a["action_type"] == "purchase"), None
            )
            revenue = next(
                (
                    a["value"]
                    for a in row.get("action_values", [])
                    if a["action_type"] == "purchase"
                ),
                None,
            )
            items.append(
                metrics(
                    row["campaign_id"],
                    name=row.get("campaign_name"),
                    spend=row.get("spend", 0),
                    impressions=row.get("impressions", 0),
                    clicks=row.get("clicks", 0),
                    conversions=purchases,
                    revenue=revenue,
                )
            )
        paging = data.get("paging", {})
        next_cursor = paging.get("cursors", {}).get("after") if paging.get("next") else None
        return {
            "items": items,
            "next_cursor": next_cursor,
            "complete": not bool(next_cursor),
            "attribution": "Meta purchase action only; account attribution window, reported by conversion time; missing actions are unknown",
        }

    def preview(self, d):
        return {
            "campaign": {
                "name": d.name,
                "objective": "OUTCOME_TRAFFIC",
                "status": "PAUSED",
                "buying_type": "AUCTION",
                "special_ad_categories": [],
                "daily_budget": to_units(d.daily_budget, self.account.minor_units),
                "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
            },
            "adset": {
                "name": d.name + " audience",
                "status": "ACTIVE",
                "billing_event": "IMPRESSIONS",
                "optimization_goal": "LINK_CLICKS",
                "destination_type": "WEBSITE",
                "targeting": {
                    "targeting_automation": {"advantage_audience": 0},
                    "geo_locations": {"countries": d.countries},
                    "age_min": d.age_min,
                    "age_max": d.age_max,
                },
            },
            "creative": {
                "name": d.name + " creative",
                "object_story_spec": {
                    "page_id": d.page_id,
                    "link_data": {
                        "link": d.final_url,
                        "message": d.primary_text,
                        "name": d.headlines[0],
                        "image_hash": d.image_hash,
                        "call_to_action": {"type": "LEARN_MORE", "value": {"link": d.final_url}},
                    },
                },
            },
            "ad": {"name": d.name + " ad", "status": "ACTIVE"},
        }

    async def create(self, draft, receipt):
        if draft.kind != "meta_traffic":
            raise AdsError("Use meta_traffic for Meta accounts")
        p = self.preview(draft)
        path = "act_" + self.account.account_id + "/"
        campaign = await self.post(path + "campaigns", p["campaign"])
        await receipt("meta_campaign", campaign)
        adset = await self.post(path + "adsets", {**p["adset"], "campaign_id": campaign["id"]})
        await receipt("meta_adset", adset)
        creative = await self.post(path + "adcreatives", p["creative"])
        await receipt("meta_creative", creative)
        ad = await self.post(
            path + "ads",
            {**p["ad"], "adset_id": adset["id"], "creative": {"creative_id": creative["id"]}},
        )
        await receipt("meta_ad", ad)
        result = await self.get_campaign(campaign["id"])
        if result["status"] != "PAUSED":
            raise ProviderError("Created Meta campaign failed PAUSED verification", uncertain=True)
        return {
            "campaign": result,
            "adset_id": adset["id"],
            "creative_id": creative["id"],
            "ad_id": ad["id"],
            "launch_note": "Campaign PAUSED; ad set and ad ACTIVE behind paused parent. A separately approved campaign resume can launch the bundle.",
        }

    def change_payload(self, draft, baseline):
        if draft.action == "set_budget":
            if baseline.get("daily_budget") is None:
                raise AdsError(
                    "This campaign uses ad-set/lifetime budgets; campaign budget changes unsupported",
                    "unsupported",
                    422,
                )
            body = {"daily_budget": to_units(draft.daily_budget, self.account.minor_units)}
        else:
            body = {"status": "PAUSED" if draft.action == "pause" else "ACTIVE"}
        return {"method": "POST", "url": self.root + "/" + draft.campaign_id, "body": body}

    async def change(self, draft, baseline, receipt):
        plan = self.change_payload(draft, baseline)
        data = await self.request(
            plan["method"], plan["url"], headers=self.headers(), body=plan["body"]
        )
        await receipt("meta_change", data)
        return {"campaign_id": draft.campaign_id, "action": draft.action}
