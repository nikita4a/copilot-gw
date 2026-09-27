"""Vercel AI Gateway (ai-gateway.vercel.sh) upstream provider — fourth provider.

Contract (checked live 2026-09-27): OpenAI-compat API under
https://ai-gateway.vercel.sh/v1 — GET /models returns the full
{"object":"list","data":[...]} catalog (391 models, dynamic, ids are
"vendor/model" e.g. anthropic/claude-opus-4.5, openai/gpt-5.3-codex) and
answers 200 without a key; POST /chat/completions requires
Authorization: Bearer <AI_GATEWAY_API_KEY> and streams standard OpenAI SSE,
so the gateway passes the stream through untouched (no puter-style one-chunk
transform needed — see gateway._vercel_proxy).

Free credits: $5/month on a new key, no card. Key: https://vercel.com/dashboard/ai-gateway

Env: AI_GATEWAY_API_KEY (no default; absent -> provider disabled, copilot-only),
VERCEL_ENABLED (default: on iff a key is present), VERCEL_AI_GATEWAY (base
override), VERCEL_MODELS_TTL_SECONDS (default 3600), VERCEL_TIMEOUT_SECONDS
(default 120).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

from copilot import ApiError, make_session
from puter import parse_catalog

log = logging.getLogger("copilot-gw.vercel")

VERCEL_PREFIX = "vercel:"
VERCEL_API = os.environ.get(
    "VERCEL_AI_GATEWAY", "https://ai-gateway.vercel.sh/v1").rstrip("/")
VERCEL_CHAT_URL = f"{VERCEL_API}/chat/completions"
VERCEL_MODELS_URL = f"{VERCEL_API}/models"
VERCEL_MODELS_TTL = int(os.environ.get("VERCEL_MODELS_TTL_SECONDS", "3600"))
VERCEL_TIMEOUT = float(os.environ.get("VERCEL_TIMEOUT_SECONDS", "120"))

try:
    from aiohttp import ClientTimeout
except ImportError:  # pragma: no cover - aiohttp is a hard dep
    ClientTimeout = None

_TRUE = ("1", "true", "yes", "on")


def vercel_key() -> str:
    """AI_GATEWAY_API_KEY, trimmed (empty string when unset)."""
    return os.environ.get("AI_GATEWAY_API_KEY", "").strip()


def vercel_enabled() -> bool:
    """VERCEL_ENABLED wins; unset -> enabled iff a key is configured."""
    flag = os.environ.get("VERCEL_ENABLED", "").strip().lower()
    if flag:
        return flag in _TRUE
    return bool(vercel_key())


def openai_model_entry(m: dict) -> dict:
    """Catalog model -> OpenAI /v1/models entry with the vercel: prefix."""
    mid = str(m.get("id", ""))
    return {
        "id": f"{VERCEL_PREFIX}{mid}",
        "object": "model",
        "type": "model",
        "created": int(m.get("created") or 0),
        "owned_by": "vercel",
        "display_name": m.get("name") or mid,
    }


class VercelGateway:
    """Thin transport over an injected aiohttp session + cached catalog.

    Keyless = disabled: every entry point degrades to copilot-only routing
    instead of raising. Catalog cache TTL 1h; on fetch failure the stale list
    is kept (or [] when never fetched) — never raises.
    """

    def __init__(self, key: Optional[str] = None, session: Optional[Any] = None):
        self.key = (vercel_key() if key is None else str(key)).strip()
        self._session = session if session is not None else make_session()
        self._timeout = (ClientTimeout(total=VERCEL_TIMEOUT)
                         if ClientTimeout is not None else 120)
        self.models_cache: dict[str, Any] = {"entries": None, "fetched_at": 0.0}

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def _headers(self) -> dict:
        if not self.key:
            raise ApiError(401, '{"code":"key_missing"}')
        return {"Authorization": f"Bearer {self.key}",
                "content-type": "application/json"}

    # --- chat ---
    async def chat(self, model: str, messages: list, stream: bool = True,
                   **extra) -> tuple[int, dict, Any]:
        """One chat completion on /v1/chat/completions.

        Returns (status, headers, body): body is the response text when
        stream is False, the raw upstream response when True (its SSE is
        already OpenAI-format — the gateway passes it through).
        """
        body = dict(extra, model=model, messages=messages, stream=stream)
        resp = await self._session.post(
            VERCEL_CHAT_URL,
            headers=self._headers(),
            data=json.dumps(body),
            timeout=self._timeout,
        )
        if stream:
            return resp.status, {"content-type": resp.headers.get(
                "content-type", "application/json")}, resp
        text = await resp.text()
        status = resp.status
        resp.close()
        return status, {"content-type": resp.headers.get(
            "content-type", "application/json")}, text

    # --- catalog ---
    def _models_payload(self) -> dict:
        entries = self.models_cache["entries"] or []
        return {"object": "list", "data": [openai_model_entry(e) for e in entries]}

    async def list_models(self) -> list[dict]:
        """Cached upstream catalog (raw entries, unprefixed ids).

        Never raises: on network/HTTP failure keeps the stale list, or returns
        [] when nothing was ever fetched. Keyless -> [] without a request.
        """
        cache = self.models_cache
        if cache["entries"] is not None and \
                time.time() - cache["fetched_at"] < VERCEL_MODELS_TTL:
            return cache["entries"]
        if not self.key:
            return cache["entries"] or []
        try:
            resp = await self._session.get(
                VERCEL_MODELS_URL,
                headers={"Authorization": f"Bearer {self.key}"},
                timeout=self._timeout,
            )
            text = await resp.text()
            status = resp.status
            resp.close()
            if status != 200:
                raise ApiError(status, text)
            entries = parse_catalog(json.loads(text or "[]"))
            if entries:
                cache["entries"] = entries
                cache["fetched_at"] = time.time()
            else:
                log.warning("vercel catalog returned 0 models")
        except Exception as e:  # noqa: BLE001 - stale cache / empty, logged
            log.warning("vercel catalog fetch failed (%s); serving %s",
                        e, "stale cache" if cache["entries"] else "empty list")
        return cache["entries"] or []

    async def get_models(self) -> dict:
        """Cached catalog as an OpenAI /v1/models payload (vercel: prefix)."""
        await self.list_models()
        return self._models_payload()

    # --- routing ---
    def route(self, model_id: str) -> Optional[str]:
        """Explicit vercel:<vendor/model> -> upstream id; else -> None."""
        if model_id.startswith(VERCEL_PREFIX):
            return model_id[len(VERCEL_PREFIX):] or None
        return None


if __name__ == "__main__":  # pragma: no cover - trivial smoke
    assert parse_catalog({"data": [{"id": "anthropic/claude-opus-4.5"}]}) == \
        [{"id": "anthropic/claude-opus-4.5"}]
    assert openai_model_entry({"id": "x/y", "name": "Y"})["id"] == "vercel:x/y"
    g = VercelGateway("k", session=object())
    assert g.route("vercel:anthropic/claude-opus-4.5") == "anthropic/claude-opus-4.5"
    assert g.route("gpt-5.6-luna") is None
    assert VercelGateway("", session=object()).enabled is False
    print("vercel.py self-check OK")
