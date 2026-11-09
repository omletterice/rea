"""Exercise actual Google/Meta adapters through the shared approval service.
All native replies are mocked; this verifies wiring, not live API acceptance.
"""

import json

import httpx
import pytest
from cryptography.fernet import Fernet
from test_core import account, meta, search

from internal_ads.config import Settings
from internal_ads.models import ChangeDraft
from internal_ads.providers_base import AdsError
from internal_ads.service import Service


def settings(tmp_path, platform):
    return Settings(
        api_key="a" * 40,
        admin_key="b" * 40,
        encryption_key=Fernet.generate_key().decode(),
        database=str(tmp_path / "live.sqlite3"),
        accounts=[account(platform)],
        enable_writes=True,
    )


@pytest.mark.parametrize("platform", ["google", "meta"])
async def test_native_create_then_approved_resume(tmp_path, monkeypatch, platform):
    monkeypatch.setenv("TEST_ACCESS_TOKEN", "fake-access")
    monkeypatch.setenv("TEST_DEVELOPER_TOKEN", "fake-dev")
    state = {"id": "111", "status": "PAUSED"}
    writes = []

    async def handler(r):
        path = r.url.path
        body = json.loads(r.content) if r.content else {}
        if platform == "google":
            if path.endswith("/googleAds:search"):
                if "FROM customer" in body["query"]:
                    return httpx.Response(
                        200,
                        json={
                            "results": [{"customer": {"id": "1234567890", "currencyCode": "INR"}}]
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "campaign": {
                                    "id": state["id"],
                                    "name": "Bindee search",
                                    "status": state["status"],
                                },
                                "campaignBudget": {
                                    "resourceName": "customers/1234567890/campaignBudgets/222",
                                    "amountMicros": "200000000",
                                    "explicitlyShared": False,
                                },
                                "customer": {"currencyCode": "INR"},
                            }
                        ]
                    },
                )
            if path.endswith("/googleAds:mutate"):
                if body.get("validateOnly"):
                    return httpx.Response(200, json={})
                writes.append(body)
                return httpx.Response(
                    200,
                    json={
                        "mutateOperationResponses": [
                            {
                                "campaignResult": {
                                    "resourceName": "customers/1234567890/campaigns/111"
                                }
                            }
                        ]
                    },
                )
            if path.endswith("/campaigns:mutate"):
                writes.append(body)
                state["status"] = body["operations"][0]["update"]["status"]
                return httpx.Response(
                    200, json={"results": [{"resourceName": "customers/1234567890/campaigns/111"}]}
                )
        else:
            if path.endswith("/act_1234567890"):
                return httpx.Response(200, json={"account_id": "1234567890", "currency": "INR"})
            if r.method == "POST":
                writes.append(body)
                if path.endswith("/111"):
                    state["status"] = body["status"]
                    return httpx.Response(200, json={"success": True})
                return httpx.Response(
                    200, json={"id": "111" if path.endswith("/campaigns") else "333"}
                )
            if path.endswith("/111"):
                return httpx.Response(
                    200,
                    json={
                        "id": "111",
                        "account_id": "1234567890",
                        "name": "Bindee traffic",
                        "status": state["status"],
                        "daily_budget": "20000",
                    },
                )
        raise AssertionError(f"Unexpected provider call {r.method} {path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = Service(settings(tmp_path, platform), client)
        draft = search() if platform == "google" else meta()
        op = await service.prepare_campaign(draft, "native-create-001")
        assert not writes
        assert op["plan"]["provider_payload"]
        service.store.approve(op["id"], op["digest"])
        result = await service.execute(op["id"])
        assert result["state"] == "succeeded" and result["result"]["campaign"]["status"] == "PAUSED"
        expected_writes = 1 if platform == "google" else 4
        assert len(writes) == expected_writes
        assert (await service.execute(op["id"]))["state"] == "succeeded" and len(
            writes
        ) == expected_writes
        change = await service.prepare_change(
            ChangeDraft(account=draft.account, campaign_id="111", action="resume"),
            "native-resume-001",
        )
        assert change["plan"]["provider_payload"]["body"]
        service.store.approve(change["id"], change["digest"])
        result = await service.execute(change["id"])
        assert result["state"] == "succeeded"
        assert result["result"]["campaign"]["status"] in ("ENABLED", "ACTIVE")
        assert len(writes) == expected_writes + 1


async def test_inventory_does_not_try_refresh_on_misconfigured_account(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_REFRESH_TOKEN", "will-not-be-sent")

    def no_calls(r):
        raise AssertionError("Inventory must not refresh or call provider")

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_calls)) as client:
        service = Service(settings(tmp_path, "google"), client)
        result = await service.connections()
        assert (
            result["accounts"][0]["credentials_configured"]
            and not result["accounts"][0]["verified"]
        )
        with pytest.raises(AdsError, match="CLIENT_ID"):
            await service.verify("test-google")
