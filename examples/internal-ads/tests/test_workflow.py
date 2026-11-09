"""Public HTTP/MCP workflows and durable approval/OAuth boundaries.

Demo assertions prove orchestration and transport behavior only. They do not
establish live ad-platform permissions, acceptance, delivery or attribution.
"""

import asyncio
import base64
import hashlib
import json
import time
from datetime import timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import anyio
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from internal_ads.auth import token_hash
from internal_ads.config import Settings
from internal_ads.models import Account, CampaignDraft
from internal_ads.providers_base import AdsError, ProviderError
from internal_ads.server import build_app, build_mcp
from internal_ads.service import Service


@pytest.fixture
def settings(tmp_path):
    return Settings(
        api_key="api-" + "a" * 40,
        admin_key="operator-" + "b" * 40,
        encryption_key=Fernet.generate_key().decode(),
        database=str(tmp_path / "ads.sqlite3"),
        demo=True,
        enable_writes=True,
        accounts=[
            Account(
                alias="bindee-meta",
                platform="meta",
                account_id="123456",
                currency="INR",
                daily_budget_limit="1000",
                credentials_prefix="BINDEE_META",
            ),
            Account(
                alias="labs-search",
                platform="google",
                account_id="1234567890",
                currency="USD",
                daily_budget_limit="1000",
                credentials_prefix="LABS_GOOGLE",
            ),
        ],
    )


def isolated_service(settings):
    def no_network(request):
        pytest.fail("Demo workflow must not make a platform HTTP request")

    return Service(settings, httpx.AsyncClient(transport=httpx.MockTransport(no_network)))


@pytest.fixture
def service(settings):
    return isolated_service(settings)


@pytest.fixture
def web(service):
    app = build_app(service=service)
    with TestClient(app) as client:
        yield client


def api(settings):
    return {"Authorization": f"Bearer {settings.api_key}"}


def admin(settings):
    return {"Authorization": f"Bearer {settings.admin_key}"}


def meta_draft(**changes):
    return {
        "account": "bindee-meta",
        "kind": "meta_traffic",
        "name": "Bindee test",
        "daily_budget": "250.00",
        "final_url": "https://bindee.example/shop",
        "page_id": "1234",
        "image_hash": "a" * 32,
        "primary_text": "Made for your everyday.",
        "headlines": ["Your everyday, Bindee"],
        "countries": ["IN"],
        **changes,
    }


def prepare(web, settings, key="create-workflow-001", **changes):
    response = web.post(
        "/api/v1/campaigns/prepare",
        headers=api(settings),
        json={"draft": meta_draft(**changes), "idempotency_key": key},
    )
    assert response.status_code == 200, response.text
    return response.json()


def approve(web, settings, op):
    response = web.post(
        f"/admin/operations/{op['id']}/approve",
        headers=admin(settings),
        json={"expected_digest": op["digest"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_http_auth_and_account_boundary(web, settings):
    assert web.get("/healthz").json() == {"status": "ok", "mode": "demo"}
    for path in ("/api/v1/connections", "/api/v1/operations", "/docs", "/openapi.json"):
        response = web.get(path)
        assert response.status_code == 401
        assert "resource_metadata=" in response.headers["www-authenticate"]
    assert (
        web.get("/api/v1/connections", headers={"Authorization": "Bearer wrong"}).status_code == 401
    )
    result = web.get("/api/v1/connections", headers=api(settings))
    assert result.status_code == 200
    assert result.json()["mode"] == "demo"
    assert {item["account"] for item in result.json()["accounts"]} == {"bindee-meta", "labs-search"}
    assert settings.api_key not in result.text and settings.admin_key not in result.text
    assert web.post("/api/v1/accounts/unknown/verify", headers=api(settings)).status_code == 404
    assert (
        web.get(
            "/api/v1/connections", headers={**api(settings), "Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert web.get("/healthz", headers={"Host": "evil.example"}).status_code == 400


def test_http_draft_approval_execute_once_and_audit(web, settings):
    op = prepare(web, settings)
    assert op["state"] == "pending"
    assert "paused" in op["plan"]["effect"].lower()
    assert (
        web.post(f"/api/v1/operations/{op['id']}/execute", headers=api(settings)).status_code == 409
    )
    response = web.post(
        f"/admin/operations/{op['id']}/approve",
        headers=api(settings),
        json={"expected_digest": op["digest"]},
    )
    assert response.status_code == 403 and response.json()["error"] == "operator_required"
    mismatch = web.post(
        f"/admin/operations/{op['id']}/approve",
        headers=admin(settings),
        json={"expected_digest": "0" * 64},
    )
    assert mismatch.status_code == 409
    assert approve(web, settings, op)["state"] == "approved"
    executed = web.post(f"/api/v1/operations/{op['id']}/execute", headers=api(settings))
    assert executed.status_code == 200
    result = executed.json()
    assert result["state"] == "succeeded"
    assert result["result"]["campaign"]["status"] == "PAUSED"
    assert result["result"]["simulated"] is True
    assert len(result["receipts"]) == 1
    replay = web.post(f"/api/v1/operations/{op['id']}/execute", headers=api(settings)).json()
    assert replay == result
    campaigns = web.get("/api/v1/accounts/bindee-meta/campaigns", headers=api(settings)).json()
    assert len(campaigns["items"]) == 2
    events = web.get("/api/v1/audit", headers=api(settings)).json()["items"]
    assert [x["event"] for x in events if x["subject"] == op["id"]] == [
        "prepared",
        "operator_approved",
        "execution_started",
        "provider_receipt",
        "succeeded",
    ]
    assert all(settings.admin_key not in json.dumps(event) for event in events)


def test_idempotency_keeps_original_preview_and_rejects_reused_intent_key(web, settings):
    first = prepare(web, settings, key="same-intent-001")
    duplicate = prepare(web, settings, key="same-intent-001")
    assert duplicate["id"] == first["id"] and duplicate["digest"] == first["digest"]
    response = web.post(
        "/api/v1/campaigns/prepare",
        headers=api(settings),
        json={
            "draft": meta_draft(name="Different intention"),
            "idempotency_key": "same-intent-001",
        },
    )
    assert response.status_code == 409 and response.json()["error"] == "conflict"


@pytest.mark.parametrize(
    "budget,status,error",
    [("1000.01", 422, "budget_limit"), ("0", 422, None), ("100.001", 400, "invalid_request")],
)
def test_invalid_or_excess_budget_never_prepares(web, settings, budget, status, error):
    response = web.post(
        "/api/v1/campaigns/prepare",
        headers=api(settings),
        json={"draft": meta_draft(daily_budget=budget), "idempotency_key": "invalid-budget-001"},
    )
    assert response.status_code == status, response.text
    if error:
        assert response.json()["error"] == error
    assert web.get("/api/v1/operations", headers=api(settings)).json()["items"] == []


def test_approval_expiry_cancellation_and_writes_disabled(web, settings, service):
    op = prepare(web, settings)
    with service.store.db() as db:
        db.execute("UPDATE operations SET expires=? WHERE id=?", (time.time() - 1, op["id"]))
    response = web.post(
        f"/admin/operations/{op['id']}/approve",
        headers=admin(settings),
        json={"expected_digest": op["digest"]},
    )
    assert response.status_code == 409
    op = prepare(web, settings, key="approved-expiry-001")
    approve(web, settings, op)
    with service.store.db() as db:
        db.execute("UPDATE operations SET expires=? WHERE id=?", (time.time() - 1, op["id"]))
    assert (
        web.post(f"/api/v1/operations/{op['id']}/execute", headers=api(settings)).status_code == 409
    )
    op = prepare(web, settings, key="cancelled-intent-001")
    assert (
        web.post(f"/admin/operations/{op['id']}/cancel", headers=admin(settings)).json()["state"]
        == "cancelled"
    )
    assert (
        web.post(f"/api/v1/operations/{op['id']}/execute", headers=api(settings)).status_code == 409
    )
    op = prepare(web, settings, key="write-disabled-001")
    approve(web, settings, op)
    settings.enable_writes = False
    response = web.post(f"/api/v1/operations/{op['id']}/execute", headers=api(settings))
    assert response.status_code == 409 and response.json()["error"] == "writes_disabled"
    assert service.store.get(op["id"])["state"] == "approved"


def test_plan_tampering_and_policy_changes_invalidate_approval(web, settings, service):
    op = prepare(web, settings)
    with service.store.db() as db:
        tampered = op["plan"]
        tampered["request"]["draft"]["name"] = "Tampered after review"
        db.execute("UPDATE operations SET plan=? WHERE id=?", (json.dumps(tampered), op["id"]))
    response = web.post(
        f"/admin/operations/{op['id']}/approve",
        headers=admin(settings),
        json={"expected_digest": op["digest"]},
    )
    assert response.status_code == 409
    op = prepare(web, settings, key="policy-drift-001")
    approve(web, settings, op)
    service.accounts["bindee-meta"].daily_budget_limit = Decimal("100")
    response = web.post(f"/api/v1/operations/{op['id']}/execute", headers=api(settings))
    assert response.status_code == 409 and response.json()["error"] == "stale_preview"
    assert service.store.get(op["id"])["state"] == "failed"
    assert service.store.get(op["id"])["receipts"] == []


def test_change_state_drift_then_fresh_resume_and_budget(web, settings, service):
    response = web.post(
        "/api/v1/changes/prepare",
        headers=api(settings),
        json={
            "draft": {"account": "bindee-meta", "campaign_id": "1001", "action": "resume"},
            "idempotency_key": "resume-original-001",
        },
    )
    assert response.status_code == 200
    op = response.json()
    approve(web, settings, op)
    values = service.store.brand("__demo:bindee-meta")
    values["1001"]["name"] = "Changed outside our workflow"
    service.store.brand("__demo:bindee-meta", values)
    response = web.post(f"/api/v1/operations/{op['id']}/execute", headers=api(settings))
    assert response.status_code == 409 and response.json()["error"] == "stale_preview"
    assert service.store.get(op["id"])["state"] == "failed"
    assert service.store.brand("__demo:bindee-meta")["1001"]["status"] == "PAUSED"
    for action, budget in [("resume", None), ("set_budget", "200.25"), ("pause", None)]:
        draft = {"account": "bindee-meta", "campaign_id": "1001", "action": action}
        if budget is not None:
            draft["daily_budget"] = budget
        response = web.post(
            "/api/v1/changes/prepare",
            headers=api(settings),
            json={"draft": draft, "idempotency_key": f"fresh-change-{action}"},
        )
        assert response.status_code == 200, response.text
        op = response.json()
        approve(web, settings, op)
        response = web.post(f"/api/v1/operations/{op['id']}/execute", headers=api(settings))
        assert response.status_code == 200 and response.json()["state"] == "succeeded", (
            response.text
        )
    campaign = service.store.brand("__demo:bindee-meta")["1001"]
    assert campaign["status"] == "PAUSED" and campaign["daily_budget"] == "200.25"


@pytest.mark.asyncio
async def test_mid_write_failure_is_ambiguous_and_never_auto_retried(service, monkeypatch):
    op = await service.prepare_campaign(
        CampaignDraft.model_validate(meta_draft()), "ambiguous-write-001"
    )
    service.store.approve(op["id"], op["digest"])
    provider = await service.provider("bindee-meta")
    calls = 0

    async def crash(draft, receipt):
        nonlocal calls
        calls += 1
        await receipt("native_campaign_created", {"id": "55"})
        raise ProviderError("Transport interrupted after campaign creation", uncertain=True)

    async def injected_provider(alias):
        return provider

    monkeypatch.setattr(provider, "create", crash)
    monkeypatch.setattr(service, "provider", injected_provider)
    with pytest.raises(ProviderError):
        await service.execute(op["id"])
    failed = service.store.get(op["id"])
    assert failed["state"] == "needs_reconciliation" and failed["receipts"][0]["body"]["id"] == "55"
    with pytest.raises(AdsError) as exc:
        await service.execute(op["id"])
    assert exc.value.code == "approval_required" and calls == 1
    assert not service.active_accounts
    await service.client.aclose()


@pytest.mark.asyncio
async def test_concurrent_execution_does_not_duplicate_provider_write(service, monkeypatch):
    op = await service.prepare_campaign(
        CampaignDraft.model_validate(meta_draft()), "concurrent-write-001"
    )
    service.store.approve(op["id"], op["digest"])
    provider = await service.provider("bindee-meta")
    real_create = provider.create
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def delayed_create(draft, receipt):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return await real_create(draft, receipt)

    async def injected_provider(alias):
        return provider

    monkeypatch.setattr(provider, "create", delayed_create)
    monkeypatch.setattr(service, "provider", injected_provider)
    first = asyncio.create_task(service.execute(op["id"]))
    await asyncio.wait_for(entered.wait(), 2)
    with pytest.raises(AdsError) as exc:
        await service.execute(op["id"])
    assert exc.value.code == "conflict"
    release.set()
    assert (await first)["state"] == "succeeded" and calls == 1
    await service.client.aclose()


def test_restart_recovery_preserves_receipts_and_requires_reconciliation(settings):
    previous = isolated_service(settings)
    with TestClient(build_app(service=previous)) as old_web:
        op = prepare(old_web, settings)
        approve(old_web, settings, op)
        previous.store.claim(op["id"])
        previous.store.receipt(op["id"], "created_before_process_exit", {"campaign_id": "1002"})
    # Only a stopped owner may be replaced. Recovery must not run in parallel
    # with an existing server's potentially in-flight provider write.
    recovered = isolated_service(settings)
    with TestClient(build_app(service=recovered)) as restarted:
        status = restarted.get(f"/api/v1/operations/{op['id']}", headers=api(settings)).json()
        assert status["state"] == "needs_reconciliation"
        assert status["receipts"][0]["body"]["campaign_id"] == "1002"
        assert (
            restarted.post(
                f"/api/v1/operations/{op['id']}/execute", headers=api(settings)
            ).status_code
            == 409
        )
        assert (
            restarted.post(
                f"/admin/operations/{op['id']}/reconcile",
                headers=api(settings),
                json={"note": "Confirmed platform state with the operator."},
            ).status_code
            == 403
        )
        response = restarted.post(
            f"/admin/operations/{op['id']}/reconcile",
            headers=admin(settings),
            json={"note": "Confirmed platform state and recorded replacement decision."},
        )
        assert response.status_code == 200 and response.json()["state"] == "reconciled"
        assert (
            restarted.post(
                f"/api/v1/operations/{op['id']}/execute", headers=api(settings)
            ).status_code
            == 409
        )


def test_second_server_cannot_recover_the_active_owners_operation(settings):
    owner = isolated_service(settings)
    with TestClient(build_app(service=owner)) as web:
        op = prepare(web, settings)
        approve(web, settings, op)
        owner.store.claim(op["id"])
        contender = isolated_service(settings)
        with pytest.raises(AdsError) as exc:
            with TestClient(build_app(service=contender)):
                pytest.fail("A second server must not start on an owned database")
        assert exc.value.code == "database_in_use"
        assert owner.store.get(op["id"])["state"] == "executing"
        assert contender.client.is_closed
    # Lifespan cleanup releases the lease even with an unfinished operation.
    with contender.store.process_lease():
        contender.store.recover()
    assert contender.store.get(op["id"])["state"] == "needs_reconciliation"


def register_client(web, uri="http://127.0.0.1:4567/callback"):
    response = web.post(
        "/oauth/register", json={"redirect_uris": [uri], "client_name": "Workflow test"}
    )
    assert response.status_code == 201, response.text
    return response.json()["client_id"], uri


def authorization_code(web, settings, client_id, redirect_uri, verifier="v" * 64):
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    args = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": "keep-this-state",
        "resource": settings.public_url + "/mcp",
        "scope": "ads",
    }
    page = web.get("/oauth/authorize", params=args)
    assert page.status_code == 200
    assert settings.admin_key not in page.text
    response = web.post(
        "/oauth/authorize", data={**args, "admin_key": settings.admin_key}, follow_redirects=False
    )
    assert response.status_code == 303
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["state"] == ["keep-this-state"]
    return query["code"][0], verifier, args


def test_oauth_pkce_token_one_use_refresh_rotation_and_no_approval(web, settings):
    metadata = web.get("/.well-known/oauth-authorization-server").json()
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    protected = web.get("/.well-known/oauth-protected-resource/mcp").json()
    assert protected["resource"] == settings.public_url + "/mcp"
    client_id, uri = register_client(web)
    code, verifier, _ = authorization_code(web, settings, client_id, uri)
    args = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "redirect_uri": uri,
        "code": code,
        "code_verifier": verifier,
    }
    response = web.post("/oauth/token", data=args)
    assert response.status_code == 200, response.text
    assert "no-store" in response.headers["cache-control"]
    tokens = response.json()
    assert tokens["token_type"] == "Bearer" and tokens["scope"] == "ads"
    assert web.post("/oauth/token", data=args).status_code == 400
    headers = {"Authorization": "Bearer " + tokens["access_token"]}
    assert web.get("/api/v1/connections", headers=headers).status_code == 200
    op = prepare(web, settings)
    assert (
        web.post(
            f"/admin/operations/{op['id']}/approve",
            headers=headers,
            json={"expected_digest": op["digest"]},
        ).status_code
        == 403
    )
    refresh_args = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": tokens["refresh_token"],
    }
    rotated = web.post("/oauth/token", data=refresh_args)
    assert rotated.status_code == 200
    assert rotated.json()["refresh_token"] != tokens["refresh_token"]
    assert rotated.json()["access_token"] != tokens["access_token"]
    assert web.post("/oauth/token", data=refresh_args).status_code == 400


def test_oauth_rejects_wrong_pkce_redirect_plain_challenge_and_operator_key(web, settings):
    assert (
        web.post(
            "/oauth/register", json={"redirect_uris": ["http://evil.example/callback"]}
        ).status_code
        == 400
    )
    assert (
        web.post(
            "/oauth/register", json={"redirect_uris": ["https://example.com/#fragment"]}
        ).status_code
        == 400
    )
    client_id, uri = register_client(web)
    code, verifier, args = authorization_code(web, settings, client_id, uri)
    assert (
        web.get(
            "/oauth/authorize", params={**args, "redirect_uri": "https://evil.example"}
        ).status_code
        == 400
    )
    assert (
        web.get("/oauth/authorize", params={**args, "code_challenge_method": "plain"}).status_code
        == 400
    )
    assert (
        web.post("/oauth/authorize", data={**args, "admin_key": settings.api_key}).status_code
        == 401
    )
    token_args = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "redirect_uri": uri,
        "code": code,
        "code_verifier": "x" * 64,
    }
    assert web.post("/oauth/token", data=token_args).status_code == 400
    # A failed exchange cannot turn a consumed code into a usable second attempt.
    assert (
        web.post("/oauth/token", data={**token_args, "code_verifier": verifier}).status_code == 400
    )
    code, verifier, _ = authorization_code(web, settings, client_id, uri)
    assert (
        web.post(
            "/oauth/token",
            data={
                **token_args,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": "https://evil.example",
            },
        ).status_code
        == 400
    )


def test_oauth_expiry_and_token_storage_contains_hashes_only(web, settings, service):
    client_id, uri = register_client(web)
    code, verifier, _ = authorization_code(web, settings, client_id, uri)
    auth = web.app.state.client_auth
    value = auth.get("code:" + token_hash(code))
    value["expires"] = time.time() - 1
    auth.put("code:" + token_hash(code), value)
    assert (
        web.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "redirect_uri": uri,
                "code": code,
                "code_verifier": verifier,
            },
        ).status_code
        == 400
    )
    tokens = auth.tokens(client_id)
    with service.store.db() as db:
        rows = [tuple(row) for row in db.execute("SELECT key,value FROM meta")]
    assert tokens["access_token"] not in json.dumps(rows) and tokens[
        "refresh_token"
    ] not in json.dumps(rows)
    access_key = "access:" + token_hash(tokens["access_token"])
    value = auth.get(access_key)
    value["expires"] = time.time() - 1
    auth.put(access_key, value)
    assert (
        web.get(
            "/api/v1/connections", headers={"Authorization": "Bearer " + tokens["access_token"]}
        ).status_code
        == 401
    )


def test_encrypted_platform_token_roundtrip_and_mode_isolation(service, settings):
    service.store.save_tokens(
        "bindee-meta", {"ACCESS_TOKEN": "never-print-this-platform-token", "EXPIRES_AT": 456}
    )
    assert service.store.tokens("bindee-meta")["ACCESS_TOKEN"] == "never-print-this-platform-token"
    with service.store.db() as db:
        raw = db.execute("SELECT body FROM secrets").fetchone()["body"]
    assert b"never-print-this-platform-token" not in raw
    settings.demo = False
    with pytest.raises(ValueError, match="separate ADS_DATABASE"):
        Service(settings)


def mcp_payload(result):
    value = result.model_dump(by_alias=True) if hasattr(result, "model_dump") else result
    return value.get("structuredContent") or json.loads(value["content"][0]["text"])


def test_mcp_streamable_http_auth_initialize_discover_call_and_no_approve(web, settings):
    headers = {
        **api(settings),
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "workflow-test", "version": "1"},
        },
    }
    assert web.post("/mcp", json=initialize).status_code == 401
    assert (
        web.post("/mcp", json=initialize, headers={**headers, **admin(settings)}).status_code == 401
    )
    response = web.post("/mcp", json=initialize, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["result"]["serverInfo"]["name"] == "Riality Ads"
    headers["MCP-Protocol-Version"] = response.json()["result"]["protocolVersion"]
    tools = web.post(
        "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    ).json()["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert {"get_connections", "prepare_campaign", "execute_operation"} <= names
    assert not any("approv" in name for name in names)
    result = web.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "get_connections", "arguments": {}},
        },
    )
    assert result.status_code == 200, result.text
    assert mcp_payload(result.json()["result"])["mode"] == "demo"
    failed = web.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "approve_operation", "arguments": {}},
        },
    )
    assert failed.json()["result"]["isError"] is True


@pytest.mark.asyncio
async def test_real_mcp_sdk_memory_handshake_schema_validation_and_shared_workflow(service):
    mcp = build_mcp(service)
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                mcp._mcp_server.run,
                *server_streams,
                mcp._mcp_server.create_initialization_options(),
            )
            async with ClientSession(
                *client_streams, read_timeout_seconds=timedelta(seconds=5)
            ) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "Riality Ads"
                tools = (await session.list_tools()).tools
                names = {tool.name for tool in tools}
                assert len(names) == len(tools) and "prepare_campaign" in names
                assert not any("approv" in name for name in names)
                malformed = await session.call_tool(
                    "prepare_campaign", {"request": {"draft": {"account": "bindee-meta"}}}
                )
                assert malformed.isError
                response = await session.call_tool(
                    "prepare_campaign",
                    {"request": {"draft": meta_draft(), "idempotency_key": "mcp-shared-draft-001"}},
                )
                assert not response.isError
                op = mcp_payload(response)
                assert op["state"] == "pending"
                assert service.store.get(op["id"])["digest"] == op["digest"]
                blocked = await session.call_tool("execute_operation", {"operation_id": op["id"]})
                assert mcp_payload(blocked)["error"] == "approval_required"
                service.store.approve(op["id"], op["digest"])
                result = await session.call_tool("execute_operation", {"operation_id": op["id"]})
                assert mcp_payload(result)["state"] == "succeeded"
                assert mcp_payload(result)["result"]["campaign"]["status"] == "PAUSED"
            tg.cancel_scope.cancel()
    await service.client.aclose()


def test_platform_oauth_google_pkce_and_single_use_callback(settings, monkeypatch):
    settings.demo = False
    monkeypatch.setenv("LABS_GOOGLE_CLIENT_ID", "google-client-id")
    monkeypatch.setenv("LABS_GOOGLE_CLIENT_SECRET", "google-client-secret")
    exchanges = []

    def exchange(request):
        assert str(request.url) == "https://oauth2.googleapis.com/token"
        body = parse_qs(request.content.decode())
        exchanges.append(body)
        assert body["grant_type"] == ["authorization_code"]
        assert body["client_secret"] == ["google-client-secret"]
        return httpx.Response(
            200,
            json={
                "access_token": "private-google-token",
                "refresh_token": "private-google-refresh",
                "expires_in": 3600,
            },
        )

    live = Service(settings, httpx.AsyncClient(transport=httpx.MockTransport(exchange)))
    with TestClient(build_app(service=live)) as web:
        assert (
            web.post("/admin/accounts/labs-search/connect", headers=api(settings)).status_code
            == 403
        )
        start = web.post("/admin/accounts/labs-search/connect", headers=admin(settings))
        assert start.status_code == 200
        query = parse_qs(urlsplit(start.json()["authorization_url"]).query)
        assert query["code_challenge_method"] == ["S256"]
        assert query["scope"] == ["https://www.googleapis.com/auth/adwords"]
        assert query["access_type"] == ["offline"]
        state = query["state"][0]
        with live.store.db() as db:
            verifier = db.execute(
                "SELECT verifier FROM oauth_states WHERE state=?", (state,)
            ).fetchone()["verifier"]
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        assert query["code_challenge"] == [challenge]
        callback = web.get(
            "/oauth/platform/callback", params={"state": state, "code": "returned-code"}
        )
        assert callback.status_code == 200
        assert exchanges[0]["code_verifier"] == [verifier]
        assert "private-google-token" not in callback.text
        assert live.store.tokens("labs-search")["REFRESH_TOKEN"] == "private-google-refresh"
        assert (
            web.get(
                "/oauth/platform/callback", params={"state": state, "code": "returned-code"}
            ).status_code
            == 400
        )
        assert len(exchanges) == 1
        with live.store.db() as db:
            encrypted = db.execute(
                "SELECT body FROM secrets WHERE account=?", ("labs-search",)
            ).fetchone()["body"]
        assert b"private-google-token" not in encrypted
        assert b"private-google-refresh" not in encrypted


@pytest.mark.asyncio
async def test_google_native_refresh_is_serialized_and_retains_rotated_token(settings, monkeypatch):
    settings.demo = False
    monkeypatch.setenv("LABS_GOOGLE_CLIENT_ID", "client")
    monkeypatch.setenv("LABS_GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("LABS_GOOGLE_DEVELOPER_TOKEN", "developer")
    calls = []

    async def exchange(request):
        assert str(request.url) == "https://oauth2.googleapis.com/token"
        calls.append(parse_qs(request.content.decode()))
        await asyncio.sleep(0)
        return httpx.Response(
            200,
            json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(exchange)) as client:
        live = Service(settings, client)
        live.store.save_tokens("labs-search", {"REFRESH_TOKEN": "old-refresh", "EXPIRES_AT": 0})
        providers = await asyncio.gather(live.provider("labs-search"), live.provider("labs-search"))
        assert len(calls) == 1
        assert calls[0]["refresh_token"] == ["old-refresh"]
        assert calls[0]["grant_type"] == ["refresh_token"]
        assert all(provider.secrets["ACCESS_TOKEN"] == "new-access" for provider in providers)
        assert live.store.tokens("labs-search")["REFRESH_TOKEN"] == "new-refresh"
        assert set(live.store.tokens("labs-search")) == {
            "ACCESS_TOKEN",
            "REFRESH_TOKEN",
            "EXPIRES_AT",
        }


def test_read_pagination_context_and_date_validation(web, settings):
    op = prepare(web, settings)
    approve(web, settings, op)
    assert (
        web.post(f"/api/v1/operations/{op['id']}/execute", headers=api(settings)).status_code == 200
    )
    first = web.get(
        "/api/v1/accounts/bindee-meta/campaigns", headers=api(settings), params={"limit": 1}
    ).json()
    assert len(first["items"]) == 1 and not first["complete"] and first["next_cursor"]
    second = web.get(
        "/api/v1/accounts/bindee-meta/campaigns",
        headers=api(settings),
        params={"limit": 1, "cursor": first["next_cursor"]},
    ).json()
    assert second["complete"] and second["next_cursor"] is None
    assert first["items"][0]["id"] != second["items"][0]["id"]
    invalid = web.get(
        "/api/v1/accounts/bindee-meta/performance",
        headers=api(settings),
        params={"start": "2026-01-02", "end": "2026-01-01"},
    )
    assert invalid.status_code == 400
    result = web.get(
        "/api/v1/accounts/bindee-meta/performance",
        headers=api(settings),
        params={"start": "2026-01-01", "end": "2026-01-02"},
    ).json()
    assert result["simulated"] and "SIMULATED" in result["attribution"]
    assert result["currency"] == "INR"
    assert (
        web.put(
            "/api/v1/brands/bindee",
            headers=api(settings),
            json={"context": {"voice": "Hinglish older sister", "target_roas": 3}},
        ).status_code
        == 200
    )
    assert (
        web.get("/api/v1/brands/bindee", headers=api(settings)).json()["context"]["target_roas"]
        == 3
    )
    assert web.get("/api/v1/brands/bindee!", headers=api(settings)).status_code == 400
