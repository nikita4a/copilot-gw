"""GitHub Copilot upstream client: device flow, token exchange, LLM endpoints.

Facts from SPEC.md (extracted from caozhiyuan/copilot-api). No secrets in code.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

# --- constants (verbatim from reference repo, see SPEC.md) ---
GITHUB_CLIENT_ID = "Iv1.b507a08c87ecfe98"
GITHUB_APP_SCOPES = "read:user"
GITHUB_BASE_URL = "https://github.com"
GITHUB_API_BASE_URL = "https://api.github.com"
DEVICE_CODE_URL = f"{GITHUB_BASE_URL}/login/device/code"
ACCESS_TOKEN_URL = f"{GITHUB_BASE_URL}/login/oauth/access_token"

COPILOT_VERSION = "0.58.0"
EDITOR_PLUGIN_VERSION = f"copilot-chat/{COPILOT_VERSION}"
USER_AGENT = f"GitHubCopilotChat/{COPILOT_VERSION}"
API_VERSION = "2026-06-01"  # x-github-api-version on copilot calls
GITHUB_API_VERSION_GH = "2025-04-01"  # x-github-api-version on api.github.com
VSCODE_VERSION_FALLBACK = "1.130.0"
FALLBACK_INDIVIDUAL_API = "https://api.githubcopilot.com"

# refresh loop (lib/token.ts)
EARLY_REFRESH_BUFFER_MS = 60_000
MIN_REFRESH_DELAY_MS = 1_000

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
    "content-encoding", "date", "server",
}

GRANT_TYPE_DEVICE = "urn:ietf:params:oauth:grant-type:device_code"


class ApiError(Exception):
    """Upstream (or flow) failure with HTTP status and raw body."""

    def __init__(self, status: int, body: str, *, force_refresh: bool = False):
        super().__init__(f"upstream {status}: {body[:300]}")
        self.status = status
        self.body = body
        self.force_refresh = force_refresh


class LoopGetaddrinfoResolver:
    """aiohttp resolver backed by loop.getaddrinfo.

    aiohttp's ThreadedResolver fails with "Could not contact DNS servers"
    (WSAHOST_NOT_FOUND 11001) on this Windows host even though the plain
    socket call succeeds; loop.getaddrinfo with the same args works.
    """

    def __init__(self, family: int = 0):
        self._family = family

    async def resolve(self, host: str, port: int, family: int = 0):
        import socket
        family = family or self._family
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=family, type=socket.SOCK_STREAM)
        return [{
            "hostname": host,
            "host": info[4][0],
            "port": info[4][1],
            "family": info[0],
            "proto": info[1],
            "flags": info[2],
            "socktype": socket.SOCK_STREAM,
            "addr": info[4],
        } for info in infos]

    async def close(self) -> None:
        pass


def make_session(*, family: int = 0) -> Any:
    """ClientSession with the working resolver (see class docstring)."""
    from aiohttp import ClientSession, TCPConnector
    import socket
    resolver = LoopGetaddrinfoResolver(family=family or socket.AF_INET)
    return ClientSession(
        connector=TCPConnector(resolver=resolver, family=family or 0))


@dataclass
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


@dataclass
class CopilotToken:
    token: str
    expires_at: int  # unix seconds
    refresh_in: int  # seconds until refresh
    endpoints: dict = field(default_factory=dict)


def new_request_id() -> str:
    return str(uuid.uuid4())


def new_device_id() -> str:
    """VS Code device id: randomUUID().toLowerCase() (see SPEC §7)."""
    return str(uuid.uuid4()).lower()


def github_headers(gh_token: str) -> dict:
    """Headers for api.github.com endpoints (authorization: token gho_...)."""
    return {
        "authorization": f"token {gh_token}",
        "user-agent": USER_AGENT,
        "x-github-api-version": GITHUB_API_VERSION_GH,
        "x-vscode-user-agent-library-version": "electron-fetch",
        "accept": "application/json",
    }


def copilot_headers(token: str, device_id: str, *, intent: str,
                    interaction_type: str, request_id: Optional[str] = None,
                    vision: bool = False) -> dict:
    """Headers for api.githubcopilot.com endpoints (see SPEC §3/§4)."""
    req_id = request_id or new_request_id()
    headers = {
        "Authorization": f"Bearer {token}",
        "content-type": "application/json",
        "copilot-integration-id": "vscode-chat",
        "editor-device-id": device_id,
        "editor-version": f"vscode/{VSCODE_VERSION_FALLBACK}",
        "editor-plugin-version": EDITOR_PLUGIN_VERSION,
        "user-agent": USER_AGENT,
        "openai-intent": intent,
        "x-github-api-version": API_VERSION,
        "x-request-id": req_id,
        "x-agent-task-id": req_id,
        "x-vscode-user-agent-library-version": "electron-fetch",
        "x-interaction-type": interaction_type,
    }
    if vision:
        headers["copilot-vision-request"] = "true"
    return headers


def normalize_model_id(model_id: str) -> str:
    """Client-facing ids -> upstream id.

    - strips client-side `[1m]` suffix
    - claude dots -> hyphens (claude-opus-4.6 -> claude-opus-4-6; inverse of
      toClientModelId in spec, so hyphenated ids pass through unchanged)
    """
    mid = (model_id or "").strip()
    if mid.endswith("[1m]"):
        mid = mid[:-4]
    parts = mid.split("-")
    if len(parts) >= 3 and parts[0] == "claude":
        # claude-{family}-{major}.{minor} -> claude-{family}-{major}-{minor}
        for i, p in enumerate(parts):
            if "." in p and p.replace(".", "").isdigit():
                parts[i] = p.replace(".", "-")
        return "-".join(parts)
    return mid


class CopilotClient:
    """Thin aiohttp wrapper; every call maps to one upstream request.

    Pool/gateway own token caching and rotation; this class is stateless per
    request (session injected) so tests can swap it with a fake.
    """

    def __init__(self, session: Any, device_id: str):
        self._session = session
        self.device_id = device_id

    # --- device flow ---
    async def device_code(self) -> DeviceCode:
        resp = await self._session.post(
            DEVICE_CODE_URL,
            headers={"content-type": "application/json", "accept": "application/json"},
            data=json.dumps({"client_id": GITHUB_CLIENT_ID, "scope": GITHUB_APP_SCOPES}),
        )
        body = await resp.text()
        if resp.status != 200:
            raise ApiError(resp.status, body)
        data = json.loads(body or "{}")
        return DeviceCode(
            device_code=data["device_code"],
            user_code=data["user_code"],
            verification_uri=data["verification_uri"],
            expires_in=int(data["expires_in"]),
            interval=int(data["interval"]),
        )

    async def poll_access_token(self, dc: DeviceCode, *, poll_hook=None) -> str:
        """Poll until gho_ token. Non-200 responses are retried forever."""
        while True:
            resp = await self._session.post(
                ACCESS_TOKEN_URL,
                headers={"content-type": "application/json", "accept": "application/json"},
                data=json.dumps({
                    "client_id": GITHUB_CLIENT_ID,
                    "device_code": dc.device_code,
                    "grant_type": GRANT_TYPE_DEVICE,
                }),
            )
            body = await resp.text()
            if resp.status == 200:
                data = json.loads(body or "{}")
                token = data.get("access_token")
                if token:
                    return token
            if poll_hook:
                poll_hook(resp.status, body[:200])
            await asyncio.sleep(dc.interval + 1)

    # --- token & plan ---
    async def get_copilot_token(self, gh_token: str) -> CopilotToken:
        resp = await self._session.get(
            f"{GITHUB_API_BASE_URL}/copilot_internal/v2/token",
            headers=github_headers(gh_token),
        )
        body = await resp.text()
        if resp.status != 200:
            raise ApiError(resp.status, body)
        data = json.loads(body)
        return CopilotToken(
            token=data["token"],
            expires_at=int(data.get("expires_at", 0)),
            refresh_in=int(data.get("refresh_in", 1500)),
            endpoints=data.get("endpoints") or {},
        )

    async def resolve_api_base(self, gh_token: str, token: CopilotToken) -> str:
        """endpoints.api is authoritative; fallback per account plan."""
        api = token.endpoints.get("api")
        if api:
            return api.rstrip("/")
        try:
            resp = await self._session.get(
                f"{GITHUB_API_BASE_URL}/copilot_internal/user",
                headers=github_headers(gh_token),
            )
            body = await resp.text()
            if resp.status == 200:
                plan = (json.loads(body).get("copilot_plan") or "").lower()
                if "enterprise" in plan:
                    return "https://api.enterprise.githubcopilot.com"
                if "business" in plan:
                    return "https://api.business.githubcopilot.com"
        except Exception:
            pass
        return FALLBACK_INDIVIDUAL_API

    # --- LLM endpoints ---
    async def get_models(self, token: str, api_base: str) -> Any:
        resp = await self._session.get(
            f"{api_base}/models",
            headers=copilot_headers(
                token, self.device_id, intent="model-access",
                interaction_type="model-access"),
        )
        body = await resp.text()
        if resp.status != 200:
            raise ApiError(resp.status, body)
        return json.loads(body)

    async def chat_completions(self, token: str, api_base: str,
                               payload: dict, *,
                               vision: bool = False) -> Any:
        req_id = new_request_id()
        last = (payload.get("messages") or [{}])[-1] or {}
        initiator = "user" if last.get("role") == "user" else "agent"
        headers = copilot_headers(
            token, self.device_id, intent="conversation-agent",
            interaction_type="conversation-agent",
            request_id=req_id, vision=vision)
        headers["x-initiator"] = initiator
        return await self._session.post(
            f"{api_base}/chat/completions",
            headers=headers, data=json.dumps(payload),
        )

    async def messages(self, token: str, api_base: str, payload: dict, *,
                       vision: bool = False) -> Any:
        req_id = new_request_id()
        last = (payload.get("messages") or [{}])[-1] or {}
        if isinstance(last.get("content"), list) and any(
            b.get("type") == "tool_result" for b in last["content"]
        ):
            initiator = "agent"
        else:
            initiator = "user" if last.get("role") == "user" else "agent"
        headers = copilot_headers(
            token, self.device_id, intent="messages-proxy",
            interaction_type="messages-proxy",
            request_id=req_id, vision=vision)
        headers["x-initiator"] = initiator
        # anthropic-beta: only whitelisted values, mirrors vscode extension
        thinking = payload.get("thinking") or {}
        if thinking.get("budget_tokens") and thinking.get("type") != "adaptive":
            headers["anthropic-beta"] = "interleaved-thinking-2025-05-14"
        return await self._session.post(
            f"{api_base}/v1/messages",
            headers=headers, data=json.dumps(payload),
        )


def has_vision(payload: dict) -> bool:
    for m in payload.get("messages") or []:
        content = m.get("content")
        if isinstance(content, list):
            if any(b.get("type") == "image_url" for b in content):
                return True
            if any(b.get("type") == "image" for b in content):
                return True
    return False


if __name__ == "__main__":  # pragma: no cover - trivial smoke
    assert normalize_model_id("gpt-5.6-luna[1m]") == "gpt-5.6-luna"
    assert normalize_model_id("claude-opus-4.6[1m]") == "claude-opus-4-6"
    assert normalize_model_id("claude-opus-4-6") == "claude-opus-4-6"
    assert normalize_model_id("gemini-3.x") == "gemini-3.x"
    print("copilot.py self-check OK")