"""Exercise the actual HTTP and stdio MCP processes on isolated demo databases."""

import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]
API_KEY = "test-api-" + "a" * 40
ADMIN_KEY = "test-operator-" + "b" * 40


def environment(tmp_path, name):
    accounts = tmp_path / (name + "-accounts.json")
    accounts.write_text(
        json.dumps(
            [
                {
                    "alias": "test-meta",
                    "platform": "meta",
                    "account_id": "123456",
                    "currency": "USD",
                    "daily_budget_limit": "1000",
                    "credentials_prefix": "TEST_META",
                }
            ]
        )
    )
    env = {**os.environ}
    # These tests never contact an ad platform. Local subprocess transports
    # must not inherit a runner's external HTTP/SOCKS proxy configuration.
    for key in list(env):
        if key.lower() in ("http_proxy", "https_proxy", "all_proxy"):
            del env[key]
    env.update(
        PYTHONPATH=str(ROOT),
        ADS_API_KEY=API_KEY,
        ADS_ADMIN_KEY=ADMIN_KEY,
        ADS_ENCRYPTION_KEY=Fernet.generate_key().decode(),
        ADS_ACCOUNTS_FILE=str(accounts),
        ADS_DATABASE=str(tmp_path / (name + ".sqlite3")),
        ADS_DEMO="true",
        ADS_ENABLE_WRITES="true",
        ADS_ALLOWED_HOSTS="127.0.0.1,localhost",
    )
    return env


def payload(result):
    return result.structuredContent or json.loads(result.content[0].text)


@contextmanager
def http_server(tmp_path, env):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        env = {**env, "ADS_PUBLIC_URL": url}
        log_path = tmp_path / "http-process.log"
        with log_path.open("w+") as log:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "internal_ads.server:build_app",
                    "--factory",
                    "--fd",
                    str(listener.fileno()),
                    "--log-level",
                    "warning",
                    "--no-access-log",
                ],
                cwd=ROOT,
                env=env,
                pass_fds=(listener.fileno(),),
                stdout=log,
                stderr=log,
            )
            try:
                with httpx.Client(trust_env=False, timeout=0.25) as client:
                    for _ in range(100):
                        if proc.poll() is not None:
                            pytest.fail(
                                "HTTP process stopped during startup: " + log_path.read_text()
                            )
                        try:
                            if client.get(url + "/healthz").status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(0.05)
                    else:
                        pytest.fail("HTTP process did not become ready: " + log_path.read_text())
                yield url, env, proc
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


@pytest.mark.asyncio
async def test_real_http_and_stdio_mcp_sdk_processes(tmp_path):
    http_env = environment(tmp_path, "http")
    stdio_env = environment(tmp_path, "stdio")
    with http_server(tmp_path, http_env) as (url, _, proc):
        async with httpx.AsyncClient(
            headers={"Authorization": "Bearer " + API_KEY}, trust_env=False, timeout=5
        ) as transport:
            async with streamable_http_client(url + "/mcp", http_client=transport) as (
                read,
                write,
                _,
            ):
                async with ClientSession(
                    read, write, read_timeout_seconds=timedelta(seconds=5)
                ) as session:
                    initialized = await session.initialize()
                    assert initialized.serverInfo.name == "Riality Ads"
                    tools = (await session.list_tools()).tools
                    assert "get_connections" in {t.name for t in tools}
                    assert not any("approv" in t.name for t in tools)
                    connections = payload(await session.call_tool("get_connections", {}))
                    assert connections["mode"] == "demo"
                    assert connections["accounts"][0]["account"] == "test-meta"
                    prepared = payload(
                        await session.call_tool(
                            "prepare_change",
                            {
                                "request": {
                                    "draft": {
                                        "account": "test-meta",
                                        "campaign_id": "1001",
                                        "action": "resume",
                                    },
                                    "idempotency_key": "http-real-process-001",
                                }
                            },
                        )
                    )
                    assert prepared["state"] == "pending"
                    blocked = payload(
                        await session.call_tool(
                            "execute_operation", {"operation_id": prepared["id"]}
                        )
                    )
                    assert blocked["error"] == "approval_required"
                    approved = await transport.post(
                        url + f"/admin/operations/{prepared['id']}/approve",
                        headers={"Authorization": "Bearer " + ADMIN_KEY},
                        json={"expected_digest": prepared["digest"]},
                    )
                    assert approved.status_code == 200
                    executed = payload(
                        await session.call_tool(
                            "execute_operation", {"operation_id": prepared["id"]}
                        )
                    )
                    assert executed["state"] == "succeeded"
                    assert executed["result"]["campaign"]["status"] == "ACTIVE"
                    # The local SDK stdio process runs concurrently with HTTP on
                    # a separate database, as required by process ownership.
                    params = StdioServerParameters(
                        command=sys.executable,
                        args=["-m", "internal_ads.server", "stdio"],
                        env=stdio_env,
                        cwd=ROOT,
                    )
                    stderr_path = tmp_path / "stdio-process.log"
                    with stderr_path.open("w") as stderr:
                        async with stdio_client(params, errlog=stderr) as (stdio_read, stdio_write):
                            async with ClientSession(
                                stdio_read, stdio_write, read_timeout_seconds=timedelta(seconds=5)
                            ) as local:
                                assert (await local.initialize()).serverInfo.name == "Riality Ads"
                                local_tools = (await local.list_tools()).tools
                                assert {t.name for t in tools} == {t.name for t in local_tools}
                                local_connections = payload(
                                    await local.call_tool("get_connections", {})
                                )
                                assert local_connections["mode"] == "demo"
                                campaigns = payload(
                                    await local.call_tool(
                                        "list_campaigns", {"account": "test-meta"}
                                    )
                                )
                                assert campaigns["items"][0]["status"] == "PAUSED"
                                local_op = payload(
                                    await local.call_tool(
                                        "prepare_change",
                                        {
                                            "request": {
                                                "draft": {
                                                    "account": "test-meta",
                                                    "campaign_id": "1001",
                                                    "action": "resume",
                                                },
                                                "idempotency_key": "stdio-real-process-001",
                                            }
                                        },
                                    )
                                )
                                assert local_op["state"] == "pending"
                                assert (
                                    payload(
                                        await local.call_tool(
                                            "execute_operation", {"operation_id": local_op["id"]}
                                        )
                                    )["error"]
                                    == "approval_required"
                                )
                    assert proc.poll() is None
    # Both transport lifecycles release their durable database ownership.
    from internal_ads.store import Store

    for env in (http_env, stdio_env):
        with Store(env["ADS_DATABASE"], env["ADS_ENCRYPTION_KEY"]).process_lease():
            pass


def test_second_os_process_rejected_while_http_owner_is_running(tmp_path):
    env = environment(tmp_path, "owned")
    with http_server(tmp_path, env) as (_, running_env, proc):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os; from internal_ads.store import Store; "
                'store=Store(os.environ["ADS_DATABASE"], os.environ["ADS_ENCRYPTION_KEY"]); '
                "lease=store.process_lease(); lease.__enter__()",
            ],
            cwd=ROOT,
            env=running_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode != 0
        assert "Another HTTP or stdio process already owns this database" in result.stderr
        assert proc.poll() is None
    # Once the owner exits, a new OS process can acquire the same lock.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os; from internal_ads.store import Store; "
            'store=Store(os.environ["ADS_DATABASE"], os.environ["ADS_ENCRYPTION_KEY"]); '
            "lease=store.process_lease(); lease.__enter__(); lease.__exit__(None,None,None)",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
