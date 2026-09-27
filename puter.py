"""Puter (api.puter.com) upstream provider — OpenAI-compat chat + catalog.

Driver contract (verified 2026-09-27): POST /drivers/call with
{"interface":"puter-chat-completion","driver":"ai-chat",
"method":"complete","test_mode":false,"args":{openai payload}} and
Authorization: Bearer <auth_token>. The driver wraps the OpenAI response in
{"success":true,"result":{...}}; parse_result() unwraps both that and the
bare OpenAI shape. Catalog: GET /puterai/chat/models/ (~1030 models).

Free tier: ~30 req/10s, 3 concurrent. Streaming is emulated at the gateway
(non-stream upstream -> one-chunk SSE), the simplest robust option given the
driver wrapper — see gateway._puter_sse.

Env: PUTER_AUTH_TOKEN, PUTER_AUTH_TOKENS (comma-separated; pooled,
round-robin), PUTER_ENABLED (default: on iff a token is present),
PUTER_FALLBACK (default off), PUTER_MODELS_TTL_SECONDS (default 600),
PUTER_TIMEOUT_SECONDS (default 120).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

from copilot import ApiError, make_session

log = logging.getLogger("copilot-gw.puter")

PUTER_API = os.environ.get("PUTER_API", "https://api.puter.com").rstrip("/")
PUTER_CALL_URL = f"{PUTER_API}/drivers/call"
PUTER_MODELS_URL = f"{PUTER_API}/puterai/chat/models/"
PUTER_MODELS_TTL = int(os.environ.get("PUTER_MODELS_TTL_SECONDS", "600"))
PUTER_TIMEOUT = float(os.environ.get("PUTER_TIMEOUT_SECONDS", "120"))

try:
    from aiohttp import ClientTimeout
except ImportError:  # pragma: no cover - aiohttp is a hard dep
    ClientTimeout = None

_TRUE = ("1", "true", "yes", "on")


def puter_tokens() -> list[str]:
    """PUTER_AUTH_TOKEN + PUTER_AUTH_TOKENS merged, trimmed, deduped."""
    raw = [os.environ.get("PUTER_AUTH_TOKEN", "").strip()]
    raw += [t.strip() for t in os.environ.get("PUTER_AUTH_TOKENS", "").split(",")]
    seen: set[str] = set()
    out: list[str] = []
    for t in raw:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def puter_enabled() -> bool:
    """PUTER_ENABLED wins; unset -> enabled iff a token is configured."""
    flag = os.environ.get("PUTER_ENABLED", "").strip().lower()
    if flag:
        return flag in _TRUE
    return bool(puter_tokens())


def parse_result(body: Any) -> dict:
    """Unwrap driver envelope {"success":true,"result":{...}}; also tolerate
    bare OpenAI wrappers and envelope whose result is a list of messages."""
    if isinstance(body, dict):
        result = body.get("result")
        if isinstance(result, dict):
            if any(k in result for k in ("choices", "content", "message")):
                return result
        elif isinstance(result, list):
            for item in reversed(result):
                if isinstance(item, dict) and any(
                        k in item for k in ("choices", "content")):
                    return item
        if any(k in body for k in ("choices", "content", "message")):
            return body
        if isinstance(result, dict):
            return result
    return body if isinstance(body, dict) else {}


def extract_content(result: dict) -> str:
    """Best-effort content out of an OpenAI-style result dict."""
    for choice in result.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        msg = choice.get("message")
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):  # multimodal parts
                return "".join(
                    b.get("text", "") for b in content if isinstance(b, dict))
        text = choice.get("text")
        if isinstance(text, str):
            return text
    content = result.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    output = result.get("output")
    return output if isinstance(output, str) else ""


def parse_catalog(raw: Any) -> list[dict]:
    """Tolerate list-of-strings, list-of-dicts, {"data": [...]} and
    {"models": [...]} catalog shapes."""
    if isinstance(raw, list):
        entries: Any = raw
    elif isinstance(raw, dict):
        entries = raw.get("data") if isinstance(raw.get("data"), list) else (
            raw.get("models") if isinstance(raw.get("models"), list) else [])
    else:
        entries = []
    out: list[dict] = []
    for e in entries:
        if isinstance(e, str) and e:
            out.append({"id": e})
        elif isinstance(e, dict) and e.get("id"):
            out.append(e)
    return out


def openai_model_entry(m: dict) -> dict:
    """Catalog model -> OpenAI /v1/models entry with the puter: prefix."""
    mid = str(m.get("id", ""))
    return {
        "id": f"puter:{mid}",
        "object": "model",
        "type": "model",
        "created": 0,
        "owned_by": "puter",
        "display_name": m.get("name") or mid,
    }


class PuterProvider:
    """Stateless-per-request transport over an injected aiohttp session.

    Token pool (round-robin; 429 rotates to the next token), catalog cache
    with TTL (stale-keep on fetch failure, just a log — no refetch hot-loop).
    """

    def __init__(self, tokens: list[str], session: Optional[Any] = None):
        self.tokens = list(tokens)
        self._session = session if session is not None else make_session()
        self._rr = 0
        self._timeout = (ClientTimeout(total=PUTER_TIMEOUT)
                         if ClientTimeout is not None else 120)
        self.fallback = os.environ.get("PUTER_FALLBACK", "").strip().lower() in _TRUE
        self.models_cache: dict[str, Any] = {
            "entries": None, "ids": set(), "fetched_at": 0.0}

    @property
    def enabled(self) -> bool:
        return bool(self.tokens)

    # --- token pool ---
    def _next_token(self) -> str:
        if not self.tokens:
            raise ApiError(401, '{"code":"token_missing"}')
        self._rr = (self._rr + 1) % len(self.tokens)
        return self.tokens[self._rr]

    # --- chat ---
    async def chat(self, payload: dict) -> tuple[int, dict, str]:
        """One chat completion. 429 rotates to the next token and is returned
        only once every token was tried. Returns (status, headers, body)."""
        if not self.tokens:
            raise ApiError(401, '{"code":"token_missing"}')
        args = dict(payload, stream=False)  # gateway emulates streaming
        body = {"interface": "puter-chat-completion", "driver": "ai-chat",
                "method": "complete", "test_mode": False, "args": args}
        last_text = ""
        for _ in range(len(self.tokens)):
            token = self._next_token()
            resp = await self._session.post(
                PUTER_CALL_URL,
                headers={"Authorization": f"Bearer {token}",
                         "content-type": "application/json"},
                data=json.dumps(body),
                timeout=self._timeout,
            )
            text = await resp.text()
            status = resp.status
            resp.close()
            if status == 429:
                log.warning("puter 429 on token %d — trying next", self._rr)
                last_text = text
                continue
            headers = {"content-type": resp.headers.get(
                "content-type", "application/json")}
            return status, headers, text
        return 429, {"content-type": "application/json"}, last_text

    # --- catalog ---
    def _models_payload(self) -> dict:
        entries = self.models_cache["entries"] or []
        return {"object": "list", "data": [openai_model_entry(e) for e in entries]}

    async def get_models(self) -> dict:
        """Cached catalog as OpenAI /v1/models payload (puter: prefix).
        Never raises: on failure keeps stale entries or serves [] (logged)."""
        cache = self.models_cache
        if cache["entries"] is not None and \
                time.time() - cache["fetched_at"] < PUTER_MODELS_TTL:
            return self._models_payload()
        if not self.tokens:
            return self._models_payload()
        token = self._next_token()
        try:
            resp = await self._session.get(
                PUTER_MODELS_URL,
                headers={"Authorization": f"Bearer {token}"},
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
                cache["ids"] = {e["id"] for e in entries}
                cache["fetched_at"] = time.time()
            else:
                log.warning("puter catalog returned 0 models")
        except Exception as e:  # noqa: BLE001 - stale cache / empty, logged
            log.warning("puter catalog fetch failed (%s); serving %s",
                        e, "stale cache" if cache["entries"] else "empty list")
        return self._models_payload()

    # --- routing ---
    def route(self, model_id: str) -> Optional[str]:
        """Explicit puter:<id> -> upstream id; anything else -> None."""
        if model_id.startswith("puter:"):
            return model_id[len("puter:"):] or None
        return None

    async def fallback_route(self, model_id: str,
                             copilot_ids: set[str]) -> Optional[str]:
        """PUTER_FALLBACK path: model unknown to copilot and present in the
        puter catalog -> upstream id. Copilot listing always wins."""
        if not self.fallback or not model_id or model_id in copilot_ids:
            return None
        await self.get_models()  # cached; ensures catalog is warm
        return model_id if model_id in self.models_cache["ids"] else None


if __name__ == "__main__":  # pragma: no cover - trivial smoke
    wrapped = driver = {"success": True, "result": {
        "choices": [{"message": {"role": "assistant", "content": "hi"}}]}}
    assert parse_result(wrapped) is wrapped["result"]
    assert parse_result(wrapped["result"]) is wrapped["result"]
    assert extract_content(wrapped["result"]) == "hi"
    assert parse_catalog(["a", {"id": "b"}]) == [{"id": "a"}, {"id": "b"}]
    p = PuterProvider(["t"])
    assert p.route("puter:gpt-6-luna") == "gpt-6-luna"
    assert p.route("gpt-6-luna") is None
    print("puter.py self-check OK")