import fakeredis
import fakeredis.aioredis
import httpx
import pytest
from asgi_lifespan import LifespanManager

from sandbox_mux.api import create_app
from sandbox_mux.backends.fake import FakeSandboxBackend
from sandbox_mux.config import Config


@pytest.fixture
async def client():
    cfg = Config.model_validate({"backend": "fake", "auth": {"mode": "dev"},
                                 "pool": {"max_sandboxes": 1, "users_per_sandbox": 1},
                                 "session": {"queue_timeout_seconds": 1}})
    app = create_app(cfg, backend=FakeSandboxBackend(),
                     redis_client=fakeredis.aioredis.FakeRedis(), run_reaper=False)
    async with LifespanManager(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as c:
            yield c


async def test_invoke_and_history(client):
    h = {"X-Dev-User": "alice"}
    r = await client.post("/v1/agent/invoke", json={"conversation_id": "c1", "prompt": "hi"}, headers=h)
    assert r.status_code == 200 and r.json()["reply"] == "echo: hi"
    m = await client.get("/v1/conversations/c1/messages", headers=h)
    assert [x["role"] for x in m.json()] == ["user", "assistant"]


async def test_requires_identity(client):
    r = await client.post("/v1/agent/invoke", json={"prompt": "hi"})
    assert r.status_code == 401


async def test_rejects_bad_conversation_id_and_empty_prompt(client):
    h = {"X-Dev-User": "alice"}
    assert (await client.post("/v1/agent/invoke", headers=h,
                              json={"conversation_id": "../x", "prompt": "hi"})).status_code == 422
    assert (await client.post("/v1/agent/invoke", headers=h,
                              json={"prompt": "  "})).status_code == 422


async def test_second_user_gets_429_when_pool_is_full(client):
    assert (await client.post("/v1/agent/invoke", json={"prompt": "a"},
                              headers={"X-Dev-User": "alice"})).status_code == 200
    r = await client.post("/v1/agent/invoke", json={"prompt": "b"}, headers={"X-Dev-User": "bob"})
    assert r.status_code == 429 and "retry-after" in r.headers
    # alice ends her session, bob can now be served
    assert (await client.delete("/v1/me/session", headers={"X-Dev-User": "alice"})).status_code == 204
    assert (await client.post("/v1/agent/invoke", json={"prompt": "b"},
                              headers={"X-Dev-User": "bob"})).status_code == 200
