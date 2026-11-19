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

- **qwen4-max**: activate in the international console (Model Market) — the
  bl planner starts working immediately after.
- **~/.env parse failures**: `aihubmix` has a trailing backslash; `NOUS` is a
  two-key comma-separated line. The secure parser rejects both; fix pending
  operator approval (backup first).
- **zenmux**: payg-source keys cannot access the free models (403).
- **omniroute :20128**: server up, API-key auth under investigation.
- **freeglm / free-gateway :20530**: no binary found on air or pansa.
- **github-models**: key accepted, catalog route unverified → no default model.
- **kluster / tokenrouter / orcarouter / coarena / paws / yeta**: endpoints
  unreachable or unknown from air.

## Local aggregators (air)

- orproxy :8455 — OpenRouter key-rotation proxy (running, systemd --user)
- FCM router :19280 — sorted free coding models (running, `--daemon-bg`)
- omniroute :20128 — intelligent router (auth pending)
- randomesh-ai :8089 — local ollama models; model-router :8090 (mesh)

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