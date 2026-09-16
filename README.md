# llm-relay

**One file, no dependencies, one OpenAI-compatible endpoint in front of many providers and their free tiers.**

[![CI](https://github.com/GerateGuo/llm-relay/actions/workflows/ci.yml/badge.svg)](https://github.com/GerateGuo/llm-relay/actions/workflows/ci.yml)
![license: MIT](https://img.shields.io/badge/license-MIT-blue)
![python: 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)

`llm-relay` is a single-file, standard-library-only OpenAI-compatible relay. It exposes one
`POST /v1/chat/completions` and absorbs everything behind it: several providers, several API keys per
provider, per-key rate limits, cooldowns with backoff, chained failover, response-shape normalization
and `json_schema` handling. Change a key or a model without touching — or restarting — the caller.

It ships with a built-in ops dashboard (candidate chain, key pool, request log, usage, capability
probes, settings) and is tuned out of the box for **long-prompt structured extraction**, where the
per-attempt timeout, the total request budget, the minimum timeout for reasoning models and the
`max_tokens` floor for schema requests are all derived from measurements instead of defaults.

```
your app ──► llm-relay:9110 ──► candidate #1  (provider A / model X / key 1)
   (one URL,      │             candidate #2  (provider A / model Y / key 2)
  one model name) │             candidate #3  (provider B / model Z / key 1)
                  └── cooldowns, rpm buckets, retries, schema validation, usage log
```

## Why another relay?

| | llm-relay | LiteLLM / one-api & co. |
|---|---|---|
| Footprint | 1 file, Python stdlib, `python3 llm_relay.py` | service + deps + DB |
| Free-tier quota governance | first-class: per-key rpm buckets, per-key / per-model / per-provider cooldowns, backoff caps | usually a global rate limit |
| Ops dashboard | built in, zero extra deps | separate UI / DB |
| Structured extraction | strict `json_schema` validation, schema-echo rejection, hard 502 instead of a bad 200 | pass-through |
| Multi-tenant billing, accounts, invoices | **not attempted — by design** | yes |

If you need a full gateway with tenants and cost accounting, use one of those. This one is for the
case where you want a small, auditable process that squeezes a pile of free keys hard and never
returns junk to your pipeline.

## Features

- **Chained failover.** Every model has a `chain` number; candidates are built by filtering, sorting
  by `chain`, then truncating to `request.max_candidates`. A candidate that fails for a *retryable*
  reason simply hands over to the next one.
- **Per-key throttle.** A token bucket per provider enforces `rpm`; a semaphore enforces
  `max_concurrency`; requests that would exceed either wait up to `request.queue_timeout_s` and then
  move on — they never silently hit the upstream.
- **Cooldowns that stay bounded.** 429 honours `Retry-After`/reset headers; 401/403 disables the key;
  402 or a "quota exhausted" body parks the key for an hour; 5xx/timeouts cool down the *provider*,
  with `provider_cooldown_cap_s` (default 120 s) for a single failure and
  `provider_cooldown_hard_cap_s` (default 300 s) only after `provider_escalate_after_failures`
  failures inside a short window. One jittery timeout can no longer park a whole provider for
  15 minutes.
- **Key-level isolation.** A model that ran out of quota cools down *its key*, not the provider —
  healthy models on other keys keep serving.
- **Response normalization.** Empty `content` but non-empty `reasoning`/`reasoning_content` is
  promoted to `content`; missing `usage` is backfilled with zeros; `tool_calls` pass through.
- **JSON that is actually correct.** When the caller asks for `json_schema`, the reply is validated
  against that schema (required keys, types, enums, `additionalProperties`, nested items). Upstreams
  that echo the schema back as `content`, or answer with a `properties` fragment, are rejected — and
  if no candidate can produce a conforming object the relay answers **502
  `relay_unusable_content`** rather than a 200 carrying junk.
- **Named routes.** A model name can map to its own chain and policy (`max_candidates`,
  `validate_json`, `schema_min_max_tokens`, `free_only`) instead of the global behaviour.
- **Callers.** Named callers authenticate with a key from the environment and get their own `rpm`,
  `daily_tokens` budget and `allow_routes` list. `auth.mode` is either `loopback_trust` (single
  machine, default) or `require_key`.
- **Accounting.** Every attempt — success or failure — appends one JSON line to `usage.jsonl`
  (`ts / alias / caller / provider / model / key_index / http / latency_ms / attempts[] / verdict /
  *_tokens`). Keys are recorded by index and fingerprint, never by value. `GET /admin/usage` groups
  by caller / route / provider / model over 1 h, 24 h or 7 d; Prometheus `/metrics` is available
  behind `usage_log.metrics: true`.
- **Built-in dashboard** (`llm-relay-dashboard.py`, also dependency-free) — candidate chain, key
  pool, models, request log, usage, capability probes, callers, settings, appearance. Loopback is
  password-less; anything else needs the access key and is **read-only** by default.
- **Optional integrations.** A "local fallback" window for a local model server, and an
  `integrations.upstream` block used to surface a companion service inside the dashboard (see
  `examples/hindsight/`). Both are off unless you configure them.

## Quick start

```bash
git clone https://github.com/GerateGuo/llm-relay.git
cd llm-relay

python3 llm_relay.py --init        # writes config.json from config.example.json (refuses to overwrite)
$EDITOR config.json                # point providers at your endpoints, list the env var names of your keys
$EDITOR keys.env                   # EXAMPLE_OPENAI_KEY=sk-...   (chmod 600; already in .gitignore)

python3 llm_relay.py --check       # config check + the candidate order it will use, without serving
python3 llm_relay.py               # relay on 127.0.0.1:9110
```

Then call it like any OpenAI-compatible endpoint:

```bash
curl -s http://127.0.0.1:9110/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"default","messages":[{"role":"user","content":"hello"}]}'
```

`model` is a **route name** (`default` unless you change `default_alias`), or an exact
`provider/model` pair. `GET /v1/models` lists both, together with each model's `chain` and current
availability.

Optionally start the dashboard (default `0.0.0.0`, access-key gated and read-only from non-loopback
addresses):

```bash
python3 llm-relay-dashboard.py                 # http://127.0.0.1:9111
python3 llm-relay-dashboard.py --host 127.0.0.1
```

No pip install, no database, no build step. Python 3.9 or newer.

## How a request is routed

1. **Resolve the model name.** In order: a `routes` entry → an exact `provider/model` name → the
   `default_alias` when `model` is missing → otherwise **HTTP 400** listing the names you may use.
2. **Build the candidate list.** Providers with `enabled: false`, providers with no usable key
   (unknown/disabled/cooling), models with `disabled`, and — for routes with `policy.free_only` —
   anything not explicitly marked `free: true` are filtered out. The rest are sorted by `chain`
   ascending and truncated to the effective `max_candidates`.
3. **Try candidates in order** until the total budget (`request.total_budget_s`) runs out. Each
   attempt gets `request.per_attempt_timeout_s` (raised to `request.reasoning_min_timeout_s` for
   models declared `caps.reasoning_only`) and records its own verdict.
4. **Decide whether the body is usable.** If the caller asked for JSON, the body must parse *and*
   conform to the schema; the echoed schema itself is rejected. Non-conforming bodies count as a
   failed attempt and the next candidate is tried.
5. **Answer.** The winning upstream body is returned unchanged apart from the normalization above.
   If nothing qualified, the relay answers 502 with a reason and the attempt chain — it will not
   hand your pipeline a 200 with bad content.

**Capabilities are declared, never guessed.** Each model carries `caps` (`json_schema`:
`native` | `degradable` | `none`, `tools`, `reasoning_only`) and optional `params`
(`strip_temperature`, `max_tokens_multiplier`, …). A `degradable` model is called with the schema
inlined into the prompt as a fallback — and is still validated the same way, because the fallback
changes *how* JSON is requested, not *what shape* is expected.

## Configuration

`config.json` is generated by `--init`, is **not** committed (only `config.example.json` is), and
hot-reloads on save (no restart needed). Top-level keys:

| Key | Purpose |
|---|---|
| `listen` | `{host, port}` — defaults to `127.0.0.1:9110` |
| `default_alias` | route used when a request omits `model` (default `default`) |
| `local_token` | legacy ops token; when set, `/admin/*` also requires `Authorization: Bearer <token>` |
| `auth` | `{mode: "loopback_trust" \| "require_key"}` — who must present a caller key |
| `callers` | named identities: `key_env` (or inline `key`, keep that out of git), `rpm`, `daily_tokens`, `allow_routes`, `note` |
| `request` | timeouts, budgets, candidate count, JSON validation, cooldown caps |
| `concurrency` | `{global: N}` — process-wide in-flight limit |
| `usage_log` | `{enabled, path, max_mb, keep, metrics}` — one JSON line per attempt, rotated |
| `key_state` | persisted enable/disable + cooldown state written by the dashboard |
| `providers` | the provider list (see below) |
| `routes` | named routes with their own chain and policy |
| `local_fallback` | optional local model server, optionally limited to a `window` (e.g. `01:00-07:00`) |
| `integrations` | optional companion-service integration surfaced in the dashboard |

A provider:

```jsonc
{
  "name": "example-openai",
  "enabled": true,
  "base_url": "https://api.example.com/v1",
  "wire": { "max_tokens_param": "max_tokens", "extra_headers": {} },
  "keys": ["EXAMPLE_OPENAI_KEY"],          // env var names, resolved from keys.env
  "rpm": 10,                               // local token bucket, measured against your free tier
  "max_concurrency": 2,
  "key_cooldown_s": 120,
  "provider_cooldown_s": 180,
  "cooldown_backoff": { "initial_s": 60, "max_s": 900, "factor": 2 },
  "free": false,                           // mark free tiers explicitly — never guessed from the name
  "models": [
    { "id": "example-chat", "chain": 10,
      "caps": { "json_schema": "native", "tools": true, "reasoning_only": false }, "params": {} }
  ]
}
```

The full field table lives in [`docs/providers.md`](docs/providers.md); a working two-provider
example (one HTTP provider plus a local mock) is [`config.example.json`](config.example.json).

Routes and callers:

```jsonc
"routes": {
  "default":          { "chain": "auto", "policy": {} },
  "cheap":            { "chain": "auto", "policy": { "free_only": true, "max_candidates": 2 } },
  "strict-json":      { "chain": ["example-openai/example-chat", "local-mock/mock-json"],
                        "policy": { "validate_json": true, "schema_min_max_tokens": 1024 } }
},
"callers": {
  "my-app":  { "key_env": "RELAY_CALLER_MY_APP", "rpm": 60, "daily_tokens": 2000000,
               "allow_routes": ["default", "strict-json"] }
}
```

Environment variables used by the two programs:

| Variable | Default | Meaning |
|---|---|---|
| `LLM_RELAY_BASE` | `http://127.0.0.1:9110` | relay URL the dashboard talks to |
| `LLM_RELAY_KEY_FILE` | `<script dir>/access-key.txt` | dashboard access-key file (generated on first start) |
| `HS_DASH_CONFIG_JSON`, `HINDSIGHT_CP_KEY_FILE`, `HINDSIGHT_CP_ACCESS_KEY`, `HINDSIGHT_DASH_BASE`, `HINDSIGHT_CP_BASE` | — | only for the optional companion-service integration |

Both programs bypass HTTP proxies for loopback addresses — a system-wide proxy (Clash, Charles, …)
otherwise turns `127.0.0.1` calls into 502s.

## CLI

```bash
python3 llm_relay.py [--config config.json] [--keys keys.env] [--host H] [--port P] [--check] [--init]
python3 llm-relay-dashboard.py [--host H] [--port P] [--key-file F] [--relay URL] [--allow-remote-write]
python3 mock_provider.py [--host H] [--port P] [--scenario normal|rate_limit|timeout|empty_content|schema_echo|slow] [--hang-s S]
python3 -m unittest test_relay                     # the test suite (no network, no deps)
python3 scripts/sanitize_check.py                  # personal-path / plaintext-secret guard (used by CI)
```

`--check` validates `config.json` and prints the candidate order it would use, without serving
anything; a broken field is reported as "field path + expectation + what you wrote" and disables that
provider instead of refusing to start.

## HTTP API

| Method | Path | Notes |
|---|---|---|
| POST | `/v1/chat/completions` | OpenAI-compatible; streaming supported |
| GET | `/v1/models` | alias + `provider/model` entries with `chain` / `available` |
| GET | `/health` | `{"status": "healthy"\|"degraded", "candidates_healthy": N}`; 503 when no candidate is usable |
| GET | `/` | minimal dark status table (same data as `/status`) |
| GET | `/status` | counters, per-provider/per-key/per-model state, last 20 requests, effective request settings |
| GET | `/admin/config` | on-disk config + mtime; keys appear as env name + fingerprint only |
| POST | `/admin/config` | `{"patch": {...}}` — read from disk, apply only the given keys, back up, write atomically, hot-reload |
| POST | `/admin/reorder` | `{"order": ["provider/model", ...]}` — rewrites `chain` as 1..N |
| POST | `/admin/model` | `{"action": "add"\|"update"\|"remove", ...}` — touches `providers[].models` only |
| POST | `/admin/key` | `{"provider", "env_name", "secret"}` — returns a fingerprint, never the value |
| POST | `/admin/key/state` | enable/disable or clear a cooldown |
| POST | `/admin/key/replace` | replace a key value in place; sha256 duplicate detection; other lines untouched |
| POST | `/admin/key/remove` | `{"provider", "env_name", "confirm": "REMOVE"}` |
| POST | `/admin/probe` | `{"provider", "model"}` — chat + strict `json_schema` + tools, three real requests |
| POST | `/admin/reload` | force config/key reload (same as SIGHUP) |
| POST | `/admin/restart` | `{"confirm": "RESTART"}` — replies first, then exits so the supervisor restarts it |
| GET | `/admin/requests` | `usage.jsonl` detail: `?limit=&offset=&provider=&model=&verdict=&since=&probe=1` |
| GET | `/admin/usage` | `?group=caller\|route\|provider\|model&window=1h\|24h\|7d`, or `?days=7\|30\|90` for the per-day view |
| GET | `/admin/callers` | caller view: configured?, fingerprint, rpm used, tokens today, allowed routes |
| GET | `/admin/upstream` | companion-service health + start-script status; `{"configured": false}` when unset |
| POST | `/admin/upstream/rollback` | `{"confirm": "ROLLBACK"}` |
| GET | `/metrics` | Prometheus text format, only when `usage_log.metrics` is true (404 otherwise) |

Every `/admin/*` route requires a loopback source (403 otherwise) and, when `local_token` is set, the
matching bearer token. The dashboard and the internal capability probe use the `probe` alias, which
bypasses route resolution and consumes no caller quota.

## Dashboard

`llm-relay-dashboard.py` renders a single self-contained page (no CDN, no framework, no external
requests):

- **Overview / chain / keys / models / requests / usage / capability matrix / callers / settings /
  appearance** — each tab reads live from the relay over loopback.
- **Access key gate.** Loopback is password-less; a LAN/mobile visitor must supply `?k=<key>`, which
  then sticks as a 30-day cookie. The key file is generated on first start with mode 600.
- **Remote is read-only.** Every write path (config patch, key management, restart) is rejected from
  non-loopback addresses unless you explicitly pass `--allow-remote-write`. Write payloads are
  applied the safe way: read the newest file from disk, patch only the keys you changed, keep a
  timestamped backup, write atomically, then hot-reload.
- **Capability probe** issues three real requests per model (plain chat, strict schema, tools) so you
  can see which model actually does what instead of trusting a name.

The relay itself serves a much smaller status page at `/` if you do not want the dashboard.

## Testing

```bash
python3 -m unittest test_relay            # 106 tests, no network, no dependencies
python3 dev/dashboard_smoke_test.py       # dashboard self-check (needs the live services + headless Chrome)
```

`test_relay.py` starts the bundled mock upstream in-process and covers routing, cooldowns, budgets,
rpm accounting, schema validation (including the schema-echo and fragment cases), key management and
the red lines around `config.json`. It runs unmodified in CI on 3.9 / 3.11 / 3.13.

`dev/dashboard_smoke_test.py` is a local harness, not CI material: it drives a real browser over CDP
to click through the dashboard. Note that its exit code is not the verdict — read the printed
`OK` / `FAILED` line.

## Mock upstream

`mock_provider.py` is a fake provider used by the tests, the demo screenshots and manual poking. The
behaviour is selected by the **model name** in the request (`mock-json`, `mock-429`, `mock-cot`, …)
and additionally by `--scenario`, which can inject a rate limit, a hang, an empty body, a schema echo
or a slow response. It speaks several vendor path shapes (`/v1/...`, `/openai/v1/...`,
`/proxy/llm/...`) so routing code can be exercised without a single real key or any quota.

```bash
python3 mock_provider.py --port 9199 --scenario rate_limit      # then point a provider at it
```

## Examples

- [`examples/launchd/`](examples/launchd/) — launchd templates for keeping the relay (and the
  dashboard) alive on macOS. Replace the placeholders, install, done.
- [`examples/hindsight/`](examples/hindsight/) — using the relay in front of an agent memory service:
  an `integrations.upstream` snippet, its dashboard tab, and a rollback script. Optional; the core
  never depends on it.

## Tuning notes

All of these are in `request` and all of them were measured on a real long-prompt extraction
workload (≈7 KB system prompt, strict schema, reasoning models), not derived from theory:

| Setting | Default | Why |
|---|---|---|
| `per_attempt_timeout_s` | 60 | a real extraction on a reasoning model takes 16–50 s; 25 s and 45 s both time out in practice |
| `reasoning_min_timeout_s` | 45 | floor for models that think before answering |
| `total_budget_s` | 110 | must stay **below your caller's own request timeout** (the reference caller uses 120 s) |
| `max_candidates` | 3 | enough failover to survive two bad keys, few enough to stay inside the budget |
| `schema_min_max_tokens` | 1024 | below this, thinking models burn the budget on reasoning and echo the schema back |
| `provider_cooldown_cap_s` / `_hard_cap_s` | 120 / 300 | one timeout must not remove a provider for 15 minutes |
| `provider_escalate_after_failures` | 3 | only repeated failures justify the hard cap |

## Security

Loopback by default, keys only from the environment or a local file, no plaintext keys in logs or
API responses, remote dashboard access read-only. The full threat model — including when to switch
`auth.mode` to `require_key` — is in [`SECURITY.md`](SECURITY.md). Please report vulnerabilities
through GitHub's private vulnerability reporting rather than a public issue.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Summary: standard library only, tests green
(`python3 -m unittest test_relay`, count only ever goes up), never commit `config.json`, `keys.env`
or `access-key.txt`.

## License

MIT — see [`LICENSE`](LICENSE).

---

[中文说明 →](README.zh-CN.md)
