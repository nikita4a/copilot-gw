"""Offline tests for the Puter provider and its gateway routing.

Fakes the HTTP transport at the session seam (PuterProvider gets an injected
session object), mirroring how test_gateway.py fakes the CopilotClient.
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

import puter
from puter import PuterProvider, parse_result, extract_content, parse_catalog

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture(autouse=True)
def _isolate_gateway_key():
    """Tests run keyless; .env (load_dotenv at import) may set GATEWAY_KEY."""
    import gateway
    gateway.GATEWAY_KEY = ""
    yield
    gateway.GATEWAY_KEY = ""

# --- fakes ---------------------------------------------------------------


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


class FakeSession:
    """In-memory HTTP for puter: queued call responses + recorded calls."""

    def __init__(self):
        self.calls: list[dict] = []
        self.responses: list[FakeResp] = []

    def _record(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})

    async def post(self, url, **kw):
        self._record("post", url, **kw)
        return self.responses.pop(0)

    async def get(self, url, **kw):
        self._record("get", url, **kw)
        return self.responses.pop(0)


def make_provider(session, tokens=("t1", "t2"), fallback=False):
    p = PuterProvider(list(tokens), session=session)
    p.fallback = fallback
    return p


def driver_body(model="gpt-6-luna", content="hello from puter"):
    """OpenAI-compat response wrapped in the driver envelope."""
    return {"success": True, "result": {
        "id": "chatcmpl-puter-1", "object": "chat.completion", "created": 0,
        "model": model,
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
    }}


PATCHER_PAYLOAD = {"model": "gpt-6-luna",
                   "messages": [{"role": "user", "content": "hi"}]}

# -- unit: env gating ------------------------------------------------------


def test_puter_tokens_merges_and_dedupes(monkeypatch):
    monkeypatch.setenv("PUTER_AUTH_TOKEN", "tok-1")
    monkeypatch.setenv("PUTER_AUTH_TOKENS", "tok-2, tok-1 , tok-3")
    assert puter.puter_tokens() == ["tok-1", "tok-2", "tok-3"]


def test_puter_enabled_defaults_off_without_token(monkeypatch):
    monkeypatch.delenv("PUTER_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("PUTER_AUTH_TOKENS", raising=False)
    monkeypatch.delenv("PUTER_ENABLED", raising=False)
    assert puter.puter_enabled() is False
    monkeypatch.setenv("PUTER_AUTH_TOKEN", "tok-1")
    assert puter.puter_enabled() is True  # token present -> on by default


def test_puter_enabled_explicit_flag_wins(monkeypatch):
    monkeypatch.setenv("PUTER_ENABLED", "true")
    assert puter.puter_enabled() is True
    monkeypatch.setenv("PUTER_ENABLED", "false")
    monkeypatch.setenv("PUTER_AUTH_TOKEN", "tok-1")
    assert puter.puter_enabled() is False  # explicit off beats token presence


# -- unit: envelope parsing ----------------------------------------------


def test_parse_result_unwraps_driver_envelope():
    body = driver_body()
    assert parse_result(body) is body["result"]


def test_parse_result_accepts_bare_openai_wrapper():
    bare = driver_body()["result"]
    assert parse_result(bare) is bare


def test_extract_content_edge_shapes():
    # non-dict choice skipped, later choice's text used
    assert extract_content({"choices": ["junk", {"text": "z"}]}) == "z"
    # multimodal content list joined
    assert extract_content({"choices": [{"message": {"content": [
        {"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}}]}) == "ab"
    # result is a list without matchable items -> original envelope returned
    raw = {"result": [{"foo": 1}]}
    assert parse_result(raw) is raw
    assert parse_result("not-a-dict") == {}


def test_parse_result_accepts_driver_with_list_result():
    body = {"success": True,
            "result": [{"id": "x", "object": "chat.completion", "choices": [
                {"index": 0, "message": {"role": "assistant",
                                         "content": "hi"},
                 "finish_reason": "stop"}]}]}
    got = parse_result(body)
    assert got["choices"][0]["message"]["content"] == "hi"


def test_parse_result_missing_result_returns_original():
    bare = {"choices": [{"message": {"content": "x"}}]}
    assert parse_result(bare) is bare


def test_extract_content_from_choices_message():
    body = driver_body(content="ping")
    assert extract_content(parse_result(body)) == "ping"


def test_extract_content_falls_back_to_text_and_content_keys():
    assert extract_content({"choices": [{"text": "via-text"}]}) == "via-text"
    assert extract_content({"content": "direct"}) == "direct"
    assert extract_content({"result": "ignored"}) == ""


def test_parse_catalog_strings_and_dicts():
    raw = ["gpt-6-luna", {"id": "openai:gpt-4o", "name": "GPT-4o"},
           "debug/gpt-5", 42]
    entries = parse_catalog(raw)
    assert [e["id"] for e in entries] == [
        "gpt-6-luna", "openai:gpt-4o", "debug/gpt-5"]
    assert parse_catalog({"data": raw}) == entries
    assert parse_catalog({"models": raw}) == entries
    assert parse_catalog({}) == []


# -- unit: token pool & chat ---------------------------------------------


def test_chat_sends_expected_driver_body():
    s = FakeSession()
    s.responses.append(FakeResp(200, json.dumps(driver_body())))
    p = make_provider(s)

    async def go():
        return await p.chat(dict(PATCHER_PAYLOAD))

    status, headers, text = asyncio.run(go())
    assert status == 200
    call = s.calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/drivers/call")
    assert call["headers"]["Authorization"].startswith("Bearer ")
    body = json.loads(call["data"])
    assert body["interface"] == "puter-chat-completion"
    assert body["driver"] == "ai-chat"
    assert body["method"] == "complete"
    assert body["args"]["model"] == "gpt-6-luna"
    assert body["args"]["stream"] is False  # forced non-stream


def test_chat_rotates_tokens_across_calls():
    s = FakeSession()
    s.responses.append(FakeResp(200, json.dumps(driver_body())))
    s.responses.append(FakeResp(200, json.dumps(driver_body())))
    p = make_provider(s)

    async def go():
        await p.chat(dict(PATCHER_PAYLOAD))
        await p.chat(dict(PATCHER_PAYLOAD))

    asyncio.run(go())
    auths = [c["headers"]["Authorization"] for c in s.calls]
    assert auths[0] != auths[1]  # round-robin across the pool
    assert {a for a in auths} == {"Bearer t1", "Bearer t2"}


def test_chat_429_retries_next_token():
    s = FakeSession()
    s.responses.append(FakeResp(429, '{"error":"rate"}'))
    s.responses.append(FakeResp(200, json.dumps(driver_body())))
    p = make_provider(s)

    async def go():
        return await p.chat(dict(PATCHER_PAYLOAD))

    status, _, _ = asyncio.run(go())
    assert status == 200
    auths = [c["headers"]["Authorization"] for c in s.calls]
    assert auths[0] == "Bearer t2" and auths[1] == "Bearer t1"  # rr order


def test_chat_all_tokens_429_returns_429():
    s = FakeSession()
    s.responses.append(FakeResp(429, '{"error":"rate"}'))
    s.responses.append(FakeResp(429, '{"error":"rate"}'))
    p = make_provider(s)

    async def go():
        return await p.chat(dict(PATCHER_PAYLOAD))

    status, _, _ = asyncio.run(go())
    assert status == 429
    assert len(s.calls) == 2


def test_chat_401_returned_immediately():
    s = FakeSession()
    s.responses.append(FakeResp(401, '{"code":"token_auth_failed"}'))
    p = make_provider(s)

    async def go():
        return await p.chat(dict(PATCHER_PAYLOAD))

    status, _, text = asyncio.run(go())
    assert status == 401
    assert "token_auth_failed" in text
    assert len(s.calls) == 1  # no retry on 401


def test_chat_without_tokens_raises_auth_error():
    s = FakeSession()
    p = make_provider(s, tokens=())

    async def go():
        return await p.chat(dict(PATCHER_PAYLOAD))

    with pytest.raises(puter.ApiError) as ei:
        asyncio.run(go())
    assert ei.value.status == 401


# -- unit: catalog cache ---------------------------------------------------


def test_get_models_cached_within_ttl():
    s = FakeSession()
    s.responses.append(FakeResp(200, json.dumps(["gpt-6-luna",
                                                 "openai:gpt-4o"])))
    p = make_provider(s)

    async def go():
        first = await p.get_models()
        second = await p.get_models()
        return first, second

    first, second = asyncio.run(go())
    assert len(s.calls) == 1  # second served from cache
    ids = [m["id"] for m in first["data"]]
    assert ids == ["puter:gpt-6-luna", "puter:openai:gpt-4o"]
    assert first["data"] == second["data"]


def test_get_models_refetches_after_ttl():
    s = FakeSession()
    s.responses.append(FakeResp(200, json.dumps(["gpt-6-luna"])))
    p = make_provider(s)
    p.models_cache = {"entries": [{"id": "old-model"}], "ids": {"old-model"},
                      "fetched_at": time.time() - puter.PUTER_MODELS_TTL - 1}

    data = asyncio.run(p.get_models())
    assert len(s.calls) == 1  # stale cache -> refetch
    ids = [m["id"] for m in data["data"]]
    assert ids == ["puter:gpt-6-luna"]  # fresh list replaced the stale one


def test_get_models_error_keeps_stale_cache(monkeypatch):
    s = FakeSession()
    monkeypatch.setattr(puter, "PUTER_MODELS_TTL", 60)
    s.responses.append(FakeResp(200, json.dumps(["gpt-6-luna"])))
    s.responses.append(FakeResp(500, "boom"))
    p = make_provider(s)

    async def go():
        first = await p.get_models()
        p.models_cache["fetched_at"] = 0.0  # force refetch
        second = await p.get_models()
        return first, second

    first, second = asyncio.run(go())
    assert second["data"] == first["data"]  # stale cache served


def test_get_models_without_tokens_returns_empty():
    s = FakeSession()
    p = make_provider(s, tokens=())
    data = asyncio.run(p.get_models())
    assert data["data"] == []


# -- unit: routing ---------------------------------------------------------


def test_route_strips_puter_prefix():
    p = make_provider(FakeSession(), tokens=("t",))
    assert p.route("puter:gpt-6-luna") == "gpt-6-luna"
    assert p.route("gpt-6-luna") is None


def test_fallback_route_off_by_default():
    p = make_provider(FakeSession(), tokens=("t",))
    assert p.fallback is False  # PUTER_FALLBACK default off
    assert asyncio.run(p.fallback_route("openai:gpt-4o", set())) is None


def test_fallback_route_matches_only_catalog_models():
    s = FakeSession()
    s.responses.append(FakeResp(200, json.dumps(["openai:gpt-4o"])))
    p = make_provider(s, tokens=("t",), fallback=True)

    async def go():
        in_catalog = await p.fallback_route("openai:gpt-4o", set())
        in_copilot = await p.fallback_route("openai:gpt-4o", {"openai:gpt-4o"})
        unknown = await p.fallback_route("no-such-model", set())
        return in_catalog, in_copilot, unknown

    routed, copilot_win, unknown = asyncio.run(go())
    assert routed == "openai:gpt-4o"
    assert copilot_win is None  # copilot listing wins (determinism)
    assert unknown is None


# -- gateway integration ---------------------------------------------------

def _gateway_fakes(catalog=("gpt-6-luna",), puter_responses=None):
    """Build app with a FakeClient-style copilot pool + injected puter.

    The puter catalog is seeded into the provider cache (no network);
    FakeSession queue carries only the chat responses the test schedules.
    """
    import gateway
    from test_gateway import FakeClient, FakeResp as CResp, make_pool
    fake = FakeClient()
    fake.chat_responses.append(CResp(200, json.dumps(
        {"id": "copilot-1", "choices": [{"index": 0, "message": {
            "role": "assistant", "content": "copilot-answer"},
            "finish_reason": "stop"}]})))
    pool = make_pool(fake)
    s = FakeSession()
    s.responses.extend(puter_responses or [])
    provider = make_provider(s, tokens=("t1",))
    provider.models_cache = {
        "entries": [{"id": m} for m in catalog],
        "ids": set(catalog),
        "fetched_at": time.time(),
    }
    app = gateway.build_app(pool=pool, puter=provider)
    return app, fake, s


async def _http(client, method, path, payload=None):
    kw = {"json": payload} if payload is not None else {}
    resp = await getattr(client, method)(path, **kw)
    return SimpleNamespace(status=resp.status, body=await resp.read(),
                           headers=resp.headers)


async def _go(app, fn):
    async with TestClient(TestServer(app)) as client:
        return await fn(client)


def test_gateway_models_includes_puter_prefix():
    import gateway
    app, fake, s = _gateway_fakes()

    async def go(client):
        r = await _http(client, "get", "/v1/models")
        return r

    resp = asyncio.run(_go(app, go))
    assert resp.status == 200
    ids = [m["id"] for m in json.loads(resp.body)["data"]]
    assert "gpt-5.6-luna" in ids            # copilot models unchanged
    assert "puter:gpt-6-luna" in ids        # puter models prefixed
    assert s.calls == []                    # catalog served from seeded cache


def test_gateway_routes_puter_prefix_to_puter():
    import gateway
    app, fake, s = _gateway_fakes(
        puter_responses=[FakeResp(200, json.dumps(driver_body()))])

    async def go(client):
        return await _http(client, "post", "/v1/chat/completions",
                           {"model": "puter:gpt-6-luna",
                            "messages": [{"role": "user", "content": "hi"}]})

    resp = asyncio.run(_go(app, go))
    assert resp.status == 200
    out = json.loads(resp.body)
    assert out["choices"][0]["message"]["content"] == "hello from puter"
    assert fake.chat_calls == []                                   # copilot untouched
    assert s.calls[0]["method"] == "post"


def test_gateway_puter_401_maps_to_auth_error():
    import gateway
    app, fake, s = _gateway_fakes(
        puter_responses=[FakeResp(401, '{"code":"token_auth_failed"}')])

    async def go(client):
        return await _http(client, "post", "/v1/chat/completions",
                           {"model": "puter:gpt-6-luna",
                            "messages": [{"role": "user", "content": "hi"}]})

    resp = asyncio.run(_go(app, go))
    assert resp.status == 401
    err = json.loads(resp.body)["error"]
    assert err["type"] == "auth"
    assert "invalid/expired" in err["message"]


def test_gateway_puter_429_all_tokens_maps_to_429():
    import gateway
    from test_gateway import FakeClient, make_pool
    s = FakeSession()
    s.responses.append(FakeResp(429, '{"error":"rate"}'))
    s.responses.append(FakeResp(429, '{"error":"rate"}'))
    provider = make_provider(s, tokens=("t1", "t2"))
    fake = FakeClient()
    fake.chat_responses.append(FakeResp(401, "irrelevant"))
    app = gateway.build_app(pool=make_pool(fake), puter=provider)

    async def go(client):
        return await _http(client, "post", "/v1/chat/completions",
                           {"model": "puter:gpt-6-luna",
                            "messages": [{"role": "user", "content": "hi"}]})

    resp = asyncio.run(_go(app, go))
    assert resp.status == 429
    assert json.loads(resp.body)["error"]["type"] == "rate_limit"
    assert len(s.calls) >= 2  # both pool tokens tried


def test_gateway_puter_stream_returns_sse():
    import gateway
    app, fake, s = _gateway_fakes(
        puter_responses=[FakeResp(200, json.dumps(driver_body(content="streamed")))])
    provider = s  # noqa - keep reference; fake session holds no state

    async def go(client):
        return await _http(client, "post", "/v1/chat/completions",
                           {"model": "puter:gpt-6-luna",
                            "messages": [{"role": "user", "content": "hi"}],
                            "stream": True})

    resp = asyncio.run(_go(app, go))
    assert resp.status == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    body = resp.body.decode()
    assert "streamed" in body
    assert "data: [DONE]" in body
    # upstream forced to non-stream; SSE built at the gateway
    assert s.calls[0]["data"].count('"stream": false') == 1


def test_gateway_puter_route_on_messages_endpoint_400():
    import gateway
    app, fake, s = _gateway_fakes()

    async def go(client):
        return await _http(client, "post", "/v1/messages",
                           {"model": "puter:claude-x", "max_tokens": 8,
                            "messages": [{"role": "user", "content": "hi"}]})

    resp = asyncio.run(_go(app, go))
    assert resp.status == 400
    assert fake.messages_calls == []


def test_gateway_puter_prefix_disabled_provider_400():
    import gateway
    from test_gateway import FakeClient, make_pool
    fake = FakeClient()
    fake.chat_responses.append(FakeResp(200, json.dumps({"choices": []})))
    app = gateway.build_app(pool=make_pool(fake))  # no puter injected

    async def go(client):
        return await _http(client, "post", "/v1/chat/completions",
                           {"model": "puter:gpt-6-luna",
                            "messages": [{"role": "user", "content": "hi"}]})

    resp = asyncio.run(_go(app, go))
    assert resp.status == 400
    assert fake.chat_calls == []


def test_gateway_fallback_flag_routes_to_puter():
    import gateway
    app, fake, s = _gateway_fakes(
        catalog=("openai:gpt-4o",),
        puter_responses=[FakeResp(200, json.dumps(driver_body(model="openai:gpt-4o")))])
    app["puter"].fallback = True

    async def go(client):
        return await _http(client, "post", "/v1/chat/completions",
                           {"model": "openai:gpt-4o",
                            "messages": [{"role": "user", "content": "hi"}]})

    resp = asyncio.run(_go(app, go))
    assert resp.status == 200
    out = json.loads(resp.body)
    assert out["choices"][0]["message"]["content"] == "hello from puter"
    assert fake.chat_calls == []


def test_gateway_fallback_off_keeps_copilot_path():
    import gateway
    app, fake, s = _gateway_fakes(catalog=("openai:gpt-4o",))
    assert app["puter"].fallback is False  # default off

    async def go(client):
        return await _http(client, "post", "/v1/chat/completions",
                           {"model": "openai:gpt-4o",
                            "messages": [{"role": "user", "content": "hi"}]})

    resp = asyncio.run(_go(app, go))
    assert resp.status == 200
    assert json.loads(resp.body)["choices"][0]["message"]["content"] == \
        "copilot-answer"  # went to copilot, not puter
    assert s.calls == []  # puter never touched