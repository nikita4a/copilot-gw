"""Offline tests for the Vercel AI Gateway provider and its gateway routing.

No network: the transport is faked at the session seam (VercelGateway takes an
injected session), reusing the FakeResp/FakeClient/make_pool seams from the
existing suites.
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

import vercel
from vercel import VercelGateway, openai_model_entry
from test_gateway import FakeClient, FakeResp, make_pool
from test_puter import FakeSession

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Keyless by default; .env may set GATEWAY_KEY / AI_GATEWAY_API_KEY."""
    import gateway
    gateway.GATEWAY_KEY = ""
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("VERCEL_ENABLED", raising=False)
    yield
    gateway.GATEWAY_KEY = ""


def make_gateway(key="vk-1", session=None):
    return VercelGateway(key, session=session if session is not None
                         else FakeSession())


def catalog_body(*ids):
    return {"object": "list", "data": [
        {"id": i, "name": i.split("/")[-1], "owned_by": i.split("/")[0],
         "created": 1755815280, "object": "model"} for i in ids]}


def completion_body(model="anthropic/claude-opus-4.5", content="hi from vercel"):
    return {"id": "chatcmpl-vg-1", "object": "chat.completion", "created": 0,
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant",
                         "content": content}, "finish_reason": "stop"}]}


MESSAGES = [{"role": "user", "content": "hi"}]

# --- env gating -----------------------------------------------------------


def test_vercel_key_reads_env(monkeypatch):
    assert vercel.vercel_key() == ""
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "  vk-1  ")
    assert vercel.vercel_key() == "vk-1"


def test_vercel_enabled_defaults_to_key_presence(monkeypatch):
    assert vercel.vercel_enabled() is False
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vk-1")
    assert vercel.vercel_enabled() is True


def test_vercel_enabled_explicit_flag_wins(monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vk-1")
    monkeypatch.setenv("VERCEL_ENABLED", "false")
    assert vercel.vercel_enabled() is False
    monkeypatch.delenv("AI_GATEWAY_API_KEY")
    monkeypatch.setenv("VERCEL_ENABLED", "true")
    assert vercel.vercel_enabled() is True


def test_provider_without_key_is_disabled():
    assert make_gateway(key="").enabled is False
    assert make_gateway(key="vk-1").enabled is True


# --- routing --------------------------------------------------------------


def test_route_strips_vercel_prefix():
    g = make_gateway()
    assert g.route("vercel:anthropic/claude-opus-4.5") == \
        "anthropic/claude-opus-4.5"
    assert g.route("gpt-5.6-luna") is None
    assert g.route("vercel:") is None


def test_openai_model_entry_prefix_and_display_name():
    e = openai_model_entry({"id": "openai/gpt-5.3-codex", "name": "GPT-5.3 Codex",
                            "created": 111})
    assert e["id"] == "vercel:openai/gpt-5.3-codex"
    assert e["owned_by"] == "vercel"
    assert e["display_name"] == "GPT-5.3 Codex"
    assert e["created"] == 111
    assert openai_model_entry({"id": "x/y"})["display_name"] == "x/y"


# --- chat -----------------------------------------------------------------


def test_chat_posts_openai_payload_with_bearer_key():
    s = FakeSession()
    s.responses.append(FakeResp(200, json.dumps(completion_body())))
    g = make_gateway(session=s)

    async def go():
        return await g.chat("anthropic/claude-opus-4.5", MESSAGES,
                            stream=False, max_tokens=16, temperature=0.2)

    status, headers, text = asyncio.run(go())
    assert status == 200
    call = s.calls[0]
    assert call["url"] == "https://ai-gateway.vercel.sh/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer vk-1"
    body = json.loads(call["data"])
    assert body["model"] == "anthropic/claude-opus-4.5"
    assert body["messages"] == MESSAGES
    assert body["stream"] is False
    assert body["max_tokens"] == 16  # extra payload params forwarded
    assert headers["content-type"] == "application/json"


def test_chat_without_key_raises_and_sends_nothing():
    s = FakeSession()
    g = make_gateway(key="", session=s)
    with pytest.raises(vercel.ApiError) as ei:
        asyncio.run(g.chat("anthropic/claude-opus-4.5", MESSAGES))
    assert ei.value.status == 401
    assert s.calls == []


# --- catalog --------------------------------------------------------------


def test_list_models_cached_within_ttl():
    s = FakeSession()
    s.responses.append(FakeResp(200, json.dumps(catalog_body(
        "anthropic/claude-opus-4.5", "openai/gpt-5.3-codex"))))
    g = make_gateway(session=s)

    async def go():
        first = await g.list_models()
        second = await g.list_models()
        return first, second

    first, second = asyncio.run(go())
    assert len(s.calls) == 1
    assert [m["id"] for m in first] == [
        "anthropic/claude-opus-4.5", "openai/gpt-5.3-codex"]
    assert first == second


def test_get_models_payload_is_prefixed():
    s = FakeSession()
    s.responses.append(FakeResp(200, json.dumps(catalog_body("google/x"))))
    g = make_gateway(session=s)
    data = asyncio.run(g.get_models())
    assert data["object"] == "list"
    assert [m["id"] for m in data["data"]] == ["vercel:google/x"]


def test_list_models_network_failure_returns_empty_list():
    s = FakeSession()
    s.responses.append(FakeResp(500, "boom"))
    g = make_gateway(session=s)
    assert asyncio.run(g.list_models()) == []   # no raise
    assert asyncio.run(g.get_models()) == {"object": "list", "data": []}


def test_list_models_transport_exception_returns_empty():
    class Boom(FakeSession):
        async def get(self, url, **kw):
            self.calls.append({"method": "get", "url": url})
            raise OSError("dns down")

    g = make_gateway(session=Boom())
    assert asyncio.run(g.list_models()) == []


def test_list_models_error_keeps_stale_cache(monkeypatch):
    s = FakeSession()
    monkeypatch.setattr(vercel, "VERCEL_MODELS_TTL", 60)
    s.responses.append(FakeResp(200, json.dumps(catalog_body("openai/y"))))
    g = make_gateway(session=s)

    async def go():
        first = await g.list_models()
        g.models_cache["fetched_at"] = 0.0  # force refetch
        s.responses.append(FakeResp(500, "boom"))
        second = await g.list_models()
        return first, second

    first, second = asyncio.run(go())
    assert second == first  # stale served instead of []


def test_list_models_refetches_after_ttl():
    s = FakeSession()
    s.responses.append(FakeResp(200, json.dumps(catalog_body("openai/z"))))
    g = make_gateway(session=s)
    g.models_cache = {"entries": [{"id": "old/model"}], "fetched_at":
                      time.time() - vercel.VERCEL_MODELS_TTL - 1}
    assert [m["id"] for m in asyncio.run(g.list_models())] == ["openai/z"]
    assert len(s.calls) == 1


def test_list_models_without_key_skips_network():
    s = FakeSession()
    g = make_gateway(key="", session=s)
    assert asyncio.run(g.list_models()) == []
    assert s.calls == []


# --- gateway integration --------------------------------------------------


def _app(vercel_responses=None, key="vk-1", catalog=("anthropic/claude-opus-4.5",),
         puter=None, warm_cache=True):
    """build_app with a fake copilot pool + injected vercel provider.

    The catalog is seeded into the cache (no network; warm_cache=False leaves
    it cold so a test can queue the /models response itself). The FakeSession
    queue carries only what a test schedules.
    """
    import gateway
    fake = FakeClient()
    fake.chat_responses.append(FakeResp(200, json.dumps(
        {"id": "copilot-1", "choices": [{"index": 0, "message": {
            "role": "assistant", "content": "copilot-answer"},
            "finish_reason": "stop"}]})))
    s = FakeSession()
    s.responses.extend(vercel_responses or [])
    provider = make_gateway(key=key, session=s)
    provider.models_cache = {
        "entries": [{"id": m, "name": m} for m in catalog] if warm_cache else None,
        "fetched_at": time.time(),
    }
    return (gateway.build_app(pool=make_pool(fake), vercel=provider, puter=puter),
            fake, s)


def _puter_stub(catalog=("gpt-6-luna",), responses=None, fallback=True):
    """Puter provider with a seeded catalog, for precedence tests."""
    from puter import PuterProvider
    ps = FakeSession()
    ps.responses.extend(responses or [])
    p = PuterProvider(["pt-1"], session=ps)
    p.fallback = fallback
    p.models_cache = {"entries": [{"id": m} for m in catalog],
                      "ids": set(catalog), "fetched_at": time.time()}
    return p, ps


async def _http(client, method, path, payload=None):
    kw = {"json": payload} if payload is not None else {}
    resp = await getattr(client, method)(path, **kw)
    return SimpleNamespace(status=resp.status, body=await resp.read(),
                           headers=resp.headers)


async def _go(app, fn):
    async with TestClient(TestServer(app)) as client:
        return await fn(client)


def test_gateway_models_includes_vercel_prefix():
    app, fake, s = _app()

    async def go(client):
        return await _http(client, "get", "/v1/models")

    resp = asyncio.run(_go(app, go))
    assert resp.status == 200
    ids = [m["id"] for m in json.loads(resp.body)["data"]]
    assert "gpt-5.6-luna" in ids                      # copilot unchanged
    assert "vercel:anthropic/claude-opus-4.5" in ids  # vercel prefixed
    assert s.calls == []                              # served from cache


def test_gateway_routes_vercel_prefix_to_upstream_id():
    app, fake, s = _app(vercel_responses=[FakeResp(200, json.dumps(
        completion_body(content="pong")))])

    async def go(client):
        return await _http(client, "post", "/v1/chat/completions",
                           {"model": "vercel:anthropic/claude-opus-4.5",
                            "messages": MESSAGES})

    resp = asyncio.run(_go(app, go))
    assert resp.status == 200
    assert json.loads(resp.body)["choices"][0]["message"]["content"] == "pong"
    assert fake.chat_calls == []                       # copilot untouched
    call = s.calls[0]
    assert call["url"].endswith("/v1/chat/completions")
    assert json.loads(call["data"])["model"] == "anthropic/claude-opus-4.5"


def test_gateway_vercel_stream_is_sse_passthrough():
    sse = (b'data: {"id":"c1","object":"chat.completion.chunk","choices":'
           b'[{"index":0,"delta":{"content":"streamed"}}]}\n\n'
           b"data: [DONE]\n\n")
    app, fake, s = _app(vercel_responses=[
        FakeResp(200, sse, {"content-type": "text/event-stream"})])

    async def go(client):
        return await _http(client, "post", "/v1/chat/completions",
                           {"model": "vercel:openai/gpt-5.3-codex",
                            "messages": MESSAGES, "stream": True})

    resp = asyncio.run(_go(app, go))
    assert resp.status == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    body = resp.body.decode()
    assert "streamed" in body and "data: [DONE]" in body
    assert json.loads(s.calls[0]["data"])["stream"] is True  # real upstream SSE
    assert fake.chat_calls == []


def test_gateway_no_key_is_clean_copilot_mode():
    app, fake, s = _app(key="")

    async def go(client):
        models = await _http(client, "get", "/v1/models")
        plain = await _http(client, "post", "/v1/chat/completions",
                           {"model": "gpt-5.6-luna", "messages": MESSAGES})
        prefixed = await _http(client, "post", "/v1/chat/completions",
                              {"model": "vercel:anthropic/claude-opus-4.5",
                               "messages": MESSAGES})
        return models, plain, prefixed

    models, plain, prefixed = asyncio.run(_go(app, go))
    ids = [m["id"] for m in json.loads(models.body)["data"]]
    assert not any(i.startswith("vercel:") for i in ids)  # not merged
    assert plain.status == 200
    assert json.loads(plain.body)["choices"][0]["message"]["content"] == \
        "copilot-answer"                                  # copilot still works
    assert prefixed.status == 400
    # repo error shape for the disabled path: "type" beside "error" (see puter)
    assert json.loads(prefixed.body)["type"] == "config"
    assert len(fake.chat_calls) == 1                      # only the plain call


def test_gateway_messages_endpoint_with_vercel_model_400():
    app, fake, s = _app()

    async def go(client):
        return await _http(client, "post", "/v1/messages",
                          {"model": "vercel:anthropic/claude-opus-4.5",
                           "max_tokens": 8, "messages": MESSAGES})

    resp = asyncio.run(_go(app, go))
    assert resp.status == 400
    assert fake.messages_calls == []
    assert s.calls == []


def test_gateway_vercel_401_maps_to_auth_error():
    app, fake, s = _app(vercel_responses=[FakeResp(401, '{"error":"invalid key"}')])

    async def go(client):
        return await _http(client, "post", "/v1/chat/completions",
                          {"model": "vercel:anthropic/claude-opus-4.5",
                           "messages": MESSAGES})

    resp = asyncio.run(_go(app, go))
    assert resp.status == 401
    assert json.loads(resp.body)["error"]["type"] == "auth"


def test_gateway_vercel_429_maps_to_rate_limit():
    app, fake, s = _app(vercel_responses=[FakeResp(429, '{"error":"rate"}')])

    async def go(client):
        return await _http(client, "post", "/v1/chat/completions",
                          {"model": "vercel:anthropic/claude-opus-4.5",
                           "messages": MESSAGES})

    resp = asyncio.run(_go(app, go))
    assert resp.status == 429
    assert json.loads(resp.body)["error"]["type"] == "rate_limit"


def test_gateway_copilot_path_unaffected_by_vercel():
    """A bare id the vercel catalog doesn't know (and without vendor '/') is
    still copilot's — the new default route only fires on a catalog match."""
    app, fake, s = _app()

    async def go(client):
        return await _http(client, "post", "/v1/chat/completions",
                          {"model": "gpt-5.6-luna", "messages": MESSAGES})

    resp = asyncio.run(_go(app, go))
    assert resp.status == 200
    assert json.loads(resp.body)["choices"][0]["message"]["content"] == \
        "copilot-answer"
    assert s.calls == []


# --- default routing (no prefix): vercel -> puter -> copilot ---------------


def test_resolve_matches_catalog_and_shape():
    g = make_gateway(session=FakeSession())
    g.models_cache = {"entries": [{"id": "anthropic/claude-opus-4.5"},
                                  {"id": "openai/gpt-4o"}],
                      "fetched_at": time.time()}

    async def go():
        return (await g.resolve("anthropic/claude-opus-4.5"),   # exact
                await g.resolve("gpt-4o"),                      # vendor suffix
                await g.resolve("mistral/large"),               # vendor/model
                await g.resolve("gpt-5.6-luna"),                # copilot id
                await g.resolve("vercel:openai/gpt-4o"),        # prefixed
                await g.resolve(""))                            # empty

    exact, suffix, shaped, miss, prefixed, empty = asyncio.run(go())
    assert exact == "anthropic/claude-opus-4.5"
    assert suffix == "openai/gpt-4o"
    assert shaped == "mistral/large"
    assert miss is None and prefixed is None and empty is None


def test_resolve_prefers_matching_vendor_on_ambiguous_suffix():
    g = make_gateway(session=FakeSession())
    g.models_cache = {"entries": [{"id": "deepseek/deepseek-r1"},
                                  {"id": "openai/deepseek-r1"}],
                      "fetched_at": time.time()}
    assert asyncio.run(g.resolve("deepseek-r1")) == "deepseek/deepseek-r1"


def test_resolve_without_key_is_inert():
    s = FakeSession()
    g = make_gateway(key="", session=s)
    assert asyncio.run(g.resolve("anthropic/claude-opus-4.5")) is None
    assert s.calls == []                    # no catalog fetch either


def test_resolve_warms_cold_catalog_once():
    s = FakeSession()
    s.responses.append(FakeResp(200, json.dumps(
        catalog_body("anthropic/claude-opus-4.5"))))
    g = make_gateway(session=s)
    g.models_cache = {"entries": None, "fetched_at": 0.0}
    assert asyncio.run(g.resolve("claude-opus-4.5")) == \
        "anthropic/claude-opus-4.5"
    assert asyncio.run(g.resolve("claude-opus-4.5")) == \
        "anthropic/claude-opus-4.5"
    assert len([c for c in s.calls if c["method"] == "get"]) == 1  # cached


def test_gateway_default_bare_model_routes_to_vercel():
    """Owner policy: copilot is the last resort — claude-opus-4.5 goes vercel."""
    app, fake, s = _app(vercel_responses=[FakeResp(200, json.dumps(
        completion_body(content="vercel-default")))])

    async def go(client):
        return await _http(client, "post", "/v1/chat/completions",
                          {"model": "claude-opus-4.5", "messages": MESSAGES})

    resp = asyncio.run(_go(app, go))
    assert resp.status == 200
    assert json.loads(resp.body)["choices"][0]["message"]["content"] == \
        "vercel-default"
    assert fake.chat_calls == []                       # copilot skipped
    # dotted catalog id survives: normalization must not mangle the match
    assert json.loads(s.calls[0]["data"])["model"] == "anthropic/claude-opus-4.5"


def test_gateway_default_exact_vendor_model_routes_to_vercel():
    app, fake, s = _app(vercel_responses=[FakeResp(200, json.dumps(
        completion_body()))])

    async def go(client):
        return await _http(client, "post", "/v1/chat/completions",
                          {"model": "anthropic/claude-opus-4.5",
                           "messages": MESSAGES})

    resp = asyncio.run(_go(app, go))
    assert resp.status == 200
    assert fake.chat_calls == []
    assert json.loads(s.calls[0]["data"])["model"] == "anthropic/claude-opus-4.5"


def test_gateway_default_vercel_beats_puter_fallback():
    puter, ps = _puter_stub(catalog=("gpt-6-luna",))
    app, fake, s = _app(puter=puter, catalog=("vendor/gpt-6-luna",),
                        vercel_responses=[FakeResp(200, json.dumps(
                            completion_body("vendor/gpt-6-luna",
                                            content="vercel wins")))])

    async def go(client):
        return await _http(client, "post", "/v1/chat/completions",
                          {"model": "gpt-6-luna", "messages": MESSAGES})

    resp = asyncio.run(_go(app, go))
    assert json.loads(resp.body)["choices"][0]["message"]["content"] == \
        "vercel wins"
    assert ps.calls == []          # puter is second in line, never reached
    assert fake.chat_calls == []


def test_gateway_default_falls_to_puter_when_vercel_misses():
    puter, ps = _puter_stub(
        catalog=("gpt-6-luna",),
        responses=[FakeResp(200, json.dumps({"success": True, "result": {
            "choices": [{"index": 0, "message": {"role": "assistant",
                                                 "content": "puter-answer"},
                        "finish_reason": "stop"}]}}))])
    app, fake, s = _app(puter=puter, catalog=("anthropic/claude-opus-4.5",))

    async def go(client):
        return await _http(client, "post", "/v1/chat/completions",
                          {"model": "gpt-6-luna", "messages": MESSAGES})

    resp = asyncio.run(_go(app, go))
    assert json.loads(resp.body)["choices"][0]["message"]["content"] == \
        "puter-answer"
    assert s.calls == []           # vercel catalog missed (no vendor '/')
    assert fake.chat_calls == []


def test_gateway_default_without_any_provider_key_stays_copilot():
    app, fake, s = _app(key="", catalog=())

    async def go(client):
        return await _http(client, "post", "/v1/chat/completions",
                          {"model": "claude-opus-4.5", "messages": MESSAGES})

    resp = asyncio.run(_go(app, go))
    assert json.loads(resp.body)["choices"][0]["message"]["content"] == \
        "copilot-answer"
    assert s.calls == []                              # vercel inert
    assert fake.chat_calls[0][2]["model"] == "claude-opus-4-5"  # normalized


def test_gateway_github_prefix_forces_copilot_over_vercel():
    app, fake, s = _app(catalog=("gpt-5.6-luna",))

    async def go(client):
        return await _http(client, "post", "/v1/chat/completions",
                          {"model": "github:gpt-5.6-luna", "messages": MESSAGES})

    resp = asyncio.run(_go(app, go))
    assert json.loads(resp.body)["choices"][0]["message"]["content"] == \
        "copilot-answer"
    assert s.calls == []                              # vercel not used
    assert fake.chat_calls[0][2]["model"] == "gpt-5.6-luna"  # prefix stripped


def test_gateway_github_prefix_normalizes_like_copilot_ids():
    app, fake, s = _app()

    async def go(client):
        return await _http(client, "post", "/v1/chat/completions",
                          {"model": "github:claude-opus-4.6[1m]",
                           "messages": MESSAGES})

    resp = asyncio.run(_go(app, go))
    assert resp.status == 200
    assert fake.chat_calls[0][2]["model"] == "claude-opus-4-6"
    assert s.calls == []


def test_gateway_default_route_on_messages_stays_copilot():
    """/v1/messages is Anthropic-format: vercel/puter are chat-only, so the
    default route must not hijack it."""
    app, fake, s = _app()
    fake.messages_responses.append(FakeResp(200, json.dumps(
        {"id": "msg_1", "type": "message", "role": "assistant",
         "content": [{"type": "text", "text": "copilot-msg"}]})))

    async def go(client):
        return await _http(client, "post", "/v1/messages",
                          {"model": "claude-opus-4.5", "max_tokens": 8,
                           "messages": MESSAGES})

    resp = asyncio.run(_go(app, go))
    assert resp.status == 200
    assert json.loads(resp.body)["content"][0]["text"] == "copilot-msg"
    assert fake.messages_calls[0][2]["model"] == "claude-opus-4-5"
    assert s.calls == []
