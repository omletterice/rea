import json

import httpx
import pytest

from internal_ads.models import Account, ChangeDraft, DateRange
from internal_ads.providers_base import AdsError, ProviderError
from internal_ads.providers_extra import AmazonProvider, LinkedInProvider, TikTokProvider


def account(platform):
    return Account(
        alias="test-" + platform,
        platform=platform,
        account_id="123",
        currency="USD",
        daily_budget_limit="1000",
        credentials_prefix=platform.upper(),
    )


@pytest.mark.asyncio
async def test_tiktok_pagination_ownership_and_budget_units():
    calls, receipts = [], []

    def handle(request):
        calls.append(request)
        if request.url.path.endswith("/campaign/get/"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "list": [
                            {
                                "campaign_id": "45",
                                "advertiser_id": "123",
                                "campaign_name": "Example",
                                "operation_status": "DISABLE",
                                "budget_mode": "BUDGET_MODE_DAY",
                                "budget": "21.25",
                            }
                        ],
                        "page_info": {"total_page": 2},
                    },
                },
            )
        return httpx.Response(200, json={"code": 0, "request_id": "receipt-1", "data": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = TikTokProvider(account("tiktok"), {"ACCESS_TOKEN": "secret"}, client)
        page = await provider.campaigns(10)
        assert page["items"][0]["daily_budget"] == "21.25"
        assert page["items"][0]["status"] == "PAUSED"
        assert page["next_cursor"] == "2" and not page["complete"]

        async def receipt(name, data):
            receipts.append((name, data))

        await provider.change(
            ChangeDraft(
                account="test-tiktok", campaign_id="45", action="set_budget", daily_budget="22.50"
            ),
            page["items"][0],
            receipt,
        )
        assert json.loads(calls[-1].content) == {
            "advertiser_id": "123",
            "campaign_id": "45",
            "budget": 22.5,
        }
        assert receipts[0][1]["request_id"] == "receipt-1"
        with pytest.raises(AdsError, match="another advertiser"):
            provider.normalize({"campaign_id": "45", "advertiser_id": "999"})


@pytest.mark.asyncio
async def test_tiktok_unknown_revenue_and_missing_conversion_are_not_zero():
    def handle(request):
        if request.url.path.endswith("/advertiser/info/"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"list": [{"advertiser_id": "123", "currency": "USD"}]}},
            )
        assert request.url.params["data_level"] == "AUCTION_CAMPAIGN"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "list": [
                        {
                            "dimensions": {"campaign_id": "45"},
                            "metrics": {"spend": "10", "impressions": "100", "clicks": "5"},
                        }
                    ],
                    "page_info": {"total_page": 1},
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = TikTokProvider(account("tiktok"), {"ACCESS_TOKEN": "secret"}, client)
        page = await provider.performance(DateRange(start="2026-01-01", end="2026-01-02"))
        assert page["items"][0]["conversions"] is None
        assert page["items"][0]["revenue"] is None and page["items"][0]["roas"] is None
        assert page["complete"]


@pytest.mark.asyncio
async def test_linkedin_cursor_and_partial_update(monkeypatch):
    monkeypatch.setenv("ADS_LINKEDIN_VERSION", "202610")
    calls, receipts = [], []

    def handle(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(204)
        return httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "id": 45,
                        "account": "urn:li:sponsoredAccount:123",
                        "name": "Example",
                        "status": "ACTIVE",
                        "dailyBudget": {"amount": "20.25", "currencyCode": "USD"},
                    }
                ],
                "metadata": {"nextPageToken": "token-next"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = LinkedInProvider(account("linkedin"), {"ACCESS_TOKEN": "secret"}, client)
        page = await provider.campaigns(10, "token-start")
        assert calls[0].url.params["pageToken"] == "token-start"
        assert calls[0].headers["LinkedIn-Version"] == "202610"
        assert page["next_cursor"] == "token-next" and not page["complete"]

        async def receipt(name, data):
            receipts.append(data)

        await provider.change(
            ChangeDraft(account="test-linkedin", campaign_id="45", action="pause"),
            page["items"][0],
            receipt,
        )
        assert calls[-1].url.path == "/rest/adAccounts/123/adCampaigns/45"
        assert calls[-1].headers["X-RestLi-Method"] == "PARTIAL_UPDATE"
        assert json.loads(calls[-1].content) == {"patch": {"$set": {"status": "PAUSED"}}}
        assert receipts[0]["campaign_id"] == "45"
        with pytest.raises(AdsError, match="another account"):
            provider.normalize({"id": 45, "account": "urn:li:sponsoredAccount:999"})


@pytest.mark.asyncio
async def test_linkedin_analytics_is_bound_to_inventory_page():
    def handle(request):
        if request.url.path == "/rest/adAccounts/123":
            return httpx.Response(200, json={"id": 123, "currency": "USD"})
        if request.url.path.endswith("/adCampaigns"):
            return httpx.Response(
                200,
                json={
                    "elements": [
                        {"id": 45, "account": "urn:li:sponsoredAccount:123", "status": "PAUSED"}
                    ],
                    "metadata": {"nextPageToken": "next"},
                },
            )
        assert request.url.params["campaigns"] == "List(urn:li:sponsoredCampaign:45)"
        return httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "pivotValues": ["urn:li:sponsoredCampaign:45"],
                        "costInLocalCurrency": "25",
                        "impressions": 500,
                        "clicks": 10,
                        "externalWebsiteConversions": 2,
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = LinkedInProvider(account("linkedin"), {"ACCESS_TOKEN": "secret"}, client)
        page = await provider.performance(DateRange(start="2026-01-01", end="2026-01-02"))
        assert page["items"][0]["cpa"] == "12.5"
        assert page["items"][0]["revenue"] is None
        assert page["next_cursor"] == "next" and not page["complete"]
        assert page["campaign_ids_examined"] == ["45"]


@pytest.mark.asyncio
async def test_amazon_scope_region_pagination_and_multi_status_failure():
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path == "/v2/profiles":
            return httpx.Response(200, json=[{"profileId": 123, "currencyCode": "USD"}])
        if request.method == "PUT":
            return httpx.Response(207, json={"campaigns": {"success": [], "error": [{"index": 0}]}})
        return httpx.Response(
            200,
            json={
                "campaigns": [
                    {
                        "campaignId": "45",
                        "name": "Example",
                        "state": "PAUSED",
                        "budget": {"budget": 20, "budgetType": "DAILY"},
                    }
                ],
                "nextToken": "next",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        a = account("amazon").model_copy(update={"region": "eu"})
        provider = AmazonProvider(a, {"ACCESS_TOKEN": "secret", "CLIENT_ID": "client"}, client)
        assert (await provider.verify_account())["currency"] == "USD"
        page = await provider.campaigns(5, "start")
        assert calls[-1].url.host == "advertising-api-eu.amazon.com"
        assert calls[-1].headers["Amazon-Advertising-API-Scope"] == "123"
        assert json.loads(calls[-1].content) == {"maxResults": 5, "nextToken": "start"}
        assert page["next_cursor"] == "next" and not page["complete"]
        assert page["items"][0]["daily_budget"] == "20"

        async def receipt(name, data):
            pytest.fail("A failed batch item must not produce a success receipt")

        with pytest.raises(ProviderError, match="did not confirm") as exc:
            await provider.change(
                ChangeDraft(account="test-amazon", campaign_id="45", action="resume"),
                page["items"][0],
                receipt,
            )
        assert exc.value.uncertain
        with pytest.raises(AdsError, match="asynchronous Reporting"):
            await provider.performance(DateRange(start="2026-01-01", end="2026-01-02"))
        assert "performance" not in provider.capabilities


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cls,platform",
    [(TikTokProvider, "tiktok"), (LinkedInProvider, "linkedin"), (AmazonProvider, "amazon")],
)
async def test_missing_credentials_make_no_requests(cls, platform):
    def handle(request):
        pytest.fail("No network request is allowed before required credentials are configured")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = cls(account(platform), {}, client)
        with pytest.raises(AdsError, match="configure"):
            await provider.verify_account()


@pytest.mark.asyncio
async def test_foreign_account_currency_and_unknown_budget_fail_closed():
    def handle(request):
        return httpx.Response(
            200, json={"code": 0, "data": {"list": [{"advertiser_id": "123", "currency": "INR"}]}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = TikTokProvider(account("tiktok"), {"ACCESS_TOKEN": "secret"}, client)
        with pytest.raises(AdsError, match="Configured currency"):
            await provider.verify_account()

        async def receipt(name, data):
            pytest.fail("No receipt for rejected budget-mode conversion")

        with pytest.raises(AdsError, match="no supported daily budget"):
            await provider.change(
                ChangeDraft(
                    account="test-tiktok", campaign_id="45", action="set_budget", daily_budget=25
                ),
                {"daily_budget": None},
                receipt,
            )


@pytest.mark.asyncio
async def test_amazon_budget_update_confirmed_only_for_exact_campaign():
    calls, receipts = [], []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            207, json={"campaigns": {"success": [{"campaignId": "45", "index": 0}], "error": []}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = AmazonProvider(
            account("amazon"), {"ACCESS_TOKEN": "secret", "CLIENT_ID": "client"}, client
        )

        async def receipt(name, data):
            receipts.append(data)

        result = await provider.change(
            ChangeDraft(
                account="test-amazon", campaign_id="45", action="set_budget", daily_budget="25.75"
            ),
            {"daily_budget": "20"},
            receipt,
        )
        assert json.loads(calls[0].content) == {
            "campaigns": [{"campaignId": "45", "budget": {"budget": 25.75, "budgetType": "DAILY"}}]
        }
        assert result["campaign_id"] == "45" and receipts[0]["campaign_id"] == "45"


@pytest.mark.asyncio
async def test_tiktok_account_scoped_lookup_rejects_unmatched_id():
    def handle(request):
        assert json.loads(request.url.params["filtering"]) == {"campaign_ids": ["45"]}
        return httpx.Response(200, json={"code": 0, "data": {"list": [{"campaign_id": "99"}]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = TikTokProvider(account("tiktok"), {"ACCESS_TOKEN": "secret"}, client)
        with pytest.raises(AdsError, match="exact configured"):
            await provider.get_campaign("45")
        with pytest.raises(AdsError, match="numeric campaign"):
            await provider.get_campaign("../99")


@pytest.mark.asyncio
async def test_linkedin_version_override_matches_shared_binding(monkeypatch):
    monkeypatch.setenv("ADS_LINKEDIN_VERSION", "202607")

    def handle(request):
        assert request.headers["LinkedIn-Version"] == "202607"
        return httpx.Response(200, json={"id": 123, "currency": "USD"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = LinkedInProvider(account("linkedin"), {"ACCESS_TOKEN": "secret"}, client)
        await provider.verify_account()
        monkeypatch.setenv("ADS_LINKEDIN_VERSION", "v26")
        with pytest.raises(AdsError, match="YYYYMM"):
            await provider.verify_account()
