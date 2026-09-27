# GitHub Copilot Pool Gateway — SPEC (из copilot-api, ветка dev)

Источник: `ref_copapi/copilot-api-dev` (caozhiyuan/copilot-api, main.tar.gz, HEAD от 2026-09).
Архитектура: GitHub device-flow OAuth → gho_ токен → короткоживущий Copilot API-токен →
OpenAI-compat запросы на `api.githubcopilot.com` (хост из `endpoints.api` токена — авторитетный).

---

## 1. Device Flow (регистрация/логин аккаунта, VS Code OAuth app)

| Параметр | Значение |
|---|---|
| client_id (VS Code) | `Iv1.b507a08c87ecfe98` |
| client_id (opencode, альтернатива) | `Ov23li8tweQw6odWQebz` |
| scope | `read:user` (join по пробелу) |

```http
POST https://github.com/login/device/code
Content-Type: application/json
Accept: application/json

{"client_id": "Iv1.b507a08c87ecfe98", "scope": "read:user"}
```
→ `200` `{"device_code", "user_code", "verification_uri", "expires_in", "interval"}`

Поллинг:
```http
POST https://github.com/login/oauth/access_token
Content-Type: application/json
Accept: application/json

{"client_id": "Iv1.b507a08c87ecfe98",
 "device_code": "...",
 "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}
```
→ `{"access_token": "gho_...", "token_type": "bearer", "scope": "read:user"}`.
Non-200 / ещё нет `access_token` → спать `(interval + 1) * 1000` мс и повторять (бесконечно).

## 2. Copilot-токен (короткоживущий API-ключ)

```http
GET https://api.github.com/copilot_internal/v2/token
authorization: token {gho_...}
user-agent: GitHubCopilotChat/0.58.0
x-github-api-version: 2025-04-01
x-vscode-user-agent-library-version: electron-fetch
```
→ `{"token": "sk-...", "expires_at": <unix-сек>, "refresh_in": <сек>,
     "endpoints": {"api": "https://api.githubcopilot.com", "proxy": ..., "telemetry": ...}}`

- **TTL**: `expires_at` — unix-секунды; `refresh_in` — через сколько секунд обновлять.
- **endpoints.api — авторитетный роутинг** для выданного токена (комментарий в коде:
  `/copilot_internal/user` может рекламировать другой host → 421 Misdirected Request).
- Fallback без `endpoints.api`: `individual` → `https://api.githubcopilot.com`,
  `business`/`enterprise` → `https://api.{type}.githubcopilot.com`.
- Тип аккаунта: `GET https://api.github.com/copilot_internal/user` (те же github-заголовки)
  → `copilot_plan` содержит `enterprise`/`business`, иначе `individual`.
  Там же квоты: `quota_snapshots {chat, completions, premium_interactions}` с
  `percent_remaining`, `overage_permitted`, `quota_reset_date`.

**Auto-refresh (паттерн из `lib/token.ts`)**: фоновый цикл, поллинг раз в 15 c;
обновление в момент `now + refresh_in*1000 - 60_000` (буфер 60 c); ошибка →
экспоненциальный backoff 15 c → кап 600 c, jitter 15 c.
```ts
REFRESH_POLL_INTERVAL_MS = 15_000; EARLY_REFRESH_BUFFER_MS = 60_000;
RETRY_REFRESH_DELAY_MS = 15_000; MAX_RETRY_REFRESH_DELAY_MS = 600_000;
RETRY_REFRESH_JITTER_MS = 15_000; MIN_REFRESH_DELAY_MS = 1_000
```

## 3. Chat Completions

```http
POST {endpoints.api}/chat/completions
```

Заголовки (дословно, `githubCopilotHeaders` + доп.):
```
Authorization: Bearer {copilot_token}
content-type: application/json
copilot-integration-id: vscode-chat
editor-device-id: {uuid4().lower(), персистентный per-machine}
editor-version: vscode/1.130.0          # fallback getVSCodeVersion()
editor-plugin-version: copilot-chat/0.58.0
user-agent: GitHubCopilotChat/0.58.0
openai-intent: conversation-agent
x-github-api-version: 2026-06-01
x-request-id: {uuid}
x-agent-task-id: {тот же uuid}
x-vscode-user-agent-library-version: electron-fetch
x-interaction-type: conversation-agent
x-initiator: user | agent               # последнее сообщение role==user → user, иначе agent
# опционально:
copilot-vision-request: true            # если в messages есть image_url
vscode-machineid: ... / vscode-sessionid: ...
x-interaction-id: {sessionId}           # session affinity
x-interaction-type: conversation-subagent
x-interaction-type: conversation-compaction + openai-intent: conversation-agent  # compact
```

Тело — OpenAI `chat.completions` passthrough (messages, model, stream, max_tokens,
max_completion_tokens, stop, n, temperature, top_p, tools, tool_choice, response_format,
seed, user, stream_options, thinking_budget, reasoning_effort, top_k, prompt_cache_key,
parallel_tool_calls; message content может быть string | [{type:text|image_url|file}],
`copilot_cache_control: {type:"ephemeral"}`).

Ответ:
- non-stream: OpenAI-форма + `copilot_usage {total_nano_aiu}`, в message:
  `reasoning_text/reasoning_content/reasoning_opaque/reasoning`.
- stream: SSE `data: {chat.completion.chunk}` — delta с content/role/tool_calls/
  `reasoning_text/reasoning_content/reasoning_opaque/reasoning`.

**Rate limits** (заголовки ответа):
```
x-usage-ratelimit-session: rem=..&rst=<ISO-date>      # 5h-окно
x-usage-ratelimit-weekly:  rem=..&rst=<ISO-date>
```
(URLSearchParams: keys `rem`, `rst`). Квоты: session (5h) + weekly. Ошибки: 429/403 — исчерпание квоты/WAF.

## 4. Модели

```http
GET {endpoints.api}/models
Authorization: Bearer {copilot_token}
copilot-integration-id: vscode-chat
editor-device-id: ...
editor-version: vscode/1.130.0
editor-plugin-version: copilot-chat/0.58.0
user-agent: GitHubCopilotChat/0.58.0
openai-intent: model-access
x-github-api-version: 2026-06-01
x-request-id: {uuid}
x-agent-task-id: {uuid}
x-vscode-user-agent-library-version: electron-fetch
x-interaction-type: model-access
# без content-type, без x-interaction-id
```
→ `{"data": [Model...], "object": "list"}`; `Model = {id, name, vendor, version,
model_picker_enabled, preview, object, capabilities: {family, type, tokenizer,
 limits: {max_context_window_tokens, max_output_tokens, vision: {...}},
 supports: {tool_calls, streaming, vision, reasoning_effort, ...}},
 policy?: {state: "enabled"|"disabled", terms}, supported_endpoints?: [...]}`

- **Динамический список** (из апстрима). `models.json` в репозитории — только
  dev/фикстура (slugs: gpt-5.4, gpt-5.5, gpt-5.6-luna/sol/terra, gpt-6-astra/luna/sol,
  gpt-daybreak-*-latest, codex-auto-review).
- Фильтр при кэшировании: `policy.state !== "disabled" && model_picker_enabled`.
- Refresh ~каждые 30 мин (MODELS_REFRESH_BASE_MS = 30*60*1000, +jitter).
- Клиентский id: Claude-модели — точки в версии → дефис (`claude-sonnet-4.6` →
  `claude-sonnet-4-6`); суффикс `[1m]` если `max_context_window_tokens >= 1_000_000`
  (поле `claude_model_id`).

## 5. Anthropic /v1/messages

```http
POST {endpoints.api}/v1/messages
```
Те же chat-заголовки + правила:
- `x-initiator`: user, если последнее сообщение user без `tool_result`, иначе agent.
- `anthropic-beta`: разрешены только `interleaved-thinking-2025-05-14`,
  `context-management-2025-06-27`, `advanced-tool-use-2025-11-20`; ставится когда есть
  thinking.budget_tokens (non-adaptive) или клиент передал.
- Если `metadata.user_id` = `{safety_identifier}:{session_id}` — заголовки как
  Claude-Agent, перегенерация id:
  `x-agent-task-id`/`x-request-id` = новый uuid, `x-interaction-type: messages-proxy`,
  `openai-intent: messages-proxy`, `user-agent: vscode_claude_code/2.1.112 (external,
  sdk-ts, agent-sdk/0.2.112)`, `copilot-integration-id` удаляется.
- Тело — Anthropic-формат passthrough (system, messages[{role, content[]}], max_tokens,
  stream, thinking, tools, metadata). Ответ — Anthropic-формат; stream — SSE
  (`event: message_start/content_block_delta/...`), passthrough.
- Известный гейт апстрима: claude-opus-4.8 + Claude-Code-identity → 403 WAF;
  дефолтная identity (vscode-chat + GitHubCopilotChat UA + conversation-agent) — 200.

## 6. Обработка ошибок / ротация (для пула)

- 401 от апстрима → copilot-токен протух → принудительный refresh раз, повтор.
- 429/403/quota (x-usage-ratelimit-* rem=0) → аккаунт в cooldown, следующй из пула.
- HTTPError наружу, тело апстрима пробрасывается клиенту как есть.

## 7. Константы (дословно)

```ts
GITHUB_CLIENT_ID = "Iv1.b507a08c87ecfe98"
OPENCODE_GITHUB_CLIENT_ID = "Ov23li8tweQw6odWQebz"
GITHUB_APP_SCOPES = "read:user"
GITHUB_BASE_URL = "https://github.com"
GITHUB_API_BASE_URL = "https://api.github.com"
COPILOT_VERSION = "0.58.0"
EDITOR_PLUGIN_VERSION = "copilot-chat/0.58.0"
USER_AGENT = "GitHubCopilotChat/0.58.0"
API_VERSION = "2026-06-01"              // x-github-api-version на copilot-вызовах
GITHUB_API_VERSION_GH = "2025-04-01"   // x-github-api-version на api.github.com
VSCODE_VERSION_FALLBACK = "1.130.0"
CLAUDE_AGENT_USER_AGENT = "vscode_claude_code/2.1.112 (external, sdk-ts, agent-sdk/0.2.112)"
copilot-integration-id = "vscode-chat"
openai-intent: "conversation-agent" | "model-access" | "messages-proxy"
x-interaction-type: "conversation-agent" | "model-access" | "messages-proxy" | "conversation-subagent" | "conversation-compaction"
device id: uuid4().lower()  // Windows: реестр \SOFTWARE\Microsoft\DeveloperTools deviceid; *nix: файл
```

## 8. Vercel AI Gateway — четвёртый провайдер (`vercel:`)

<a name="vercel"></a>
### 8.1 Контракт

OpenAI-совместимый API, base `https://ai-gateway.vercel.sh/v1` (переопределяется
`VERCEL_AI_GATEWAY`). Проверено живьём 2026-09-27:

```http
GET https://ai-gateway.vercel.sh/v1/models
-> 200 {"object":"list","data":[{"id":"anthropic/claude-opus-4.5","object":"model",
     "created":<unix>,"owned_by":"anthropic","name":"Claude Opus 4.5", ...}]}
```

Каталог отдаётся без ключа (200, 391 модель на момент проверки). id моделей —
`vendor/model`: `anthropic/claude-opus-4.5`, `openai/gpt-5.3-codex`,
`google/gemini-3.1-pro-preview`. Каталог динамический — единственный источник
истины, хардкода списка в репо нет.

```http
POST https://ai-gateway.vercel.sh/v1/chat/completions
Authorization: Bearer {AI_GATEWAY_API_KEY}
content-type: application/json

{"model":"anthropic/claude-opus-4.5","messages":[...],"stream":true}
```

Ключ из дашборда `https://vercel.com/dashboard/ai-gateway`, env
`AI_GATEWAY_API_KEY`. Free-кредиты: $5/месяц на новый ключ, без карты.
`stream:true` -> обычный OpenAI SSE (`data: {chat.completion.chunk}` …
`data: [DONE]`), поэтому в шлюзе это сырой passthrough (`_stream_pass`), в
отличие от puter с его конвертом `{"success":true,"result":{...}}`.

<a name="vercel-anchors"></a>
### 8.2 Якоря в коде

| Якорь | Что делает |
|---|---|
| `vercel.py::vercel_key` / `vercel_enabled` | читают `AI_GATEWAY_API_KEY`; без ключа провайдер выключен — деградация в copilot-only, кода не падает |
| `vercel.py::VercelGateway.chat(model, messages, stream=True, **extra)` | один `POST /v1/chat/completions`; при `stream=True` возвращает сырой ответ (закрывает шлюз), иначе текст |
| `vercel.py::VercelGateway.list_models()` | кэш каталога, TTL 1ч (`VERCEL_MODELS_TTL_SECONDS`); сетевой сбой -> stale-кэш или `[]`, наружу исключений не выпускает |
| `vercel.py::VercelGateway.get_models` / `openai_model_entry` | OpenAI-payload `/v1/models` с префиксом `vercel:` |
| `gateway.py::_proxy_llm`, блок `# --- vercel routing (fourth provider) ---` | `vercel:<id>` -> upstream id (strip) до copilot-цикла; без ключа 400 `{"type":"config"}`; `/v1/messages` 400 |
| `gateway.py::_vercel_proxy` | JSON + SSE passthrough; 401 -> `type:auth`, 429 -> `type:rate_limit`, транспорт/парсинг -> 502 |
| `gateway.py::_models_handler` | слияние каталогов puter и vercel в `/v1/models`; copilot-список не меняется |
| `gateway.py::build_app(pool, puter, vercel)` | инъекция провайдера для тестов + авто-конфиг из env в `on_startup` |
| `test_vercel.py` | офлайн-тесты на фейке транспорта (`FakeSession`/`FakeResp`): роутинг, деградация без ключа, сбой каталога, стрим, 400 на `/v1/messages` |