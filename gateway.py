"""GitHub Copilot pool gateway — OpenAI/Anthropic-compatible.

Round-robins Copilot free-quota accounts (gho_ -> short-lived copilot token),
cooldowns accounts on 429/403/quota, streams SSE through. See SPEC.md.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from aiohttp import web

from copilot import (
    ApiError,
    CopilotClient,
    CopilotToken,
    HOP_BY_HOP,
    make_session,
    new_device_id,
    has_vision,
    normalize_model_id,
)

log = logging.getLogger("copilot-gw")

try:
    import dotenv  # type: ignore
    dotenv.load_dotenv()
except ImportError:
    pass

PORT = int(os.environ.get("PORT", "8787"))
GATEWAY_KEY = os.environ.get("GATEWAY_KEY", "").strip()
ACCOUNTS_PATH = os.environ.get("ACCOUNTS_PATH", "accounts.json")
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "600"))
MODELS_TTL_SECONDS = int(os.environ.get("MODELS_TTL_SECONDS", "1800"))
TOKEN_MIN_TTL_SECONDS = 60  # never refresh more often than this

# Static fallback when the live /models fetch fails (opposite of dev-fixture
# list in the reference repo; upstream list is dynamic and authoritative).
FALLBACK_MODELS = [
    {"id": "gpt-5.6-sol", "name": "GPT-5.6 Sol", "vendor": "openai"},
    {"id": "gpt-5.6-luna", "name": "GPT-5.6 Luna", "vendor": "openai"},
    {"id": "gpt-5.6-terra", "name": "GPT-5.6 Terra", "vendor": "openai"},
    {"id": "gpt-5.5", "name": "GPT-5.5", "vendor": "openai"},
    {"id": "gpt-5.4", "name": "GPT-5.4", "vendor": "openai"},
    {"id": "gpt-6-astra", "name": "GPT-6 Astra", "vendor": "openai"},
    {"id": "gpt-6-luna", "name": "GPT-6 Luna", "vendor": "openai"},
    {"id": "gpt-6-sol", "name": "GPT-6 Sol", "vendor": "openai"},
    {"id": "claude-opus-4-6", "name": "Claude Opus 4.6", "vendor": "anthropic"},
    {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6", "vendor": "anthropic"},
    {"id": "gemini-3.x", "name": "Gemini 3.x", "vendor": "google"},
]

MODEL_ID_KEYS = ("id", "name", "vendor", "object")


class QuotaExhausted(Exception):
    """Every account is cooling down or failed; surface 429."""


@dataclass
class Account:
    label: str
    gh_token: str
    api_base: Optional[str] = None
    # copilot token cache (token chain: gho_ -> short-lived api token)
    copilot_token: Optional[str] = None
    token_expires_at: int = 0
    refresh_in: int = 0
    fetched_at: float = 0.0
    cooldown_until: float = 0.0
    _inflight: Optional[asyncio.Future] = field(default=None, repr=False)

    @property
    def cooled_down(self) -> bool:
        return self.cooldown_until > time.time()

    def in_cooldown(self, now: Optional[float] = None) -> bool:
        return self.cooldown_until > (now if now is not None else time.time())


class AccountPool:
    """Round-robin over accounts; cooldown on 429/403; token chain per account."""

    def __init__(self, client: CopilotClient, accounts: list[Account],
                 cooldown: int = COOLDOWN_SECONDS):
        self.client = client
        self.accounts = accounts
        self.cooldown = cooldown
        self._rr = 0
        self.models_cache: dict[str, Any] = {"data": None, "fetched_at": 0.0}

    @classmethod
    def from_file(cls, client: CopilotClient, path: str) -> "AccountPool":
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            log.warning("accounts file %s not readable: %s", path, e)
            raw = []
        accounts = [
            Account(label=str(a.get("label", f"acc{i}")), gh_token=str(a["gh_token"]))
            for i, a in enumerate(raw)
            if a.get("gh_token")
        ]
        return cls(client, accounts)

    @property
    def labels(self) -> list[str]:
        return [a.label for a in self.accounts]

    def mark_error(self, account: Account, status: int) -> None:
        if status in (429, 403):
            account.cooldown_until = time.time() + self.cooldown
            log.warning("account %s cooldown %ss (status %s)",
                        account.label, self.cooldown, status)

    def mark_ok(self, account: Account) -> None:
        account.cooldown_until = 0.0

    def _next_ready(self, now: Optional[float] = None) -> Optional[Account]:
        now = now if now is not None else time.time()
        accounts = self.accounts
        if not accounts:
            return None
        for _ in range(len(accounts)):
            self._rr = (self._rr + 1) % len(accounts)
            acc = accounts[self._rr]
            if acc.cooldown_until <= now:
                return acc
        return None

    def next(self) -> Account:
        acc = self._next_ready()
        if acc is None:
            raise QuotaExhausted("all accounts cooling down")
        return acc

    # --- token chain: gho_ -> copilot token, cached + TTL + single-flight ---
    def _token_fresh(self, account: Account) -> bool:
        if not account.copilot_token or account.fetched_at <= 0:
            return False
        refresh_at = account.fetched_at + max(
            account.refresh_in - 60, TOKEN_MIN_TTL_SECONDS)
        return time.time() < refresh_at and account.token_expires_at > time.time() + 30

    async def _fetch_token(self, account: Account) -> str:
        token = await self.client.get_copilot_token(account.gh_token)
        account.copilot_token = token.token
        account.token_expires_at = token.expires_at
        account.refresh_in = token.refresh_in
        account.fetched_at = time.time()
        if account.api_base is None:
            account.api_base = await self.client.resolve_api_base(
                account.gh_token, token)
        return token.token

    async def get_token(self, account: Account, *, force: bool = False) -> str:
        if force:
            account.copilot_token = None
            account.fetched_at = 0.0
        if self._token_fresh(account):
            return account.copilot_token  # type: ignore[return-value]
        if account._inflight is not None:
            return await asyncio.shield(account._inflight)
        fut = asyncio.create_task(self._fetch_token(account))
        account._inflight = fut
        try:
            return await fut
        finally:
            account._inflight = None

    # --- models with cache + static fallback ---
    async def get_models(self) -> dict:
        cache = self.models_cache
        if cache["data"] is not None and \
                time.time() - cache["fetched_at"] < MODELS_TTL_SECONDS:
            return cache["data"]
        data = None
        for _ in range(len(self.accounts) or 1):
            try:
                account = self.next()
                token = await self.get_token(account)
                api_base = account.api_base or await self.client.resolve_api_base(
                    account.gh_token, CopilotToken(token, 0, 0))
                resp = await self.client.get_models(token, api_base)
                items = [
                    m for m in (resp.get("data") or [])
                    if (m.get("policy") or {}).get("state") != "disabled"
                ]
                if not items:
                    log.warning("live models response on %s had %d entries, "
                                "all filtered or empty (keys: %s)",
                                account.label, len(resp.get("data") or []),
                                sorted((resp.get("data") or [{}])[0].keys())
                                if resp.get("data") else "n/a")
                if items:
                    data = {"object": "list", "data": items,
                            "from_cache": False}
                    self.mark_ok(account)
                    break
            except QuotaExhausted:
                break  # all accounts cooldown (or pool empty) -> fallback
            except Exception as e:  # noqa: BLE001 - try next account
                log.warning("models fetch failed on %s: %s",
                            getattr(account, "label", "?"), e)
                if isinstance(e, ApiError):
                    self.mark_error(account, e.status)
        if data is None:
            log.info("serving fallback model list")
            data = {"object": "list", "data": FALLBACK_MODELS,
                    "from_cache": False, "fallback": True}
        self.models_cache = {"data": data, "fetched_at": time.time()}
        return data


def _openai_model_entry(m: dict) -> dict:
    """Normalize upstream Model -> OpenAI /v1/models entry."""
    caps = m.get("capabilities") or {}
    limits = caps.get("limits") or {}
    client_id = normalize_model_id(m.get("id", ""))
    return {
        "id": client_id,
        "object": "model",
        "type": "model",
        "created": 0,
        "owned_by": m.get("vendor", "github-copilot"),
        "display_name": m.get("name", client_id),
        "context_window": limits.get("max_context_window_tokens", 0),
        "capabilities": caps,
        "supported_endpoints": m.get("supported_endpoints"),
    }


# --- streaming passthrough helpers ---

async def _stream_pass(request: web.Request, upstream_resp: Any,
                       status: int) -> web.StreamResponse:
    stream = web.StreamResponse(status=status)
    for k, v in upstream_resp.headers.items():
        if k.lower() not in HOP_BY_HOP:
            stream.headers[k] = v
    await stream.prepare(request)
    try:
        async for chunk in upstream_resp.content.iter_any():
            await stream.write(chunk)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    await stream.write_eof()
    upstream_resp.close()
    return stream


def _json_response(upstream_resp: Any, status: int, body_text: str) -> web.Response:
    headers = {
        k: v for k, v in upstream_resp.headers.items()
        if k.lower() not in HOP_BY_HOP
        and k.lower() not in ("content-length", "content-type")
    }
    content_type = upstream_resp.headers.get("content-type", "application/json")
    return web.Response(status=status, text=body_text,
                        content_type=content_type.split(";")[0], headers=headers)


# --- app ---

async def _check_gateway_key(request: web.Request) -> Optional[web.Response]:
    if not GATEWAY_KEY:
        return None
    auth = request.headers.get("Authorization", "")
    key = auth[7:] if auth.lower().startswith("bearer ") else ""
    if not key:
        key = request.headers.get("x-api-key", "")
    if key != GATEWAY_KEY:
        return web.json_response(
            {"error": {"message": "invalid gateway key", "type": "auth_error"}},
            status=401)
    return None


async def _models_handler(request: web.Request) -> web.Response:
    if (err := await _check_gateway_key(request)):
        return err
    pool: AccountPool = request.app["pool"]
    try:
        data = await pool.get_models()
    except QuotaExhausted:
        return web.json_response(
            {"error": {"message": "all accounts cooling down"}}, status=429)
    return web.json_response(data)


async def _send_with_refresh(pool: AccountPool, account: Account,
                             send):  # send(token, account) -> client response
    """One upstream POST with a single 401 -> forced token refresh retry."""
    try:
        token = await pool.get_token(account)
        return await send(token, account), None
    except ApiError as e:
        if e.status == 401:
            token = await pool.get_token(account, force=True)
            return await send(token, account), None
        return None, e


async def _proxy_llm(request: web.Request,
                     endpoint: str) -> web.Response:
    """Shared chat-completions / messages loop: rotate on 429/403, stream
    passthrough, single 401-refresh per account."""
    if (err := await _check_gateway_key(request)):
        return err
    pool: AccountPool = request.app["pool"]
    try:
        payload = await request.json()
    except Exception:
        return web.json_response(
            {"error": {"message": "invalid JSON body"}}, status=400)
    if payload.get("model"):
        payload = dict(payload, model=normalize_model_id(payload["model"]))
    stream = bool(payload.get("stream"))
    vision = has_vision(payload)

    client = pool.client
    if endpoint == "chat":
        def send(token, account):
            return client.chat_completions(
                token, account.api_base, payload, vision=vision)
    else:
        def send(token, account):
            return client.messages(
                token, account.api_base, payload, vision=vision)

    attempts = len(pool.accounts) or 1
    last_err: Optional[ApiError] = None
    for _ in range(attempts):
        try:
            account = pool.next()
        except QuotaExhausted:
            break
        resp, err = await _send_with_refresh(pool, account, send)

        # api_base may resolve inside get_token; token chain owns it
        if resp is None:
            pool.mark_error(account, err.status)
            last_err = err
            continue

        # 401 response = stale copilot token -> force refresh, retry once
        if resp.status == 401:
            body = await resp.text()
            resp.close()
            try:
                token = await pool.get_token(account, force=True)
                resp = await send(token, account)
            except ApiError as e:
                resp = None
                pool.mark_error(account, e.status)
                last_err = ApiError(e.status, e.body)
                continue
            if resp.status == 401:
                body = await resp.text()
                resp.close()
                pool.mark_error(account, 429)  # unrefreshable -> cooldown
                last_err = ApiError(401, body)
                continue

        if resp.status < 400:
            pool.mark_ok(account)
            if stream:
                return await _stream_pass(request, resp, resp.status)
            body = await resp.text()
            resp.close()
            return _json_response(resp, resp.status, body)
        body = await resp.text()
        resp.close()
        pool.mark_error(account, resp.status)
        last_err = ApiError(resp.status, body)

    status = last_err.status if last_err else 429
    message = (last_err.body[:500] if last_err and last_err.body
               else "all accounts cooling down")
    return web.json_response({"error": {"message": message}}, status=status)


async def _health_handler(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def build_app(pool: Optional[AccountPool] = None) -> web.Application:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    app = web.Application()
    app["accounts_path"] = ACCOUNTS_PATH
    app["session"] = None
    app["pool"] = pool

    async def on_startup(app: web.Application) -> None:
        if app.get("pool") is not None:
            return  # injected (tests) or prebuilt; client already attached
        import socket
        if app["session"] is None:
            app["session"] = make_session(family=socket.AF_INET)
        client = CopilotClient(app["session"], new_device_id())
        pool = AccountPool.from_file(client, app["accounts_path"])
        if not pool.accounts:
            log.warning("no accounts loaded from %s — add via add_account.py",
                        app["accounts_path"])
        app["client"] = client
        app["pool"] = pool

    async def on_cleanup(app: web.Application) -> None:
        if app["session"] is not None:
            await app["session"].close()

    app.router.add_get("/healthz", _health_handler)
    app.router.add_get("/v1/models", _models_handler)
    app.router.add_get("/models", _models_handler)
    app.router.add_post("/v1/chat/completions",
                        lambda r: _proxy_llm(r, "chat"))
    app.router.add_post("/chat/completions",
                        lambda r: _proxy_llm(r, "chat"))
    app.router.add_post("/v1/messages",
                        lambda r: _proxy_llm(r, "messages"))
    app.router.add_get("/", lambda r: web.json_response(
        {"service": "copilot-gw", "endpoints": [
            "/v1/models", "/v1/chat/completions", "/v1/messages"]}))
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


async def main() -> None:
    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("copilot-gw listening on 0.0.0.0:%s", PORT)
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())