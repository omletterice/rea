import json

import httpx
import pytest
from pydantic import ValidationError

from internal_ads.models import Account, CampaignDraft, ChangeDraft, DateRange
from internal_ads.providers_base import AdsError, ProviderError, to_units
from internal_ads.providers_core import Google, Meta


def account(platform):
    return Account(
        alias="test-" + platform,
        platform=platform,
        account_id="1234567890",
        currency="INR",
        daily_budget_limit="1000",
        credentials_prefix="TEST",
    )


def search(**extra):
    return CampaignDraft(
        account="test-google",
        kind="google_search",
        name="Bindee search",
        daily_budget="200",
        final_url="https://example.com",
        keywords=["colourful bangles"],
        headlines=["Your next everyday favourite", "Bangles with character", "Meet Bindee"],
        descriptions=[
            "A little colour for your everyday life.",
            "Shop your next favourite pair of bangles.",
        ],
        location_ids=["2356"],
        language_ids=["1000"],
        cpc_bid="5",
        **extra,
    )


def meta(**extra):
    return CampaignDraft(
        account="test-meta",
        kind="meta_traffic",
        name="Bindee traffic",
        daily_budget="200",
        final_url="https://example.com",
        headlines=["Bangles for everyday life"],
        page_id="1234",
        image_hash="a" * 32,
        primary_text="Wear your colour.",
        countries=["IN"],
        **extra,
    )


def test_money_and_models():
    assert to_units("12.34", 2) == 1234 and to_units("12", 0) == 12
    with pytest.raises(AdsError):
        to_units("12.345", 2)
    with pytest.raises(ValidationError):
        meta(keywords=["ignored"])
    with pytest.raises(ValidationError):
        search(countries=["IN"])
    d = search().model_dump()
    d["headlines"] = ["Same"] * 3
    with pytest.raises(ValidationError):
        CampaignDraft.model_validate(d)
    with pytest.raises(ValidationError):
        DateRange(start="2026-11-09 OR 1=1", end="2026-11-09")
    with pytest.raises(ValidationError):
        ChangeDraft(account="x", campaign_id="1/2", action="pause")


def test_google_atomic_payload():
    p = Google(account("google"), {}, None)
    body = p.create_payload(search())
    ops = body["mutateOperations"]
    assert body["partialFailure"] is False
    assert ops[0]["campaignBudgetOperation"]["create"]["amountMicros"] == "200000000"
    c = ops[1]["campaignOperation"]["create"]
    assert c["status"] == "PAUSED" and c["campaignBudget"].endswith("/-1")
    assert c["networkSettings"]["targetContentNetwork"] is False
    assert ops[-1]["adGroupAdOperation"]["create"]["status"] == "ENABLED"
    assert any(
        o.get("campaignCriterionOperation", {}).get("create", {}).get("location")
        == {"geoTargetConstant": "geoTargetConstants/2356"}
        for o in ops
    )


async def test_google_validate_create_and_verify():
    calls = []

    async def handler(r):
        body = json.loads(r.content)
        calls.append((r, body))
        if r.url.path.endswith("/googleAds:mutate"):
            if body.get("validateOnly"):
                return httpx.Response(200, json={})
            return httpx.Response(
                200,
                json={
                    "mutateOperationResponses": [
                        {"campaignResult": {"resourceName": "customers/1234567890/campaigns/111"}}
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "campaign": {"id": "111", "name": "Bindee search", "status": "PAUSED"},
                        "campaignBudget": {"amountMicros": "200000000"},
                        "customer": {"currencyCode": "INR"},
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        p = Google(account("google"), {"ACCESS_TOKEN": "secret", "DEVELOPER_TOKEN": "dev"}, client)
        receipts = []

        async def receipt(step, body):
            receipts.append((step, body))

        await p.validate_create(search())
        result = await p.create(search(), receipt)
        assert result["campaign"]["status"] == "PAUSED" and len(receipts) == 1
        assert calls[0][1]["validateOnly"] is True and not calls[1][1].get("validateOnly")
        assert calls[1][0].headers["developer-token"] == "dev"


def test_meta_preview_can_launch_via_parent_resume():
    p = Meta(account("meta"), {}, None)
    body = p.preview(meta())
    assert body["campaign"]["status"] == "PAUSED"
    assert body["adset"]["status"] == "ACTIVE" and body["ad"]["status"] == "ACTIVE"
    assert body["campaign"]["daily_budget"] == 20000 and "daily_budget" not in body["adset"]
    assert body["adset"]["targeting"]["targeting_automation"]["advantage_audience"] == 0


async def test_meta_partial_creation_keeps_receipts_and_redacts_error():
    calls = []

    async def handler(r):
        calls.append(r)
        if r.url.path.endswith("/campaigns"):
            return httpx.Response(200, json={"id": "12"})
        if r.url.path.endswith("/adsets"):
            return httpx.Response(400, json={"error": {"message": "sensitive secret"}})
        raise AssertionError("Must stop after rejected ad set")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        p = Meta(account("meta"), {"ACCESS_TOKEN": "private"}, client)
        receipts = []

        async def receipt(step, data):
            receipts.append((step, data))

        with pytest.raises(ProviderError) as e:
            await p.create(meta(), receipt)
        assert "sensitive" not in str(e.value) and "private" not in str(e.value)
        assert receipts == [("meta_campaign", {"id": "12"})] and len(calls) == 2


async def test_meta_account_scope_and_no_credentials():
    async def handler(r):
        return httpx.Response(200, json={"account_id": "foreign", "currency": "INR"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        p = Meta(account("meta"), {"ACCESS_TOKEN": "x"}, client)
        with pytest.raises(AdsError, match="ID/currency"):
            await p.verify_account()
    p = Meta(account("meta"), {}, None)
    with pytest.raises(AdsError, match="not in this account"):
        p.campaign({"id": "1", "account_id": "2", "name": "n", "status": "PAUSED"})
    with pytest.raises(AdsError, match="configure"):
        await p.get_campaign("1")


async def test_google_no_page_truncation():
    async def handler(r):
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "campaign": {"id": str(i), "name": "campaign", "status": "PAUSED"},
                        "campaignBudget": {},
                    }
                    for i in range(3)
                ],
                "nextPageToken": "abc",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        p = Google(account("google"), {"ACCESS_TOKEN": "x", "DEVELOPER_TOKEN": "x"}, client)
        result = await p.campaigns(limit=1)
        assert (
            len(result["items"]) == 3
            and result["next_cursor"] == "abc"
            and result["complete"] is False
        )


async def test_provider_network_write_is_uncertain():
    async def handler(r):
        raise httpx.ReadTimeout("secret url and token", request=r)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        p = Meta(account("meta"), {"ACCESS_TOKEN": "x"}, client)
        with pytest.raises(ProviderError) as e:
            await p.change(
                ChangeDraft(account="test-meta", campaign_id="1", action="pause"),
                {},
                lambda *a: None,
            )
        assert e.value.uncertain and "secret" not in str(e.value)
