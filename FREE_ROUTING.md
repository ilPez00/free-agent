# Free LLM routing policy — the sorted pool

Verified live 2026-11-19 on air; pansa mirrors the worker set. This is the
operator-facing map of every free inference route wired into free-agent,
ranked by quota cadence and verification level.

## How routing sorts

- `free-agent run` walks workers in ascending priority; only `automatic`
  workers join that walk. Everything else is opt-in via `--worker NAME`.
- Pi variants fall back through `providers.<name>.model_order` (agents.json)
  on 429/quota signals; a successful turn never rotates.
- The FCM router (`fcm` worker) sorts *internally*: live latency + stability
  score across its model set, 429 failover, auto-heal on restart.
- `free-agent status` shows the live pool; exhausted workers cool down and
  the cron nudge re-prompts them.

## Tier 0 — daily-renewable, automatic

| Worker | Priority | Route |
|---|---|---|
| freebuff | 5 | ad-supported daily free (PTY) |
| freepi | 6 | ad-supported daily free (PTY) |
| opencode | 10 | free providers via opencode |
| copilot | 12 | GitHub Free plan AI credits |
| pi-nvidia | 40 | NVIDIA dev-evaluation free models |
| pi-openrouter | 41 | OpenRouter `:free` routes (orproxy :8455 rotates keys) |

## Tier 1 — verified free grants/deals, opt-in

| Worker | Route | Quota |
|---|---|---|
| bl-flash | Bailian qwen-flash-character, planner/orchestrator | 1M tokens to 2027-02-10 |
| bl | Bailian qwen4-max, planner/orchestrator | 1M grant listed; **403 until console activation** |
| fcm | FCM smart router 127.0.0.1:19280, planner/executor | live-sorted set: flint-v1.0:free (openrouter), nemotron-3-ultra-550b (nvidia + openrouter:free), poolside/laguna-xs-2.1 |
| omniroute | OmniRoute router 127.0.0.1:20128, planner/executor, model auto/best-coding (also auto/best-free) | 19 verified connections (groq×3, openrouter×4, cerebras, mistral, siliconflow, novita, ollama-cloud, zai, llm7, api-airforce, freeaiapikey, zenmux, kilo-gateway, orproxy node); **key is OMNIROUTE_API_KEY** — OMNIROUTE/OMNIROUTE_2 are rejected by the local server |
| cc-free / pi-commandcode | commandcode.ai `/provider/v1`, stealth/glyph-cluster:free | ~1000 req/day deal; deals rotate — re-probe before trusting; recursive/flint-v1.0:free is a reasoning fallback (max_tokens ≥ 1024) |
| pi-kilo-ai-gateway | kilo-auto/free via **kilo2** key (primary kilo token expired) | account free tier |
| pi-venice | llama-3.3-70b | unverified inference |
| pi-agnes-ai | agnes-3.0-flash | unverified inference |
| pi-ollama-cloud | gpt-oss:120b | unverified inference |
| pi-apifreellm | apifree-flash | **entitlement-gated** (paid); community model below |

## Tier 2 — keyless / public, opt-in (privacy: prompts leave the machine)

| Worker | State |
|---|---|
| pi-ollama-public | verified OK (llama3.2 on a public instance) |
| apifree-community | works, ~20s between calls — last-resort fallback |
| pi-pollinations-ai | catalog OK (466 models), inference unverified |
| gptoss-proxy | **broken**: returns empty content |
| pi-g4f | **blocked**: needs proof-of-work "cake" credits or signup |

## Tier 3 — unverified account free tiers, opt-in

pi-groq-cloud, pi-cerebras, pi-gemini (GOOGLE_AI_STUDIO1..33 key pool),
pi-siliconflow, pi-z-ai, pi-zhipu-bigmodel, pi-novita-ai, pi-opentyphoon,
pi-routeway, pi-llm-kiwi, pi-llm7-io, pi-aion-labs, pi-scaleway,
pi-qwen-alibaba, pi-nvidia-nim, pi-mistral (trial), pi-cohere (trial),
pi-together (trial), pi-deepinfra (trial).

## Blocked / needs operator action

- **qwen4-max**: free quota VALID commerce-side (1M to 2027-02-13) but the
  API still 403s AccessDenied.Unpurchased — the workspace/postpaid service
  activation is missing. Fix: `bl auth login --open-api` (AK/SK) then
  `bl workspace init --access-key-id … --access-key-secret …`, or the
  console activation banner (may require a payment method on the
  international account). Model authorization alone is not sufficient.
- **~/.env parse failures**: `aihubmix` has a trailing backslash; `NOUS` is a
  two-key comma-separated line. The secure parser rejects both; fix pending
  operator approval (backup first).
- **zenmux**: payg-source keys cannot access the free models (403).
- **omniroute :20128**: RESOLVED — running with 19 verified connections;
  worker `omniroute` registered. Local server auth uses OMNIROUTE_API_KEY
  (the OMNIROUTE / OMNIROUTE_2 values in ~/.env are rejected by it).
- **freeglm / free-gateway :20530**: identified as a modified
  `cuthbertnogood/opencode-free-gateway` — it spoofs the OpenCode client
  fingerprint to reach Zen free-tier models from non-OpenCode clients
  (ToS evasion, ban risk). Deliberately NOT installed/started. The
  legitimate route to the same free pool is the `opencode` worker
  (big-pickle etc.) already in Tier 0.
- **github-models**: key accepted, catalog route unverified → no default model.
- **kluster / tokenrouter / orcarouter / coarena / paws / yeta**: endpoints
  unreachable or unknown from air.

## Local aggregators (air)

- orproxy :8455 — OpenRouter key-rotation proxy (running, systemd --user)
- FCM router :19280 — sorted free coding models (running, `--daemon-bg`)
- omniroute :20128 — intelligent router (running; 19 connections; auto/* combos)
- randomesh-ai :8089 — local ollama models; model-router :8090 (mesh)

## Key health (verified 2026-11-19 via omniroute test-batch)

- AI_GEMINI_KEY: all 21 comma-split keys INVALID upstream → gemini aliases
  reordered to prefer the GOOGLE_AI_STUDIO1..33 pool.
- AI_CEREBRAS_KEY invalid; CEREBRAS valid but chat 402 (quota/billing).
- MISTRAL_AI2 invalid; MISTRAL_AI1 valid, free-tier 429-prone.
- AI_NVIDIA_KEY: models list OK but chat 401 via omniroute (pi-nvidia is a
  separate route, previously verified — re-check before trusting).
- AI_SAMBA_NOVA_KEY contains a '#' placeholder; together 403 CF-1010;
  tokenrouter/deepinfra/cohere keys invalid.
- groq needs a browser User-Agent per connection (Cloudflare 1010);
  llama-3.3-70b gone from the account, gpt-oss-120b/20b + qwen3.8-27b work.

## Adding a new free route

1. Probe `<base>/models` and ONE sentinel chat with **curl** (urllib gets
   Cloudflare 1010). Reasoning models need max_tokens ≥ 1024 or they
   false-negative with empty content.
2. Keyed + pi-compatible streaming: add the slug to `providers.PROVIDERS`,
   `profiles.KEY_ALIASES`, and `DEFAULT_MODELS` (verified id only).
3. Keyless, or streaming-broken: `free-agent cli-agents add NAME` with
   `scripts/free_chat.py` (non-streaming curl runner; key via 0600 config
   file, never argv).
4. Keys stay in ~/.env; never in agents.json, argv, logs, or commits.