# copilot-gw

OpenAI/Anthropic-compatible gateway over GitHub Copilot free quotas, with an
account pool and rotation (OctopusX pattern: farm accounts -> pool -> rotate
on limit). Reverse-engineered from
[caozhiyuan/copilot-api](https://github.com/caozhiyuan/copilot-api) — exact
client_id, endpoints and headers in [SPEC.md](SPEC.md).

> **Unofficial / educational.** Not affiliated with GitHub. Spoon-feeds a
> non-public internal API under editor headers; heavy use of farmed accounts
> can trigger GitHub abuse detection (WAF 403 / account flags) — see
> "Known environment quirks". Use responsibly.

## Install

```bash
pip install -r requirements.txt   # aiohttp
```

## Accounts

```bash
python add_account.py --label acc1          # device flow: prints code + URL
# repeat per account; accounts land in accounts.json (gitignored)
```

Manual alternative — `accounts.json`:

```json
[{"label": "acc1", "gh_token": "gho_..."}]
```

Token chain per account: `gho_` -> short-lived Copilot API token (cached,
auto-refresh by `expires_at`/`refresh_in`, single-flight, forced refresh on
401). Headers are spoofed as `GitHubCopilotChat/0.58.0` + `vscode-chat`
integration (see SPEC.md §3).

## Run

```bash
PORT=8787 python gateway.py
```

Endpoints:

| Route | Behavior |
|---|---|
| `GET /v1/models` | live upstream list (30 min cache, static fallback) + puter catalog when enabled (`puter:` prefix) |
| `POST /v1/chat/completions` | non-stream JSON + SSE passthrough (`stream:true`); copilot native, puter via non-stream→SSE transform |
| `POST /v1/messages` | Anthropic — native upstream passthrough (non-stream + stream); puter models rejected (chat-only provider) |
| `GET /healthz` | liveness |

Pool: round-robin; on 429/403 the account goes into cooldown (`COOLDOWN_SECONDS`,
default 600) and the next account is tried; if all are cooling down -> 429.
`[1m]` model-id suffix and `claude-x.y` dotted ids are normalized before the
upstream call (`gpt-5.6-luna[1m]` -> `gpt-5.6-luna`).

Optional inbound auth: `GATEWAY_KEY=... python gateway.py` — requires
`Authorization: Bearer <key>` or `x-api-key: <key>` on every request.

Env: `PORT` (default 8787), `ACCOUNTS_PATH`, `COOLDOWN_SECONDS`,
`MODELS_TTL_SECONDS`, `LOG_LEVEL`; puter: `PUTER_AUTH_TOKEN`,
`PUTER_AUTH_TOKENS`, `PUTER_ENABLED`, `PUTER_FALLBACK`,
`PUTER_MODELS_TTL_SECONDS` (see below). A `.env` file is loaded if
python-dotenv is installed.

## Puter provider (third upstream)

Puter (`api.puter.com`) adds GPT-6 luna/astra/sol, claude-*, `openai:*` and
~1030 more models behind the same OpenAI-compat API. Driver contract verified
against the live API: `POST /drivers/call` with
`{"interface":"puter-chat-completion","driver":"ai-chat","method":"complete",
"args":{openai payload}}`, `Authorization: Bearer <auth_token>`; responses come
wrapped in `{"success":true,"result":{...}}` which the gateway unwraps
(robust parser tolerates the bare OpenAI shape too).

Enable — put your token in `.env` (DevTools → Local Storage → `auth_token`
on puter.com) and restart:

```bash
PUTER_AUTH_TOKEN=eyJ...   # in .env; PUTER_AUTH_TOKENS for a round-robin pool
```

If `PUTER_ENABLED` is unset the provider turns on automatically once a token
is present; `PUTER_ENABLED=false` keeps copilot-only routing.

Routing:

| Request model | Goes to |
|---|---|
| `puter:<id>` (e.g. `puter:gpt-6-luna`) | puter always (strips prefix) |
| anything else | copilot; with `PUTER_FALLBACK=true` models unknown to copilot *and* present in the puter catalog fall back to puter |

`GET /v1/models` lists puter models with the `puter:` prefix (catalog cached
~10 min; on fetch failure the stale list is kept and the failure only logged).
`401 token_auth_failed` → clear error
`{"error":{"message":"Puter token invalid/expired","type":"auth"}}` (status 401);
`429` rotates to the next pool token and returns 429 only when all are exhausted.

**Streaming**: `stream:true` on a `puter:` model is answered as a one-chunk
SSE transform — the gateway asks puter non-stream and emits a single
`chat.completion.chunk` + `data: [DONE]`. This is the simplest working option
because the puter driver wraps its own SSE inside the `result` envelope, so a
raw SSE passthrough would not be OpenAI-compatible. Upstream puter caps: ~30
req/10s, 3 concurrent (free tier).

## Tests

```bash
pytest test_gateway.py test_puter.py   # offline: fakes the upstream clients
```

## Known environment quirks

- **Windows DNS**: aiohttp's `ThreadedResolver` fails with "Could not contact
  DNS servers" on some hosts while `socket.getaddrinfo` works; the gateway uses
  its own resolver backed by `loop.getaddrinfo` (`make_session`).
- **GitHub WAF**: `api.github.com/copilot_internal/v2/token` returns 403
  ("For more on scraping GitHub…") from datacenter/IPs with bad reputation and
  can be flaky. The gateway treats 403 as cooldown + rotation. Run from a clean
  residential/VPN egress for stable operation.
- Port 8787 can collide with other lab services (127.0.0.1-pinned) — use
  `PORT` to pick a free one.

No secrets in code; `accounts.json` and `.env` are gitignored.