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

## Providers

| Prefix | Upstream | Credential | Chat | `/v1/messages` |
|---|---|---|---|---|
| *(none)* | see Routing below | — | — | — |
| `github:` | GitHub Copilot (`api.githubcopilot.com`) | `accounts.json` (`gho_` device flow) | native JSON + SSE | native passthrough |
| `puter:` | Puter (`api.puter.com` `/drivers/call`) | `PUTER_AUTH_TOKEN` (pool: `PUTER_AUTH_TOKENS`) | JSON; `stream:true` → one-chunk SSE transform | 400 (chat-only) |
| `vercel:` | Vercel AI Gateway (`ai-gateway.vercel.sh/v1`) | `AI_GATEWAY_API_KEY` | JSON + real OpenAI SSE passthrough | 400 (chat-only) |

### Routing

Copilot is the **last resort**, not the default. A model with no prefix on
`/v1/chat/completions` is resolved in this order:

| # | Provider | Fires when |
|---|---|---|
| 1 | `vercel` | `VERCEL_ENABLED` (default on) **and** `AI_GATEWAY_API_KEY` set **and** the id is in the Vercel catalog — exact `vendor/model`, or the catalog's `vendor/<model>` suffix (`claude-opus-4.5` → `anthropic/claude-opus-4.5`), or already shaped `vendor/model` |
| 2 | `puter` | `PUTER_ENABLED=true` **and** `PUTER_FALLBACK=true` **and** the id is in the Puter catalog but not in the copilot one |
| 3 | `copilot` | everything else — the id is normalized (`claude-opus-4.5` → `claude-opus-4-5`) and sent to GitHub |

`github:<id>` skips 1–2 and forces copilot (prefix stripped, then normalized).
Explicit `vercel:` / `puter:` prefixes behave exactly as before. `/v1/messages`
is copilot-only — puter and vercel are chat upstreams, so the default chain
never hijacks an Anthropic-shaped body. Without any provider key the gateway is
copilot-only, 1:1 as it used to be.

`GET /v1/models` merges the enabled providers' catalogs under their prefixes.
Missing credentials degrade to copilot-only: the provider stays disabled,
nothing crashes.

```bash
# no prefix → Vercel AI Gateway (catalog hit), not copilot
curl -s localhost:8787/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"claude-opus-4.5","messages":[{"role":"user","content":"hi"}]}'

# explicit copilot
curl -s localhost:8787/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"github:gpt-5.6-luna","messages":[{"role":"user","content":"hi"}]}'
```

Endpoints:

| Route | Behavior |
|---|---|
| `GET /v1/models` | live copilot list (30 min cache, static fallback) + enabled puter (`puter:`) and vercel (`vercel:`) catalogs |
| `POST /v1/chat/completions` | non-stream JSON + SSE passthrough (`stream:true`); copilot/vercel native SSE, puter via non-stream→SSE transform |
| `POST /v1/messages` | Anthropic — native copilot passthrough (non-stream + stream); puter and vercel models rejected (chat-only providers) |
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
`PUTER_MODELS_TTL_SECONDS`; vercel: `AI_GATEWAY_API_KEY`, `VERCEL_ENABLED`,
`VERCEL_MODELS_TTL_SECONDS`, `VERCEL_TIMEOUT_SECONDS` (see below). A `.env`
file is loaded if python-dotenv is installed.

### Quickstart (all three providers)

```bash
pip install -r requirements.txt
python add_account.py --label acc1                    # copilot: device flow
cp .env.example .env                                  # then fill in keys:
#   PUTER_AUTH_TOKEN=eyJ...        puter.com -> DevTools -> Local Storage
#   AI_GATEWAY_API_KEY=ai_...      https://vercel.com/dashboard/ai-gateway
python gateway.py                                     # PORT=8787 by default
curl -s localhost:8787/v1/models | python -m json.tool | grep '"id"' | head
curl -s localhost:8787/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"vercel:anthropic/claude-opus-4.5","messages":[{"role":"user","content":"hi"}]}'
```

Without any provider key the gateway still runs copilot-only; a `vercel:`
request then answers 400 with `{"type":"config"}` instead of crashing.


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
| `github:<id>` | copilot always (see Routing above) |
| anything else | vercel if its catalog knows the id, else copilot; with `PUTER_FALLBACK=true` models unknown to copilot *and* present in the puter catalog (and not matched by vercel) fall back to puter |

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

## Vercel AI Gateway provider (`vercel:` prefix)

[Vercel AI Gateway](https://vercel.com/docs/ai-gateway) is a plain
OpenAI-compatible API at `https://ai-gateway.vercel.sh/v1`: dynamic catalog
(`GET /models`, ~391 models, ids are `vendor/model` — `anthropic/claude-opus-4.5`,
`openai/gpt-5.3-codex`, `google/gemini-3-pro`…) and `POST /chat/completions`.
Free credits: **$5 per month on a new key, no card required**.

```bash
AI_GATEWAY_API_KEY=ai_gw_...   # https://vercel.com/dashboard/ai-gateway
```

The key is optional: with no key the provider is disabled, `/v1/models` lists
no `vercel:` entries and a `vercel:` model answers 400 `{"type":"config"}` —
copilot keeps working untouched. `VERCEL_ENABLED=false` forces it off even
with a key present.

Routing: `vercel:<vendor>/<model>` (e.g. `vercel:anthropic/claude-opus-4.5`)
always goes upstream with the prefix stripped. An **unprefixed** id that the
catalog knows (exact, `vendor/<id>` suffix, or already `vendor/model`) is
default-routed here too — see Routing above; `github:` forces copilot instead.
Catalog is cached 1 h (`VERCEL_MODELS_TTL_SECONDS`); a fetch failure keeps the
stale list, or answers `[]` when nothing was cached — it never raises.

**Streaming**: `stream:true` is a raw SSE passthrough — the upstream already
emits OpenAI-format `chat.completion.chunk` events, so no transform is needed
(unlike puter's envelope). `401` → `{"error":{"message":"Vercel AI Gateway key
invalid/missing","type":"auth"}}`, `429` → `type: rate_limit`, transport/parse
failures → `502`.

## Tests

```bash
pytest test_gateway.py test_puter.py test_vercel.py   # offline: fakes the upstream clients
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