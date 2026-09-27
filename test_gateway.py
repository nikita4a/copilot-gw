"""No-network tests for the copilot pool gateway.

Fakes the upstream aiohttp client at the CopilotClient seam (injected into
AccountPool / build_app), so rotation, cooldown and the token chain are
exercised without touching the network.
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

import copilot as cp
import gateway
from gateway import Account, AccountPool, QuotaExhausted

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture(autouse=True)
def _isolate_gateway_key():
    """Tests run keyless; .env (load_dotenv at import) may set GATEWAY_KEY."""
    gateway.GATEWAY_KEY = ""
    yield
    gateway.GATEWAY_KEY = ""


class FakeResp:
    def __init__(self, status=200, body=b"", headers=None):
        self.status = status
        self._body = body if isinstance(body, bytes) else str(body).encode()
        self.headers = headers or {"content-type": "application/json"}
        self.closed = False

    async def text(self):
        return self._body.decode("utf-8", "replace")

    def close(self):
        self.closed = True

    @property
    def content(self):
        return self

    async def iter_any(self):
        yield self._body


class FakeClient:
    """In-memory upstream: queued chat responses, per-token counters."""

    def __init__(self):
        self.token_fetch_count = 0
        self.chat_calls: list[tuple[str, str, dict]] = []
        self.models_calls = 0
        self.messages_calls: list[tuple[str, str, dict]] = []
        self.chat_responses: list[FakeResp] = []
        self.messages_responses: list[FakeResp] = []
        self.models_response = {"object": "list", "data": [
            {"id": "gpt-5.6-luna", "name": "GPT-5.6 Luna",
             "vendor": "openai", "model_picker_enabled": True,
             "policy": {"state": "enabled"}},
            {"id": "claude-opus-4-6", "name": "Claude Opus 4.6",
             "vendor": "anthropic", "model_picker_enabled": True,
             "policy": {"state": "enabled"}},
        ]}
        self.api_base = "https://api.githubcopilot.com"

    async def get_copilot_token(self, gh_token):
        self.token_fetch_count += 1
        return cp.CopilotToken(
            token=f"sk-fake-{self.token_fetch_count}",
            expires_at=int(time.time()) + 3600,
            refresh_in=1500,
            endpoints={"api": self.api_base},
        )

    async def resolve_api_base(self, gh_token, token):
        return self.api_base

    async def get_models(self, token, api_base):
        self.models_calls += 1
        return dict(self.models_response)

    async def chat_completions(self, token, api_base, payload, vision=False):
        self.chat_calls.append((token, api_base, dict(payload)))
        return self.chat_responses.pop(0)

    async def messages(self, token, api_base, payload, vision=False):
        self.messages_calls.append((token, api_base, dict(payload)))
        return self.messages_responses.pop(0)


def make_pool(fake: FakeClient, n: int = 2, *, cooldown=600) -> AccountPool:
    accounts = [
        Account(label=f"acc{i}", gh_token=f"gho_test_{i}") for i in range(n)
    ]
    return AccountPool(fake, accounts, cooldown=cooldown)


def make_app(fake: FakeClient, pool: AccountPool = None):
    pool = pool or make_pool(fake)
    return gateway.build_app(pool=pool)


def chat_body(model="gpt-5.6-luna", stream=False):
    return {"model": model, "messages": [{"role": "user",
                                          "content": "ping"}],
            "max_tokens": 16, "stream": stream}


# --- rotation / cooldown -------------------------------------------------

def test_round_robin_cycles_all_accounts():
    pool = make_pool(FakeClient(), n=3)
    seen = [pool.next().label for _ in range(6)]
    assert seen == ["acc1", "acc2", "acc0", "acc1", "acc2", "acc0"]


def test_cooldown_skips_account_and_rounds_robin():
    pool = make_pool(FakeClient(), n=2)
    a0, a1 = pool.accounts
    a0.cooldown_until = time.time() + 1000
    assert pool.next().label == "acc1"
    a1.cooldown_until = time.time() + 1000
    with pytest.raises(QuotaExhausted):
        pool.next()


def test_mark_error_sets_cooldown_and_mark_ok_clears():
    pool = make_pool(FakeClient(), n=1)
    acc = pool.accounts[0]
    pool.mark_error(acc, 429)
    assert acc.in_cooldown()
    pool.mark_error(acc, 403)
    assert acc.in_cooldown()
    pool.mark_ok(acc)
    assert not acc.in_cooldown()


def test_cooldown_does_not_apply_to_401():
    pool = make_pool(FakeClient(), n=1)
    acc = pool.accounts[0]
    pool.mark_error(acc, 401)
    assert not acc.in_cooldown()  # 401 handled by token refresh, not cooldown


# --- token chain: cache / TTL / refresh ----------------------------------

def test_token_cached_until_refresh_deadline():
    fake = FakeClient()
    pool = make_pool(fake, n=1)
    acc = pool.accounts[0]

    async def go():
        t1 = await pool.get_token(acc)
        t2 = await pool.get_token(acc)
        return t1, t2

    t1, t2 = asyncio.run(go())
    assert t1 == t2 == "sk-fake-1"
    assert fake.token_fetch_count == 1


def test_token_refetched_after_refresh_in_window():
    fake = FakeClient()
    pool = make_pool(fake, n=1)
    acc = pool.accounts[0]
    acc.copilot_token = "sk-stale"      # cached but past refresh deadline
    acc.fetched_at = time.time() - 2000  # refresh_in 1500 -> stale
    acc.refresh_in = 1500

    async def go():
        return await pool.get_token(acc)

    assert asyncio.run(go()) == "sk-fake-1"  # stale cache replaced
    assert fake.token_fetch_count == 1
    assert acc.copilot_token == "sk-fake-1"


def test_token_expired_by_expires_at_refetches():
    fake = FakeClient()
    pool = make_pool(fake, n=1)
    acc = pool.accounts[0]
    acc.fetched_at = time.time()
    acc.refresh_in = 1500
    acc.token_expires_at = int(time.time()) - 10  # already dead
    asyncio.run(pool.get_token(acc))
    assert fake.token_fetch_count == 1  # stale by expires_at -> refetched


def test_force_refresh_always_refetches():
    fake = FakeClient()
    pool = make_pool(fake, n=1)
    acc = pool.accounts[0]

    async def go():
        await pool.get_token(acc)
        return await pool.get_token(acc, force=True)

    asyncio.run(go())
    assert fake.token_fetch_count == 2


# --- HTTP: models endpoint ------------------------------------------------

def test_models_route_returns_list():
    fake = FakeClient()
    app = make_app(fake)
    resp = asyncio.run(http_get(app, "/v1/models"))
    assert resp.status == 200
    data = json.loads(resp.body)
    assert data["object"] == "list"
    ids = [m["id"] for m in data["data"]]
    assert "gpt-5.6-luna" in ids


def test_models_route_uses_cache_within_ttl():
    fake = FakeClient()
    pool = make_pool(fake)
    app = gateway.build_app(pool=pool)

    async def go():
        r1 = await http_get(app, "/v1/models")
        r2 = await http_get(app, "/v1/models")
        return r1, r2

    r1, r2 = asyncio.run(go())
    assert r1.status == r2.status == 200
    assert fake.models_calls == 1  # second served from cache


# --- HTTP: chat completions -----------------------------------------------

def test_chat_nonstream_passthrough_and_rotation():
    fake = FakeClient()
    huge = {"id": "chatcmpl-1", "object": "chat.completion", "created": 0,
            "model": "gpt-5.6-luna",
            "choices": [{"index": 0, "message": {"role": "assistant",
                        "content": "pong"}, "finish_reason": "stop"}]}
    fake.chat_responses.append(FakeResp(200, json.dumps(huge)))
    app = make_app(fake)
    resp = asyncio.run(http_post(app, "/v1/chat/completions", chat_body()))
    assert resp.status == 200
    out = json.loads(resp.body)
    assert out["choices"][0]["message"]["content"] == "pong"
    # request carried upstream with per-account token + api base
    token, api_base, payload = fake.chat_calls[0]
    assert token == "sk-fake-1"
    assert api_base == "https://api.githubcopilot.com"
    assert payload["model"] == "gpt-5.6-luna"


def test_chat_strips_1m_suffix_before_upstream():
    fake = FakeClient()
    fake.chat_responses.append(FakeResp(200, json.dumps({"choices": []})))
    app = make_app(fake)
    asyncio.run(http_post(app, "/v1/chat/completions",
                          chat_body(model="gpt-5.6-luna[1m]")))
    assert fake.chat_calls[0][2]["model"] == "gpt-5.6-luna"


def test_chat_429_rotates_to_next_account():
    fake = FakeClient()
    fake.chat_responses.append(FakeResp(429, '{"error":"quota"}'))
    fake.chat_responses.append(FakeResp(200, json.dumps({"choices": [
        {"message": {"role": "assistant", "content": "ok"}}]})))
    pool = make_pool(fake, n=2)
    app = gateway.build_app(pool=pool)
    resp = asyncio.run(http_post(app, "/v1/chat/completions", chat_body()))
    assert resp.status == 200
    assert len(fake.chat_calls) == 2
    tokens = {c[0] for c in fake.chat_calls}
    assert len(tokens) == 2  # both accounts tried
    assert sum(a.in_cooldown() for a in pool.accounts) == 1
    assert not pool.accounts[0].in_cooldown() or not pool.accounts[1].in_cooldown()


def test_chat_all_accounts_in_cooldown_returns_429():
    fake = FakeClient()
    fake.chat_responses.append(FakeResp(429, '{"error":"quota"}'))
    pool = make_pool(fake, n=1)
    app = gateway.build_app(pool=pool)
    resp = asyncio.run(http_post(app, "/v1/chat/completions", chat_body()))
    assert resp.status == 429
    assert pool.accounts[0].in_cooldown()


def test_chat_401_forces_token_refresh_once():
    fake = FakeClient()
    fake.chat_responses.append(FakeResp(401, '{"error":"token expired"}'))
    fake.chat_responses.append(FakeResp(200, json.dumps({"choices": [
        {"message": {"role": "assistant", "content": "fresh"}}]})))
    pool = make_pool(fake, n=1)
    app = gateway.build_app(pool=pool)
    resp = asyncio.run(http_post(app, "/v1/chat/completions", chat_body()))
    assert resp.status == 200
    assert fake.token_fetch_count == 2  # initial + forced refresh
    assert fake.chat_calls[1][0] == "sk-fake-2"


# --- HTTP: stream passthrough ---------------------------------------------

def test_chat_stream_passthrough_sse():
    fake = FakeClient()
    sse = b"data: " + b'{"id":"chunk-1","choices":[{"delta":{"content":"hi"}}]}' \
        + b"\n\n" + b"data: [DONE]\n\n"
    fake.chat_responses.append(FakeResp(200, sse,
                                        {"content-type": "text/event-stream"}))
    app = make_app(fake)
    resp = asyncio.run(http_post(app, "/v1/chat/completions",
                                 chat_body(stream=True)))
    assert resp.status == 200
    assert b"data: [DONE]" in resp.body
    assert fake.chat_calls[0][2]["stream"] is True


# --- HTTP: anthropic /v1/messages ------------------------------------------

def test_messages_passthrough_nonstream():
    fake = FakeClient()
    fake.messages_responses.append(FakeResp(200, json.dumps(
        {"id": "msg_1", "type": "message", "role": "assistant",
         "content": [{"type": "text", "text": "salut"}],
         "model": "claude-opus-4-6"})))
    app = make_app(fake)
    body = {"model": "claude-opus-4-6", "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}]}
    resp = asyncio.run(http_post(app, "/v1/messages", body))
    assert resp.status == 200
    assert json.loads(resp.body)["content"][0]["text"] == "salut"
    assert fake.messages_calls[0][2]["model"] == "claude-opus-4-6"


def test_messages_stream_passthrough():
    fake = FakeClient()
    sse = b"event: content_block_delta\ndata: {\"delta\":{\"text\":\"yo\"}}\n\n"
    fake.messages_responses.append(FakeResp(200, sse,
                                            {"content-type":
                                             "text/event-stream"}))
    app = make_app(fake)
    body = {"model": "claude-opus-4-6", "max_tokens": 16, "stream": True,
            "messages": [{"role": "user", "content": "hi"}]}
    resp = asyncio.run(http_post(app, "/v1/messages", body))
    assert resp.status == 200
    assert b"content_block_delta" in resp.body


# --- gateway key -----------------------------------------------------------

def test_gateway_key_enforced_and_accepted(monkeypatch):
    monkeypatch.setattr(gateway, "GATEWAY_KEY", "s3cret")
    try:
        app = make_app(FakeClient())

        async def go():
            unauth = await http_get(app, "/v1/models")
            auth = await http_get(app, "/v1/models",
                                  headers={"Authorization": "Bearer s3cret"})
            return unauth, auth

        unauth, auth = asyncio.run(go())
        assert unauth.status == 401
        assert auth.status == 200
    finally:
        monkeypatch.setattr(gateway, "GATEWAY_KEY", "")


# --- helpers ---------------------------------------------------------------

async def http_get(app, path, headers=None):
    return await _http(app, "get", path, headers=headers)


async def http_post(app, path, payload, headers=None):
    return await _http(app, "post", path, payload=payload, headers=headers)


async def _http(app, method, path, payload=None, headers=None):
    async with TestClient(TestServer(app)) as client:
        kw = {"headers": headers} if headers else {}
        if payload is not None:
            kw["json"] = payload
        resp = await getattr(client, method)(path, **kw)
        return SimpleNamespace(status=resp.status, body=await resp.read(),
                               headers=resp.headers)


# --- model id normalization (regression) ------------------------------------

def test_normalize_model_ids():
    assert cp.normalize_model_id("claude-opus-4.6") == "claude-opus-4-6"
    assert cp.normalize_model_id("claude-opus-4-6") == "claude-opus-4-6"
    assert cp.normalize_model_id("gpt-5.6-luna[1m]") == "gpt-5.6-luna"
    assert cp.normalize_model_id("gemini-3.x") == "gemini-3.x"